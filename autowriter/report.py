"""The fidelity report: what was copied, and everything that was not."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .ir import Document, Note

KIND_TITLES = {
    "unsupported": "Not reproducible in Google Docs",
    "approximation": "Copied approximately",
    "field": "Word fields",
    "tracked-change": "Tracked changes",
}


@dataclass
class FidelityReport:
    source: str
    document_url: Optional[str] = None
    paragraphs: int = 0
    tables: int = 0
    characters: int = 0
    images: int = 0
    requests: int = 0
    batches: int = 0
    notes: List[Note] = field(default_factory=list)
    differences: List[str] = field(default_factory=list)
    verified: Optional[bool] = None

    def add_notes(self, notes: List[Note]) -> None:
        for note in notes:
            if note not in self.notes:
                self.notes.append(note)

    def add_messages(self, kind: str, messages: List[str]) -> None:
        for message in messages:
            self.add_notes([Note(kind, message)])

    # -- rendering ---------------------------------------------------------

    def to_dict(self) -> Dict:
        return {
            "source": self.source,
            "documentUrl": self.document_url,
            "verified": self.verified,
            "counts": {
                "paragraphs": self.paragraphs,
                "tables": self.tables,
                "characters": self.characters,
                "images": self.images,
                "requests": self.requests,
                "batches": self.batches,
            },
            "notes": [
                {"kind": note.kind, "message": note.message, "location": note.location}
                for note in self.notes
            ],
            "differences": list(self.differences),
        }

    def to_text(self) -> str:
        lines: List[str] = []
        lines.append("Source: %s" % self.source)
        if self.document_url:
            lines.append("Google Doc: %s" % self.document_url)
        lines.append(
            "Copied: %d paragraphs, %d tables, %d images, %d characters "
            "(%d requests in %d batches)"
            % (
                self.paragraphs,
                self.tables,
                self.images,
                self.characters,
                self.requests,
                self.batches,
            )
        )

        if self.verified is True:
            lines.append("Verification: every paragraph, character and property matches the source.")
        elif self.verified is False:
            lines.append("Verification: %d difference(s) found." % len(self.differences))
        else:
            lines.append("Verification: not run.")

        if self.differences:
            lines.append("")
            lines.append("Differences")
            lines.append("-" * 11)
            for difference in self.differences[:60]:
                lines.append("  %s" % difference)
            if len(self.differences) > 60:
                lines.append("  ... and %d more" % (len(self.differences) - 60))

        grouped: Dict[str, List[Note]] = {}
        for note in self.notes:
            grouped.setdefault(note.kind, []).append(note)
        for kind, notes in grouped.items():
            lines.append("")
            title = KIND_TITLES.get(kind, kind.replace("-", " ").capitalize())
            lines.append(title)
            lines.append("-" * len(title))
            for note in notes:
                location = " (%s)" % note.location if note.location else ""
                lines.append("  - %s%s" % (note.message, location))

        if not self.notes:
            lines.append("")
            lines.append("Nothing in the source document needed approximating.")
        return "\n".join(lines)


def count_content(document: Document) -> Dict[str, int]:
    from .ir import Paragraph, Table

    counts = {"paragraphs": 0, "tables": 0, "images": len(document.assets)}

    def walk(blocks) -> None:
        for block in blocks:
            if isinstance(block, Table):
                counts["tables"] += 1
                for row in block.rows:
                    for cell in row.cells:
                        walk(cell.blocks)
            elif isinstance(block, Paragraph):
                counts["paragraphs"] += 1

    for section in document.sections:
        walk(section.blocks)
        for store in (section.headers, section.footers):
            for content in store.values():
                walk(content.blocks)
    return counts
