"""Tiny namespace-agnostic XML helpers for Shimeji pack files.

Shimeji ``actions.xml`` / ``behaviors.xml`` use the group-finity namespace, so every
element/attribute lookup strips any ``{...}`` namespace prefix. Pure stdlib — no Qt,
no deps (a leaf of the M9 split, see ``docs/09_mascot_engine/README.md`` §5).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Union


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def ns_el(el_tag: str) -> str:
    return el_tag.rsplit("}", 1)[0] + "}" if "}" in el_tag else ""


def _attr_of(el: ET.Element, name: str, default=None):
    for k, v in el.attrib.items():
        if local_name(k) == name:
            return v
    return default


def _attrs(el: ET.Element) -> list[tuple[str, str]]:
    return [(local_name(k), v) for k, v in el.attrib.items()]


def _iter_elements(doc: ET.ElementTree):
    root = doc.getroot()
    yield from [root] + list(root.iter())


def _load(path: Union[str, Path]) -> ET.ElementTree:
    return ET.parse(str(path))
