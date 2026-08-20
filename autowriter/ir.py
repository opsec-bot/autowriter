"""The intermediate representation shared by the reader and the writer.

The .docx reader resolves every OOXML inheritance chain (document defaults ->
style hierarchy -> numbering style -> direct formatting) and emits this IR with
*fully explicit* formatting.  The Google Docs writer then re-types that IR into
a document, never relying on the target document's own default styles.  That is
what makes the copy 1:1 instead of "close enough": nothing is left to be
inherited from a stylesheet that does not exist on the other side.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from typing import Dict, List, Optional, Sequence, Union


# --------------------------------------------------------------------------
# Character formatting
# --------------------------------------------------------------------------


@dataclass
class TextStyle:
    """Effective character formatting for a run.

    ``None`` means "not specified anywhere in the source document".  Once the
    style chain has been resolved most fields are populated, because the .docx
    document defaults specify them.
    """

    bold: Optional[bool] = None
    italic: Optional[bool] = None
    underline: Optional[bool] = None
    strikethrough: Optional[bool] = None
    small_caps: Optional[bool] = None
    all_caps: Optional[bool] = None
    baseline: Optional[str] = None  # NONE | SUPERSCRIPT | SUBSCRIPT
    font_family: Optional[str] = None
    font_weight: Optional[int] = None  # 100..900, Docs' WeightedFontFamily
    font_size_pt: Optional[float] = None
    color: Optional[str] = None  # RRGGBB
    background_color: Optional[str] = None  # RRGGBB, from w:highlight / w:shd
    link_url: Optional[str] = None
    hidden: bool = False

    def merged_with(self, other: "TextStyle") -> "TextStyle":
        """Overlay ``other`` on top of ``self``; ``None`` fields defer."""
        values = {}
        for f in fields(self):
            override = getattr(other, f.name)
            if f.name == "hidden":
                values[f.name] = bool(getattr(self, f.name)) or bool(override)
            elif override is None:
                values[f.name] = getattr(self, f.name)
            else:
                values[f.name] = override
        return TextStyle(**values)

    def with_link(self, url: Optional[str]) -> "TextStyle":
        return replace(self, link_url=url)


# --------------------------------------------------------------------------
# Paragraph formatting
# --------------------------------------------------------------------------


@dataclass
class Border:
    color: Optional[str] = None
    width_pt: Optional[float] = None
    dash_style: Optional[str] = None  # SOLID | DOT | DASH
    padding_pt: Optional[float] = None


@dataclass
class ParagraphProps:
    named_style: str = "NORMAL_TEXT"  # NORMAL_TEXT | TITLE | SUBTITLE | HEADING_1..6
    alignment: Optional[str] = None  # START | CENTER | END | JUSTIFIED
    direction: Optional[str] = None  # LEFT_TO_RIGHT | RIGHT_TO_LEFT
    line_spacing: Optional[float] = None  # percent; 100 == single spacing
    space_above_pt: Optional[float] = None
    space_below_pt: Optional[float] = None
    space_mode: Optional[str] = None  # NEVER_COLLAPSE | COLLAPSE_LISTS
    indent_start_pt: Optional[float] = None
    indent_end_pt: Optional[float] = None
    indent_first_line_pt: Optional[float] = None
    keep_with_next: Optional[bool] = None
    keep_lines_together: Optional[bool] = None
    avoid_widow_and_orphan: Optional[bool] = None
    shading_color: Optional[str] = None
    border_top: Optional[Border] = None
    border_bottom: Optional[Border] = None
    border_left: Optional[Border] = None
    border_right: Optional[Border] = None
    border_between: Optional[Border] = None
    page_break_before: bool = False
    tab_stops_pt: List[float] = field(default_factory=list)


@dataclass
class ListMarker:
    """Where a paragraph sits in a .docx numbering definition."""

    num_id: str
    level: int
    number_format: str = "bullet"  # bullet | decimal | lowerLetter | upperRoman | ...
    glyph_symbol: Optional[str] = None
    start_at: Optional[int] = None
    level_text: Optional[str] = None


# --------------------------------------------------------------------------
# Inline content
# --------------------------------------------------------------------------


@dataclass
class TextRun:
    text: str
    style: TextStyle = field(default_factory=TextStyle)


@dataclass
class ImageRun:
    asset_id: str
    width_pt: Optional[float] = None
    height_pt: Optional[float] = None
    alt_title: Optional[str] = None
    alt_description: Optional[str] = None
    style: TextStyle = field(default_factory=TextStyle)


@dataclass
class PageBreakRun:
    style: TextStyle = field(default_factory=TextStyle)


@dataclass
class FootnoteRun:
    """A footnote reference plus the paragraphs that make up its body."""

    blocks: List["Block"] = field(default_factory=list)
    style: TextStyle = field(default_factory=TextStyle)


Inline = Union[TextRun, ImageRun, PageBreakRun, FootnoteRun]


@dataclass
class Paragraph:
    inlines: List[Inline] = field(default_factory=list)
    props: ParagraphProps = field(default_factory=ParagraphProps)
    list_marker: Optional[ListMarker] = None
    mark_style: TextStyle = field(default_factory=TextStyle)
    source_style_id: Optional[str] = None

    @property
    def text(self) -> str:
        out = []
        for inline in self.inlines:
            if isinstance(inline, TextRun):
                out.append(inline.text)
            elif isinstance(inline, ImageRun):
                out.append("")  # Docs' inline-object placeholder rune
            elif isinstance(inline, PageBreakRun):
                out.append("\f")
        return "".join(out)


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------


@dataclass
class TableCell:
    blocks: List["Block"] = field(default_factory=list)
    row_span: int = 1
    col_span: int = 1
    background_color: Optional[str] = None
    vertical_alignment: Optional[str] = None  # TOP | MIDDLE | BOTTOM
    padding_top_pt: Optional[float] = None
    padding_bottom_pt: Optional[float] = None
    padding_left_pt: Optional[float] = None
    padding_right_pt: Optional[float] = None
    border_top: Optional[Border] = None
    border_bottom: Optional[Border] = None
    border_left: Optional[Border] = None
    border_right: Optional[Border] = None
    merged_away: bool = False  # covered by another cell's span


@dataclass
class TableRow:
    cells: List[TableCell] = field(default_factory=list)
    min_height_pt: Optional[float] = None
    is_header: bool = False


@dataclass
class Table:
    rows: List[TableRow] = field(default_factory=list)
    column_widths_pt: List[Optional[float]] = field(default_factory=list)

    @property
    def column_count(self) -> int:
        if self.column_widths_pt:
            return len(self.column_widths_pt)
        return max((sum(c.col_span for c in r.cells) for r in self.rows), default=0)


Block = Union[Paragraph, Table]


# --------------------------------------------------------------------------
# Sections and the document
# --------------------------------------------------------------------------


@dataclass
class HeaderFooter:
    blocks: List[Block] = field(default_factory=list)


@dataclass
class SectionProps:
    page_width_pt: Optional[float] = None
    page_height_pt: Optional[float] = None
    margin_top_pt: Optional[float] = None
    margin_bottom_pt: Optional[float] = None
    margin_left_pt: Optional[float] = None
    margin_right_pt: Optional[float] = None
    margin_header_pt: Optional[float] = None
    margin_footer_pt: Optional[float] = None
    column_count: int = 1
    column_gap_pt: Optional[float] = None
    different_first_page: bool = False
    different_even_odd: bool = False
    section_type: str = "NEXT_PAGE"  # NEXT_PAGE | CONTINUOUS


@dataclass
class Section:
    blocks: List[Block] = field(default_factory=list)
    props: SectionProps = field(default_factory=SectionProps)
    headers: Dict[str, HeaderFooter] = field(default_factory=dict)  # default|first|even
    footers: Dict[str, HeaderFooter] = field(default_factory=dict)


@dataclass
class ImageAsset:
    asset_id: str
    data: bytes
    content_type: str
    extension: str


@dataclass
class Note:
    """Something the source document contains that the copy cannot mirror."""

    kind: str
    message: str
    location: Optional[str] = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        where = f" [{self.location}]" if self.location else ""
        return f"{self.kind}: {self.message}{where}"


@dataclass
class Document:
    sections: List[Section] = field(default_factory=list)
    assets: Dict[str, ImageAsset] = field(default_factory=dict)
    notes: List[Note] = field(default_factory=list)
    title: Optional[str] = None

    def iter_blocks(self) -> Sequence[Block]:
        out: List[Block] = []
        for section in self.sections:
            out.extend(section.blocks)
        return out

    def note(self, kind: str, message: str, location: Optional[str] = None) -> None:
        self.notes.append(Note(kind, message, location))
