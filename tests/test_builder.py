"""Writing side: index arithmetic, batching order, and structural fidelity.

Every test here runs a real copy against the in-memory Docs model, which
enforces the same index rules as the API and refuses out-of-range ranges.  If
the bookkeeping in :mod:`autowriter.gdocs.builder` slips by one, these fail.
"""

from __future__ import annotations

import pytest

from autowriter.docxread import read_docx
from autowriter.gdocs.builder import Copier, CopyOptions
from autowriter.gdocs.simulator import SimulatedDocs
from autowriter.gdocs.verify import verify

from .fixtures import PNG_BYTES, SECTION, build_docx, paragraph
from .test_reader import cell_xml, list_paragraph, row_xml, table_xml


def copy(body, options=None, image_uris=None, **kwargs):
    """Read a fixture, copy it into the simulator, return everything involved."""
    document = read_docx(build_docx(body + SECTION, **kwargs))
    simulator = SimulatedDocs()
    uris = image_uris if image_uris is not None else {
        asset_id: "https://example.invalid/%s" % asset_id for asset_id in document.assets
    }
    result = Copier(simulator, uris, options).copy(document)
    return document, simulator, result


def body_paragraphs(simulator):
    """Every paragraph in the body, tables included, in document order."""
    out = []

    def walk(content):
        for element in content:
            if "paragraph" in element:
                out.append(element)
            elif "table" in element:
                for row in element["table"]["tableRows"]:
                    for cell in row["tableCells"]:
                        walk(cell["content"])

    walk(simulator.get_document()["body"]["content"])
    return out


def text_of(element):
    joined = "".join(
        item.get("textRun", {}).get("content", "")
        for item in element["paragraph"]["elements"]
    )
    return joined[:-1] if joined.endswith("\n") else joined


def texts(simulator):
    return [text_of(element) for element in body_paragraphs(simulator)]


def first_table(simulator):
    for element in simulator.get_document()["body"]["content"]:
        if "table" in element:
            return element["table"]
    raise AssertionError("the copy contains no table")


# -- text and indices -------------------------------------------------------


def test_paragraphs_land_in_order_without_extra_blanks():
    _document, simulator, _result = copy(
        paragraph("one") + paragraph("two") + paragraph("three")
    )
    assert texts(simulator) == ["one", "two", "three"]


def test_indices_are_contiguous_and_start_after_the_section_break():
    _document, simulator, _result = copy(paragraph("abc") + paragraph("de"))
    content = simulator.get_document()["body"]["content"]
    assert content[0]["startIndex"] == 0 and "sectionBreak" in content[0]
    assert (content[1]["startIndex"], content[1]["endIndex"]) == (1, 5)
    assert (content[2]["startIndex"], content[2]["endIndex"]) == (5, 8)


def test_astral_characters_count_as_two_index_units():
    # Google Docs indexes in UTF-16 code units, so an emoji is two.
    _document, simulator, _result = copy(paragraph("a\U0001F600b") + paragraph("next"))
    content = simulator.get_document()["body"]["content"]
    assert content[1]["endIndex"] - content[1]["startIndex"] == 5  # a + 2 + b + newline
    assert texts(simulator) == ["a\U0001F600b", "next"]


def test_styles_land_on_the_right_characters():
    body = (
        "<w:p><w:r><w:t>plain </w:t></w:r>"
        '<w:r><w:rPr><w:b/><w:color w:val="FF0000"/></w:rPr><w:t>red</w:t></w:r>'
        "<w:r><w:t> tail</w:t></w:r></w:p>"
    )
    _document, simulator, _result = copy(body)
    elements = body_paragraphs(simulator)[0]["paragraph"]["elements"]
    runs = [
        (item["textRun"]["content"], item["textRun"]["textStyle"].get("bold"))
        for item in elements
        if "textRun" in item
    ]
    assert runs[0][0] == "plain " and not runs[0][1]
    assert runs[1][0] == "red" and runs[1][1] is True
    assert runs[2][0] == " tail" and not runs[2][1]


def test_empty_paragraphs_keep_their_mark_style():
    body = paragraph("first") + '<w:p><w:pPr><w:rPr><w:sz w:val="40"/></w:rPr></w:pPr></w:p>'
    _document, simulator, _result = copy(body)
    blank = body_paragraphs(simulator)[1]
    mark = blank["paragraph"]["elements"][-1]["textRun"]["textStyle"]
    assert mark["fontSize"]["magnitude"] == 20.0


