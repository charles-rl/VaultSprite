"""M9 data types — the pose/animation and behavior-pool model.

Pure Python, no Qt (see ``docs/09_mascot_engine/README.md``). These mirror the
Shimeji-ee ``Mascot`` live state, the ``Animation``/``Pose`` pair and the
behavior-pool ``list`` types from ``DalekCraft2/Shimeji-Desktop``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .mascot_environment import Vec2


@dataclass
class Pose:
    image: str                       # e.g. "shime1.png" (relative to the mascot img dir)
    anchor: Vec2                     # image-space feet point ("ImageAnchor")
    velocity: Vec2                   # world px per tick; vx mirrored by look direction
    duration: int                    # ticks


class AnimList:                      # one <Animation> block (a looping pose sequence)
    __slots__ = ("poses", "condition_js", "total_duration")

    def __init__(self, poses: list[Pose], condition_js: str = "true"):
        self.poses = poses
        self.condition_js = condition_js
        self.total_duration = max(1, sum(p.duration for p in poses))

    def get_pose(self, t: int) -> Pose:          # loops; C++ `time %= duration`
        if not self.poses:
            raise RuntimeError("animation without poses")
        t %= self.total_duration
        for pose in self.poses:
            t -= pose.duration
            if t < 0:
                return pose
        return self.poses[-1]

    @property
    def duration(self) -> int:
        return self.total_duration


@dataclass
class MascotState:                   # per-mascot live state (subset of Shimeji-ee `Mascot`)
    anchor: Vec2 = field(default_factory=Vec2)
    looking_right: bool = True
    dragging: bool = False
    time: int = 0                    # tick counter, incremented in pre_tick
    active_frame: Optional[Pose] = None
    behavior_name: str = ""
    queued_behavior: str = ""
    was_on_ie: bool = False
    dead: bool = False
    foot_x: Optional[float] = None    # pendulum oscillator (C++ Dragged) — None ⇒ anchor.x
    foot_dx: float = 0.0


class _BehaviorList:                 # children + condition groups (mirrors C++ `list`)
    __slots__ = ("children", "sublists")

    def __init__(self):
        self.children: list[BehaviorNode] = []
        self.sublists: list[tuple[str, BehaviorNode]] = []   # (cond_js, sublist)


@dataclass
class BehaviorNode:                  # one candidate behavior in a pool (inline <Action> or ref)
    name: str                        # its own name (for <NextBehavior> refs & logging)
    action: Any                      # an Action instance (parsed once, re-init per use)
    frequency: int = 100
    hidden: bool = False


@dataclass
class _PoolEntry:                    # a flattened, condition-checked candidate
    behavior_node: BehaviorNode
    add_next: bool                   # "Add" attr of the <Behavior> element that owns it
    next_js_children: list[tuple[str, "_PoolSource"]]  # (cond_js or "", source) next pool
    next_is_full_replace_on_pick: bool = False


# -- behavior pool source types ---------------------------------------------------------
class _PoolSource:
    name = ""
    cond_js = ""

    @property
    def frequency(self):  # resolved by the core via defs; kept for clarity
        raise NotImplementedError


class _NamedSource(_PoolSource):     # a <Behavior> entry (or BehaviorReference) in a pool
    __slots__ = ("name", "cond_js", "freq")

    def __init__(self, name: str, cond_js: str = "", freq: int = 0):
        self.name, self.cond_js, self.freq = name, cond_js, freq   # freq>0 overrides the def's own


class _GroupSources(_PoolSource):    # a <Condition> group containing behaviors
    __slots__ = ("cond_js", "sub")

    def __init__(self, cond_js: str, sub: list[_PoolSource]):
        self.cond_js, self.sub = cond_js, sub


class _FlatSources(list):            # plain flat list of sources (top-level BehaviorList / next pools)
    pass


def _PoolSourcesOf(defn: "BehaviorDef", initial=None, add_next=False) -> "_FlatSources":
    """The pool that follows picking `defn`: its <NextBehavior> refs; Add=true also keeps the initial list."""
    srcs = [_NamedSource(n.name, n.cond_js) for n in defn.next_children] \
        if isinstance(defn.next_children, list) and defn.next_children and \
           isinstance(defn.next_children[0], _PoolSource) else []
    # (next_children parsed as named sources already; nothing extra to build here)
    out = _FlatSources()
    if add_next and initial is not None:
        out.extend(initial)                 # keep initial pool (conditions re-evaluated at pick time)
    out.extend(srcs)
    return out


class BehaviorDef:
    __slots__ = ("node", "next_children", "add_next")

    def __init__(self, node: BehaviorNode, next_children: list, add_next: bool):
        self.node = node
        self.next_children = next_children     # list[_NamedSource] (with cond_js)
        self.add_next = add_next
