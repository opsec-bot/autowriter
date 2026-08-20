"""numbering.xml: list definitions, levels and overrides."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

from .oxml import attr, findall, get, int_val, val


@dataclass
class LevelDef:
    level: int
    number_format: str = "bullet"
    level_text: str = ""
    start: Optional[int] = None
    justification: Optional[str] = None
    suffix: Optional[str] = None
    ppr: Optional[ET.Element] = None
    rpr: Optional[ET.Element] = None


@dataclass
class AbstractNum:
    abstract_id: str
    levels: Dict[int, LevelDef] = field(default_factory=dict)
    num_style_link: Optional[str] = None
    style_link: Optional[str] = None
    multi_level_type: Optional[str] = None


@dataclass
class NumInstance:
    num_id: str
    abstract_id: Optional[str] = None
    overrides: Dict[int, LevelDef] = field(default_factory=dict)
    start_overrides: Dict[int, int] = field(default_factory=dict)


class Numbering:
    """Answers "what marker does this paragraph get?" for a (numId, ilvl)."""

    def __init__(self, root: Optional[ET.Element] = None):
        self.abstract: Dict[str, AbstractNum] = {}
        self.instances: Dict[str, NumInstance] = {}
        self._style_links: Dict[str, str] = {}  # styleId -> abstractNumId
        if root is not None:
            self._load(root)

    def _load(self, root: ET.Element) -> None:
        for node in findall(root, "w:abstractNum"):
            abstract_id = attr(node, "w:abstractNumId")
            if abstract_id is None:
                continue
            definition = AbstractNum(
                abstract_id=abstract_id,
                num_style_link=val(get(node, "w:numStyleLink")),
                style_link=val(get(node, "w:styleLink")),
                multi_level_type=val(get(node, "w:multiLevelType")),
            )
            for level_node in findall(node, "w:lvl"):
                level = self._parse_level(level_node)
                if level is not None:
                    definition.levels[level.level] = level
            self.abstract[abstract_id] = definition
            if definition.style_link:
                self._style_links[definition.style_link] = abstract_id

        for node in findall(root, "w:num"):
            num_id = attr(node, "w:numId")
            if num_id is None:
                continue
            instance = NumInstance(
                num_id=num_id,
                abstract_id=val(get(node, "w:abstractNumId")),
            )
            for override in findall(node, "w:lvlOverride"):
                index = int_val(override, "w:ilvl") or 0
                start_override = int_val(get(override, "w:startOverride"))
                if start_override is not None:
                    instance.start_overrides[index] = start_override
                level_node = get(override, "w:lvl")
                if level_node is not None:
                    level = self._parse_level(level_node)
                    if level is not None:
                        instance.overrides[index] = level
            self.instances[num_id] = instance

    @staticmethod
    def _parse_level(node: ET.Element) -> Optional[LevelDef]:
        index = int_val(node, "w:ilvl")
        if index is None:
            index = 0
        return LevelDef(
            level=index,
            number_format=(val(get(node, "w:numFmt")) or "bullet"),
            level_text=(val(get(node, "w:lvlText")) or ""),
            start=int_val(get(node, "w:start")),
            justification=val(get(node, "w:lvlJc")),
            suffix=val(get(node, "w:suff")),
            ppr=get(node, "w:pPr"),
            rpr=get(node, "w:rPr"),
        )

    # -- lookup ------------------------------------------------------------

    def resolve_abstract(self, num_id: Optional[str],
                         style_numbering=None) -> Optional[AbstractNum]:
        """Follow numId -> abstractNumId, including ``w:numStyleLink`` hops."""
        instance = self.instances.get(num_id or "")
        if instance is None:
            return None
        definition = self.abstract.get(instance.abstract_id or "")
        seen = set()
        while definition is not None and definition.num_style_link:
            if definition.abstract_id in seen:
                break
            seen.add(definition.abstract_id)
            linked = self._style_links.get(definition.num_style_link)
            if linked is None and style_numbering is not None:
                linked_num_id, _ = style_numbering(definition.num_style_link)
                linked_instance = self.instances.get(linked_num_id or "")
                linked = linked_instance.abstract_id if linked_instance else None
            if linked is None:
                break
            definition = self.abstract.get(linked)
        return definition

    def level(self, num_id: Optional[str], level: int,
              style_numbering=None) -> Optional[LevelDef]:
        instance = self.instances.get(num_id or "")
        if instance is None:
            return None
        if level in instance.overrides:
            return instance.overrides[level]
        definition = self.resolve_abstract(num_id, style_numbering)
        if definition is None:
            return None
        found = definition.levels.get(level)
        if found is None:
            return None
        if level in instance.start_overrides:
            found = LevelDef(**{**found.__dict__, "start": instance.start_overrides[level]})
        return found

    def levels_for(self, num_id: Optional[str], style_numbering=None) -> List[LevelDef]:
        definition = self.resolve_abstract(num_id, style_numbering)
        if definition is None:
            return []
        return [definition.levels[key] for key in sorted(definition.levels)]

    def is_bullet(self, num_id: Optional[str], level: int, style_numbering=None) -> bool:
        found = self.level(num_id, level, style_numbering)
        if found is None:
            return True
        return found.number_format.lower() == "bullet"
