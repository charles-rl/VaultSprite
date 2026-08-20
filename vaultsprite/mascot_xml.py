"""Tiny namespace- and locale-agnostic XML helpers for Shimeji pack files.

Shimeji ``actions.xml`` / ``behaviors.xml`` use the group-finity namespace, so every
element/attribute lookup strips any ``{...}`` namespace prefix. Community packs also ship in
localized form (e.g. Japanese: ``<動作 名前=… 種類="移動">``); element and attribute names are
normalized to their English canonicals through :data:`_TAG_ALIAS` / :data:`_ATTR_ALIAS`, and the
small closed set of enum *values* (action types, border kinds) through :data:`_VALUE_ALIASES`.
Pure stdlib — no Qt, no deps (a leaf of the M9 split, see ``docs/09_mascot_engine/README.md`` §5).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Union


# JP pack element tags -> English canonicals (Shimeji-ee ja-JP localization)
_TAG_ALIAS: dict[str, str] = {
    "マスコット": "Mascot",
    "動作リスト": "ActionList",
    "動作参照": "ActionReference",
    "動作": "Action",
    "アニメーション": "Animation",
    "ポーズ": "Pose",
    "行動リスト": "BehaviorList",
    "行動参照": "BehaviorReference",
    "次の行動リスト": "NextBehavior",
    "条件": "Condition",
    "行動": "Behavior",
}

# JP pack attribute keys -> English canonicals
_ATTR_ALIAS: dict[str, str] = {
    "名前": "Name",
    "種類": "Type",
    "クラス": "Class",
    "枠": "BorderType",
    "画像": "Image",
    "基準座標": "ImageAnchor",
    "移動速度": "Velocity",
    "長さ": "Duration",
    "条件": "Condition",
    "IEの端X": "IeOffsetX",
    "IEの端Y": "IeOffsetY",
    "初速X": "InitialVX",
    "初速Y": "InitialVY",
    "重力": "Gravity",
    "速度": "VelocityParam",
    "空気抵抗X": "RegistanceX",      # canonical (misspelled) name used by the English packs
    "空気抵抗Y": "RegistanceY",
    "繰り返し": "Loop",
    "目的地X": "TargetX",
    "右向き": "LookRight",
    "目的地Y": "TargetY",
    "ずれ": "Gap",
    "生まれる場所X": "BornX",
    "生まれる場所Y": "BornY",
    "生まれた時の行動": "BornBehavior",
    "頻度": "Frequency",
    "追加": "Add",
}

# enum values localized in JP packs, per canonical attribute key
_VALUE_ALIASES: dict[str, dict[str, str]] = {
    "Type": {
        "組み込み": "Embedded",
        "静止": "Stay",
        "移動": "Move",
        "固定": "Animate",
        "複合": "Sequence",
        "選択": "Select",
    },
    "BorderType": {
        "地面": "Floor",
        "壁": "Wall",
        "天井": "Ceiling",
    },
}


def local_name(tag: str) -> str:
    base = tag.rsplit("}", 1)[-1] if "}" in tag else tag
    return _TAG_ALIAS.get(base, base)


def ns_el(el_tag: str) -> str:
    return el_tag.rsplit("}")[0] + "}" if "}" in el_tag else ""


def _attr_key(attr: str) -> str:
    base = attr.rsplit("}", 1)[-1] if "}" in attr else attr
    return _ATTR_ALIAS.get(base, base)


def _attr_of(el: ET.Element, name: str, default=None):
    values = _VALUE_ALIASES.get(name)
    for k, v in el.attrib.items():
        if _attr_key(k) == name:
            return (values or {}).get(v, v)
    return default


def _attrs(el: ET.Element) -> list[tuple[str, str]]:
    out = []
    for k, v in el.attrib.items():
        ln = _attr_key(k)
        values = _VALUE_ALIASES.get(ln)
        out.append((ln, (values or {}).get(v, v)))
    return out


def _iter_elements(doc: ET.ElementTree):
    root = doc.getroot()
    yield from [root] + list(root.iter())


def _load(path: Union[str, Path]) -> ET.ElementTree:
    return ET.parse(str(path))