def test_page_break_is_its_own_element():
    body = '<w:p><w:r><w:t>a</w:t><w:br w:type="page"/><w:t>b</w:t></w:r></w:p>'
    _document, simulator, _result = copy(body)
    kinds = [
        next(iter(set(item) - {"startIndex", "endIndex"}))
        for item in body_paragraphs(simulator)[0]["paragraph"]["elements"]
    ]
    assert kinds == ["textRun", "pageBreak", "textRun", "textRun"]


# -- lists ------------------------------------------------------------------


def test_bullets_are_applied_with_the_right_nesting_levels():
    body = (
        list_paragraph("top", 0, "1")
        + list_paragraph("nested", 1, "1")
        + list_paragraph("top again", 0, "1")
    )
    _document, simulator, _result = copy(body)
    paragraphs = body_paragraphs(simulator)
    assert texts(simulator) == ["top", "nested", "top again"]
    assert [p["paragraph"]["bullet"]["nestingLevel"] for p in paragraphs] == [0, 1, 0]


def test_bullet_tabs_are_removed_and_following_text_stays_put():
    body = list_paragraph("nested", 1, "1") + paragraph("after")
    _document, simulator, _result = copy(body)
    assert texts(simulator) == ["nested", "after"]
    content = simulator.get_document()["body"]["content"]
    # "nested" + newline == 7 units, with no leftover tab.
    assert content[1]["endIndex"] - content[1]["startIndex"] == 7


def test_source_indents_survive_bullet_creation():
    # createParagraphBullets imposes its own indents; the copy puts Word's back.
    body = list_paragraph("nested", 1, "1")
    document, simulator, _result = copy(body)
    style = body_paragraphs(simulator)[0]["paragraph"]["paragraphStyle"]
    assert style["indentStart"]["magnitude"] == 72.0
    assert style["indentFirstLine"]["magnitude"] == 54.0
    assert document.sections[0].blocks[0].props.indent_start_pt == 72.0


def test_separate_numbering_definitions_become_separate_lists():
    body = list_paragraph("bullet", 0, "1") + list_paragraph("number", 0, "2")
    _document, simulator, _result = copy(body)
    lists = [p["paragraph"]["bullet"]["listId"] for p in body_paragraphs(simulator)]
    assert lists[0] != lists[1]


def test_long_list_runs_survive_a_forced_flush():
    body = "".join(list_paragraph("item %d" % index, index % 2, "1") for index in range(60))
    _document, simulator, _result = copy(body)
    assert texts(simulator) == ["item %d" % index for index in range(60)]
    levels = [p["paragraph"]["bullet"]["nestingLevel"] for p in body_paragraphs(simulator)]
    assert levels == [index % 2 for index in range(60)]


# -- tables -----------------------------------------------------------------


def test_table_cells_receive_their_own_text():
    body = table_xml(
        [
            row_xml([cell_xml("a1"), cell_xml("b1")]),
            row_xml([cell_xml("a2"), cell_xml("b2")]),
        ]
    )
    _document, simulator, _result = copy(body)
    table = first_table(simulator)
    grid = [
        [text_of(cell["content"][0]) for cell in row["tableCells"]]
        for row in table["tableRows"]
    ]
    assert grid == [["a1", "b1"], ["a2", "b2"]]


def test_text_after_a_table_continues_at_the_right_index():
    body = paragraph("before") + table_xml([row_xml([cell_xml("in")])], grid=("9360",)) + paragraph("after")
    _document, simulator, _result = copy(body)
    assert texts(simulator) == ["before", "in", "after"]
    content = simulator.get_document()["body"]["content"]
    table_element = content[2]
    assert content[3]["startIndex"] == table_element["endIndex"]


def test_column_widths_and_header_rows_are_set():
    body = table_xml(
        [
            "<w:tr><w:trPr><w:tblHeader/></w:trPr>%s%s</w:tr>"
            % (cell_xml("h1"), cell_xml("h2")),
            row_xml([cell_xml("a"), cell_xml("b")]),
        ],
        grid=("3000", "6360"),
    )
    _document, simulator, _result = copy(body)
    table = first_table(simulator)
    widths = [
        properties["width"]["magnitude"]
        for properties in table["tableStyle"]["tableColumnProperties"]
    ]
    assert widths == [150.0, 318.0]
    assert table["tableRows"][0]["tableRowStyle"]["tableHeader"] is True


