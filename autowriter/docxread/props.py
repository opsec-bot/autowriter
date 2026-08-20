"""Parsers that turn ``w:rPr`` / ``w:pPr`` XML into IR formatting objects.

These functions are deliberately dumb: they read exactly what a single
properties element says and leave everything else ``None``.  Combining the
layers (document defaults, style chain, numbering, direct formatting) is
:mod:`autowriter.docxread.styles`' job.
"""

from __future__ import annotations

from dataclasses import fields, replace
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

from ..ir import Border, ParagraphProps, TextStyle
from ..units import eighth_points_to_pt, half_points_to_pt, twips_to_pt
from .oxml import attr, findall, get, int_val, on_off, val

# ECMA-376 highlight names -> RGB, matching what Word renders.
HIGHLIGHT_COLORS = {
    "black": "000000",
    "blue": "0000FF",
    "cyan": "00FFFF",
    "darkblue": "000080",
    "darkcyan": "008080",
    "darkgray": "808080",
    "darkgreen": "008000",
    "darkmagenta": "800080",
    "darkred": "800000",
    "darkyellow": "808000",
    "green": "00FF00",
    "lightgray": "C0C0C0",
    "magenta": "FF00FF",
    "red": "FF0000",
    "white": "FFFFFF",
    "yellow": "FFFF00",
}

ALIGNMENT = {
    "left": "START",
    "start": "START",
    "center": "CENTER",
    "centre": "CENTER",
    "right": "END",
    "end": "END",
    "both": "JUSTIFIED",
    "distribute": "JUSTIFIED",
}

# Word border styles -> the three dash styles the Docs API exposes.
BORDER_DASH = {
    "single": "SOLID",
    "thick": "SOLID",
    "double": "SOLID",
    "wave": "SOLID",
    "dotted": "DOT",
    "dotdash": "DOT",
    "dotdotdash": "DOT",
    "dashed": "DASH",
    "dashsmallgap": "DASH",
    "dashdotstroked": "DASH",
}

NO_BORDER = {"none", "nil"}


def _merge(base, override):
    """Field-wise overlay of two dataclass instances; ``None`` defers."""
    if base is None:
        return override
    if override is None:
        return base
    values = {}
    for f in fields(base):
        new = getattr(override, f.name)
        old = getattr(base, f.name)
        if isinstance(new, list):
            values[f.name] = list(new) if new else list(old or [])
        elif new is None:
            values[f.name] = old
        else:
            values[f.name] = new
    return type(base)(**values)


merge_paragraph_props = _merge
merge_border = _merge


# ---------------------------------------------------------------------------
# Character properties
# ---------------------------------------------------------------------------


def parse_run_props(rpr: Optional[ET.Element], theme_fonts: Optional[Dict[str, str]] = None,
                    notes: Optional[List[str]] = None) -> TextStyle:
    style = TextStyle()
    if rpr is None:
        return style

    style.bold = on_off(get(rpr, "w:b"), None)
    style.italic = on_off(get(rpr, "w:i"), None)
    style.small_caps = on_off(get(rpr, "w:smallCaps"), None)
    style.all_caps = on_off(get(rpr, "w:caps"), None)
    style.hidden = bool(on_off(get(rpr, "w:vanish"), False))

    underline = get(rpr, "w:u")
    if underline is not None:
        kind = (val(underline) or "single").lower()
        style.underline = kind != "none"
        if notes is not None and kind not in ("none", "single", "words"):
            notes.append(f"underline style {kind!r} rendered as a plain underline")

    strike = on_off(get(rpr, "w:strike"), None)
    double_strike = on_off(get(rpr, "w:dstrike"), None)
    if strike is not None or double_strike is not None:
        style.strikethrough = bool(strike) or bool(double_strike)
        if notes is not None and double_strike:
            notes.append("double strikethrough rendered as a single strikethrough")

    vert = val(get(rpr, "w:vertAlign"))
    if vert:
        style.baseline = {
            "superscript": "SUPERSCRIPT",
            "subscript": "SUBSCRIPT",
            "baseline": "NONE",
        }.get(vert.lower())

    size = int_val(get(rpr, "w:sz"))
    if size is not None:
        style.font_size_pt = half_points_to_pt(size)

    color = get(rpr, "w:color")
    if color is not None:
        raw = val(color)
        theme_color = attr(color, "w:themeColor")
        if raw and raw.lower() != "auto":
            style.color = raw.upper()
        elif theme_color and notes is not None:
            notes.append(f"theme colour {theme_color!r} could not be resolved to RGB")

    highlight = val(get(rpr, "w:highlight"))
    if highlight and highlight.lower() != "none":
        style.background_color = HIGHLIGHT_COLORS.get(highlight.lower(), highlight.upper())
    else:
        shading = get(rpr, "w:shd")
        fill = attr(shading, "w:fill")
        if fill and fill.lower() not in ("auto", "none"):
            style.background_color = fill.upper()

    fonts = get(rpr, "w:rFonts")
    if fonts is not None:
        style.font_family = _resolve_font(fonts, theme_fonts or {})

    if notes is not None:
        if get(rpr, "w:spacing") is not None:
            notes.append("character spacing (tracking) has no Google Docs equivalent")
        if get(rpr, "w:position") is not None:
            notes.append("raised/lowered text position has no Google Docs equivalent")
        if get(rpr, "w:em") is not None:
            notes.append("emphasis marks have no Google Docs equivalent")
        if get(rpr, "w:outline") is not None or get(rpr, "w:shadow") is not None:
            notes.append("outline/shadow text effects have no Google Docs equivalent")

    return style


