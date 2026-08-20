"""Walk a .docx package and produce the fully-resolved :mod:`autowriter.ir`."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from ..ir import (
    Block,
    Document,
    FootnoteRun,
    HeaderFooter,
    ImageAsset,
    ImageRun,
    ListMarker,
    PageBreakRun,
    Paragraph,
    ParagraphProps,
    Section,
    SectionProps,
    Table,
    TableCell,
    TableRow,
    TextRun,
    TextStyle,
)
from ..units import emu_to_pt, twips_to_pt
from .numbering import Numbering
from .oxml import attr, findall, get, int_val, local_name, on_off, qn, val
from .package import DocxPackage
from .props import (
    finalize_paragraph_props,
    merge_paragraph_props,
    parse_paragraph_props,
    parse_run_props,
)
from .styles import (
    CellFormat,
    StyleResolver,
    cell_format_from_tbl_pr,
    cell_format_from_tc_pr,
    merge_cell_format,
    theme_fonts,
)

# Characters Word encodes structurally that have a plain-text equivalent.
NON_BREAKING_HYPHEN = "\u2011"  # U+2011 NON-BREAKING HYPHEN
SOFT_HYPHEN = "\u00ad"  # U+00AD SOFT HYPHEN
LINE_BREAK = "\u000b"  # what Google Docs uses for a Shift+Enter break


def read_docx(source) -> Document:
    """Read a .docx (path or file-like) into the intermediate representation."""
    with DocxPackage(source) as package:
        return DocxReader(package).read()


class DocxReader:
    def __init__(self, package: DocxPackage):
        self.package = package
        self.document = Document()
        self._style_notes: List[str] = []
        styles_part = package.main_related("styles") or "word/styles.xml"
        theme_part = package.main_related("theme")
        self.theme = theme_fonts(package.xml(theme_part)) if theme_part else {}
        self.styles = StyleResolver(package.xml(styles_part), self.theme, self._style_notes)
        numbering_part = package.main_related("numbering")
        self.numbering = Numbering(package.xml(numbering_part) if numbering_part else None)
        self.footnotes = self._load_notes("footnotes", "w:footnote")
        self.endnotes = self._load_notes("endnotes", "w:endnote")
        settings_part = package.main_related("settings")
        self.settings = package.xml(settings_part) if settings_part else None
        self._asset_ids: Dict[str, str] = {}
        self._seen_notes = set()

    # -- entry point -------------------------------------------------------

    def read(self) -> Document:
        root = self.package.xml(self.package.main_part)
        if root is None:
            raise ValueError("document part %r not found in package" % self.package.main_part)
        body = get(root, "w:body")
        if body is None:
            raise ValueError("document has no body")

        self.document.title = self.package.core_title()
        sections: List[Section] = []
        blocks: List[Block] = []

        for child in list(body):
            tag = local_name(child.tag)
            if tag == "p":
                section_props = get(get(child, "w:pPr"), "w:sectPr")
                blocks.append(self.read_paragraph(child, self.package.main_part))
                if section_props is not None:
                    sections.append(self._make_section(blocks, section_props))
                    blocks = []
            elif tag == "tbl":
                blocks.append(self.read_table(child, self.package.main_part))
            elif tag == "sdt":
                blocks.extend(self.read_blocks(get(child, "w:sdtContent"), self.package.main_part))
            elif tag == "sectPr":
                sections.append(self._make_section(blocks, child))
                blocks = []
            elif tag == "altChunk":
                self.note("unsupported", "embedded altChunk content was skipped")

        if blocks or not sections:
            sections.append(self._make_section(blocks, None))

        self.document.sections = sections
        for message in dict.fromkeys(self._style_notes):
            self.note("approximation", message)
        return self.document

    def read_blocks(self, container: Optional[ET.Element], part: str) -> List[Block]:
        blocks: List[Block] = []
        if container is None:
            return blocks
        for child in list(container):
            tag = local_name(child.tag)
            if tag == "p":
                blocks.append(self.read_paragraph(child, part))
            elif tag == "tbl":
                blocks.append(self.read_table(child, part))
            elif tag == "sdt":
                blocks.extend(self.read_blocks(get(child, "w:sdtContent"), part))
        return blocks

    def note(self, kind: str, message: str, location: Optional[str] = None) -> None:
        key = (kind, message, location)
        if key in self._seen_notes:
            return
        self._seen_notes.add(key)
        self.document.note(kind, message, location)

    # -- sections ----------------------------------------------------------

    def _make_section(self, blocks: List[Block], sect_pr: Optional[ET.Element]) -> Section:
        props = SectionProps()
        headers: Dict[str, HeaderFooter] = {}
        footers: Dict[str, HeaderFooter] = {}
        if sect_pr is not None:
            page_size = get(sect_pr, "w:pgSz")
            if page_size is not None:
                props.page_width_pt = twips_to_pt(int_val(page_size, "w:w"))
                props.page_height_pt = twips_to_pt(int_val(page_size, "w:h"))
            margins = get(sect_pr, "w:pgMar")
            if margins is not None:
                props.margin_top_pt = twips_to_pt(int_val(margins, "w:top"))
                props.margin_bottom_pt = twips_to_pt(int_val(margins, "w:bottom"))
                props.margin_left_pt = twips_to_pt(int_val(margins, "w:left"))
                props.margin_right_pt = twips_to_pt(int_val(margins, "w:right"))
                props.margin_header_pt = twips_to_pt(int_val(margins, "w:header"))
                props.margin_footer_pt = twips_to_pt(int_val(margins, "w:footer"))
            columns = get(sect_pr, "w:cols")
            if columns is not None:
                props.column_count = int_val(columns, "w:num") or 1
                props.column_gap_pt = twips_to_pt(int_val(columns, "w:space"))
            kind = (val(get(sect_pr, "w:type")) or "nextPage").lower()
            props.section_type = "CONTINUOUS" if kind == "continuous" else "NEXT_PAGE"
            if kind in ("evenpage", "oddpage"):
                self.note("approximation", "section break type %r copied as a page break" % kind)
            props.different_first_page = bool(on_off(get(sect_pr, "w:titlePg"), False))
            props.different_even_odd = self._even_odd_headers()

            for reference, bucket in (
                ("w:headerReference", headers),
                ("w:footerReference", footers),
            ):
                for node in findall(sect_pr, reference):
                    kind_attr = (attr(node, "w:type") or "default").lower()
                    rel_id = attr(node, "r:id")
                    part = self.package.related_part(self.package.main_part, rel_id or "")
                    if not part:
                        continue
                    root = self.package.xml(part)
                    if root is None:
                        continue
                    bucket[kind_attr] = HeaderFooter(blocks=self.read_blocks(root, part))

        return Section(blocks=blocks, props=props, headers=headers, footers=footers)

    def _even_odd_headers(self) -> bool:
        if self.settings is None:
            return False
        return bool(on_off(get(self.settings, "w:evenAndOddHeaders"), False))

    # -- paragraphs --------------------------------------------------------

    def read_paragraph(self, node: ET.Element, part: str) -> Paragraph:
        ppr = get(node, "w:pPr")
        style_id = val(get(ppr, "w:pStyle"))
        base_props, base_text = self.styles.paragraph_style(style_id)

        num_id, level = self._paragraph_numbering(ppr, style_id)
        marker: Optional[ListMarker] = None
        if num_id is not None and num_id != "0":
            level_def = self.numbering.level(num_id, level or 0, self.styles.style_numbering)
            if level_def is not None:
                # Word gives the numbering level's own pPr/rPr precedence over
                # the paragraph style, which is why list indents survive a
                # style that says otherwise.
                base_props = merge_paragraph_props(
                    base_props, parse_paragraph_props(level_def.ppr, self._style_notes)
                )
                # The level's own w:rPr styles the bullet/number glyph, not the
                # paragraph text, so it deliberately does not reach the runs.
                marker = ListMarker(
                    num_id=num_id,
                    level=level or 0,
                    number_format=level_def.number_format,
                    glyph_symbol=level_def.level_text or None,
                    start_at=level_def.start,
                    level_text=level_def.level_text,
                )
            else:
                marker = ListMarker(num_id=num_id, level=level or 0)

        direct_props = parse_paragraph_props(ppr, self._style_notes)
        props = merge_paragraph_props(base_props, direct_props)
        props = replace(props, named_style=self.styles.named_style(style_id))

        mark_style = base_text.merged_with(
            parse_run_props(get(ppr, "w:rPr"), self.theme, self._style_notes)
        )
        props = finalize_paragraph_props(props, mark_style.font_size_pt)

        paragraph = Paragraph(
            inlines=self.read_inlines(node, base_text, part),
            props=props,
            list_marker=marker,
            mark_style=mark_style,
            source_style_id=style_id,
        )
        if get(ppr, "w:framePr") is not None:
            self.note("unsupported", "text frame positioning is not reproducible in Google Docs")
        return paragraph

    def _paragraph_numbering(
        self, ppr: Optional[ET.Element], style_id: Optional[str]
    ) -> Tuple[Optional[str], Optional[int]]:
        num_pr = get(ppr, "w:numPr")
        num_id = val(get(num_pr, "w:numId"))
        level = int_val(get(num_pr, "w:ilvl"))
        style_num_id, style_level = self.styles.style_numbering(style_id)
        if num_id is None:
            num_id = style_num_id
        if level is None:
            level = style_level
        return num_id, level

    # -- inline content ----------------------------------------------------

    def read_inlines(
        self,
        container: ET.Element,
        inherited: TextStyle,
        part: str,
        link_url: Optional[str] = None,
    ) -> List:
        inlines: List = []
        for child in list(container):
            tag = local_name(child.tag)
            if tag == "r":
                inlines.extend(self.read_run(child, inherited, part, link_url))
            elif tag == "hyperlink":
                url = self._hyperlink_target(child, part)
                inlines.extend(self.read_inlines(child, inherited, part, url or link_url))
            elif tag in ("ins", "smartTag", "sdt", "sdtContent", "bdo", "dir"):
                if tag == "ins":
                    self.note(
                        "tracked-change", "an insertion tracked change was copied as final text"
                    )
                target = get(child, "w:sdtContent") if tag == "sdt" else child
                if target is not None:
                    inlines.extend(self.read_inlines(target, inherited, part, link_url))
            elif tag == "del":
                self.note("tracked-change", "a deletion tracked change was left out, as Word shows it")
            elif tag == "fldSimple":
                instruction = (attr(child, "w:instr") or "").strip().split(" ")[0]
                self.note(
                    "field", "field %r copied as its cached text" % (instruction or "unknown")
                )
                inlines.extend(self.read_inlines(child, inherited, part, link_url))
            elif tag in ("commentRangeStart", "commentRangeEnd"):
                self.note("unsupported", "comments are not carried over")
            elif tag == "bookmarkStart":
                name = attr(child, "w:name")
                if name and not name.startswith("_"):
                    self.note("unsupported", "bookmarks are not carried over")
        return inlines

    def read_run(
        self, node: ET.Element, inherited: TextStyle, part: str, link_url: Optional[str]
    ) -> List:
        rpr = get(node, "w:rPr")
        style = inherited
        char_style_id = val(get(rpr, "w:rStyle"))
        if char_style_id:
            style = style.merged_with(self.styles.character_style(char_style_id))
        style = style.merged_with(parse_run_props(rpr, self.theme, self._style_notes))
        if link_url:
            style = style.with_link(link_url)

        inlines: List = []
        buffer: List[str] = []

        def flush() -> None:
            if buffer:
                inlines.append(TextRun("".join(buffer), style))
                buffer.clear()

        for child in list(node):
            tag = local_name(child.tag)
            if tag == "t":
                buffer.append(child.text or "")
            elif tag == "delText":
                continue
            elif tag == "tab":
                buffer.append("\t")
            elif tag == "br":
                kind = (attr(child, "w:type") or "textWrapping").lower()
                if kind == "page":
                    flush()
                    inlines.append(PageBreakRun(style))
                elif kind == "column":
                    self.note("approximation", "a column break was copied as a line break")
                    buffer.append(LINE_BREAK)
                else:
                    buffer.append(LINE_BREAK)
            elif tag == "cr":
                buffer.append(LINE_BREAK)
            elif tag == "noBreakHyphen":
                buffer.append(NON_BREAKING_HYPHEN)
            elif tag == "softHyphen":
                buffer.append(SOFT_HYPHEN)
            elif tag == "sym":
                buffer.append(self._symbol_char(child))
            elif tag == "drawing":
                flush()
                inlines.extend(self.read_drawing(child, part, style))
            elif tag == "pict":
                flush()
                inlines.extend(self.read_vml_picture(child, part, style))
            elif tag == "object":
                self.note("unsupported", "an embedded OLE object was skipped")
            elif tag == "footnoteReference":
                flush()
                inlines.append(self._make_note_run(child, self.footnotes, part, style, "footnote"))
            elif tag == "endnoteReference":
                flush()
                inlines.append(self._make_note_run(child, self.endnotes, part, style, "endnote"))
            elif tag == "instrText":
                continue
            elif tag == "fldChar":
                if (attr(child, "w:fldCharType") or "").lower() == "begin":
                    self.note("field", "a field was copied as its cached result text")
            elif tag == "ruby":
                self.note("unsupported", "ruby (phonetic guide) text was flattened")
                inlines.extend(self.read_inlines(child, style, part, link_url))

        flush()
        return [item for item in inlines if not isinstance(item, TextRun) or item.text]

    def _symbol_char(self, node: ET.Element) -> str:
        code = attr(node, "w:char")
        font = attr(node, "w:font") or ""
        if not code:
            return ""
        try:
            value = int(code, 16)
        except ValueError:
            return ""
        if 0xF000 <= value <= 0xF0FF:
            # Symbol-font private use area; the glyph depends on a font Google
            # Docs will not have, so fall back to the Latin-1 code point.
            self.note(
                "approximation", "symbol from font %r mapped to its Latin-1 code point" % font
            )
            value -= 0xF000
        return chr(value)

    def _hyperlink_target(self, node: ET.Element, part: str) -> Optional[str]:
        rel_id = attr(node, "r:id")
        if rel_id:
            relationship = self.package.relationship(part, rel_id)
            if relationship is not None:
                fragment = attr(node, "w:anchor")
                return relationship.target + ("#" + fragment if fragment else "")
        if attr(node, "w:anchor"):
            self.note("unsupported", "internal bookmark links are copied as plain text")
        return None

    def _make_note_run(
        self,
        node: ET.Element,
        store: Dict[str, ET.Element],
        part: str,
        style: TextStyle,
        kind: str,
    ) -> FootnoteRun:
        note_id = attr(node, "w:id") or ""
        body = store.get(note_id)
        blocks: List[Block] = []
        if body is not None:
            note_part = self.package.main_related(kind + "s") or part
            blocks = self.read_blocks(body, note_part)
        if kind == "endnote":
            self.note("approximation", "endnotes were copied as footnotes")
        return FootnoteRun(blocks=blocks, style=style)

    def _load_notes(self, kind: str, tag: str) -> Dict[str, ET.Element]:
        part = self.package.main_related(kind)
        root = self.package.xml(part) if part else None
        out: Dict[str, ET.Element] = {}
        if root is None:
            return out
        for node in findall(root, tag):
            note_type = (attr(node, "w:type") or "normal").lower()
            if note_type in ("separator", "continuationseparator", "continuationnotice"):
                continue
            note_id = attr(node, "w:id")
            if note_id is not None:
                out[note_id] = node
        return out

    # -- images ------------------------------------------------------------

    def read_drawing(self, node: ET.Element, part: str, style: TextStyle) -> List[ImageRun]:
        out: List[ImageRun] = []
        for wrapper_tag in ("wp:inline", "wp:anchor"):
            for wrapper in findall(node, wrapper_tag):
                if wrapper_tag == "wp:anchor":
                    self.note(
                        "approximation",
                        "a floating image was placed inline (Docs has no equivalent anchor)",
                    )
                extent = get(wrapper, "wp:extent")
                width = emu_to_pt(int_val(extent, "cx")) if extent is not None else None
                height = emu_to_pt(int_val(extent, "cy")) if extent is not None else None
                doc_pr = get(wrapper, "wp:docPr")
                blip = wrapper.find(".//" + qn("a:blip"))
                rel_id = None
                if blip is not None:
                    rel_id = attr(blip, "r:embed") or attr(blip, "r:link")
                asset_id = self._register_image(part, rel_id)
                if asset_id is None:
                    if wrapper.find(".//" + qn("wps:wsp")) is not None:
                        self.note("unsupported", "a drawing canvas / shape was skipped")
                    continue
                out.append(
                    ImageRun(
                        asset_id=asset_id,
                        width_pt=width,
                        height_pt=height,
                        alt_title=doc_pr.get("name") if doc_pr is not None else None,
                        alt_description=doc_pr.get("descr") if doc_pr is not None else None,
                        style=style,
                    )
                )
        return out

    def read_vml_picture(self, node: ET.Element, part: str, style: TextStyle) -> List[ImageRun]:
        data = node.find(".//" + qn("v:imagedata"))
        if data is None:
            self.note("unsupported", "a VML drawing was skipped")
            return []
        asset_id = self._register_image(part, attr(data, "r:id"))
        if asset_id is None:
            return []
        width_pt = height_pt = None
        shape = node.find(".//" + qn("v:shape"))
        if shape is not None:
            width_pt, height_pt = _parse_vml_size(shape.get("style") or "")
        return [ImageRun(asset_id=asset_id, width_pt=width_pt, height_pt=height_pt, style=style)]

    def _register_image(self, part: str, rel_id: Optional[str]) -> Optional[str]:
        if not rel_id:
            return None
        target = self.package.related_part(part, rel_id)
        if not target or not self.package.has_part(target):
            relationship = self.package.relationship(part, rel_id)
            if relationship is not None and relationship.is_external:
                self.note("unsupported", "an externally linked image was skipped")
            return None
        if target in self._asset_ids:
            return self._asset_ids[target]
        data = self.package.read(target)
        digest = hashlib.sha1(data).hexdigest()[:16]
        extension = target.rsplit(".", 1)[-1].lower() if "." in target else "png"
        asset_id = "img-%s" % digest
        self.document.assets[asset_id] = ImageAsset(
            asset_id=asset_id,
            data=data,
            content_type=self.package.content_type(target) or "application/octet-stream",
            extension=extension,
        )
        self._asset_ids[target] = asset_id
        return asset_id

    # -- tables ------------------------------------------------------------

    def read_table(self, node: ET.Element, part: str) -> Table:
        tbl_pr = get(node, "w:tblPr")
        style_id = val(get(tbl_pr, "w:tblStyle"))
        grid = [
            twips_to_pt(int_val(column, "w:w"))
            for column in findall(get(node, "w:tblGrid"), "w:gridCol")
        ]
        look = self._table_look(get(tbl_pr, "w:tblLook"))
        direct_table_format = cell_format_from_tbl_pr(tbl_pr)

        rows_xml = [child for child in node if local_name(child.tag) == "tr"]
        rows: List[TableRow] = []
        pending_merges: Dict[int, Tuple[int, int]] = {}  # column -> (row index, cell index)

        for row_index, row_xml in enumerate(rows_xml):
            tr_pr = get(row_xml, "w:trPr")
            height = get(tr_pr, "w:trHeight")
            row = TableRow(
                min_height_pt=twips_to_pt(int_val(height)) if height is not None else None,
                is_header=bool(on_off(get(tr_pr, "w:tblHeader"), False)),
            )
            column = 0
            for cell_xml in [child for child in row_xml if local_name(child.tag) == "tc"]:
                tc_pr = get(cell_xml, "w:tcPr")
                span = int_val(get(tc_pr, "w:gridSpan")) or 1
                merge = get(tc_pr, "w:vMerge")
                merge_kind = (val(merge) or "continue").lower() if merge is not None else None

                conditions = self._cell_conditions(
                    look, row_index, len(rows_xml), column, span, len(grid) or 1
                )
                style_cell, style_text, style_para = self.styles.table_style_format(
                    style_id, conditions
                )
                cell_format = merge_cell_format(style_cell, direct_table_format)
                cell_format = merge_cell_format(cell_format, cell_format_from_tc_pr(tc_pr))

                cell = TableCell(
                    blocks=self.read_blocks(cell_xml, part),
                    col_span=span,
                    background_color=cell_format.background_color,
                    vertical_alignment=cell_format.vertical_alignment,
                    padding_top_pt=cell_format.padding_top_pt,
                    padding_bottom_pt=cell_format.padding_bottom_pt,
                    padding_left_pt=cell_format.padding_left_pt,
                    padding_right_pt=cell_format.padding_right_pt,
                )
                self._apply_cell_borders(
                    cell, cell_format, row_index, len(rows_xml), column, span, len(grid) or 1
                )
                if style_text != TextStyle() or style_para != ParagraphProps(named_style=None):
                    _apply_table_style_to_blocks(cell.blocks, style_text, style_para)

                if merge_kind == "continue":
                    origin = pending_merges.get(column)
                    if origin is not None:
                        rows[origin[0]].cells[origin[1]].row_span += 1
                        cell.merged_away = True
                    else:
                        pending_merges[column] = (row_index, len(row.cells))
                elif merge_kind == "restart":
                    pending_merges[column] = (row_index, len(row.cells))
                else:
                    pending_merges.pop(column, None)

                row.cells.append(cell)
                column += span
            rows.append(row)

        return Table(rows=rows, column_widths_pt=grid)

    def _apply_cell_borders(
        self,
        cell: TableCell,
        fmt: CellFormat,
        row_index: int,
        row_count: int,
        column: int,
        span: int,
        column_count: int,
    ) -> None:
        """Turn table-level inside/outside borders into per-cell borders.

        The Docs API has no table-level border concept: every edge belongs to a
        cell.  So ``insideH``/``insideV`` get pushed down onto the individual
        cells sitting on those edges.
        """
        first_row = row_index == 0
        last_row = row_index == row_count - 1
        first_col = column == 0
        last_col = column + span >= column_count

        cell.border_top = fmt.border_top if first_row else (fmt.border_inside_h or fmt.border_top)
        cell.border_bottom = (
            fmt.border_bottom if last_row else (fmt.border_inside_h or fmt.border_bottom)
        )
        cell.border_left = (
            fmt.border_left if first_col else (fmt.border_inside_v or fmt.border_left)
        )
        cell.border_right = (
            fmt.border_right if last_col else (fmt.border_inside_v or fmt.border_right)
        )

    @staticmethod
    def _table_look(node: Optional[ET.Element]) -> Dict[str, bool]:
        look = {
            "firstRow": False,
            "lastRow": False,
            "firstColumn": False,
            "lastColumn": False,
            "noHBand": False,
            "noVBand": False,
        }
        if node is None:
            return look
        legacy = attr(node, "w:val")
        if legacy:
            try:
                mask = int(legacy, 16)
            except ValueError:
                mask = 0
            look["firstRow"] = bool(mask & 0x0020)
            look["lastRow"] = bool(mask & 0x0040)
            look["firstColumn"] = bool(mask & 0x0080)
            look["lastColumn"] = bool(mask & 0x0100)
            look["noHBand"] = bool(mask & 0x0200)
            look["noVBand"] = bool(mask & 0x0400)
        for key in list(look):
            explicit = attr(node, "w:" + key)
            if explicit is not None:
                look[key] = explicit in ("1", "true", "on")
        return look

    @staticmethod
    def _cell_conditions(
        look: Dict[str, bool],
        row_index: int,
        row_count: int,
        column: int,
        span: int,
        column_count: int,
    ) -> List[str]:
        conditions = ["wholeTable"]
        first_row = row_index == 0 and look["firstRow"]
        last_row = row_index == row_count - 1 and look["lastRow"]
        first_col = column == 0 and look["firstColumn"]
        last_col = column + span >= column_count and look["lastColumn"]

        if not look["noHBand"]:
            band_index = row_index - (1 if look["firstRow"] else 0)
            if band_index >= 0 and not first_row and not last_row:
                conditions.append("band1Horz" if band_index % 2 == 0 else "band2Horz")
        if not look["noVBand"]:
            band_index = column - (1 if look["firstColumn"] else 0)
            if band_index >= 0 and not first_col and not last_col:
                conditions.append("band1Vert" if band_index % 2 == 0 else "band2Vert")
        if last_col:
            conditions.append("lastCol")
        if first_col:
            conditions.append("firstCol")
        if last_row:
            conditions.append("lastRow")
        if first_row:
            conditions.append("firstRow")
        if first_row and first_col:
            conditions.append("nwCell")
        if first_row and last_col:
            conditions.append("neCell")
        if last_row and first_col:
            conditions.append("swCell")
        if last_row and last_col:
            conditions.append("seCell")
        return conditions


def _apply_table_style_to_blocks(
    blocks: List[Block], text: TextStyle, paragraph: ParagraphProps
) -> None:
    """Push table-style formatting underneath each cell paragraph's own."""
    for block in blocks:
        if isinstance(block, Paragraph):
            block.props = merge_paragraph_props(paragraph, block.props)
            block.mark_style = text.merged_with(block.mark_style)
            for inline in block.inlines:
                if hasattr(inline, "style"):
                    inline.style = text.merged_with(inline.style)
        elif isinstance(block, Table):
            for row in block.rows:
                for cell in row.cells:
                    _apply_table_style_to_blocks(cell.blocks, text, paragraph)


def _parse_vml_size(style: str) -> Tuple[Optional[float], Optional[float]]:
    width = height = None
    for chunk in style.split(";"):
        name, _, value = chunk.partition(":")
        name = name.strip().lower()
        value = value.strip().lower()
        if name not in ("width", "height") or not value:
            continue
        try:
            if value.endswith("pt"):
                number = float(value[:-2])
            elif value.endswith("in"):
                number = float(value[:-2]) * 72.0
            elif value.endswith("px"):
                number = float(value[:-2]) * 0.75
            else:
                number = float(value)
        except ValueError:
            continue
        if name == "width":
            width = number
        else:
            height = number
    return width, height
