"""Namespace plumbing for OOXML.

``xml.etree.ElementTree`` reports tags in Clark notation
(``{namespace}local``), so every lookup goes through :func:`qn`, which turns a
readable ``"w:pPr"`` into the expanded form.
"""

from __future__ import annotations

from typing import Iterable, Iterator, Optional
from xml.etree import ElementTree as ET

NAMESPACES = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "v": "urn:schemas-microsoft-com:vml",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
}

for _prefix, _uri in NAMESPACES.items():
    ET.register_namespace("" if _prefix == "w" else _prefix, _uri)


def qn(tag: str) -> str:
    """``"w:pPr"`` -> ``"{http://...main}pPr"``."""
    prefix, _, local = tag.partition(":")
    if not local:
        return tag
    return "{%s}%s" % (NAMESPACES[prefix], local)


def local_name(tag: str) -> str:
    return tag.rpartition("}")[2]


def get(element: Optional[ET.Element], tag: str) -> Optional[ET.Element]:
    if element is None:
        return None
    return element.find(qn(tag))


def findall(element: Optional[ET.Element], tag: str) -> Iterable[ET.Element]:
    if element is None:
        return ()
    return element.findall(qn(tag))


def attr(element: Optional[ET.Element], name: str, default=None) -> Optional[str]:
    if element is None:
        return default
    value = element.get(qn(name))
    return default if value is None else value


def val(element: Optional[ET.Element], default=None) -> Optional[str]:
    """The ubiquitous ``w:val`` attribute."""
    return attr(element, "w:val", default)


def int_val(element: Optional[ET.Element], name: str = "w:val") -> Optional[int]:
    raw = attr(element, name)
    if raw is None:
        return None
    try:
        return int(round(float(raw)))
    except (TypeError, ValueError):
        return None


def float_val(element: Optional[ET.Element], name: str = "w:val") -> Optional[float]:
    raw = attr(element, name)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


ON_OFF_FALSE = {"0", "false", "off"}


def on_off(element: Optional[ET.Element], default: Optional[bool] = None) -> Optional[bool]:
    """OOXML toggle properties: present means on unless ``w:val`` says otherwise."""
    if element is None:
        return default
    raw = val(element)
    if raw is None:
        return True
    return raw.strip().lower() not in ON_OFF_FALSE


def iter_children(element: Optional[ET.Element]) -> Iterator[ET.Element]:
    if element is None:
        return iter(())
    return iter(element)
