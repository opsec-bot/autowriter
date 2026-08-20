"""Read the finished Google Doc back and check it against the source.

"1:1" is a claim, and a claim needs evidence.  This module re-reads the
document that was written — from the real API or the simulator, the shape is
the same — and compares it with the intermediate representation the .docx was
parsed into, character by character and property by property.

Anything the Docs API cannot express (see the notes on the copy result) will
show up here as a difference; that is intentional.  A clean report means the
copy is faithful, and a dirty one says exactly where it is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .. import ir
from ..units import rgb_color_to_hex, u16len

TOLERANCE = 0.05

#: Placeholder for a position the source fills with something that is not text.
OPAQUE = "￼"  # OBJECT REPLACEMENT CHARACTER


@dataclass
class Difference:
    location: str
    field: str
    expected: Any
    actual: Any

    def __str__(self) -> str:
        return "%s: %s expected %r, found %r" % (
            self.location,
            self.field,
            self.expected,
            self.actual,
        )


@dataclass
class VerificationReport:
    differences: List[Difference] = field(default_factory=list)
    paragraphs_checked: int = 0
    tables_checked: int = 0
    characters_checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.differences

    def add(self, location: str, field_name: str, expected: Any, actual: Any) -> None:
        self.differences.append(Difference(location, field_name, expected, actual))

    def summary(self) -> str:
        if self.ok:
            return "verified: %d paragraphs, %d tables, %d characters match the source" % (
                self.paragraphs_checked,
                self.tables_checked,
                self.characters_checked,
            )
        return "%d difference(s) across %d paragraphs" % (
            len(self.differences),
            self.paragraphs_checked,
        )


# ---------------------------------------------------------------------------
# Style summaries: the properties both sides are compared on
# ---------------------------------------------------------------------------


def expected_text_style(style: ir.TextStyle) -> Dict[str, Any]:
    return {
        "bold": bool(style.bold),
        "italic": bool(style.italic),
        "underline": bool(style.underline),
        "strikethrough": bool(style.strikethrough),
        "smallCaps": bool(style.small_caps),
        "baselineOffset": style.baseline or "NONE",
        "fontSize": style.font_size_pt,
        "fontFamily": style.font_family,
        "foreground": style.color.upper() if style.color else None,
        "background": style.background_color.upper() if style.background_color else None,
        "link": style.link_url,
    }


def actual_text_style(style: Dict) -> Dict[str, Any]:
    size = style.get("fontSize", {}).get("magnitude") if style.get("fontSize") else None
    family = (style.get("weightedFontFamily") or {}).get("fontFamily")
    return {
        "bold": bool(style.get("bold")),
        "italic": bool(style.get("italic")),
        "underline": bool(style.get("underline")),
        "strikethrough": bool(style.get("strikethrough")),
        "smallCaps": bool(style.get("smallCaps")),
        "baselineOffset": style.get("baselineOffset") or "NONE",
        "fontSize": size,
        "fontFamily": family,
        "foreground": rgb_color_to_hex(style.get("foregroundColor")),
        "background": rgb_color_to_hex(style.get("backgroundColor")),
        "link": (style.get("link") or {}).get("url"),
    }


def expected_paragraph_style(props: ir.ParagraphProps) -> Dict[str, Any]:
    return {
        "namedStyleType": props.named_style or "NORMAL_TEXT",
        "alignment": props.alignment,
        "lineSpacing": props.line_spacing,
        "spaceAbove": props.space_above_pt,
        "spaceBelow": props.space_below_pt,
        "indentStart": props.indent_start_pt,
        "indentEnd": props.indent_end_pt,
        "indentFirstLine": props.indent_first_line_pt,
        "keepWithNext": props.keep_with_next,
        "keepLinesTogether": props.keep_lines_together,
        "direction": props.direction,
        "shading": props.shading_color.upper() if props.shading_color else None,
        "pageBreakBefore": props.page_break_before or None,
    }


def actual_paragraph_style(style: Dict) -> Dict[str, Any]:
    def magnitude(name: str) -> Optional[float]:
        value = style.get(name)
        return value.get("magnitude") if isinstance(value, dict) else None

    shading = style.get("shading", {}).get("backgroundColor")
    return {
        "namedStyleType": style.get("namedStyleType") or "NORMAL_TEXT",
        "alignment": style.get("alignment"),
        "lineSpacing": style.get("lineSpacing"),
        "spaceAbove": magnitude("spaceAbove"),
        "spaceBelow": magnitude("spaceBelow"),
        "indentStart": magnitude("indentStart"),
        "indentEnd": magnitude("indentEnd"),
        "indentFirstLine": magnitude("indentFirstLine"),
        "keepWithNext": style.get("keepWithNext"),
        "keepLinesTogether": style.get("keepLinesTogether"),
        "direction": style.get("direction"),
        "shading": rgb_color_to_hex(shading) if shading else None,
        "pageBreakBefore": style.get("pageBreakBefore") or None,
    }


def _same(expected: Any, actual: Any) -> bool:
    if isinstance(expected, float) or isinstance(actual, float):
        if expected is None or actual is None:
            return expected is None and actual is None
        return abs(float(expected) - float(actual)) <= TOLERANCE
    return expected == actual


# ---------------------------------------------------------------------------
# Flattening both sides into comparable sequences
# ---------------------------------------------------------------------------


def model_characters(paragraph: ir.Paragraph, options=None) -> Tuple[str, List[Dict[str, Any]]]:
    """The text a copy of this paragraph should contain, with a style per char."""
    text: List[str] = []
    styles: List[Dict[str, Any]] = []
    render_caps = getattr(options, "render_all_caps", True)
    include_hidden = getattr(options, "include_hidden_text", False)
    for inline in paragraph.inlines:
        if isinstance(inline, ir.TextRun):
            if inline.style.hidden and not include_hidden:
                continue
            content = inline.text.upper() if (inline.style.all_caps and render_caps) else inline.text
            summary = expected_text_style(inline.style)
            for _ in range(u16len(content)):
                styles.append(summary)
            text.append(content)
        elif isinstance(inline, (ir.ImageRun, ir.PageBreakRun, ir.FootnoteRun)):
            text.append(OPAQUE)
            styles.append(expected_text_style(getattr(inline, "style", ir.TextStyle())))
    return "".join(text), styles


def doc_characters(element: Dict) -> Tuple[str, List[Dict[str, Any]]]:
    text: List[str] = []
    styles: List[Dict[str, Any]] = []
    elements = element.get("paragraph", {}).get("elements", [])
    for index, item in enumerate(elements):
        run = item.get("textRun")
        if run is not None:
            content = run.get("content", "")
            if index == len(elements) - 1 and content.endswith("\n"):
                content = content[:-1]
            summary = actual_text_style(run.get("textStyle", {}))
            for _ in range(u16len(content)):
                styles.append(summary)
            text.append(content)
            continue
        for key in ("inlineObjectElement", "pageBreak", "footnoteReference"):
            if key in item:
                text.append(OPAQUE)
                styles.append(actual_text_style(item[key].get("textStyle", {})))
                break
    return "".join(text), styles


def iter_model(blocks: Sequence[ir.Block], prefix: str = "") -> Iterator[Tuple[str, Any, str]]:
    for index, block in enumerate(blocks):
        label = "%s[%d]" % (prefix, index)
        if isinstance(block, ir.Table):
            yield "table", block, label
            for row_index, row in enumerate(block.rows):
                column = 0
                for cell in row.cells:
                    if not cell.merged_away:
                        yield from iter_model(
                            cell.blocks, "%s r%dc%d" % (label, row_index, column)
                        )
                    column += cell.col_span
        else:
            yield "paragraph", block, label


def iter_doc(content: Sequence[Dict]) -> Iterator[Tuple[str, Dict]]:
    for element in content or []:
        if "paragraph" in element:
            yield "paragraph", element
        elif "table" in element:
            yield "table", element
            for row in element["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    yield from iter_doc(cell.get("content", []))


def is_empty_paragraph(element: Dict) -> bool:
    text, _styles = doc_characters(element)
    return not text.strip(" ")


# ---------------------------------------------------------------------------
# The comparison itself
# ---------------------------------------------------------------------------


def verify(
    document: ir.Document,
    snapshot: Dict,
    options=None,
    check_headers: bool = True,
) -> VerificationReport:
    report = VerificationReport()
    blocks: List[ir.Block] = []
    for section in document.sections:
        blocks.extend(section.blocks)
    _compare_segment(blocks, snapshot.get("body", {}).get("content", []), report, "body", options)

    if check_headers:
        _compare_header_footers(document, snapshot, report, options)
    _compare_page_setup(document, snapshot, report)
    return report


def _compare_header_footers(
    document: ir.Document, snapshot: Dict, report: VerificationReport, options
) -> None:
    for kind, key in (("header", "headers"), ("footer", "footers")):
        segments = list(snapshot.get(key, {}).values())
        expected = [
            section.headers.get("default") if kind == "header" else section.footers.get("default")
            for section in document.sections
        ]
        expected = [item for item in expected if item is not None and item.blocks]
        if not expected:
            continue
        if not segments:
            report.add(kind, "presence", "%d %s(s)" % (len(expected), kind), "none")
            continue
        _compare_segment(
            expected[0].blocks, segments[0].get("content", []), report, kind, options
        )


def _compare_segment(
    blocks: Sequence[ir.Block],
    content: Sequence[Dict],
    report: VerificationReport,
    location: str,
    options,
) -> None:
    model = list(iter_model(blocks, location))
    actual = list(iter_doc(content))

    model_index = 0
    actual_index = 0
    while model_index < len(model) and actual_index < len(actual):
        kind, block, label = model[model_index]
        actual_kind, element = actual[actual_index]

        if kind != actual_kind:
            # Docs always keeps a paragraph after a table, so an unmatched empty
            # paragraph on the copy's side is structural, not a difference.
            if actual_kind == "paragraph" and is_empty_paragraph(element):
                actual_index += 1
                continue
            report.add(label, "structure", kind, actual_kind)
            model_index += 1
            actual_index += 1
            continue

        if kind == "paragraph":
            _compare_paragraph(block, element, report, label, options)
            report.paragraphs_checked += 1
        else:
            _compare_table(block, element, report, label)
            report.tables_checked += 1
        model_index += 1
        actual_index += 1

    for _kind, _block, label in model[model_index:]:
        report.add(label, "presence", "present", "missing")
    for _kind, element in actual[actual_index:]:
        if _kind == "paragraph" and is_empty_paragraph(element):
            continue
        report.add(location, "presence", "nothing", "extra %s" % _kind)


def _compare_paragraph(
    paragraph: ir.Paragraph,
    element: Dict,
    report: VerificationReport,
    label: str,
    options,
) -> None:
    expected_text, expected_styles = model_characters(paragraph, options)
    actual_text, actual_styles = doc_characters(element)

    if expected_text != actual_text:
        report.add(label, "text", expected_text, actual_text)
        return
    report.characters_checked += len(actual_text)

    for position, (wanted, found) in enumerate(zip(expected_styles, actual_styles)):
        for name in wanted:
            if not _same(wanted[name], found.get(name)):
                report.add(
                    "%s char %d" % (label, position),
                    name,
                    wanted[name],
                    found.get(name),
                )
                break  # one report per character is plenty

    style = element.get("paragraph", {}).get("paragraphStyle", {})
    wanted_paragraph = expected_paragraph_style(paragraph.props)
    found_paragraph = actual_paragraph_style(style)
    for name, value in wanted_paragraph.items():
        if value is None:
            continue
        if not _same(value, found_paragraph.get(name)):
            report.add(label, name, value, found_paragraph.get(name))

    bullet = element.get("paragraph", {}).get("bullet")
    if paragraph.list_marker is not None:
        if bullet is None:
            report.add(label, "bullet", "list item", "none")
        elif bullet.get("nestingLevel", 0) != paragraph.list_marker.level:
            report.add(
                label, "bullet level", paragraph.list_marker.level, bullet.get("nestingLevel", 0)
            )
    elif bullet is not None:
        report.add(label, "bullet", "none", "list item")


def _compare_table(table: ir.Table, element: Dict, report: VerificationReport, label: str) -> None:
    actual = element.get("table", {})
    rows = actual.get("tableRows", [])
    if len(rows) != len(table.rows):
        report.add(label, "rows", len(table.rows), len(rows))
    if actual.get("columns") not in (None, table.column_count):
        report.add(label, "columns", table.column_count, actual.get("columns"))

    properties = actual.get("tableStyle", {}).get("tableColumnProperties", [])
    for index, width in enumerate(table.column_widths_pt):
        if width is None or index >= len(properties):
            continue
        found = (properties[index] or {}).get("width", {}).get("magnitude")
        if found is not None and not _same(width, found):
            report.add("%s column %d" % (label, index), "width", width, found)

    for row_index, row in enumerate(table.rows):
        if row_index >= len(rows):
            break
        cells = rows[row_index].get("tableCells", [])
        column = 0
        position = 0
        for cell in row.cells:
            if cell.merged_away:
                column += cell.col_span
                continue
            if position >= len(cells):
                break
            found = cells[position].get("tableCellStyle", {})
            if cell.background_color:
                actual_color = rgb_color_to_hex(found.get("backgroundColor"))
                if actual_color != cell.background_color.upper():
                    report.add(
                        "%s r%dc%d" % (label, row_index, column),
                        "background",
                        cell.background_color.upper(),
                        actual_color,
                    )
            if cell.col_span > 1 and (found.get("columnSpan") or 1) != cell.col_span:
                report.add(
                    "%s r%dc%d" % (label, row_index, column),
                    "columnSpan",
                    cell.col_span,
                    found.get("columnSpan") or 1,
                )
            if cell.row_span > 1 and (found.get("rowSpan") or 1) != cell.row_span:
                report.add(
                    "%s r%dc%d" % (label, row_index, column),
                    "rowSpan",
                    cell.row_span,
                    found.get("rowSpan") or 1,
                )
            column += cell.col_span
            position += 1


def _compare_page_setup(
    document: ir.Document, snapshot: Dict, report: VerificationReport
) -> None:
    if not document.sections:
        return
    props = document.sections[0].props
    style = snapshot.get("documentStyle", {})

    def magnitude(node: Optional[Dict]) -> Optional[float]:
        return node.get("magnitude") if isinstance(node, dict) else None

    page_size = style.get("pageSize", {})
    for name, expected, found in (
        ("page width", props.page_width_pt, magnitude(page_size.get("width"))),
        ("page height", props.page_height_pt, magnitude(page_size.get("height"))),
        ("margin top", props.margin_top_pt, magnitude(style.get("marginTop"))),
        ("margin bottom", props.margin_bottom_pt, magnitude(style.get("marginBottom"))),
        ("margin left", props.margin_left_pt, magnitude(style.get("marginLeft"))),
        ("margin right", props.margin_right_pt, magnitude(style.get("marginRight"))),
    ):
        if expected is None:
            continue
        if not _same(expected, found):
            report.add("page setup", name, expected, found)
