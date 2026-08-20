"""Resolution of the OOXML style hierarchy into fully explicit formatting.

A .docx paragraph's real appearance is the sum of five layers.  ECMA-376
§17.7.2 defines the order in which they apply:

    document defaults -> table styles -> numbering -> paragraph style
    -> character style -> direct formatting

Word leans on that chain heavily: a "Heading 1" paragraph usually carries no
direct formatting at all.  Google Docs has its own, different, defaults, so
copying only the direct formatting across would produce a document that looks
nothing like the original.  This module flattens the chain so the writer can
state every attribute explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from ..ir import Border, ParagraphProps, TextStyle
from ..units import twips_to_pt
from .oxml import attr, findall, get, int_val, qn, val
from .props import (
    merge_paragraph_props,
    parse_border,
    parse_paragraph_props,
    parse_run_props,
)

HEADING_NAMES = {
    "heading 1": "HEADING_1",
    "heading 2": "HEADING_2",
    "heading 3": "HEADING_3",
    "heading 4": "HEADING_4",
    "heading 5": "HEADING_5",
    "heading 6": "HEADING_6",
    "title": "TITLE",
    "subtitle": "SUBTITLE",
}

# Least specific first: later entries override earlier ones.
CONDITIONAL_ORDER = [
    "wholeTable",
    "band2Vert",
    "band1Vert",
    "band2Horz",
    "band1Horz",
    "lastCol",
    "firstCol",
    "lastRow",
    "firstRow",
    "swCell",
    "seCell",
    "nwCell",
    "neCell",
]


@dataclass
class StyleDef:
    style_id: str
    style_type: str
    name: str = ""
    based_on: Optional[str] = None
    is_default: bool = False
    ppr: Optional[ET.Element] = None
    rpr: Optional[ET.Element] = None
    tbl_pr: Optional[ET.Element] = None
    tc_pr: Optional[ET.Element] = None
    tr_pr: Optional[ET.Element] = None
    conditional: Dict[str, ET.Element] = field(default_factory=dict)
    num_id: Optional[str] = None
    num_level: Optional[int] = None


@dataclass
class CellFormat:
    background_color: Optional[str] = None
    vertical_alignment: Optional[str] = None
    padding_top_pt: Optional[float] = None
    padding_bottom_pt: Optional[float] = None
    padding_left_pt: Optional[float] = None
    padding_right_pt: Optional[float] = None
    border_top: Optional[Border] = None
    border_bottom: Optional[Border] = None
    border_left: Optional[Border] = None
    border_right: Optional[Border] = None
    border_inside_h: Optional[Border] = None
    border_inside_v: Optional[Border] = None


def merge_cell_format(base: Optional[CellFormat], override: Optional[CellFormat]) -> CellFormat:
    if base is None:
        return override or CellFormat()
    if override is None:
        return base
    values = {}
    for name in CellFormat.__dataclass_fields__:
        new = getattr(override, name)
        values[name] = getattr(base, name) if new is None else new
    return CellFormat(**values)


class StyleResolver:
    """Flattens styles.xml (plus document defaults and theme fonts)."""

    def __init__(
        self,
        styles_root: Optional[ET.Element],
        theme_fonts: Optional[Dict[str, str]] = None,
        notes: Optional[List[str]] = None,
    ):
        self.notes = notes if notes is not None else []
        self.theme_fonts = theme_fonts or {}
        self.styles: Dict[str, StyleDef] = {}
        self.default_paragraph_style: Optional[str] = None
        self.default_character_style: Optional[str] = None
        self.default_table_style: Optional[str] = None
        self.doc_default_ppr = ParagraphProps(named_style=None)  # type: ignore[arg-type]
        self.doc_default_rpr = TextStyle()
        self._paragraph_cache: Dict[str, Tuple[ParagraphProps, TextStyle]] = {}
        self._run_cache: Dict[str, TextStyle] = {}
        if styles_root is not None:
            self._load(styles_root)

    # -- loading -----------------------------------------------------------

    def _load(self, root: ET.Element) -> None:
        defaults = get(root, "w:docDefaults")
        if defaults is not None:
            rpr_default = get(defaults, "w:rPrDefault")
            self.doc_default_rpr = parse_run_props(
                get(rpr_default, "w:rPr"), self.theme_fonts, self.notes
            )
            ppr_default = get(defaults, "w:pPrDefault")
            self.doc_default_ppr = parse_paragraph_props(get(ppr_default, "w:pPr"), self.notes)

        for node in findall(root, "w:style"):
            style_id = attr(node, "w:styleId")
            if not style_id:
                continue
            style_type = (attr(node, "w:type") or "paragraph").lower()
            num_pr = get(get(node, "w:pPr"), "w:numPr")
            definition = StyleDef(
                style_id=style_id,
                style_type=style_type,
                name=(val(get(node, "w:name")) or "").strip(),
                based_on=val(get(node, "w:basedOn")),
                is_default=attr(node, "w:default") in ("1", "true", "on"),
                ppr=get(node, "w:pPr"),
                rpr=get(node, "w:rPr"),
                tbl_pr=get(node, "w:tblPr"),
                tc_pr=get(node, "w:tcPr"),
                tr_pr=get(node, "w:trPr"),
                num_id=val(get(num_pr, "w:numId")),
                num_level=int_val(get(num_pr, "w:ilvl")),
            )
            for conditional in findall(node, "w:tblStylePr"):
                kind = attr(conditional, "w:type")
                if kind:
                    definition.conditional[kind] = conditional
            self.styles[style_id] = definition

            if definition.is_default:
                if style_type == "paragraph":
                    self.default_paragraph_style = style_id
                elif style_type == "character":
                    self.default_character_style = style_id
                elif style_type == "table":
                    self.default_table_style = style_id

    # -- chains ------------------------------------------------------------

    def chain(self, style_id: Optional[str]) -> List[StyleDef]:
        """Root-first list of styles, following ``w:basedOn``."""
        out: List[StyleDef] = []
        seen = set()
        current = style_id
        while current and current in self.styles and current not in seen:
            seen.add(current)
            definition = self.styles[current]
            out.append(definition)
            current = definition.based_on
        out.reverse()
        return out

    def style_name(self, style_id: Optional[str]) -> str:
        definition = self.styles.get(style_id or "")
        return definition.name if definition else ""

    def named_style(self, style_id: Optional[str]) -> str:
        """Map a .docx paragraph style onto one of Docs' named styles."""
        for definition in reversed(self.chain(style_id)):
            key = (definition.name or "").strip().lower()
            if key in HEADING_NAMES:
                return HEADING_NAMES[key]
            compact = definition.style_id.strip().lower()
            if compact in ("title", "subtitle"):
                return HEADING_NAMES[compact]
            if compact.startswith("heading") and compact[7:].isdigit():
                level = int(compact[7:])
                if 1 <= level <= 6:
                    return "HEADING_%d" % level
        return "NORMAL_TEXT"

    def style_numbering(self, style_id: Optional[str]) -> Tuple[Optional[str], Optional[int]]:
        num_id = None
        level = None
        for definition in self.chain(style_id):
            if definition.num_id is not None:
                num_id = definition.num_id
            if definition.num_level is not None:
                level = definition.num_level
        return num_id, level

    # -- resolved formatting ----------------------------------------------

    def paragraph_style(self, style_id: Optional[str]) -> Tuple[ParagraphProps, TextStyle]:
        """Document defaults + the paragraph style chain (no direct formatting)."""
        key = style_id or ""
        if key in self._paragraph_cache:
            return self._paragraph_cache[key]

        props = self.doc_default_ppr
        text = self.doc_default_rpr
        default_id = self.default_paragraph_style
        chain: List[StyleDef] = []
        if default_id and default_id != style_id:
            chain.extend(self.chain(default_id))
        chain.extend(self.chain(style_id))
        for definition in chain:
            props = merge_paragraph_props(props, parse_paragraph_props(definition.ppr, self.notes))
            text = text.merged_with(
                parse_run_props(definition.rpr, self.theme_fonts, self.notes)
            )
        result = (props, text)
        self._paragraph_cache[key] = result
        return result

    def character_style(self, style_id: Optional[str]) -> TextStyle:
        """A ``w:rStyle`` chain resolved on its own (no document defaults)."""
        key = style_id or ""
        if key in self._run_cache:
            return self._run_cache[key]
        text = TextStyle()
        for definition in self.chain(style_id):
            text = text.merged_with(
                parse_run_props(definition.rpr, self.theme_fonts, self.notes)
            )
        self._run_cache[key] = text
        return text

    # -- tables ------------------------------------------------------------

    def table_style_format(self, style_id: Optional[str], conditions: List[str]) -> Tuple[CellFormat, TextStyle, ParagraphProps]:
        """Cell/run/paragraph formatting contributed by a table style.

        ``conditions`` lists the conditional-formatting slots that apply to the
        cell (``firstRow``, ``band1Horz``, ...); they are applied in the
        specificity order the spec mandates, not the order given.
        """
        cell = CellFormat()
        text = TextStyle()
        paragraph = ParagraphProps(named_style=None)  # type: ignore[arg-type]
        wanted = set(conditions)
        for definition in self.chain(style_id):
            cell = merge_cell_format(cell, cell_format_from_tbl_pr(definition.tbl_pr))
            cell = merge_cell_format(cell, cell_format_from_tc_pr(definition.tc_pr))
            text = text.merged_with(parse_run_props(definition.rpr, self.theme_fonts, self.notes))
            paragraph = merge_paragraph_props(
                paragraph, parse_paragraph_props(definition.ppr, self.notes)
            )
            for kind in CONDITIONAL_ORDER:
                if kind not in wanted:
                    continue
                node = definition.conditional.get(kind)
                if node is None:
                    continue
                cell = merge_cell_format(cell, cell_format_from_tbl_pr(get(node, "w:tblPr")))
                cell = merge_cell_format(cell, cell_format_from_tc_pr(get(node, "w:tcPr")))
                text = text.merged_with(
                    parse_run_props(get(node, "w:rPr"), self.theme_fonts, self.notes)
                )
                paragraph = merge_paragraph_props(
                    paragraph, parse_paragraph_props(get(node, "w:pPr"), self.notes)
                )
        return cell, text, paragraph


