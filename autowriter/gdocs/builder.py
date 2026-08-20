"""Retype a document into Google Docs, one editing operation at a time.

Google Docs has no "import this .docx" API worth trusting for fidelity, so this
module does what a person would do: put the text in, then style it.  The whole
problem is bookkeeping — every insertion shifts the indices of everything after
it, and the Docs API addresses everything by index.

Three rules keep that under control:

1. **Append forward only.**  Content is always inserted at the end of what has
   been written, so previously recorded ranges never move.
2. **Order within a batch is a contract.**  Requests in one ``batchUpdate`` see
   the document as the preceding requests left it, so each flush emits, in
   order: text/content inserts, then named styles, then paragraph styles, then
   character styles, then bullets *in reverse document order* (bullet creation
   deletes the leading tabs that encode nesting depth, which moves everything
   after it), then the re-indent pass with indices we have adjusted ourselves.
3. **Re-read when the footprint is unknowable.**  A table's index span depends
   on how Docs lays it out, so tables are inserted, then the document is
   fetched, then cells are filled *back to front* — filling a later cell cannot
   disturb an earlier one's indices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .. import ir
from ..units import u16len
from . import requests as R

MAX_REQUESTS_PER_BATCH = 300


class DocsTransport:
    """The slice of the Docs API this module needs.

    Implemented for real by :class:`autowriter.gdocs.client.ApiTransport` and
    in memory by :class:`autowriter.gdocs.simulator.SimulatedDocs`.
    """

    def get_document(self) -> Dict:  # pragma: no cover - interface
        raise NotImplementedError

    def batch_update(self, requests: List[Dict]) -> List[Dict]:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass
class CopyOptions:
    #: Word's "All caps" only changes rendering; Docs has no such toggle, so the
    #: text itself is upper-cased to keep the copy looking identical.
    render_all_caps: bool = True
    #: Word hides ``w:vanish`` runs on screen and in print; they are dropped.
    include_hidden_text: bool = False
    copy_headers_footers: bool = True
    copy_footnotes: bool = True


@dataclass
class CopyResult:
    request_count: int = 0
    batch_count: int = 0
    notes: List[ir.Note] = field(default_factory=list)

    def note(self, kind: str, message: str, location: Optional[str] = None) -> None:
        entry = ir.Note(kind, message, location)
        if entry not in self.notes:
            self.notes.append(entry)


@dataclass
class _ListItem:
    """A list paragraph waiting for its bullets, recorded during a flush."""

    start: int
    end: int
    level: int
    marker: ir.ListMarker
    props: ir.ParagraphProps


class SegmentWriter:
    """Writes a block sequence into one segment (body, header, footer, cell...)."""

    def __init__(
        self,
        copier: "Copier",
        segment_id: str = "",
        cursor: int = 1,
        in_table: bool = False,
    ):
        self.copier = copier
        self.segment_id = segment_id
        self.cursor = cursor
        self.in_table = in_table
        self._content: List[Dict] = []
        self._named: List[Dict] = []
        self._paragraph: List[Dict] = []
        self._text: List[Dict] = []
        self._open_run: List[_ListItem] = []
        self._runs: List[List[_ListItem]] = []
        self._buffer: List[str] = []
        self._buffer_start = cursor
        self._footnote_tasks: List[Tuple[int, List[ir.Block]]] = []

    # -- low level ---------------------------------------------------------

    def _append_text(self, text: str) -> None:
        if not text:
            return
        if not self._buffer:
            self._buffer_start = self.cursor
        self._buffer.append(text)
        self.cursor += u16len(text)

    def _flush_text(self) -> None:
        if not self._buffer:
            return
        self._content.append(
            R.insert_text(self._buffer_start, "".join(self._buffer), self.segment_id)
        )
        self._buffer = []

    def _emit(self, request: Dict) -> None:
        self._flush_text()
        self._content.append(request)

    def pending_request_count(self) -> int:
        return len(self._content) + len(self._named) + len(self._paragraph) + len(self._text)

    def reposition(self, index: int) -> None:
        """Point the writer at a new index (after an out-of-band change)."""
        self._flush_text()
        self.cursor = index
        self._buffer_start = index

    # -- flushing ----------------------------------------------------------

    def flush(self) -> None:
        """Send everything queued, in the order the index maths depends on."""
        self._close_run()
        self._flush_text()
        batch: List[Dict] = []
        batch.extend(self._content)
        batch.extend(self._named)
        batch.extend(self._paragraph)
        batch.extend(self._text)

        bullet_requests, reindent_requests, removed = self._bullet_requests()
        batch.extend(bullet_requests)
        batch.extend(reindent_requests)

        footnote_positions = [
            (index, blocks)
            for index, blocks in self._footnote_tasks
        ]
        if not batch:
            self._reset()
            return

        replies = self.copier.send(batch)
        self.cursor -= removed
        for position, blocks in footnote_positions:
            reply = replies[position] if position < len(replies) else {}
            footnote_id = (reply or {}).get("createFootnote", {}).get("footnoteId")
            if footnote_id:
                self.copier.write_footnote(footnote_id, blocks)
        self._reset()

    def _reset(self) -> None:
        self._content = []
        self._named = []
        self._paragraph = []
        self._text = []
        self._runs = []
        self._footnote_tasks = []
        self._buffer = []
        self._buffer_start = self.cursor

    def _bullet_requests(self) -> Tuple[List[Dict], List[Dict], int]:
        """Bullets (reverse order) plus the re-indent pass that follows them.

        ``createParagraphBullets`` strips the leading tabs that told it how deep
        to nest, so every paragraph after the request moves left.  Emitting the
        runs back to front keeps each request's own range valid, and the indent
        fix-ups are pre-shifted by the number of tabs removed ahead of them.
        """
        if not self._runs:
            return [], [], 0

        bullets: List[Dict] = []
        reindent: List[Dict] = []
        shifts: List[Tuple[int, int]] = []  # (position, tabs removed at/after it)
        total_removed = 0

        for run in self._runs:
            run_start = run[0].start
            run_end = run[-1].end
            bullets.append(
                (run_start, R.create_bullets(run_start, run_end, run[0].marker, self.segment_id))
            )
            for item in run:
                shifts.append((item.start, item.level))
                total_removed += item.level

        bullets.sort(key=lambda pair: pair[0], reverse=True)
        ordered = [request for _, request in bullets]

        shifts.sort()
        removed_before: Dict[int, int] = {}
        running = 0
        for position, tabs in shifts:
            removed_before[position] = running
            running += tabs

        for run in self._runs:
            for item in run:
                before = removed_before[item.start]
                start = item.start - before
                end = item.end - before - item.level
                request = R.update_paragraph_style(
                    start, end, item.props, self.segment_id, include_indents=True
                )
                if request is not None:
                    reindent.append(request)
        return ordered, reindent, total_removed

    def _close_run(self) -> None:
        if self._open_run:
            self._runs.append(self._open_run)
            self._open_run = []

    # -- blocks ------------------------------------------------------------

    def write_blocks(self, blocks: Sequence[ir.Block]) -> None:
        for index, block in enumerate(blocks):
            is_last = index == len(blocks) - 1
            if isinstance(block, ir.Table):
                self.write_table(block)
            else:
                self.write_paragraph(block)
                if not is_last:
                    self._append_text("\n")
                # Flush periodically so one batch never grows unbounded; only
                # safe between list runs, whose bullet maths spans the batch.
                if not self._open_run and self.pending_request_count() >= MAX_REQUESTS_PER_BATCH:
                    self.flush()

    def write_paragraph(self, paragraph: ir.Paragraph) -> None:
        start = self.cursor
        marker = paragraph.list_marker
        if marker is not None and marker.level:
            # Leading tabs are how createParagraphBullets is told the nesting
            # depth; it removes them again when the bullets are applied.
            self._append_text("\t" * marker.level)

        wrote_content = False
        for inline in paragraph.inlines:
            if isinstance(inline, ir.TextRun):
                wrote_content |= self._write_text_run(inline)
            elif isinstance(inline, ir.PageBreakRun):
                self._emit(R.insert_page_break(self.cursor, self.segment_id))
                self.cursor += 1
                wrote_content = True
            elif isinstance(inline, ir.ImageRun):
                wrote_content |= self._write_image(inline)
            elif isinstance(inline, ir.FootnoteRun):
                wrote_content |= self._write_footnote_reference(inline)

        end = self.cursor
        # A paragraph's range has to include its terminating newline, otherwise
        # an empty paragraph would be a zero-length (and rejected) range.
        style_end = end + 1

        # Always stated, never inherited: the Google Doc's "Normal text" is not
        # the source document's, so even NORMAL_TEXT is set explicitly.
        self._named.append(
            R.update_named_style(
                start, style_end, paragraph.props.named_style or "NORMAL_TEXT", self.segment_id
            )
        )

        request = R.update_paragraph_style(start, style_end, paragraph.props, self.segment_id)
        if request is not None:
            self._paragraph.append(request)

        if not wrote_content:
            # Empty paragraph: style the paragraph mark so the blank line keeps
            # the source document's line height.
            self._text.append(
                R.update_text_style(start, style_end, paragraph.mark_style, self.segment_id)
            )

        if marker is not None:
            item = _ListItem(start, style_end, marker.level, marker, paragraph.props)
            if self._open_run and self._open_run[-1].marker.num_id != marker.num_id:
                self._close_run()
            self._open_run.append(item)
        else:
            self._close_run()

    def _write_text_run(self, run: ir.TextRun) -> bool:
        text = run.text
        style = run.style
        if style.hidden and not self.copier.options.include_hidden_text:
            self.copier.result.note(
                "approximation", "hidden text was left out, as Word displays it"
            )
            return False
        if style.all_caps and self.copier.options.render_all_caps:
            text = text.upper()
            self.copier.result.note(
                "approximation",
                'Word\'s "All caps" was applied to the text itself; Google Docs has no such toggle',
            )
        if not text:
            return False
        start = self.cursor
        self._append_text(text)
        self._text.append(R.update_text_style(start, self.cursor, style, self.segment_id))
        return True

    def _write_image(self, image: ir.ImageRun) -> bool:
        uri = self.copier.image_uris.get(image.asset_id)
        if not uri:
            self.copier.result.note(
                "unsupported",
                "an image was skipped because no public URL was available for it",
            )
            return False
        self._emit(
            R.insert_inline_image(
                self.cursor, uri, image.width_pt, image.height_pt, self.segment_id
            )
        )
        self.cursor += 1
        if image.alt_description or image.alt_title:
            self.copier.result.note(
                "unsupported", "image alt text cannot be set through the Docs API"
            )
        return True

    def _write_footnote_reference(self, footnote: ir.FootnoteRun) -> bool:
        if not self.copier.options.copy_footnotes:
            return False
        if self.segment_id or self.in_table:
            self.copier.result.note(
                "unsupported",
                "footnotes inside headers, footers or table cells were left out; "
                "the Docs API can only anchor a footnote in the body",
            )
            return False
        self._flush_text()
        self._footnote_tasks.append((len(self._content), footnote.blocks))
        self._content.append(R.create_footnote(self.cursor))
        self.cursor += 1
        return True

    # -- tables ------------------------------------------------------------

    def write_table(self, table: ir.Table) -> None:
        columns = table.column_count
        rows = len(table.rows)
        if not rows or not columns:
            return

        self.flush()
        table_start = self.cursor
        batch: List[Dict] = [R.insert_table(table_start, rows, columns, self.segment_id)]
        batch.extend(self._merge_requests(table, table_start))
        self.copier.send(batch)

        self.copier.fill_table(table, table_start, self.segment_id)
        # Styling never moves an index, so one final read is enough to find out
        # where the table ended up ending.
        self.copier.send(self._table_style_requests(table, table_start))

        snapshot = self.copier.transport.get_document()
        element = find_table(segment_content(snapshot, self.segment_id), table_start)
        if element is None:
            raise RuntimeError("table inserted at %d could not be found again" % table_start)
        self._reset()
        self.reposition(element["endIndex"])

    def _merge_requests(self, table: ir.Table, table_start: int) -> List[Dict]:
        out: List[Dict] = []
        for row_index, row in enumerate(table.rows):
            column = 0
            for cell in row.cells:
                if cell.merged_away:
                    column += cell.col_span
                    continue
                if cell.row_span > 1 or cell.col_span > 1:
                    out.append(
                        R.merge_cells(
                            table_start,
                            row_index,
                            column,
                            cell.row_span,
                            cell.col_span,
                            self.segment_id,
                        )
                    )
                column += cell.col_span
        return out

    def _table_style_requests(self, table: ir.Table, table_start: int) -> List[Dict]:
        out: List[Dict] = []
        widths: Dict[float, List[int]] = {}
        for index, width in enumerate(table.column_widths_pt):
            if width:
                widths.setdefault(round(width, 3), []).append(index)
        for width, columns in widths.items():
            out.append(R.update_column_width(table_start, columns, width, self.segment_id))

        heights: Dict[Tuple[Optional[float], bool], List[int]] = {}
        for index, row in enumerate(table.rows):
            heights.setdefault((row.min_height_pt, row.is_header), []).append(index)
        for (height, header), rows in heights.items():
            request = R.update_row_style(table_start, rows, height, header, self.segment_id)
            if request is not None:
                out.append(request)

        for row_index, row in enumerate(table.rows):
            column = 0
            for cell in row.cells:
                if not cell.merged_away:
                    request = R.update_table_cell_style(
                        table_start,
                        row_index,
                        column,
                        cell,
                        self.segment_id,
                        row_span=1,
                        column_span=1,
                    )
                    if request is not None:
                        out.append(request)
                column += cell.col_span
        return out


class Copier:
    """Drives a whole document copy against a :class:`DocsTransport`."""

    def __init__(
        self,
        transport: DocsTransport,
        image_uris: Optional[Dict[str, str]] = None,
        options: Optional[CopyOptions] = None,
    ):
        self.transport = transport
        self.image_uris = image_uris or {}
        self.options = options or CopyOptions()
        self.result = CopyResult()

    # -- plumbing ----------------------------------------------------------

    def send(self, requests: List[Dict]) -> List[Dict]:
        if not requests:
            return []
        replies: List[Dict] = []
        for chunk in _chunks(requests, MAX_REQUESTS_PER_BATCH):
            replies.extend(self.transport.batch_update(chunk) or [{}] * len(chunk))
            self.result.request_count += len(chunk)
            self.result.batch_count += 1
        return replies

    # -- entry point -------------------------------------------------------

    def copy(self, document: ir.Document) -> CopyResult:
        for note in document.notes:
            self.result.notes.append(note)

        if document.sections:
            request = R.update_document_style(document.sections[0].props)
            if request is not None:
                self.send([request])

        snapshot = self.transport.get_document()
        writer = SegmentWriter(self, "", body_start_index(snapshot))

        for index, section in enumerate(document.sections):
            if index > 0:
                writer.flush()
                break_index = writer.cursor
                self.send([R.insert_section_break(break_index, section.props.section_type)])
                snapshot = self.transport.get_document()
                writer.reposition(end_of_segment(snapshot, ""))
                self._apply_section_style(section, break_index)
                if section.props.page_width_pt != document.sections[0].props.page_width_pt:
                    self.result.note(
                        "unsupported",
                        "per-section page size (e.g. a landscape section) is document-wide "
                        "in Google Docs; the first section's page size was used",
                    )
            writer.write_blocks(section.blocks)
        writer.flush()

        if self.options.copy_headers_footers:
            self._copy_headers_and_footers(document)
        return self.result

    def _apply_section_style(self, section: ir.Section, break_index: int) -> None:
        # A one-unit range inside the new section is enough to identify it, and
        # is always valid: the break is followed by at least one paragraph.
        # Measuring to the end of the section would not be — the style is
        # applied before the section has any content in it.
        start = break_index + 1
        request = R.update_section_style(start, start + 1, section.props)
        if request is not None:
            self.send([request])

    # -- sub-segments ------------------------------------------------------

    def fill_table(self, table: ir.Table, table_start: int, segment_id: str) -> None:
        """Fill a freshly inserted table, last cell first.

        Filling backwards means each cell's start index — read from a single
        snapshot taken before any of them were touched — is still accurate when
        its turn comes, because only content *after* an insertion moves.
        """
        snapshot = self.transport.get_document()
        element = find_table(segment_content(snapshot, segment_id), table_start)
        if element is None:
            raise RuntimeError("table inserted at %d could not be found" % table_start)
        layout = table_layout(element["table"])

        targets: List[Tuple[int, List[ir.Block]]] = []
        for row_index, row in enumerate(table.rows):
            column = 0
            for cell in row.cells:
                if not cell.merged_away:
                    start = layout.get((row_index, column))
                    if start is None:
                        self.result.note(
                            "approximation",
                            "a merged table cell could not be located; its text was left out",
                        )
                    else:
                        targets.append((start, cell.blocks))
                column += cell.col_span

        for start, blocks in sorted(targets, key=lambda pair: pair[0], reverse=True):
            if not blocks:
                continue
            writer = SegmentWriter(self, segment_id, start, in_table=True)
            writer.write_blocks(blocks)
            writer.flush()

    def write_footnote(self, footnote_id: str, blocks: Sequence[ir.Block]) -> None:
        if not blocks:
            return
        snapshot = self.transport.get_document()
        content = segment_content(snapshot, footnote_id)
        start = content[0]["startIndex"] if content else 0
        writer = SegmentWriter(self, footnote_id, start)
        writer.write_blocks(blocks)
        writer.flush()

    def _copy_headers_and_footers(self, document: ir.Document) -> None:
        for index, section in enumerate(document.sections):
            for kind, store, factory in (
                ("header", section.headers, R.create_header),
                ("footer", section.footers, R.create_footer),
            ):
                if not store:
                    continue
                extra = [name for name in store if name != "default"]
                if extra:
                    self.result.note(
                        "unsupported",
                        "only the default %s was copied; the Docs API cannot create "
                        "first-page or even-page %ss" % (kind, kind),
                    )
                content = store.get("default")
                if content is None or not content.blocks:
                    continue
                if index > 0:
                    self.result.note(
                        "unsupported",
                        "per-section %ss are not supported; the first section's was used" % kind,
                    )
                    continue
                replies = self.send([factory(None)])
                reply = replies[0] if replies else {}
                key = "createHeader" if kind == "header" else "createFooter"
                segment_id = (reply or {}).get(key, {}).get(
                    "headerId" if kind == "header" else "footerId"
                )
                if not segment_id:
                    self.result.note("unsupported", "could not create a %s" % kind)
                    continue
                snapshot = self.transport.get_document()
                segment = segment_content(snapshot, segment_id)
                start = segment[0]["startIndex"] if segment else 0
                writer = SegmentWriter(self, segment_id, start)
                writer.write_blocks(content.blocks)
                writer.flush()


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def segment_content(document: Dict, segment_id: str) -> List[Dict]:
    if not segment_id:
        return document.get("body", {}).get("content", [])
    for key in ("headers", "footers", "footnotes"):
        segment = document.get(key, {}).get(segment_id)
        if segment:
            return segment.get("content", [])
    return []


def body_start_index(document: Dict) -> int:
    for element in document.get("body", {}).get("content", []):
        if "paragraph" in element:
            return element["startIndex"]
    return 1


def end_of_segment(document: Dict, segment_id: str) -> int:
    content = segment_content(document, segment_id)
    if not content:
        return 1
    return content[-1]["endIndex"] - 1


def find_table(content: Iterable[Dict], start_index: int) -> Optional[Dict]:
    """Find a table's structural element by start index, nested tables included."""
    for element in content or []:
        table = element.get("table")
        if table is None:
            continue
        if element.get("startIndex") == start_index:
            return element
        for row in table.get("tableRows", []):
            for cell in row.get("tableCells", []):
                found = find_table(cell.get("content", []), start_index)
                if found is not None:
                    return found
    return None


def table_layout(table: Dict) -> Dict[Tuple[int, int], int]:
    """Map (row, column) grid coordinates to each cell's first content index.

    Merged cells make this less obvious than it sounds: the covering cell
    carries a row/column span and the cells it covers are gone from the row, so
    the grid position has to be reconstructed by laying the table out the way a
    renderer would.
    """
    layout: Dict[Tuple[int, int], int] = {}
    covered: Dict[Tuple[int, int], bool] = {}
    for row_index, row in enumerate(table.get("tableRows", [])):
        column = 0
        for cell in row.get("tableCells", []):
            while covered.get((row_index, column)):
                column += 1
            style = cell.get("tableCellStyle", {})
            row_span = style.get("rowSpan") or 1
            column_span = style.get("columnSpan") or 1
            content = cell.get("content", [])
            if content:
                layout[(row_index, column)] = content[0]["startIndex"]
            for extra_row in range(row_index, row_index + row_span):
                for extra_column in range(column, column + column_span):
                    if (extra_row, extra_column) != (row_index, column):
                        covered[(extra_row, extra_column)] = True
            column += column_span
    return layout


def _chunks(items: List[Dict], size: int) -> Iterable[List[Dict]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
