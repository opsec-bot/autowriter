"""An in-memory stand-in for the Google Docs API.

The hard part of copying a document is index arithmetic, and index arithmetic
is exactly what you cannot check by reading code.  This module applies the same
``batchUpdate`` requests the real API would, maintaining a document model with
the same index rules, so a copy can be run — and verified — with no network,
no credentials and no quota.

It backs ``autowriter check`` as well as the test suite.  It is faithful about
indices, structure and the formatting this project sets; it is not a rendering
engine and knows nothing about pagination.

Index rules, matching the Docs API:

* every character is one index, counted in UTF-16 code units
* a paragraph's terminating newline is one index
* an inline object or page break is one index
* a table, each of its rows, and each of its cells each take one index of their
  own, before their contents
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..units import u16len


class DocsError(Exception):
    """Raised for requests the real API would reject."""


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class Run:
    text: str = ""
    style: Dict = field(default_factory=dict)
    kind: str = "text"  # text | image | pageBreak | footnoteReference
    object_id: Optional[str] = None

    @property
    def length(self) -> int:
        return u16len(self.text) if self.kind == "text" else 1


@dataclass
class Paragraph:
    runs: List[Run] = field(default_factory=list)
    style: Dict = field(default_factory=dict)
    bullet: Optional[Dict] = None

    @property
    def length(self) -> int:
        return sum(run.length for run in self.runs) + 1

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs if run.kind == "text")


@dataclass
class Cell:
    content: List = field(default_factory=list)
    style: Dict = field(default_factory=dict)
    row_span: int = 1
    column_span: int = 1

    @property
    def length(self) -> int:
        return 1 + sum(node.length for node in self.content)


@dataclass
class Row:
    cells: List[Cell] = field(default_factory=list)
    style: Dict = field(default_factory=dict)

    @property
    def length(self) -> int:
        return 1 + sum(cell.length for cell in self.cells)


@dataclass
class Table:
    rows: List[Row] = field(default_factory=list)
    columns: int = 0
    column_properties: List[Dict] = field(default_factory=list)

    @property
    def length(self) -> int:
        return 1 + sum(row.length for row in self.rows)


@dataclass
class SectionBreak:
    style: Dict = field(default_factory=dict)

    @property
    def length(self) -> int:
        return 1


@dataclass
class Segment:
    content: List = field(default_factory=list)


def _new_paragraph() -> Paragraph:
    return Paragraph(runs=[])


class SimulatedDocs:
    """Applies Docs ``batchUpdate`` requests to an in-memory document."""

    def __init__(self, document_id: str = "simulated-document", title: str = "Untitled"):
        self.document_id = document_id
        self.title = title
        self.body = Segment(content=[SectionBreak(), _new_paragraph()])
        self.headers: Dict[str, Segment] = {}
        self.footers: Dict[str, Segment] = {}
        self.footnotes: Dict[str, Segment] = {}
        self.document_style: Dict = {}
        self.inline_objects: Dict[str, Dict] = {}
        self.lists: Dict[str, Dict] = {}
        self.applied: List[Dict] = []
        self._ids = itertools.count(1)

    # -- transport interface ----------------------------------------------

    def get_document(self) -> Dict:
        return self.render()

    def batch_update(self, requests: Sequence[Dict]) -> List[Dict]:
        replies = []
        for request in requests:
            replies.append(self._apply(request))
            self.applied.append(request)
        return replies

    # -- segments ----------------------------------------------------------

    def segment(self, segment_id: str) -> Segment:
        if not segment_id:
            return self.body
        for store in (self.headers, self.footers, self.footnotes):
            if segment_id in store:
                return store[segment_id]
        raise DocsError("unknown segment %r" % segment_id)

    def _next_id(self, prefix: str) -> str:
        return "%s%d" % (prefix, next(self._ids))

    # -- index resolution --------------------------------------------------

    def _walk(self, segment_id: str):
        """Yield ``(node, start, parent_list, position)`` for every node."""
        segment = self.segment(segment_id)
        start = 0 if segment_id else 0
        yield from _walk_content(segment.content, start)

    def _paragraph_at(self, segment_id: str, index: int) -> Tuple[Paragraph, int, List, int]:
        for node, start, container, position in self._walk(segment_id):
            if isinstance(node, Paragraph) and start <= index < start + node.length:
                return node, start, container, position
        raise DocsError("index %d is not inside a paragraph of segment %r" % (index, segment_id))

    def _paragraphs_in_range(self, segment_id: str, start: int, end: int) -> List[Tuple[Paragraph, int]]:
        found = []
        for node, node_start, _container, _position in self._walk(segment_id):
            if not isinstance(node, Paragraph):
                continue
            node_end = node_start + node.length
            if node_start < end and node_end > start:
                found.append((node, node_start))
        return found

    def _table_at(self, segment_id: str, index: int) -> Table:
        for node, start, _container, _position in self._walk(segment_id):
            if isinstance(node, Table) and start == index:
                return node
        raise DocsError("no table starts at index %d" % index)

    def end_index(self, segment_id: str = "") -> int:
        segment = self.segment(segment_id)
        total = 0
        for node in segment.content:
            total += node.length
        return total

    # -- request dispatch --------------------------------------------------

    def _apply(self, request: Dict) -> Dict:
        if len(request) != 1:
            raise DocsError("a request must contain exactly one operation: %r" % sorted(request))
        (name, payload), = request.items()
        handler = getattr(self, "_do_" + _snake(name), None)
        if handler is None:
            raise DocsError("unsupported request %r" % name)
        reply = handler(payload)
        # The API wraps each reply in the name of the request that produced it,
        # and sends an empty reply for requests that return nothing.
        return {name: reply} if reply else {}

    # -- content insertion -------------------------------------------------

    @staticmethod
    def _target(payload: Dict) -> Tuple[str, Optional[int]]:
        location = payload.get("location")
        if location is not None:
            return location.get("segmentId", ""), location.get("index")
        end = payload.get("endOfSegmentLocation")
        if end is not None:
            return end.get("segmentId", ""), None
        raise DocsError("request has no location")

    def _resolve(self, payload: Dict) -> Tuple[str, int]:
        segment_id, index = self._target(payload)
        if index is None:
            index = self.end_index(segment_id) - 1
        return segment_id, index

    def _do_insert_text(self, payload: Dict) -> Dict:
        segment_id, index = self._resolve(payload)
        text = payload.get("text", "")
        if not text:
            return {}
        paragraph, start, container, position = self._paragraph_at(segment_id, index)
        offset = index - start
        head, tail = _split_runs(paragraph.runs, offset)
        pieces = text.split("\n")

        first = Paragraph(runs=head + [Run(pieces[0], dict(_style_at(paragraph, offset)))],
                          style=dict(paragraph.style), bullet=paragraph.bullet)
        if len(pieces) == 1:
            first.runs.extend(tail)
            container[position] = first
            return {}

        paragraphs = [first]
        for piece in pieces[1:-1]:
            paragraphs.append(
                Paragraph(runs=[Run(piece, dict(_style_at(paragraph, offset)))],
                          style=dict(paragraph.style), bullet=paragraph.bullet)
            )
        last = Paragraph(
            runs=[Run(pieces[-1], dict(_style_at(paragraph, offset)))] + tail,
            style=dict(paragraph.style),
            bullet=paragraph.bullet,
        )
        paragraphs.append(last)
        for item in paragraphs:
            item.runs = _compact(item.runs)
        container[position : position + 1] = paragraphs
        return {}

    def _insert_object(self, payload: Dict, kind: str, style: Optional[Dict] = None) -> str:
        segment_id, index = self._resolve(payload)
        paragraph, start, container, position = self._paragraph_at(segment_id, index)
        offset = index - start
        head, tail = _split_runs(paragraph.runs, offset)
        object_id = self._next_id("kix.obj")
        run = Run("", dict(style or {}), kind=kind, object_id=object_id)
        paragraph.runs = _compact(head + [run] + tail)
        return object_id

    def _do_insert_page_break(self, payload: Dict) -> Dict:
        self._insert_object(payload, "pageBreak")
        return {}

    def _do_insert_inline_image(self, payload: Dict) -> Dict:
        object_id = self._insert_object(payload, "image")
        size = payload.get("objectSize", {})
        self.inline_objects[object_id] = {
            "objectId": object_id,
            "inlineObjectProperties": {
                "embeddedObject": {
                    "imageProperties": {"contentUri": payload.get("uri")},
                    "size": size,
                }
            },
        }
        return {"objectId": object_id}

    def _do_insert_table(self, payload: Dict) -> Dict:
        segment_id, index = self._resolve(payload)
        rows = int(payload.get("rows", 0))
        columns = int(payload.get("columns", 0))
        if rows < 1 or columns < 1:
            raise DocsError("a table needs at least one row and column")
        paragraph, start, container, position = self._paragraph_at(segment_id, index)
        if index != start:
            raise DocsError(
                "a table can only be inserted at the start of a paragraph "
                "(index %d, paragraph starts at %d)" % (index, start)
            )
        table = Table(
            rows=[
                Row(cells=[Cell(content=[_new_paragraph()]) for _ in range(columns)])
                for _ in range(rows)
            ],
            columns=columns,
        )
        container.insert(position, table)
        return {}

    def _do_insert_section_break(self, payload: Dict) -> Dict:
        segment_id, index = self._resolve(payload)
        if segment_id:
            raise DocsError("section breaks only exist in the body")
        paragraph, start, container, position = self._paragraph_at(segment_id, index)
        # The real API splits the paragraph at the location and follows the
        # break with a fresh paragraph; inserting at the end of a paragraph
        # therefore just appends an empty one.
        head, tail = _split_runs(paragraph.runs, index - start)
        paragraph.runs = _compact(head)
        container.insert(
            position + 1, SectionBreak(style={"sectionType": payload.get("sectionType", "NEXT_PAGE")})
        )
        container.insert(position + 2, Paragraph(runs=_compact(tail), style=dict(paragraph.style)))
        return {}

    def _do_create_footnote(self, payload: Dict) -> Dict:
        footnote_id = self._next_id("kix.fn")
        self.footnotes[footnote_id] = Segment(content=[_new_paragraph()])
        self._insert_object(payload, "footnoteReference")
        return {"footnoteId": footnote_id}

    def _do_create_header(self, payload: Dict) -> Dict:
        header_id = self._next_id("kix.hdr")
        self.headers[header_id] = Segment(content=[_new_paragraph()])
        self.document_style.setdefault("defaultHeaderId", header_id)
        return {"headerId": header_id}

    def _do_create_footer(self, payload: Dict) -> Dict:
        footer_id = self._next_id("kix.ftr")
        self.footers[footer_id] = Segment(content=[_new_paragraph()])
        self.document_style.setdefault("defaultFooterId", footer_id)
        return {"footerId": footer_id}

    def _do_delete_content_range(self, payload: Dict) -> Dict:
        span = payload.get("range", {})
        segment_id = span.get("segmentId", "")
        start, end = span["startIndex"], span["endIndex"]
        for paragraph, paragraph_start in reversed(self._paragraphs_in_range(segment_id, start, end)):
            local_start = max(start - paragraph_start, 0)
            local_end = min(end - paragraph_start, paragraph.length - 1)
            if local_end <= local_start:
                continue
            head, rest = _split_runs(paragraph.runs, local_start)
            _dropped, tail = _split_runs(rest, local_end - local_start)
            paragraph.runs = _compact(head + tail)
        return {}

    # -- styling -----------------------------------------------------------

    def _do_update_text_style(self, payload: Dict) -> Dict:
        span = payload.get("range", {})
        segment_id = span.get("segmentId", "")
        start, end = span["startIndex"], span["endIndex"]
        style = payload.get("textStyle", {})
        fields = _fields(payload)
        self._check_range(segment_id, start, end)
        for paragraph, paragraph_start in self._paragraphs_in_range(segment_id, start, end):
            local_start = max(start - paragraph_start, 0)
            local_end = min(end - paragraph_start, paragraph.length)
            runs: List[Run] = []
            position = 0
            for run in paragraph.runs:
                run_end = position + run.length
                if run_end <= local_start or position >= local_end:
                    runs.append(run)
                    position = run_end
                    continue
                if run.kind != "text":
                    _merge_style(run.style, style, fields)
                    runs.append(run)
                    position = run_end
                    continue
                before = run.text[: max(local_start - position, 0)]
                middle = run.text[max(local_start - position, 0) : max(local_end - position, 0)]
                after = run.text[max(local_end - position, 0) :]
                if before:
                    runs.append(Run(before, dict(run.style)))
                if middle:
                    styled = dict(run.style)
                    _merge_style(styled, style, fields)
                    runs.append(Run(middle, styled))
                if after:
                    runs.append(Run(after, dict(run.style)))
                position = run_end
            paragraph.runs = _compact(runs)
            if local_end >= paragraph.length - 1:
                _merge_style(paragraph.style.setdefault("markStyle", {}), style, fields)
        return {}

    def _do_update_paragraph_style(self, payload: Dict) -> Dict:
        span = payload.get("range", {})
        segment_id = span.get("segmentId", "")
        start, end = span["startIndex"], span["endIndex"]
        self._check_range(segment_id, start, end)
        style = payload.get("paragraphStyle", {})
        fields = _fields(payload)
        for paragraph, _start in self._paragraphs_in_range(segment_id, start, end):
            _merge_style(paragraph.style, style, fields)
        return {}

    def _do_create_paragraph_bullets(self, payload: Dict) -> Dict:
        span = payload.get("range", {})
        segment_id = span.get("segmentId", "")
        start, end = span["startIndex"], span["endIndex"]
        self._check_range(segment_id, start, end)
        preset = payload.get("bulletPreset", "BULLET_DISC_CIRCLE_SQUARE")
        list_id = self._next_id("kix.list")
        self.lists[list_id] = {"listProperties": {"bulletPreset": preset}}
        for paragraph, _start in self._paragraphs_in_range(segment_id, start, end):
            level = 0
            # Leading tabs encode the nesting level and are consumed here, which
            # is what makes every index after this request shift.
            while paragraph.runs and paragraph.runs[0].kind == "text" and paragraph.runs[0].text.startswith("\t"):
                paragraph.runs[0].text = paragraph.runs[0].text[1:]
                level += 1
                if not paragraph.runs[0].text:
                    paragraph.runs.pop(0)
            paragraph.bullet = {"listId": list_id, "nestingLevel": level}
            paragraph.style["indentStart"] = {"magnitude": 18.0 * (level + 1), "unit": "PT"}
            paragraph.style["indentFirstLine"] = {"magnitude": 18.0 * level, "unit": "PT"}
        return {}

    def _do_delete_paragraph_bullets(self, payload: Dict) -> Dict:
        span = payload.get("range", {})
        for paragraph, _start in self._paragraphs_in_range(
            span.get("segmentId", ""), span["startIndex"], span["endIndex"]
        ):
            paragraph.bullet = None
        return {}

    # -- tables ------------------------------------------------------------

    def _table_from_payload(self, payload: Dict, key: str = "tableStartLocation") -> Table:
        location = payload.get(key, {})
        return self._table_at(location.get("segmentId", ""), location["index"])

    def _do_update_table_cell_style(self, payload: Dict) -> Dict:
        table_range = payload.get("tableRange")
        style = payload.get("tableCellStyle", {})
        fields = _fields(payload)
        if table_range is None:
            location = payload["tableStartLocation"]
            table = self._table_at(location.get("segmentId", ""), location["index"])
            for row in table.rows:
                for cell in row.cells:
                    _merge_style(cell.style, style, fields)
            return {}
        cell_location = table_range["tableCellLocation"]
        table = self._table_from_payload(cell_location)
        row_index = cell_location.get("rowIndex", 0)
        column_index = cell_location.get("columnIndex", 0)
        for row_offset in range(table_range.get("rowSpan", 1)):
            for column_offset in range(table_range.get("columnSpan", 1)):
                cell = self._cell_at(table, row_index + row_offset, column_index + column_offset)
                if cell is not None:
                    _merge_style(cell.style, style, fields)
        return {}

    def _do_update_table_column_properties(self, payload: Dict) -> Dict:
        table = self._table_from_payload(payload)
        properties = payload.get("tableColumnProperties", {})
        indices = payload.get("columnIndices") or list(range(table.columns))
        while len(table.column_properties) < table.columns:
            table.column_properties.append({})
        for index in indices:
            if 0 <= index < len(table.column_properties):
                _merge_style(table.column_properties[index], properties, _fields(payload))
        return {}

    def _do_update_table_row_style(self, payload: Dict) -> Dict:
        table = self._table_from_payload(payload)
        style = payload.get("tableRowStyle", {})
        indices = payload.get("rowIndices") or list(range(len(table.rows)))
        for index in indices:
            if 0 <= index < len(table.rows):
                _merge_style(table.rows[index].style, style, _fields(payload))
        return {}

    def _do_merge_table_cells(self, payload: Dict) -> Dict:
        table_range = payload["tableRange"]
        cell_location = table_range["tableCellLocation"]
        table = self._table_from_payload(cell_location)
        row_index = cell_location.get("rowIndex", 0)
        column_index = cell_location.get("columnIndex", 0)
        row_span = table_range.get("rowSpan", 1)
        column_span = table_range.get("columnSpan", 1)

        anchor = self._cell_at(table, row_index, column_index)
        if anchor is None:
            raise DocsError("no cell at (%d, %d)" % (row_index, column_index))
        for row_offset in range(row_span):
            for column_offset in range(column_span):
                if (row_offset, column_offset) == (0, 0):
                    continue
                target_row = row_index + row_offset
                target_column = column_index + column_offset
                cell = self._cell_at(table, target_row, target_column)
                if cell is None:
                    continue
                # Merged-away cells leave the row entirely; the covering cell
                # keeps the span.  Any content they held moves into the anchor.
                for node in cell.content:
                    if isinstance(node, Paragraph) and not node.runs:
                        continue
                    anchor.content.append(node)
                _remove_identical(table.rows[target_row].cells, cell)
        anchor.row_span = row_span
        anchor.column_span = column_span
        return {}

    def _cell_at(self, table: Table, row_index: int, column_index: int) -> Optional[Cell]:
        layout = _layout(table)
        return layout.get((row_index, column_index))

    # -- document level ----------------------------------------------------

    def _do_update_document_style(self, payload: Dict) -> Dict:
        _merge_style(self.document_style, payload.get("documentStyle", {}), _fields(payload))
        return {}

    def _do_update_section_style(self, payload: Dict) -> Dict:
        span = payload.get("range", {})
        start = span.get("startIndex", 0)
        self._check_range("", start, span.get("endIndex", start))
        style = payload.get("sectionStyle", {})
        fields = _fields(payload)
        target = None
        for node, node_start, _container, _position in self._walk(""):
            if isinstance(node, SectionBreak) and node_start <= start:
                target = node
        if target is None:
            raise DocsError("no section found for range starting at %d" % start)
        _merge_style(target.style, style, fields)
        return {}

    # -- validation --------------------------------------------------------

    def _check_range(self, segment_id: str, start: int, end: int) -> None:
        if end <= start:
            raise DocsError("empty range %d..%d" % (start, end))
        limit = self.end_index(segment_id)
        if start < 0 or end > limit:
            raise DocsError(
                "range %d..%d is outside segment %r (end index %d)"
                % (start, end, segment_id or "body", limit)
            )

    # -- rendering ---------------------------------------------------------

    def render(self) -> Dict:
        document = {
            "documentId": self.document_id,
            "title": self.title,
            "body": {"content": _render_content(self.body.content, 0)},
            "documentStyle": dict(self.document_style),
            "inlineObjects": dict(self.inline_objects),
            "lists": dict(self.lists),
        }
        for key, store in (
            ("headers", self.headers),
            ("footers", self.footers),
            ("footnotes", self.footnotes),
        ):
            rendered = {}
            for segment_id, segment in store.items():
                id_key = {"headers": "headerId", "footers": "footerId", "footnotes": "footnoteId"}[key]
                rendered[segment_id] = {
                    id_key: segment_id,
                    "content": _render_content(segment.content, 0),
                }
            document[key] = rendered
        return document


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snake(name: str) -> str:
    out = []
    for character in name:
        if character.isupper():
            out.append("_")
            out.append(character.lower())
        else:
            out.append(character)
    return "".join(out)


def _fields(payload: Dict) -> Optional[List[str]]:
    fields = payload.get("fields")
    if not fields or fields == "*":
        return None
    return [name.strip() for name in fields.split(",") if name.strip()]


def _merge_style(target: Dict, style: Dict, fields: Optional[List[str]]) -> None:
    names = fields if fields is not None else list(style)
    for name in names:
        if name in style:
            target[name] = style[name]
        elif fields is not None:
            target.pop(name, None)


def _style_at(paragraph: Paragraph, offset: int) -> Dict:
    position = 0
    for run in paragraph.runs:
        if position <= offset < position + run.length and run.kind == "text":
            return run.style
        position += run.length
    if paragraph.runs and paragraph.runs[-1].kind == "text":
        return paragraph.runs[-1].style
    return {}


def _split_runs(runs: Sequence[Run], offset: int) -> Tuple[List[Run], List[Run]]:
    head: List[Run] = []
    tail: List[Run] = []
    position = 0
    for run in runs:
        run_end = position + run.length
        if run_end <= offset:
            head.append(run)
        elif position >= offset:
            tail.append(run)
        elif run.kind == "text":
            cut = offset - position
            head.append(Run(run.text[:cut], dict(run.style)))
            tail.append(Run(run.text[cut:], dict(run.style)))
        else:
            tail.append(run)
        position = run_end
    return head, tail


def _compact(runs: Sequence[Run]) -> List[Run]:
    out: List[Run] = []
    for run in runs:
        if run.kind == "text" and not run.text:
            continue
        if out and run.kind == "text" and out[-1].kind == "text" and out[-1].style == run.style:
            out[-1] = Run(out[-1].text + run.text, out[-1].style)
        else:
            out.append(run)
    return out


def _walk_content(content: Sequence, start: int):
    position = start
    for index, node in enumerate(content):
        yield node, position, content, index
        if isinstance(node, Table):
            row_position = position + 1
            for row in node.rows:
                cell_position = row_position + 1
                for cell in row.cells:
                    yield from _walk_content(cell.content, cell_position + 1)
                    cell_position += cell.length
                row_position += row.length
        position += node.length


def _remove_identical(cells: List[Cell], target: Cell) -> None:
    """Drop ``target`` by identity.

    ``list.remove`` goes by equality, and two empty cells of a fresh table
    compare equal field for field — so it would happily remove the wrong one.
    """
    for position, cell in enumerate(cells):
        if cell is target:
            del cells[position]
            return


def _layout(table: Table) -> Dict[Tuple[int, int], Cell]:
    layout: Dict[Tuple[int, int], Cell] = {}
    covered: Dict[Tuple[int, int], bool] = {}
    for row_index, row in enumerate(table.rows):
        column = 0
        for cell in row.cells:
            while covered.get((row_index, column)):
                column += 1
            layout[(row_index, column)] = cell
            for extra_row in range(row_index, row_index + cell.row_span):
                for extra_column in range(column, column + cell.column_span):
                    if (extra_row, extra_column) != (row_index, column):
                        covered[(extra_row, extra_column)] = True
            column += cell.column_span
    return layout


def _render_content(content: Sequence, start: int) -> List[Dict]:
    out = []
    position = start
    for node in content:
        element: Dict = {"startIndex": position, "endIndex": position + node.length}
        if isinstance(node, Paragraph):
            element["paragraph"] = _render_paragraph(node, position)
        elif isinstance(node, Table):
            element["table"] = _render_table(node, position)
        elif isinstance(node, SectionBreak):
            element["sectionBreak"] = {"sectionStyle": dict(node.style)}
        out.append(element)
        position += node.length
    return out


def _render_paragraph(paragraph: Paragraph, start: int) -> Dict:
    elements = []
    position = start
    for run in paragraph.runs:
        element: Dict = {"startIndex": position, "endIndex": position + run.length}
        if run.kind == "text":
            element["textRun"] = {"content": run.text, "textStyle": dict(run.style)}
        elif run.kind == "image":
            element["inlineObjectElement"] = {
                "inlineObjectId": run.object_id,
                "textStyle": dict(run.style),
            }
        elif run.kind == "pageBreak":
            element["pageBreak"] = {"textStyle": dict(run.style)}
        elif run.kind == "footnoteReference":
            element["footnoteReference"] = {
                "footnoteId": run.object_id,
                "textStyle": dict(run.style),
            }
        elements.append(element)
        position += run.length
    elements.append(
        {
            "startIndex": position,
            "endIndex": position + 1,
            "textRun": {
                "content": "\n",
                "textStyle": dict(paragraph.style.get("markStyle", {})),
            },
        }
    )
    rendered: Dict = {
        "elements": elements,
        "paragraphStyle": {
            key: value for key, value in paragraph.style.items() if key != "markStyle"
        },
    }
    if paragraph.bullet:
        rendered["bullet"] = dict(paragraph.bullet)
    return rendered


def _render_table(table: Table, start: int) -> Dict:
    rows = []
    row_position = start + 1
    for row in table.rows:
        cells = []
        cell_position = row_position + 1
        for cell in row.cells:
            style = dict(cell.style)
            style["rowSpan"] = cell.row_span
            style["columnSpan"] = cell.column_span
            cells.append(
                {
                    "startIndex": cell_position,
                    "endIndex": cell_position + cell.length,
                    "tableCellStyle": style,
                    "content": _render_content(cell.content, cell_position + 1),
                }
            )
            cell_position += cell.length
        rows.append(
            {
                "startIndex": row_position,
                "endIndex": row_position + row.length,
                "tableCells": cells,
                "tableRowStyle": dict(row.style),
            }
        )
        row_position += row.length
    return {
        "rows": len(table.rows),
        "columns": table.columns,
        "tableRows": rows,
        "tableStyle": {"tableColumnProperties": [dict(p) for p in table.column_properties]},
    }