def test_horizontally_merged_cells_are_merged_and_filled():
    body = table_xml(
        [
            row_xml([cell_xml("wide", '<w:gridSpan w:val="2"/>')]),
            row_xml([cell_xml("a"), cell_xml("b")]),
        ]
    )
    _document, simulator, _result = copy(body)
    table = first_table(simulator)
    first_row = table["tableRows"][0]["tableCells"]
    assert len(first_row) == 1
    assert first_row[0]["tableCellStyle"]["columnSpan"] == 2
    assert text_of(first_row[0]["content"][0]) == "wide"
    assert [text_of(cell["content"][0]) for cell in table["tableRows"][1]["tableCells"]] == ["a", "b"]


def test_vertically_merged_cells_keep_the_covering_cell_text():
    body = table_xml(
        [
            row_xml([cell_xml("tall", '<w:vMerge w:val="restart"/>'), cell_xml("x")]),
            row_xml([cell_xml("", "<w:vMerge/>"), cell_xml("y")]),
        ]
    )
    _document, simulator, _result = copy(body)
    table = first_table(simulator)
    anchor = table["tableRows"][0]["tableCells"][0]
    assert anchor["tableCellStyle"]["rowSpan"] == 2
    assert text_of(anchor["content"][0]) == "tall"
    assert [text_of(cell["content"][0]) for cell in table["tableRows"][1]["tableCells"]] == ["y"]


def test_cell_background_and_borders_are_applied():
    body = table_xml(
        [row_xml([cell_xml("head")]), row_xml([cell_xml("body")])],
        grid=("9360",),
        tbl_pr='<w:tblStyle w:val="TableGrid"/><w:tblLook w:firstRow="1"/>',
    )
    _document, simulator, _result = copy(body)
    table = first_table(simulator)
    style = table["tableRows"][0]["tableCells"][0]["tableCellStyle"]
    assert style["backgroundColor"]["color"]["rgbColor"]["blue"] > 0.9
    assert style["borderTop"]["width"]["magnitude"] == 0.5


def test_nested_tables_are_written_inside_their_cell():
    inner = table_xml([row_xml([cell_xml("inner")])], grid=("2000",))
    outer = (
        '<w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w="9360"/></w:tblGrid>'
        "<w:tr><w:tc>%s%s</w:tc></w:tr></w:tbl>" % (inner, paragraph("tail"))
    )
    _document, simulator, _result = copy(outer)
    outer_table = first_table(simulator)
    cell_content = outer_table["tableRows"][0]["tableCells"][0]["content"]
    inner_table = cell_content[0]["table"]
    assert text_of(inner_table["tableRows"][0]["tableCells"][0]["content"][0]) == "inner"
    assert text_of(cell_content[1]) == "tail"


def test_a_table_can_be_the_first_block():
    body = table_xml([row_xml([cell_xml("only")])], grid=("9360",))
    _document, simulator, _result = copy(body)
    assert texts(simulator)[0] == "only"


def test_lists_inside_table_cells_get_bullets():
    cell = "<w:tc><w:tcPr/>%s%s</w:tc>" % (
        list_paragraph("one", 0, "1"),
        list_paragraph("two", 1, "1"),
    )
    body = table_xml([row_xml([cell])], grid=("9360",))
    _document, simulator, _result = copy(body)
    table = first_table(simulator)
    cells = table["tableRows"][0]["tableCells"][0]["content"]
    assert [text_of(item) for item in cells] == ["one", "two"]
    assert [item["paragraph"]["bullet"]["nestingLevel"] for item in cells] == [0, 1]


# -- images, sections, headers, footnotes ------------------------------------


IMAGE_BODY = (
    "<w:p><w:r><w:t>see </w:t><w:drawing><wp:inline>"
    '<wp:extent cx="914400" cy="457200"/><wp:docPr id="1" name="Picture 1"/>'
    '<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rIdImg"/>'
    "</pic:blipFill></pic:pic></a:graphicData></a:graphic>"
    "</wp:inline></w:drawing></w:r></w:p>"
)
IMAGE_PARTS = {"word/media/image1.png": PNG_BYTES}
IMAGE_REL = '<Relationship Id="rIdImg" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>'


def test_inline_image_is_inserted_with_its_size():
    _document, simulator, _result = copy(
        IMAGE_BODY, extra_parts=IMAGE_PARTS, extra_rels=IMAGE_REL
    )
    elements = body_paragraphs(simulator)[0]["paragraph"]["elements"]
    inline = [item for item in elements if "inlineObjectElement" in item]
    assert len(inline) == 1
    object_id = inline[0]["inlineObjectElement"]["inlineObjectId"]
    size = simulator.get_document()["inlineObjects"][object_id]["inlineObjectProperties"][
        "embeddedObject"
    ]["size"]
    assert size["width"]["magnitude"] == 72.0


