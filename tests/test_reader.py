"""Reading .docx: style resolution, numbering, tables, and the odd corners."""

from __future__ import annotations

import pytest

from autowriter.docxread import read_docx
from autowriter.ir import ImageRun, PageBreakRun, Table

from .fixtures import PNG_BYTES, SECTION, build_docx, paragraph


def read(body, **kwargs):
    return read_docx(build_docx(body + SECTION, **kwargs))


def first_section_blocks(document):
    return document.sections[0].blocks


# -- character formatting ---------------------------------------------------


def test_document_defaults_reach_every_run():
    document = read(paragraph("plain text"))
    run = first_section_blocks(document)[0].inlines[0]
    assert run.style.font_family == "Calibri"
    assert run.style.font_size_pt == 11.0


def test_style_chain_is_flattened_onto_runs():
    document = read(paragraph("Title here", style="Heading1"))
    block = first_section_blocks(document)[0]
    assert block.props.named_style == "HEADING_1"
    # None of this is stated on the run itself; it all comes from styles.xml.
    assert block.inlines[0].style.font_size_pt == 16.0
    assert block.inlines[0].style.color == "2F5496"
    assert block.props.keep_with_next is True
    assert block.props.space_above_pt == 12.0


def test_direct_formatting_beats_the_style():
    body = paragraph("shouty", style="Heading1", rpr='<w:sz w:val="48"/><w:b/>')
    run = first_section_blocks(read(body))[0].inlines[0]
    assert run.style.font_size_pt == 24.0
    assert run.style.bold is True


def test_character_style_applies_between_paragraph_style_and_direct():
    body = (
        '<w:p><w:r><w:rPr><w:rStyle w:val="Strong"/></w:rPr><w:t>strong</w:t></w:r>'
        '<w:r><w:rPr><w:rStyle w:val="Strong"/><w:b w:val="0"/></w:rPr><w:t>not</w:t></w:r></w:p>'
    )
    runs = first_section_blocks(read(body))[0].inlines
    assert runs[0].style.bold is True
    assert runs[1].style.bold is False


def test_highlight_and_shading_become_background_colours():
    highlighted = read(paragraph("x", rpr='<w:highlight w:val="yellow"/>'))
    shaded = read(paragraph("x", rpr='<w:shd w:val="clear" w:fill="C0C0C0"/>'))
    assert first_section_blocks(highlighted)[0].inlines[0].style.background_color == "FFFF00"
    assert first_section_blocks(shaded)[0].inlines[0].style.background_color == "C0C0C0"


def test_toggles_and_baseline():
    body = paragraph(
        "x", rpr='<w:i/><w:u w:val="single"/><w:strike/><w:vertAlign w:val="superscript"/>'
    )
    style = first_section_blocks(read(body))[0].inlines[0].style
    assert (style.italic, style.underline, style.strikethrough) == (True, True, True)
    assert style.baseline == "SUPERSCRIPT"


def test_theme_fonts_resolve_through_the_theme_part():
    theme = (
        '<?xml version="1.0"?><a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<a:themeElements><a:fontScheme><a:majorFont><a:latin typeface=\"Georgia\"/></a:majorFont>"
        '<a:minorFont><a:latin typeface="Verdana"/></a:minorFont></a:fontScheme></a:themeElements></a:theme>'
    )
    document = read_docx(
        build_docx(
            paragraph("x", rpr='<w:rFonts w:asciiTheme="minorHAnsi"/>') + SECTION,
            extra_parts={"word/theme/theme1.xml": theme.encode()},
            extra_rels='<Relationship Id="rIdTheme" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>',
        )
    )
    assert document.sections[0].blocks[0].inlines[0].style.font_family == "Verdana"


# -- paragraph formatting ---------------------------------------------------


def test_indents_hanging_and_first_line():
    hanging = read(paragraph("x", ppr='<w:ind w:left="720" w:hanging="360"/>'))
    first_line = read(paragraph("x", ppr='<w:ind w:left="720" w:firstLine="360"/>'))
    assert first_section_blocks(hanging)[0].props.indent_first_line_pt == 18.0
    assert first_section_blocks(first_line)[0].props.indent_first_line_pt == 54.0


def test_line_spacing_rules():
    auto = read(paragraph("x", ppr='<w:spacing w:line="480" w:lineRule="auto"/>'))
    assert first_section_blocks(auto)[0].props.line_spacing == 200.0
    exact = read(paragraph("x", ppr='<w:spacing w:line="240" w:lineRule="exact"/>'))
    # 12pt of fixed leading against an 11pt font is a little under single.
    assert 90 < first_section_blocks(exact)[0].props.line_spacing < 100


def test_alignment_and_page_break_before():
    body = paragraph("x", ppr='<w:jc w:val="both"/><w:pageBreakBefore/>')
    props = first_section_blocks(read(body))[0].props
    assert props.alignment == "JUSTIFIED"
    assert props.page_break_before is True


