"""Open Packaging Conventions plumbing: parts, relationships, content types."""

from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass
from typing import Dict, Optional
from xml.etree import ElementTree as ET

from .oxml import NAMESPACES

OFFICE_DOCUMENT_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)


@dataclass(frozen=True)
class Relationship:
    rel_id: str
    rel_type: str
    target: str
    target_mode: str = "Internal"

    @property
    def is_external(self) -> bool:
        return self.target_mode.lower() == "external"

    @property
    def kind(self) -> str:
        """Trailing segment of the relationship type, e.g. ``"image"``."""
        return self.rel_type.rsplit("/", 1)[-1]


class DocxPackage:
    """Random access to the parts of a .docx (a zip of XML parts + media)."""

    def __init__(self, source):
        self._zip = zipfile.ZipFile(source)
        self._rels_cache: Dict[str, Dict[str, Relationship]] = {}
        self._xml_cache: Dict[str, ET.Element] = {}
        self.main_part = self._find_main_part()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> "DocxPackage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- raw parts ---------------------------------------------------------

    def names(self):
        return self._zip.namelist()

    def has_part(self, name: str) -> bool:
        return name.lstrip("/") in self._zip.namelist()

    def read(self, name: str) -> bytes:
        return self._zip.read(name.lstrip("/"))

    def xml(self, name: str) -> Optional[ET.Element]:
        name = name.lstrip("/")
        if name in self._xml_cache:
            return self._xml_cache[name]
        if not self.has_part(name):
            return None
        root = ET.fromstring(self.read(name))
        self._xml_cache[name] = root
        return root

    # -- relationships -----------------------------------------------------

    @staticmethod
    def rels_part_name(part_name: str) -> str:
        directory, _, base = part_name.lstrip("/").rpartition("/")
        return posixpath.join(directory, "_rels", base + ".rels") if directory else posixpath.join("_rels", base + ".rels")

    def rels(self, part_name: str) -> Dict[str, Relationship]:
        part_name = part_name.lstrip("/")
        if part_name in self._rels_cache:
            return self._rels_cache[part_name]
        rels: Dict[str, Relationship] = {}
        rels_name = self.rels_part_name(part_name)
        root = self.xml(rels_name)
        if root is not None:
            for node in root.findall("{%s}Relationship" % NAMESPACES["rel"]):
                rel = Relationship(
                    rel_id=node.get("Id", ""),
                    rel_type=node.get("Type", ""),
                    target=node.get("Target", ""),
                    target_mode=node.get("TargetMode", "Internal"),
                )
                rels[rel.rel_id] = rel
        self._rels_cache[part_name] = rels
        return rels

    def resolve(self, part_name: str, target: str) -> str:
        """Resolve a relationship target against the part that declares it."""
        if target.startswith("/"):
            return target.lstrip("/")
        base = posixpath.dirname(part_name.lstrip("/"))
        return posixpath.normpath(posixpath.join(base, target)) if base else target

    def related_part(self, part_name: str, rel_id: str) -> Optional[str]:
        rel = self.rels(part_name).get(rel_id)
        if rel is None or rel.is_external:
            return None
        return self.resolve(part_name, rel.target)

    def relationship(self, part_name: str, rel_id: str) -> Optional[Relationship]:
        return self.rels(part_name).get(rel_id)

    # -- well-known parts --------------------------------------------------

    def _find_main_part(self) -> str:
        root_rels = self.xml("_rels/.rels")
        if root_rels is not None:
            for node in root_rels.findall("{%s}Relationship" % NAMESPACES["rel"]):
                if node.get("Type") == OFFICE_DOCUMENT_REL:
                    return node.get("Target", "word/document.xml").lstrip("/")
        return "word/document.xml"

    def main_related(self, kind: str) -> Optional[str]:
        """Find a part related to the main document by relationship kind."""
        for rel in self.rels(self.main_part).values():
            if rel.kind == kind and not rel.is_external:
                return self.resolve(self.main_part, rel.target)
        return None

    def content_type(self, part_name: str) -> Optional[str]:
        root = self.xml("[Content_Types].xml")
        if root is None:
            return None
        ns = NAMESPACES["ct"]
        part_name = "/" + part_name.lstrip("/")
        for node in root.findall("{%s}Override" % ns):
            if node.get("PartName") == part_name:
                return node.get("ContentType")
        extension = part_name.rsplit(".", 1)[-1].lower()
        for node in root.findall("{%s}Default" % ns):
            if (node.get("Extension") or "").lower() == extension:
                return node.get("ContentType")
        return None

    def core_title(self) -> Optional[str]:
        root = self.xml("docProps/core.xml")
        if root is None:
            return None
        dc = "http://purl.org/dc/elements/1.1/"
        node = root.find("{%s}title" % dc)
        if node is not None and (node.text or "").strip():
            return node.text.strip()
        return None