def test_missing_image_url_is_reported_not_guessed():
    _document, _simulator, result = copy(
        IMAGE_BODY, image_uris={}, extra_parts=IMAGE_PARTS, extra_rels=IMAGE_REL
    )
    assert any("no public URL" in note.message for note in result.notes)


def test_section_break_starts_a_new_section_with_its_own_margins():
    body = (
        "<w:p><w:pPr><w:sectPr>"
        '<w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:left="1440"/>'
        "</w:sectPr></w:pPr><w:r><w:t>first</w:t></w:r></w:p>"
        + paragraph("second")
        + '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="720" w:left="720"/></w:sectPr>'
    )
    document = read_docx(build_docx(body))
    simulator = SimulatedDocs()
    Copier(simulator).copy(document)
    content = simulator.get_document()["body"]["content"]
    breaks = [element for element in content if "sectionBreak" in element]
    assert len(breaks) == 2
    assert breaks[1]["sectionBreak"]["sectionStyle"]["marginTop"]["magnitude"] == 36.0
    assert [text_of(element) for element in content if "paragraph" in element] == [
        "first",
        "second",
    ]


def test_header_content_is_written_into_the_header_segment():
    header = (
        '<?xml version="1.0"?><w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:p><w:r><w:t>running head</w:t></w:r></w:p></w:hdr>"
    )
    body = paragraph("body") + (
        '<w:sectPr><w:headerReference w:type="default" r:id="rIdHdr"/>'
        '<w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
    )
    document = read_docx(
        build_docx(
            body,
            extra_parts={"word/header1.xml": header.encode()},
            extra_rels='<Relationship Id="rIdHdr" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>',
        )
    )
    simulator = SimulatedDocs()
    Copier(simulator).copy(document)
    headers = simulator.get_document()["headers"]
    assert len(headers) == 1
    segment = next(iter(headers.values()))
    assert text_of(segment["content"][0]) == "running head"


def test_footnote_body_is_written_into_the_footnote_segment():
    footnotes = (
        '<?xml version="1.0"?><w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:footnote w:id="1"><w:p><w:r><w:t>the note</w:t></w:r></w:p></w:footnote></w:footnotes>'
    )
    body = '<w:p><w:r><w:t>text</w:t><w:footnoteReference w:id="1"/></w:r></w:p>'
    _document, simulator, _result = copy(
        body,
        extra_parts={"word/footnotes.xml": footnotes.encode()},
        extra_rels='<Relationship Id="rIdFn" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/>',
    )
    document = simulator.get_document()
    assert len(document["footnotes"]) == 1
    segment = next(iter(document["footnotes"].values()))
    assert text_of(segment["content"][0]) == "the note"


def test_page_setup_is_taken_from_the_first_section():
    _document, simulator, _result = copy(paragraph("x"))
    style = simulator.get_document()["documentStyle"]
    assert style["pageSize"]["width"]["magnitude"] == 612.0
    assert style["marginLeft"]["magnitude"] == 72.0


# -- options ----------------------------------------------------------------


def test_all_caps_is_rendered_into_the_text_by_default():
    body = paragraph("shout", rpr="<w:caps/>")
    _document, simulator, result = copy(body)
    assert texts(simulator) == ["SHOUT"]
    assert any("All caps" in note.message for note in result.notes)


def test_literal_caps_option_keeps_the_stored_text():
    body = paragraph("shout", rpr="<w:caps/>")
    _document, simulator, _result = copy(body, options=CopyOptions(render_all_caps=False))
    assert texts(simulator) == ["shout"]


def test_hidden_text_is_dropped_unless_asked_for():
    body = "<w:p><w:r><w:t>shown</w:t></w:r><w:r><w:rPr><w:vanish/></w:rPr><w:t>hidden</w:t></w:r></w:p>"
    _document, simulator, _result = copy(body)
    assert texts(simulator) == ["shown"]
    _document, simulator, _result = copy(body, options=CopyOptions(include_hidden_text=True))
    assert texts(simulator) == ["shownhidden"]


# -- the whole thing together ------------------------------------------------