def test_paragraph_borders_and_shading():
    body = paragraph(
        "x",
        ppr='<w:pBdr><w:bottom w:val="single" w:sz="12" w:space="4" w:color="FF0000"/></w:pBdr>'
        '<w:shd w:val="clear" w:fill="EEEEEE"/>',
    )
    props = first_section_blocks(read(body))[0].props
    assert props.border_bottom.width_pt == 1.5
    assert props.border_bottom.color == "FF0000"
    assert props.border_bottom.padding_pt == 4.0
    assert props.shading_color == "EEEEEE"


# -- lists ------------------------------------------------------------------


def list_paragraph(text, level, num_id):
    return (
        '<w:p><w:pPr><w:pStyle w:val="ListParagraph"/><w:numPr>'
        '<w:ilvl w:val="%d"/><w:numId w:val="%s"/></w:numPr></w:pPr>'
        "<w:r><w:t>%s</w:t></w:r></w:p>" % (level, num_id, text)
    )


def test_bullet_and_number_markers():
    document = read(list_paragraph("bullet", 0, "1") + list_paragraph("number", 0, "2"))
    blocks = first_section_blocks(document)
    assert blocks[0].list_marker.number_format == "bullet"
    assert blocks[1].list_marker.number_format == "decimal"
    assert blocks[1].list_marker.level_text == "%1."


def test_numbering_level_supplies_the_indent():
    document = read(list_paragraph("nested", 1, "1"))
    props = first_section_blocks(document)[0].props
    assert props.indent_start_pt == 72.0  # 1440 twips from the numbering level
    assert props.indent_first_line_pt == 54.0


def test_bullet_glyph_font_does_not_leak_into_the_text():
    # The numbering level sets Symbol for the glyph; the words are still Calibri.
    document = read(list_paragraph("text", 0, "1"))
    assert first_section_blocks(document)[0].inlines[0].style.font_family == "Calibri"


# -- inline content ---------------------------------------------------------


def test_tabs_line_breaks_and_page_breaks():
    body = (
        "<w:p><w:r><w:t>a</w:t><w:tab/><w:t>b</w:t><w:br/><w:t>c</w:t>"
        '<w:br w:type="page"/><w:t>d</w:t></w:r></w:p>'
    )
    inlines = first_section_blocks(read(body))[0].inlines
    assert inlines[0].text == "a\tbc"
    assert isinstance(inlines[1], PageBreakRun)
    assert inlines[2].text == "d"


def test_hyperlink_target_comes_from_the_relationship():
    body = '<w:p><w:hyperlink r:id="rIdLink"><w:r><w:t>link</w:t></w:r></w:hyperlink></w:p>'
    document = read_docx(
        build_docx(
            body + SECTION,
            extra_rels='<Relationship Id="rIdLink" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.com/a" TargetMode="External"/>',
        )
    )
    assert document.sections[0].blocks[0].inlines[0].style.link_url == "https://example.com/a"


def test_inline_image_is_registered_with_its_size():
    drawing = (
        "<w:p><w:r><w:drawing><wp:inline>"
        '<wp:extent cx="914400" cy="457200"/>'
        '<wp:docPr id="1" name="Picture 1" descr="a red dot"/>'
        '<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rIdImg"/>'
        "</pic:blipFill></pic:pic></a:graphicData></a:graphic>"
        "</wp:inline></w:drawing></w:r></w:p>"
    )
    document = read_docx(
        build_docx(
            drawing + SECTION,
            extra_parts={"word/media/image1.png": PNG_BYTES},
            extra_rels='<Relationship Id="rIdImg" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>',
        )
    )
    image = document.sections[0].blocks[0].inlines[0]
    assert isinstance(image, ImageRun)
    assert (image.width_pt, image.height_pt) == (72.0, 36.0)
    assert image.alt_description == "a red dot"
    assert document.assets[image.asset_id].content_type == "image/png"


def test_tracked_insertions_are_kept_and_deletions_dropped():
    body = (
        "<w:p>"
        '<w:ins w:id="1"><w:r><w:t>kept</w:t></w:r></w:ins>'
        '<w:del w:id="2"><w:r><w:delText>gone</w:delText></w:r></w:del>'
        "</w:p>"
    )
    document = read(body)
    assert document.sections[0].blocks[0].text == "kept"
    assert any(note.kind == "tracked-change" for note in document.notes)


def test_symbol_runs_fall_back_to_the_latin_code_point():
    body = '<w:p><w:r><w:sym w:font="Wingdings" w:char="F0E0"/></w:r></w:p>'
    document = read(body)
    assert document.sections[0].blocks[0].text == "à"


# -- tables -----------------------------------------------------------------


def table_xml(rows, grid=("4680", "4680"), tbl_pr=""):
    grid_xml = "".join('<w:gridCol w:w="%s"/>' % width for width in grid)
    return (
        "<w:tbl><w:tblPr>%s</w:tblPr><w:tblGrid>%s</w:tblGrid>%s</w:tbl>"
        % (tbl_pr, grid_xml, "".join(rows))
    )


