"""Translation of IR formatting into Google Docs ``batchUpdate`` requests.

Everything here is a pure function: IR in, request dicts out.  That keeps the
tricky part (what a request should say) separate from the even trickier part
(where in the document it should point), which lives in :mod:`.builder`.

Two conventions run through this module:

* Formatting is stated *explicitly*.  A .docx that inherits "not bold" from its
  stylesheet produces ``bold: false`` here rather than an omitted field,
  because the Google Doc's own defaults are not the same ones.
* Every request lists its ``fields`` exactly, so nothing outside what the
  source document specifies is touched.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..ir import Border, ListMarker, ParagraphProps, SectionProps, TableCell, TextStyle
from ..units import dimension, optional_color

# Word numbering formats -> the closest Docs bullet preset.  Docs only offers
# fixed three-level glyph sequences, so this is a nearest-match table, not a
# translation.
BULLET_PRESETS = {
    "disc": "BULLET_DISC_CIRCLE_SQUARE",
    "circle": "BULLET_DIAMOND_CIRCLE_SQUARE",
    "square": "BULLET_DIAMONDX_HOLLOWDIAMOND_SQUARE",
    "checkbox": "BULLET_CHECKBOX",
    "arrow": "BULLET_ARROW_DIAMOND_DISC",
    "star": "BULLET_STAR_CIRCLE_SQUARE",
    "diamond": "BULLET_DIAMONDX_ARROW3D_SQUARE",
}

NUMBER_PRESETS = {
    "decimal": "NUMBERED_DECIMAL_ALPHA_ROMAN",
    "decimalzero": "NUMBERED_ZERODECIMAL_ALPHA_ROMAN",
    "upperletter": "NUMBERED_UPPERALPHA_ALPHA_ROMAN",
    "upperroman": "NUMBERED_UPPERROMAN_UPPERALPHA_DECIMAL",
    "lowerletter": "NUMBERED_DECIMAL_ALPHA_ROMAN",
    "lowerroman": "NUMBERED_DECIMAL_ALPHA_ROMAN",
    "ordinal": "NUMBERED_DECIMAL_ALPHA_ROMAN",
    "cardinaltext": "NUMBERED_DECIMAL_ALPHA_ROMAN",
    "none": "NUMBERED_DECIMAL_ALPHA_ROMAN",
}

# Glyph characters Word writes for common bullets, including the Symbol and
# Wingdings private-use code points.
GLYPH_HINTS = {
    "\u00b7": "disc",       # MIDDLE DOT
    "\u2022": "disc",       # BULLET
    "\uf0b7": "disc",       # Symbol font bullet
    "o": "circle",
    "\u25cb": "circle",     # WHITE CIRCLE
    "\u25aa": "square",     # BLACK SMALL SQUARE
    "\u25a0": "square",     # BLACK SQUARE
    "\uf06e": "square",     # Wingdings filled square
    "\uf0a7": "square",     # Wingdings small filled square
    "\u2713": "checkbox",   # CHECK MARK
    "\u2610": "checkbox",   # BALLOT BOX
    "\uf0a8": "checkbox",   # Wingdings box
    "\u27a4": "arrow",      # BLACK RIGHTWARDS ARROWHEAD
    "\u2192": "arrow",      # RIGHTWARDS ARROW
    "\uf0d8": "arrow",      # Wingdings arrowhead
    "\uf0e0": "arrow",      # Wingdings arrow
    "\u2605": "star",       # BLACK STAR
    "\uf0ab": "star",       # Wingdings star
    "\u2666": "diamond",    # BLACK DIAMOND SUIT
    "\uf075": "diamond",    # Wingdings diamond
}


def _range(start: int, end: int, segment_id: str = "") -> Dict:
    out = {"startIndex": start, "endIndex": end}
    if segment_id:
        out["segmentId"] = segment_id
    return out


def _location(index: int, segment_id: str = "") -> Dict:
    out = {"index": index}
    if segment_id:
        out["segmentId"] = segment_id
    return out


# ---------------------------------------------------------------------------
# Content insertion
# ---------------------------------------------------------------------------


def insert_text(index: int, text: str, segment_id: str = "") -> Dict:
    return {"insertText": {"location": _location(index, segment_id), "text": text}}


def insert_page_break(index: int, segment_id: str = "") -> Dict:
    return {"insertPageBreak": {"location": _location(index, segment_id)}}


def insert_inline_image(
    index: int,
    uri: str,
    width_pt: Optional[float] = None,
    height_pt: Optional[float] = None,
    segment_id: str = "",
) -> Dict:
    request: Dict = {"insertInlineImage": {"location": _location(index, segment_id), "uri": uri}}
    if width_pt and height_pt:
        request["insertInlineImage"]["objectSize"] = {
            "width": dimension(width_pt),
            "height": dimension(height_pt),
        }
    return request


def insert_table(index: int, rows: int, columns: int, segment_id: str = "") -> Dict:
    return {
        "insertTable": {
            "location": _location(index, segment_id),
            "rows": rows,
            "columns": columns,
        }
    }


def insert_section_break(index: int, section_type: str = "NEXT_PAGE") -> Dict:
    return {
        "insertSectionBreak": {"location": _location(index), "sectionType": section_type}
    }


def create_footnote(index: int) -> Dict:
    return {"createFootnote": {"location": _location(index)}}


def create_header(section_break_index: Optional[int] = None) -> Dict:
    request: Dict = {"createHeader": {"type": "DEFAULT"}}
    if section_break_index is not None:
        request["createHeader"]["sectionBreakLocation"] = _location(section_break_index)
    return request


def create_footer(section_break_index: Optional[int] = None) -> Dict:
    request: Dict = {"createFooter": {"type": "DEFAULT"}}
    if section_break_index is not None:
        request["createFooter"]["sectionBreakLocation"] = _location(section_break_index)
    return request


def delete_content(start: int, end: int, segment_id: str = "") -> Dict:
    return {"deleteContentRange": {"range": _range(start, end, segment_id)}}


# ---------------------------------------------------------------------------
# Character formatting
# ---------------------------------------------------------------------------


def text_style_payload(style: TextStyle) -> Dict:
    """The Docs ``TextStyle`` for an IR style, with every toggle made explicit."""
    payload: Dict = {
        "bold": bool(style.bold),
        "italic": bool(style.italic),
        "underline": bool(style.underline),
        "strikethrough": bool(style.strikethrough),
        "smallCaps": bool(style.small_caps),
        "baselineOffset": style.baseline or "NONE",
    }
    if style.font_size_pt:
        payload["fontSize"] = dimension(style.font_size_pt)
    if style.font_family:
        family: Dict = {"fontFamily": style.font_family}
        if style.font_weight:
            family["weight"] = style.font_weight
        elif style.bold:
            family["weight"] = 700
        payload["weightedFontFamily"] = family
    payload["foregroundColor"] = optional_color(style.color) or {}
    payload["backgroundColor"] = optional_color(style.background_color) or {}
    if style.link_url:
        payload["link"] = {"url": style.link_url}
    return payload


def update_text_style(start: int, end: int, style: TextStyle, segment_id: str = "") -> Dict:
    payload = text_style_payload(style)
    # ``link`` stays in the field mask even when the payload has none: inserted
    # text inherits the preceding run's style, so a link has to be cleared
    # explicitly.  Absent from the payload but present in the mask is how that
    # is expressed -- an explicit empty link is rejected outright, with
    # "Links must include at least one type."
    fields = sorted(set(payload) | {"link"})
    return {
        "updateTextStyle": {
            "range": _range(start, end, segment_id),
            "textStyle": payload,
            "fields": ",".join(fields),
        }
    }


# ---------------------------------------------------------------------------
# Paragraph formatting
# ---------------------------------------------------------------------------


def _border_payload(border: Optional[Border]) -> Optional[Dict]:
    if border is None:
        return None
    return {
        "color": optional_color(border.color) or {"color": {"rgbColor": {}}},
        "width": dimension(border.width_pt or 0.0),
        "padding": dimension(border.padding_pt or 0.0),
        "dashStyle": border.dash_style or "SOLID",
    }


def paragraph_style_payload(props: ParagraphProps, include_indents: bool = True) -> Dict:
    payload: Dict = {}
    if props.alignment:
        payload["alignment"] = props.alignment
    if props.direction:
        payload["direction"] = props.direction
    if props.line_spacing is not None:
        payload["lineSpacing"] = props.line_spacing
    if props.space_mode:
        payload["spacingMode"] = props.space_mode
    if props.space_above_pt is not None:
        payload["spaceAbove"] = dimension(props.space_above_pt)
    if props.space_below_pt is not None:
        payload["spaceBelow"] = dimension(props.space_below_pt)
    if include_indents:
        if props.indent_start_pt is not None:
            payload["indentStart"] = dimension(props.indent_start_pt)
        if props.indent_end_pt is not None:
            payload["indentEnd"] = dimension(props.indent_end_pt)
        if props.indent_first_line_pt is not None:
            payload["indentFirstLine"] = dimension(props.indent_first_line_pt)
    if props.keep_with_next is not None:
        payload["keepWithNext"] = bool(props.keep_with_next)
    if props.keep_lines_together is not None:
        payload["keepLinesTogether"] = bool(props.keep_lines_together)
    if props.avoid_widow_and_orphan is not None:
        payload["avoidWidowAndOrphan"] = bool(props.avoid_widow_and_orphan)
    if props.page_break_before:
        payload["pageBreakBefore"] = True
    if props.shading_color:
        payload["shading"] = {"backgroundColor": optional_color(props.shading_color)}
    for name, border in (
        ("borderTop", props.border_top),
        ("borderBottom", props.border_bottom),
        ("borderLeft", props.border_left),
        ("borderRight", props.border_right),
        ("borderBetween", props.border_between),
    ):
        rendered = _border_payload(border)
        if rendered is not None:
            payload[name] = rendered
    return payload


def update_named_style(start: int, end: int, named_style: str, segment_id: str = "") -> Dict:
    return {
        "updateParagraphStyle": {
            "range": _range(start, end, segment_id),
            "paragraphStyle": {"namedStyleType": named_style},
            "fields": "namedStyleType",
        }
    }


def update_paragraph_style(
    start: int,
    end: int,
    props: ParagraphProps,
    segment_id: str = "",
    include_indents: bool = True,
) -> Optional[Dict]:
    payload = paragraph_style_payload(props, include_indents=include_indents)
    if not payload:
        return None
    return {
        "updateParagraphStyle": {
            "range": _range(start, end, segment_id),
            "paragraphStyle": payload,
            "fields": ",".join(sorted(payload)),
        }
    }


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------


def bullet_preset(marker: ListMarker) -> str:
    """Pick the Docs preset that comes closest to a .docx numbering level."""
    number_format = (marker.number_format or "bullet").lower()
    if number_format == "bullet":
        glyph = (marker.glyph_symbol or "").strip()
        for character in glyph:
            hint = GLYPH_HINTS.get(character)
            if hint:
                return BULLET_PRESETS[hint]
        return BULLET_PRESETS["disc"]
    level_text = marker.level_text or ""
    if number_format == "decimal" and level_text.count("%") > 1:
        return "NUMBERED_DECIMAL_NESTED"
    if number_format == "decimal" and level_text.endswith(")"):
        return "NUMBERED_DECIMAL_ALPHA_ROMAN_PARENS"
    return NUMBER_PRESETS.get(number_format, "NUMBERED_DECIMAL_ALPHA_ROMAN")


def create_bullets(start: int, end: int, marker: ListMarker, segment_id: str = "") -> Dict:
    return {
        "createParagraphBullets": {
            "range": _range(start, end, segment_id),
            "bulletPreset": bullet_preset(marker),
        }
    }


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _cell_border_payload(border: Optional[Border]) -> Dict:
    if border is None:
        return {"color": {"color": {"rgbColor": {}}}, "width": dimension(0), "dashStyle": "SOLID"}
    return {
        "color": optional_color(border.color) or {"color": {"rgbColor": {}}},
        "width": dimension(border.width_pt or 0.0),
        "dashStyle": border.dash_style or "SOLID",
    }


def update_table_cell_style(
    table_start: int,
    row: int,
    column: int,
    cell: TableCell,
    segment_id: str = "",
    row_span: int = 1,
    column_span: int = 1,
) -> Optional[Dict]:
    payload: Dict = {}
    if cell.background_color:
        payload["backgroundColor"] = optional_color(cell.background_color)
    if cell.vertical_alignment:
        payload["contentAlignment"] = cell.vertical_alignment
    for name, value in (
        ("paddingTop", cell.padding_top_pt),
        ("paddingBottom", cell.padding_bottom_pt),
        ("paddingLeft", cell.padding_left_pt),
        ("paddingRight", cell.padding_right_pt),
    ):
        if value is not None:
            payload[name] = dimension(value)
    for name, border in (
        ("borderTop", cell.border_top),
        ("borderBottom", cell.border_bottom),
        ("borderLeft", cell.border_left),
        ("borderRight", cell.border_right),
    ):
        if border is not None:
            payload[name] = _cell_border_payload(border)
    if not payload:
        return None
    return {
        "updateTableCellStyle": {
            "tableRange": {
                "tableCellLocation": {
                    "tableStartLocation": _location(table_start, segment_id),
                    "rowIndex": row,
                    "columnIndex": column,
                },
                "rowSpan": row_span,
                "columnSpan": column_span,
            },
            "tableCellStyle": payload,
            "fields": ",".join(sorted(payload)),
        }
    }


def update_column_width(
    table_start: int, columns: List[int], width_pt: float, segment_id: str = ""
) -> Dict:
    return {
        "updateTableColumnProperties": {
            "tableStartLocation": _location(table_start, segment_id),
            "columnIndices": columns,
            "tableColumnProperties": {
                "widthType": "FIXED_WIDTH",
                "width": dimension(width_pt),
            },
            "fields": "widthType,width",
        }
    }


def update_row_style(
    table_start: int,
    rows: List[int],
    min_height_pt: Optional[float],
    segment_id: str = "",
) -> Optional[Dict]:
    """The writable part of a row's style, which is only its height.

    ``tableRowStyle.tableHeader`` is readable but not writable: naming it here
    makes the API reject the whole request with "Unallowed field: tableHeader".
    A repeating header row is reported as unsupported instead.
    """
    payload: Dict = {}
    if min_height_pt is not None:
        payload["minRowHeight"] = dimension(min_height_pt)
    if not payload:
        return None
    return {
        "updateTableRowStyle": {
            "tableStartLocation": _location(table_start, segment_id),
            "rowIndices": rows,
            "tableRowStyle": payload,
            "fields": ",".join(sorted(payload)),
        }
    }


def merge_cells(
    table_start: int,
    row: int,
    column: int,
    row_span: int,
    column_span: int,
    segment_id: str = "",
) -> Dict:
    return {
        "mergeTableCells": {
            "tableRange": {
                "tableCellLocation": {
                    "tableStartLocation": _location(table_start, segment_id),
                    "rowIndex": row,
                    "columnIndex": column,
                },
                "rowSpan": row_span,
                "columnSpan": column_span,
            }
        }
    }


# ---------------------------------------------------------------------------
# Page setup
# ---------------------------------------------------------------------------


def update_document_style(props: SectionProps) -> Optional[Dict]:
    payload: Dict = {}
    if props.page_width_pt and props.page_height_pt:
        payload["pageSize"] = {
            "width": dimension(props.page_width_pt),
            "height": dimension(props.page_height_pt),
        }
    for name, value in (
        ("marginTop", props.margin_top_pt),
        ("marginBottom", props.margin_bottom_pt),
        ("marginLeft", props.margin_left_pt),
        ("marginRight", props.margin_right_pt),
        ("marginHeader", props.margin_header_pt),
        ("marginFooter", props.margin_footer_pt),
    ):
        if value is not None:
            payload[name] = dimension(value)
    if not payload:
        return None
    return {
        "updateDocumentStyle": {
            "documentStyle": payload,
            "fields": ",".join(sorted(payload)),
        }
    }


def update_section_style(start: int, end: int, props: SectionProps) -> Optional[Dict]:
    payload: Dict = {}
    for name, value in (
        ("marginTop", props.margin_top_pt),
        ("marginBottom", props.margin_bottom_pt),
        ("marginLeft", props.margin_left_pt),
        ("marginRight", props.margin_right_pt),
        ("marginHeader", props.margin_header_pt),
        ("marginFooter", props.margin_footer_pt),
    ):
        if value is not None:
            payload[name] = dimension(value)
    if props.column_count and props.column_count > 1:
        width = None
        if props.page_width_pt is not None:
            usable = props.page_width_pt - (props.margin_left_pt or 0) - (props.margin_right_pt or 0)
            gaps = (props.column_gap_pt or 0) * (props.column_count - 1)
            width = max((usable - gaps) / props.column_count, 1.0)
        column: Dict = {}
        if width is not None:
            column["width"] = dimension(width)
        if props.column_gap_pt is not None:
            column["paddingEnd"] = dimension(props.column_gap_pt)
        payload["columnProperties"] = [dict(column) for _ in range(props.column_count)]
    if not payload:
        return None
    return {
        "updateSectionStyle": {
            "range": _range(start, end),
            "sectionStyle": payload,
            "fields": ",".join(sorted(payload)),
        }
    }