# ---------------------------------------------------------------------------
# Table property parsing
# ---------------------------------------------------------------------------


def _side(borders: Optional[ET.Element], side: str) -> Optional[ET.Element]:
    """``w:left``/``w:right`` with their ``w:start``/``w:end`` aliases.

    Written the long way on purpose: an ElementTree element with no children is
    falsy, so ``get(a) or get(b)`` would silently discard ``<w:right/>``.
    """
    primary = get(borders, "w:" + side)
    if primary is not None:
        return primary
    return get(borders, "w:start" if side == "left" else "w:end")


def _margins(node: Optional[ET.Element]) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {}
    if node is None:
        return out
    for side in ("top", "bottom", "left", "right", "start", "end"):
        child = get(node, "w:" + side)
        if child is None:
            continue
        width = int_val(child, "w:w")
        if width is None:
            continue
        kind = (attr(child, "w:type") or "dxa").lower()
        points = twips_to_pt(width) if kind == "dxa" else None
        key = {"start": "left", "end": "right"}.get(side, side)
        out[key] = points
    return out


def cell_format_from_tbl_pr(tbl_pr: Optional[ET.Element]) -> CellFormat:
    fmt = CellFormat()
    if tbl_pr is None:
        return fmt
    borders = get(tbl_pr, "w:tblBorders")
    if borders is not None:
        fmt.border_top = parse_border(get(borders, "w:top"))
        fmt.border_bottom = parse_border(get(borders, "w:bottom"))
        fmt.border_left = parse_border(_side(borders, "left"))
        fmt.border_right = parse_border(_side(borders, "right"))
        fmt.border_inside_h = parse_border(get(borders, "w:insideH"))
        fmt.border_inside_v = parse_border(get(borders, "w:insideV"))
    shading = get(tbl_pr, "w:shd")
    fill = attr(shading, "w:fill")
    if fill and fill.lower() not in ("auto", "none"):
        fmt.background_color = fill.upper()
    margins = _margins(get(tbl_pr, "w:tblCellMar"))
    fmt.padding_top_pt = margins.get("top")
    fmt.padding_bottom_pt = margins.get("bottom")
    fmt.padding_left_pt = margins.get("left")
    fmt.padding_right_pt = margins.get("right")
    return fmt