def row_xml(cells):
    return "<w:tr>%s</w:tr>" % "".join(cells)


def cell_xml(text, tc_pr=""):
    return "<w:tc><w:tcPr>%s</w:tcPr>%s</w:tc>" % (tc_pr, paragraph(text))


def test_table_grid_and_cell_text():
    body = table_xml([row_xml([cell_xml("a"), cell_xml("b")])])
    table = first_section_blocks(read(body))[0]
    assert isinstance(table, Table)
    assert table.column_widths_pt == [234.0, 234.0]
    assert table.rows[0].cells[1].blocks[0].text == "b"


def test_table_style_borders_reach_every_cell_edge():
    body = table_xml(
        [row_xml([cell_xml("a"), cell_xml("b")]), row_xml([cell_xml("c"), cell_xml("d")])],
        tbl_pr='<w:tblStyle w:val="TableGrid"/>',
    )
    table = first_section_blocks(read(body))[0]
    for row in table.rows:
        for cell in row.cells:
            assert cell.border_top is not None and cell.border_top.width_pt == 0.5
            assert cell.border_right is not None
            assert cell.border_left is not None


def test_first_row_conditional_formatting():
    body = table_xml(
        [row_xml([cell_xml("head")]), row_xml([cell_xml("body")])],
        grid=("9360",),
        tbl_pr='<w:tblStyle w:val="TableGrid"/><w:tblLook w:firstRow="1"/>',
    )
    table = first_section_blocks(read(body))[0]
    assert table.rows[0].cells[0].background_color == "D9E2F3"
    assert table.rows[0].cells[0].blocks[0].inlines[0].style.bold is True
    assert table.rows[1].cells[0].background_color is None


def test_horizontal_merge_uses_grid_span():
    body = table_xml([row_xml([cell_xml("wide", '<w:gridSpan w:val="2"/>')])])
    table = first_section_blocks(read(body))[0]
    assert table.rows[0].cells[0].col_span == 2


def test_vertical_merge_collapses_into_a_row_span():
    body = table_xml(
        [
            row_xml([cell_xml("tall", '<w:vMerge w:val="restart"/>'), cell_xml("x")]),
            row_xml([cell_xml("", "<w:vMerge/>"), cell_xml("y")]),
        ]
    )
    table = first_section_blocks(read(body))[0]
    assert table.rows[0].cells[0].row_span == 2
    assert table.rows[1].cells[0].merged_away is True


def test_nested_table_is_read_as_a_block_inside_the_cell():
    inner = table_xml([row_xml([cell_xml("inner")])], grid=("2000",))
    outer = "<w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w=\"9360\"/></w:tblGrid><w:tr><w:tc>%s%s</w:tc></w:tr></w:tbl>" % (
        inner,
        paragraph(""),
    )
    table = first_section_blocks(read(outer))[0]
    assert isinstance(table.rows[0].cells[0].blocks[0], Table)


# -- sections, headers, footnotes -------------------------------------------


def test_paragraph_section_properties_split_sections():
    body = (
        "<w:p><w:pPr><w:sectPr>"
        '<w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="720" w:left="720"/>'
        "</w:sectPr></w:pPr><w:r><w:t>first</w:t></w:r></w:p>"
        + paragraph("second")
    )
    document = read(body)
    assert len(document.sections) == 2
    assert document.sections[0].props.margin_top_pt == 36.0
    assert document.sections[1].props.margin_top_pt == 72.0


def test_landscape_page_size_is_read_from_the_section():
    body = paragraph("x") + (
        "<w:sectPr><w:pgSz w:w=\"15840\" w:h=\"12240\" w:orient=\"landscape\"/></w:sectPr>"
    )
    document = read_docx(build_docx(body))
    assert document.sections[0].props.page_width_pt == 792.0


def test_header_and_footer_parts_are_read():
    header = (
        '<?xml version="1.0"?><w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:p><w:r><w:t>page header</w:t></w:r></w:p></w:hdr>"
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
    assert document.sections[0].headers["default"].blocks[0].text == "page header"


def test_footnote_body_is_attached_to_the_reference():
    footnotes = (
        '<?xml version="1.0"?><w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:footnote w:id="1"><w:p><w:r><w:t>the note</w:t></w:r></w:p></w:footnote></w:footnotes>'
    )
    body = '<w:p><w:r><w:t>text</w:t><w:footnoteReference w:id="1"/></w:r></w:p>' + SECTION
    document = read_docx(
        build_docx(
            body,
            extra_parts={"word/footnotes.xml": footnotes.encode()},
            extra_rels='<Relationship Id="rIdFn" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/>',
        )
    )
    note_run = document.sections[0].blocks[0].inlines[1]
    assert note_run.blocks[0].text == "the note"


def test_reader_rejects_a_package_without_a_document_part():
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
    buffer.seek(0)
    with pytest.raises(ValueError):
        read_docx(buffer)