RICH_BODY = (
    paragraph("Quarterly Report", style="Heading1")
    + paragraph("An introduction paragraph with plain text.")
    + '<w:p><w:r><w:t>mixed </w:t></w:r><w:r><w:rPr><w:b/><w:i/><w:u w:val="single"/>'
    '<w:color w:val="1F4E79"/><w:sz w:val="28"/></w:rPr><w:t>formatting</w:t></w:r>'
    "<w:r><w:t> in one line.</w:t></w:r></w:p>"
    + list_paragraph("first", 0, "1")
    + list_paragraph("second", 1, "1")
    + list_paragraph("third", 0, "1")
    + list_paragraph("numbered", 0, "2")
    + paragraph("A justified, indented paragraph.", ppr='<w:jc w:val="both"/><w:ind w:left="720" w:firstLine="360"/>')
    + table_xml(
        [
            row_xml([cell_xml("Name"), cell_xml("Value")]),
            row_xml([cell_xml("Revenue"), cell_xml("42")]),
            row_xml([cell_xml("Spanning", '<w:gridSpan w:val="2"/>')]),
        ],
        tbl_pr='<w:tblStyle w:val="TableGrid"/><w:tblLook w:firstRow="1"/>',
    )
    + paragraph("")
    + paragraph("Closing remarks.", ppr='<w:jc w:val="center"/>')
)


def test_rich_document_verifies_clean():
    document, simulator, result = copy(RICH_BODY)
    report = verify(document, simulator.get_document())
    assert report.differences == []
    assert report.paragraphs_checked >= 11
    assert result.request_count > 0


def test_verification_notices_a_damaged_copy():
    document, simulator, _result = copy(RICH_BODY)
    snapshot = simulator.get_document()
    for element in snapshot["body"]["content"]:
        if "paragraph" in element and element["paragraph"]["elements"][0].get("textRun"):
            element["paragraph"]["elements"][0]["textRun"]["content"] = "tampered"
            break
    report = verify(document, snapshot)
    assert not report.ok
    assert any(difference.field == "text" for difference in report.differences)


def test_every_request_the_builder_emits_is_one_the_api_defines():
    _document, simulator, _result = copy(RICH_BODY)
    known = {
        "insertText",
        "insertTable",
        "insertPageBreak",
        "insertInlineImage",
        "insertSectionBreak",
        "createFootnote",
        "createHeader",
        "createFooter",
        "updateTextStyle",
        "updateParagraphStyle",
        "createParagraphBullets",
        "updateTableCellStyle",
        "updateTableColumnProperties",
        "updateTableRowStyle",
        "mergeTableCells",
        "updateDocumentStyle",
        "updateSectionStyle",
    }
    used = {next(iter(request)) for request in simulator.applied}
    assert used <= known


@pytest.mark.parametrize("count", [1, 2, 37])
def test_documents_of_various_sizes_round_trip(count):
    body = "".join(paragraph("paragraph number %d" % index) for index in range(count))
    document, simulator, _result = copy(body)
    assert verify(document, simulator.get_document()).differences == []


def test_footnotes_inside_a_table_are_reported_not_attempted():
    # The Docs API can only anchor a footnote in the body, never in a cell.
    footnotes = (
        '<?xml version="1.0"?><w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:footnote w:id="1"><w:p><w:r><w:t>note</w:t></w:r></w:p></w:footnote></w:footnotes>'
    )
    cell = '<w:tc><w:tcPr/><w:p><w:r><w:t>cell</w:t><w:footnoteReference w:id="1"/></w:r></w:p></w:tc>'
    body = table_xml([row_xml([cell])], grid=("9360",))
    _document, simulator, result = copy(
        body,
        extra_parts={"word/footnotes.xml": footnotes.encode()},
        extra_rels='<Relationship Id="rIdFn" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/>',
    )
    assert simulator.get_document()["footnotes"] == {}
    assert any("footnotes inside" in note.message for note in result.notes)
    # The trailing blank is the paragraph Docs always keeps after a table.
    assert texts(simulator) == ["cell", ""]


def test_section_styles_use_a_range_the_api_will_accept():
    body = (
        "<w:p><w:pPr><w:sectPr><w:pgSz w:w=\"12240\" w:h=\"15840\"/>"
        '<w:pgMar w:top="1440"/></w:sectPr></w:pPr><w:r><w:t>one</w:t></w:r></w:p>'
        + paragraph("two")
        + '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="720"/></w:sectPr>'
    )
    document = read_docx(build_docx(body))
    simulator = SimulatedDocs()
    Copier(simulator).copy(document)
    for request in simulator.applied:
        span = request.get("updateSectionStyle", {}).get("range")
        if span:
            assert span["endIndex"] > span["startIndex"]