def cell_format_from_tc_pr(tc_pr: Optional[ET.Element]) -> CellFormat:
    fmt = CellFormat()
    if tc_pr is None:
        return fmt
    borders = get(tc_pr, "w:tcBorders")
    if borders is not None:
        fmt.border_top = parse_border(get(borders, "w:top"))
        fmt.border_bottom = parse_border(get(borders, "w:bottom"))
        fmt.border_left = parse_border(_side(borders, "left"))
        fmt.border_right = parse_border(_side(borders, "right"))
        fmt.border_inside_h = parse_border(get(borders, "w:insideH"))
        fmt.border_inside_v = parse_border(get(borders, "w:insideV"))
    shading = get(tc_pr, "w:shd")
    fill = attr(shading, "w:fill")
    if fill and fill.lower() not in ("auto", "none"):
        fmt.background_color = fill.upper()
    margins = _margins(get(tc_pr, "w:tcMar"))
    if "top" in margins:
        fmt.padding_top_pt = margins["top"]
    if "bottom" in margins:
        fmt.padding_bottom_pt = margins["bottom"]
    if "left" in margins:
        fmt.padding_left_pt = margins["left"]
    if "right" in margins:
        fmt.padding_right_pt = margins["right"]
    alignment = val(get(tc_pr, "w:vAlign"))
    if alignment:
        fmt.vertical_alignment = {
            "top": "TOP",
            "center": "MIDDLE",
            "both": "MIDDLE",
            "bottom": "BOTTOM",
        }.get(alignment.lower())
    return fmt


def theme_fonts(theme_root: Optional[ET.Element]) -> Dict[str, str]:
    """Extract the major/minor latin typefaces from theme1.xml."""
    out: Dict[str, str] = {}
    if theme_root is None:
        return out
    scheme = theme_root.find(".//" + qn("a:fontScheme"))
    if scheme is None:
        return out
    for tag, keys in (
        ("a:majorFont", ("majorHAnsi", "majorAscii", "majorBidi", "majorEastAsia", "major")),
        ("a:minorFont", ("minorHAnsi", "minorAscii", "minorBidi", "minorEastAsia", "minor")),
    ):
        font = get(scheme, tag)
        latin = get(font, "a:latin")
        typeface = latin.get("typeface") if latin is not None else None
        if typeface:
            for key in keys:
                out[key] = typeface
    return out