def _resolve_font(fonts: ET.Element, theme_fonts: Dict[str, str]) -> Optional[str]:
    for attribute in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        name = attr(fonts, attribute)
        if name:
            return name
    for attribute in ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme"):
        theme_key = attr(fonts, attribute)
        if theme_key:
            resolved = theme_fonts.get(theme_key) or theme_fonts.get(
                "major" if theme_key.startswith("major") else "minor"
            )
            if resolved:
                return resolved
    return None


# ---------------------------------------------------------------------------
# Paragraph properties
# ---------------------------------------------------------------------------


def parse_paragraph_props(ppr: Optional[ET.Element],
                          notes: Optional[List[str]] = None) -> ParagraphProps:
    props = ParagraphProps(named_style=None)  # type: ignore[arg-type]
    if ppr is None:
        return props

    alignment = val(get(ppr, "w:jc"))
    if alignment:
        props.alignment = ALIGNMENT.get(alignment.lower())

    bidi = on_off(get(ppr, "w:bidi"), None)
    if bidi is not None:
        props.direction = "RIGHT_TO_LEFT" if bidi else "LEFT_TO_RIGHT"

    spacing = get(ppr, "w:spacing")
    if spacing is not None:
        before = int_val(spacing, "w:before")
        after = int_val(spacing, "w:after")
        if before is not None:
            props.space_above_pt = twips_to_pt(before)
        if after is not None:
            props.space_below_pt = twips_to_pt(after)
        line = int_val(spacing, "w:line")
        rule = (attr(spacing, "w:lineRule") or "auto").lower()
        if line is not None:
            if rule == "auto":
                # w:line is in 240ths of a line: 240 == single spacing.
                props.line_spacing = round(line / 240.0 * 100.0, 3)
            else:
                # "exactly"/"atLeast" are absolute leading, which the Docs API
                # cannot express; carried through as a fixed-point marker and
                # converted once the run font size is known.
                props.line_spacing = -abs(twips_to_pt(line) or 0.0)
                if notes is not None:
                    notes.append(
                        f"line spacing rule {rule!r} approximated as proportional spacing"
                    )

    if on_off(get(ppr, "w:contextualSpacing"), None):
        props.space_mode = "COLLAPSE_LISTS"

    indent = get(ppr, "w:ind")
    if indent is not None:
        start = int_val(indent, "w:start")
        if start is None:
            start = int_val(indent, "w:left")
        end = int_val(indent, "w:end")
        if end is None:
            end = int_val(indent, "w:right")
        if start is not None:
            props.indent_start_pt = twips_to_pt(start)
        if end is not None:
            props.indent_end_pt = twips_to_pt(end)
        first_line = int_val(indent, "w:firstLine")
        hanging = int_val(indent, "w:hanging")
        if hanging is None:
            hanging = int_val(indent, "w:hangingChars")
        base_start = props.indent_start_pt if props.indent_start_pt is not None else 0.0
        if hanging is not None:
            props.indent_first_line_pt = base_start - (twips_to_pt(hanging) or 0.0)
        elif first_line is not None:
            props.indent_first_line_pt = base_start + (twips_to_pt(first_line) or 0.0)

    keep_next = on_off(get(ppr, "w:keepNext"), None)
    if keep_next is not None:
        props.keep_with_next = keep_next
    keep_lines = on_off(get(ppr, "w:keepLines"), None)
    if keep_lines is not None:
        props.keep_lines_together = keep_lines
    widow = on_off(get(ppr, "w:widowControl"), None)
    if widow is not None:
        props.avoid_widow_and_orphan = widow
    page_break = on_off(get(ppr, "w:pageBreakBefore"), None)
    if page_break is not None:
        props.page_break_before = page_break

    shading = get(ppr, "w:shd")
    fill = attr(shading, "w:fill")
    if fill and fill.lower() not in ("auto", "none"):
        props.shading_color = fill.upper()

    borders = get(ppr, "w:pBdr")
    if borders is not None:
        props.border_top = parse_border(get(borders, "w:top"))
        props.border_bottom = parse_border(get(borders, "w:bottom"))
        props.border_left = parse_border(get(borders, "w:left"))
        props.border_right = parse_border(get(borders, "w:right"))
        props.border_between = parse_border(get(borders, "w:between"))

    tabs = get(ppr, "w:tabs")
    if tabs is not None:
        stops = []
        for tab in findall(tabs, "w:tab"):
            position = int_val(tab, "w:pos")
            if position is not None and (val(tab) or "").lower() not in ("clear",):
                stops.append(twips_to_pt(position))
        props.tab_stops_pt = [s for s in stops if s is not None]

    return props


def parse_border(node: Optional[ET.Element]) -> Optional[Border]:
    if node is None:
        return None
    kind = (val(node) or "single").lower()
    if kind in NO_BORDER:
        return Border(width_pt=0.0, dash_style="SOLID", color="000000", padding_pt=0.0)
    size = int_val(node, "w:sz")
    width = eighth_points_to_pt(size) if size is not None else 0.5
    space = int_val(node, "w:space")
    color = attr(node, "w:color")
    return Border(
        color=None if not color or color.lower() == "auto" else color.upper(),
        width_pt=width,
        dash_style=BORDER_DASH.get(kind, "SOLID"),
        padding_pt=float(space) if space is not None else 0.0,
    )


def finalize_paragraph_props(props: ParagraphProps, font_size_pt: Optional[float]) -> ParagraphProps:
    """Resolve the deferred pieces once the paragraph's font size is known."""
    line_spacing = props.line_spacing
    if line_spacing is not None and line_spacing < 0:
        # Negative marks "absolute leading in points" from w:lineRule exact/atLeast.
        leading = -line_spacing
        base = (font_size_pt or 11.0) * 1.15
        line_spacing = round(leading / base * 100.0, 3) if base else 100.0
    return replace(props, line_spacing=line_spacing)
