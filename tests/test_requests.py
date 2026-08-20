"""The request payloads themselves: shapes, field masks and preset choices."""

from __future__ import annotations

import pytest

from autowriter.gdocs import requests as R
from autowriter.gdocs.simulator import DocsError, SimulatedDocs
from autowriter.ir import Border, ListMarker, ParagraphProps, SectionProps, TableCell, TextStyle
from autowriter.units import u16len


def test_text_style_states_every_toggle_explicitly():
    # A .docx that inherits "not bold" still has to say so: the Google Doc's
    # defaults are not the source document's.
    payload = R.text_style_payload(TextStyle(font_size_pt=11.0))
    assert payload["bold"] is False
    assert payload["italic"] is False
    assert payload["baselineOffset"] == "NONE"
    assert payload["foregroundColor"] == {}


def test_text_style_fields_match_the_payload_keys():
    request = R.update_text_style(1, 5, TextStyle(bold=True))["updateTextStyle"]
    assert sorted(request["textStyle"]) == request["fields"].split(",")


def test_bold_runs_carry_a_font_weight():
    payload = R.text_style_payload(TextStyle(bold=True, font_family="Calibri"))
    assert payload["weightedFontFamily"] == {"fontFamily": "Calibri", "weight": 700}


def test_links_are_cleared_when_absent():
    assert R.text_style_payload(TextStyle())["link"] == {}
    assert R.text_style_payload(TextStyle(link_url="https://example.com"))["link"] == {
        "url": "https://example.com"
    }


def test_paragraph_style_only_includes_what_the_source_specified():
    payload = R.paragraph_style_payload(ParagraphProps(alignment="CENTER"))
    assert payload == {"alignment": "CENTER"}
    assert R.update_paragraph_style(1, 2, ParagraphProps()) is None


def test_named_style_is_a_request_of_its_own():
    # Applying a named style resets other properties, so it never shares a
    # request with the explicit formatting that has to survive it.
    request = R.update_named_style(1, 5, "HEADING_2")["updateParagraphStyle"]
    assert request["fields"] == "namedStyleType"
    assert request["paragraphStyle"] == {"namedStyleType": "HEADING_2"}


def test_paragraph_borders_become_dimensions():
    props = ParagraphProps(border_bottom=Border("FF0000", 1.5, "DASH", 4.0))
    payload = R.paragraph_style_payload(props)["borderBottom"]
    assert payload["width"] == {"magnitude": 1.5, "unit": "PT"}
    assert payload["padding"] == {"magnitude": 4.0, "unit": "PT"}
    assert payload["dashStyle"] == "DASH"


@pytest.mark.parametrize(
    "marker,expected",
    [
        (ListMarker("1", 0, "bullet", "\u00b7"), "BULLET_DISC_CIRCLE_SQUARE"),
        (ListMarker("1", 0, "bullet", "\uf0a8"), "BULLET_CHECKBOX"),
        (ListMarker("1", 0, "bullet", "o"), "BULLET_DIAMOND_CIRCLE_SQUARE"),
        (ListMarker("1", 0, "decimal", None, None, "%1."), "NUMBERED_DECIMAL_ALPHA_ROMAN"),
        (ListMarker("1", 0, "decimal", None, None, "%1)"), "NUMBERED_DECIMAL_ALPHA_ROMAN_PARENS"),
        (ListMarker("1", 0, "decimal", None, None, "%1.%2."), "NUMBERED_DECIMAL_NESTED"),
        (ListMarker("1", 0, "upperRoman", None, None, "%1."), "NUMBERED_UPPERROMAN_UPPERALPHA_DECIMAL"),
        (ListMarker("1", 0, "somethingElse"), "NUMBERED_DECIMAL_ALPHA_ROMAN"),
    ],
)
def test_bullet_presets_pick_the_closest_match(marker, expected):
    assert R.bullet_preset(marker) == expected


def test_cell_style_carries_padding_borders_and_alignment():
    cell = TableCell(
        background_color="D9E2F3",
        vertical_alignment="MIDDLE",
        padding_left_pt=5.4,
        border_top=Border("000000", 0.5, "SOLID"),
    )
    payload = R.update_table_cell_style(1, 0, 0, cell)["updateTableCellStyle"]
    style = payload["tableCellStyle"]
    assert style["contentAlignment"] == "MIDDLE"
    assert style["paddingLeft"]["magnitude"] == 5.4
    assert style["borderTop"]["width"]["magnitude"] == 0.5
    assert sorted(style) == payload["fields"].split(",")


def test_cell_style_is_skipped_when_there_is_nothing_to_say():
    assert R.update_table_cell_style(1, 0, 0, TableCell()) is None


def test_document_style_carries_page_size_and_margins():
    props = SectionProps(page_width_pt=612.0, page_height_pt=792.0, margin_left_pt=72.0)
    payload = R.update_document_style(props)["updateDocumentStyle"]["documentStyle"]
    assert payload["pageSize"]["width"]["magnitude"] == 612.0
    assert payload["marginLeft"]["magnitude"] == 72.0


def test_section_style_splits_the_page_into_columns():
    props = SectionProps(
        page_width_pt=612.0,
        margin_left_pt=72.0,
        margin_right_pt=72.0,
        column_count=2,
        column_gap_pt=36.0,
    )
    payload = R.update_section_style(1, 10, props)["updateSectionStyle"]["sectionStyle"]
    columns = payload["columnProperties"]
    assert len(columns) == 2
    assert columns[0]["width"]["magnitude"] == pytest.approx((612 - 144 - 36) / 2)


def test_segment_ids_are_only_added_when_present():
    assert "segmentId" not in R.insert_text(1, "x")["insertText"]["location"]
    assert R.insert_text(1, "x", "hdr")["insertText"]["location"]["segmentId"] == "hdr"


# -- the simulator's own guarantees ------------------------------------------


def test_simulator_rejects_a_range_past_the_end():
    simulator = SimulatedDocs()
    simulator.batch_update([R.insert_text(1, "hello")])
    with pytest.raises(DocsError):
        simulator.batch_update([R.update_text_style(1, 99, TextStyle())])


def test_simulator_rejects_an_empty_range():
    simulator = SimulatedDocs()
    with pytest.raises(DocsError):
        simulator.batch_update([R.update_text_style(1, 1, TextStyle())])


def test_simulator_counts_astral_characters_as_two():
    simulator = SimulatedDocs()
    simulator.batch_update([R.insert_text(1, "\U0001F600")])
    content = simulator.get_document()["body"]["content"]
    assert content[1]["endIndex"] - content[1]["startIndex"] == 3
    assert u16len("\U0001F600") == 2


def test_simulator_splits_paragraphs_on_newlines():
    simulator = SimulatedDocs()
    simulator.batch_update([R.insert_text(1, "one\ntwo\nthree")])
    content = simulator.get_document()["body"]["content"]
    assert len([element for element in content if "paragraph" in element]) == 3
