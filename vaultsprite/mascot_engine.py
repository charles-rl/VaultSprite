"""Mascot behavior engine (Module 9) — plays standard Shimeji packs natively in Python.

Pure-Python port of the Shimeji-ee runtime (pattern source: ``DalekCraft2/Shimeji-Desktop``,
see ``docs/09_mascot_engine/README.md``): parses ``actions.xml`` + ``behaviors.xml``, runs the
weighted behavior roulette with per-tick pose animation over raw PNG frames, and evaluates
`${...}`(once)/`#{...}`(per-tick) conditions through the safe evaluator in
:mod:`vaultsprite.mascot_environment`. No Qt import — unit-tested by calling ``core.tick()``
directly (the same style as terrain/stat tests).

Ownership model (documented for AGENTS.md): external forces originate from App and are forced
onto behaviors — drag start → ``Dragged``, flick/release → ``Thrown``, stretch nudge/vision
reply/talking → our extra behaviors. The engine's ambient roulette decides idle-time behavior
on its own tick (like TerrainPhysics owns its ticks); every behavior transition is reported to
the caller so App can log to the Vault, play sounds, etc.

Fidelity notes / deliberate divergences:
- ``BORDER_TOL == 1.0 px`` and Fall integration constants are copied from Shimeji-ee defaults.
- The manager's **4-level recovery ladder** (normal → ForceFall → detach-from-borders →
  reset-position) is ported verbatim in spirit — a malformed action can never wedge the pet.
- Interpolation subticks are fixed at 1 for now (motion runs on the config tick clock); the
  API keeps the ``subtick`` seam so they can be added later without touching action code.
- Solo-pet by design: Breed/SelfDestruct/Scan/Broadcast-family types parse but act as no-op
  advances; frames 37–46 stay on disk for future use (user decision 2026-08-17).
"""
from __future__ import annotations

import logging
import random
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union

from .mascot_environment import (BORDER_TOL, DArea, DVec2, JSMascot, MascotEnvironment,
                                 Vec2, ExpressionCompiler, parse_error)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- data types --
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
        self.children: list["Behavior"] = []
        self.sublists: list[tuple[str, "BehaviorNode"]] = []   # (cond_js, sublist)


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


# --------------------------------------------------------------------------- variables --
class ActionVars:
    """Attribute store for one action run.

    ``${...}`` → evaluated **once** at init (value cached); `#{...}` → re-evaluated every
    access; plain literals parsed to number/bool where possible."""

    def __init__(self, attrs: dict[str, str], view_factory: Callable[[], JSMascot], rng):
        self._view = view_factory
        self._rng = rng
        self._static: dict[str, Any] = {}
        self._dynamic: dict[str, ExpressionCompiler] = {}
        for key, raw in attrs.items():
            v = (raw or "").strip()
            if v.startswith("${") and v.endswith("}"):
                comp = ExpressionCompiler(v[2:-1])     # raises parse_error → caught by parser
                self._static[key] = _DynOnce(comp, self._view, rng)
            elif v.startswith("#{") and v.endswith("}"):
                self._dynamic[key] = ExpressionCompiler(v[2:-1])
            else:
                self._static[key] = _coerce_literal(v)

    def has(self, key: str) -> bool:
        return key in self._static or key in self._dynamic

    def get_num(self, key: str, fallback: float = 0.0) -> float:
        v = self.resolve(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        try:
            s = str(v).strip()
            if s == "":
                return fallback
            return float(s)
        except ValueError:
            return fallback

    def get_bool(self, key: str, fallback: bool = True) -> bool:
        v = self.resolve(key)
        if v is _UNDEFINED:
            return fallback
        if isinstance(v, bool):
            return v
        try:
            s = str(v).strip().lower()
            if s in ("", "false", "0"):
                return False
            if s in ("true", "1"):
                return True
            float(s)      # a plain number → JS-truthy (nonzero)
            return v not in (None, 0, 0.0)
        except ValueError:
            return fallback

    def get_str(self, key: str, fallback: str = "") -> str:
        v = self.resolve(key)
        return fallback if v is _UNDEFINED else str(v)

    def resolve(self, key: str):
        if key in self._dynamic:
            try:
                val = self._dynamic[key].eval_value(self._view(), self._rng)
            except Exception:
                return _UNDEFINED
            return val
        v = self._static.get(key, _UNDEFINED)
        return v.value() if isinstance(v, _DynOnce) else v


class _UNDEFINED_TYPE2:
    def __bool__(self): return False

    def __repr__(self): return "undefined"


_UNDEFINED = _UNDEFINED_TYPE2()


class _DynOnce:                      # `${...}` placeholder resolved at first access / init
    def __init__(self, comp: ExpressionCompiler, view_factory, rng):
        self.comp, self._vf, self.rng = comp, view_factory, rng
        self._value: Any = None
        self._done = False

    def value(self) -> Any:
        if not self._done:
            try:
                self._value = self.comp.eval_value(self._vf(), self.rng)
            except Exception as exc:               # noqa: BLE001 - malformed expr → undefined
                logger.warning("action expression failed once-at-init: %s", exc)
                self._value = _UNDEFINED
            self._done = True
        return self._value


def _coerce_literal(v: str) -> Any:
    if v == "true":
        return True
    if v == "false":
        return False
    try:
        if "." in v:
            return float(v)
        return int(v)
    except ValueError:
        return v


# --------------------------------------------------------------------------- actions ----
class ActionCtx:                     # mirrors Shimeji-ee `Mascot` tick (script + attr overlay)
    def __init__(self, core: "MascotCore", extra_attr: Optional[dict[str, str]] = None):
        self.core = core
        self.extra_attr = dict(extra_attr or {})


class Action:                        # base runner (C++ `action::base`)
    #: sub-action list for sequence/select; override in subclasses with children
    children: list["Action"]

    def __init__(self, attrs: dict[str, str], core: "MascotCore"):
        self.init_attrs = dict(attrs)
        self.core = core
        self.active = False
        self.start_time = 0

    # -- lifecycle -----------------------------------------------------------
    def init(self, ctx: ActionCtx):
        if self.active:
            raise RuntimeError("init() called twice")
        st = self.core.state
        self.active = True
        self.real_start = st.time
        merged = dict(self.init_attrs)
        for k, v in (ctx.extra_attr or {}).items():   # reference overlays win (C++ overlay)
            merged[k] = v
        try:
            self.vars = ActionVars(merged, lambda: core_view(self.core), self.core.rng)
        except parse_error as exc:
            self.active = False
            raise MalformedAction(self.name(), f"bad expression: {exc}") from exc

    def finalize(self):
        if not self.active:
            return
        st = self.core.state
        if st.queued_behavior and _UNDEFINED is None:   # keep queued behavior across actions
            pass
        self.active = False

    @property
    def real_elapsed(self) -> int:
        return self.core.state.time - getattr(self, "real_start", 0)

    def name(self) -> str:
        return self.init_attrs.get("Name") or self.__class__.__name__.lower()

    # -- per tick --------------------------------------------------------------
    def condition_ok(self) -> bool:
        """`#{...}` Condition attr re-evaluated every tick (C++ `vars.tick()`)."""
        if not self.vars.has("Condition"):
            return True
        try:
            v = self.vars.resolve("Condition")
        except Exception:
            return False
        return bool(v)

    def duration_ok(self) -> bool:
        """Action ends after its Duration attr (ticks). Missing → runs until animation logic says stop."""
        if not self.vars.has("Duration"):
            return True
        return self.real_elapsed < int(self.vars.get_num("Duration", 10**9))

    def tick_ok(self) -> bool:      # shared gate before subclass motion; False ⇒ advance
        st = self.core.state
        if st.queued_behavior:
            return False                            # pre-empt immediately (C++ behavior queue)
        if not self.condition_ok():
            return False
        if not self.duration_ok():
            return False
        return True

    def subtick(self, idx: int = 0) -> bool:
        """Return False when this action has finished (sequence advances / manager repicks)."""
        if idx != 0:      # only tick on the real tick; subticks are a future seam
            return self.active
        if not self.tick_ok():
            return False
        return self.step()

    def step(self) -> bool:
        """Subclass motion for one tick. Default (animation actions): advance the pose."""
        raise NotImplementedError

    # -- helpers -----------------------------------------------------------------
    @property
    def st(self) -> MascotState:
        return self.core.state

    @property
    def env(self) -> MascotEnvironment:
        return self.core.env


class MalformedAction(RuntimeError):
    """An action/behavior failed to init (bad XML/expression). The manager's recovery
    ladder then falls back — a single bad behavior can never kill the pet."""

    def __init__(self, name: str, why: str):
        super().__init__(f"action {name!r}: {why}")
        self.name = name


class AnimationAction(Action):      # Stay / Move-ish poses with <Animation> blocks + BorderType
    def __init__(self, attrs, core, anim_lists: list[AnimList]):
        super().__init__(attrs, core)
        self.anims = anim_list if (anim_list := anim_lists) else []

    # pose selection: first AnimList whose Condition holds (C++ `get_animation`).
    # A literal "true"/"" branch matches unconditionally; any real `#{...}`/`${...}`
    # expression is evaluated per tick. (The previous `if not is_true_js(cond): continue`
    # was inverted — it skipped every conditional branch, so Dragged/Pinched's lean poses,
    # SitAndLookAtMouse, ClimbWall direction, etc. never animated differently.)
    def _current_anim(self) -> Optional[AnimList]:
        for anim in self.anims:
            cond = (anim.condition_js or "true").strip()
            if is_true_js(cond):
                return anim                      # unconditional branch always matches
            try:
                v = ExpressionCompiler(strip_js_expr(cond)).eval_value(
                    core_view(self.core), self.core.rng)
            except Exception:
                v = False
            if bool(v):
                return anim
        return None

    def _border_type_ok(self) -> tuple[bool, Optional[str]]:
        """Check BorderType Floor/Wall/Ceiling; returns (still_on_border, queued_behavior)."""
        st, env = self.st, self.env
        bt = str(self.vars.get_str("BorderType", "") or "").lower() if self.vars else ""
        a = st.anchor
        if bt == "floor":
            on = env.floor.is_on(a) or env.active_ie.top_border().is_on(a)
        elif bt in ("wall",):
            look_right = (env.work_area.right_border().is_on(a) or env.active_ie.left_border().is_on(a))
            on = (look_right or env.work_area.left_border().is_on(a)
                  or env.active_ie.right_border().is_on(a))
            if on:
                st.looking_right = bool(look_right)
        elif bt == "ceiling":
            on = env.work_area.top_border().is_on(a) or env.active_ie.bottom_border().is_on(a)
        else:
            on = True
        if not on:
            # slipped off its surface; fall unless we're touching any surface still (C++ queues Fall)
            queued = "Fall" if not (env.work_area.is_on(a) or env.active_ie.is_on(a)) else None
            return False, queued
        return True, None

    def _dragging_ok(self) -> bool:
        """If the user pressed us mid-action → hand control to the Dragged behavior."""
        st = self.st
        if st.dragging and self.vars is not None and self.vars.get_bool("Draggable", True):
            st.queued_behavior = "Dragged"      # C++ handle_dragging()
            return False
        return True

    def step(self) -> bool:
        if not self.tick_ok():
            return False
        ok, queued = self._border_type_ok()
        if not ok:
            if queued:
                self.st.queued_behavior = queued
            return False
        if not self._dragging_ok():
            return False
        anim = self._current_anim()
        if anim is None or not anim.poses:
            logger.debug("no matching animation branch for %s", self.name())
            return False                          # advance (sequence end / next behavior)
        pose = anim.get_pose(self.st.time - getattr(self, "_anim_t0", 0)) \
            if hasattr(self, "_anim_t0") else anim.poses[0]
        if not hasattr(self, "_anim_t0"):
            self._anim_t0 = self.st.time
        v = pose.velocity
        st, lr = self.st, self.st.looking_right
        st.anchor.x += (-1 if lr else 1) * v.x    # dx() mirror: left-facing flips horizontal
        st.anchor.y += v.y
        self.core.set_active_frame(pose)
        return True


class StayAction(AnimationAction):   # loops its poses until external end
    pass


class AnimateAction(AnimationAction):
    """C++ ``Animate``: play the effective animation ONCE, then end.

    The reference ``Animate.hasNext()`` is ``getTime() < getAnimation().getDuration()``,
    so an ``Animate`` action finishes after a single cycle. Our generic ``AnimationAction``
    looped forever instead — that's the reported "spams the shime18 water-bucket landing
    animation and is stuck in an infinite loop": ``Bouncing`` is ``Type="Animate"`` and,
    with no ``Duration``, never advanced to ``Stand``."""

    def step(self) -> bool:
        if not self.tick_ok():
            return False
        ok, queued = self._border_type_ok()
        if not ok:
            if queued:
                self.st.queued_behavior = queued
            return False
        if not self._dragging_ok():
            return False
        anim = self._current_anim()
        if anim is None or not anim.poses:
            return False
        if self.real_elapsed >= anim.total_duration:
            return False                      # one cycle done → advance to the next action
        self.core.set_active_frame(anim.get_pose(self.real_elapsed))
        return True


class InPlaceAction(AnimationAction):
    """Plays an animation loop WITHOUT moving the anchor.

    Used by the app-level hide/show walk: App owns the window position (stepping it
    toward the screen edge) while this action keeps the walk frames animating in
    place, so the pet visibly walks off-screen instead of sliding frozen."""

    def step(self) -> bool:
        if not self.tick_ok():
            return False
        anim = self._current_anim()
        if anim is None or not anim.poses:
            return False
        pose = anim.get_pose(self.st.time - getattr(self, "_anim_t0", 0)) \
            if hasattr(self, "_anim_t0") else anim.poses[0]
        if not hasattr(self, "_anim_t0"):
            self._anim_t0 = self.st.time
        self.core.set_active_frame(pose)
        return True


class MoveAction(AnimationAction):   # Stay + TargetX/TargetY crossing (C++ `move::tick`)
    def step(self) -> bool:
        if not self.tick_ok():
            return False
        ok, queued = self._border_type_ok()
        if not ok:
            if queued:
                self.st.queued_behavior = queued
            return False
        if not self._dragging_ok():
            return False
        # look direction from target (C++ move::tick)
        anim = self._current_anim()
        if anim is None or not anim.poses:
            return False
        pose = anim.get_pose(self.st.time - self._anim_t0) if hasattr(self, "_anim_t0") else None
        if pose is None:
            self._anim_t0 = self.st.time
            pose = anim.poses[0]
        v = pose.velocity
        st = self.st
        start = Vec2(st.anchor.x, st.anchor.y)
        if self.vars.has("TargetX"):
            tx = float(self.vars.get_num("TargetX", st.anchor.x))
            if v.x > 0:
                st.looking_right = tx < st.anchor.x
            elif v.x < 0:
                st.looking_right = tx > st.anchor.x
        elif self.vars.has("TargetY"):
            ty = float(self.vars.get_num("TargetY", st.anchor.y))
            vertical_dir = 1 if ty > st.anchor.y else -1
        else:
            return False                       # no target → end (C++ warns + finishes)
        st.anchor.x += (-1 if st.looking_right else 1) * v.x
        dyv = v.y
        if self.vars.has("TargetY"):
            dyv = abs(dyv) if vertical_dir == 1 else -abs(dyv)
        st.anchor.y += dyv
        self.core.set_active_frame(pose)

        def crossed(axis_start: float, axis_now: float, target: float) -> bool:
            return (axis_start >= target and axis_now <= target) or \
                   (axis_start <= target and axis_now >= target)

        if self.vars.has("TargetX"):
            tx = float(self.vars.get_num("TargetX", st.anchor.x))
            if crossed(start.x, st.anchor.x, tx):
                st.anchor.x = tx
                return False
        elif self.vars.has("TargetY"):
            ty = float(self.vars.get_num("TargetY", st.anchor.y))
            if crossed(start.y, st.anchor.y, ty):
                st.anchor.y = ty
                return False
        # animation loop wrap for velocity continuity (C++ uses pose cycle; we do the same)
        if self.real_elapsed - getattr(self, "_move_t0_e", 0) >= anim.duration * 8:   # safety cap
            pass
        return True


class FallAction(AnimationAction):   # C++ `fall.cc` per-tick integration + IE stick + clamps
    def init(self, ctx: ActionCtx):
        super().init(ctx)
        self.velocity = Vec2(float(self.vars.get_num("InitialVX", 0.0)),
                             float(self.vars.get_num("InitialVY", 0.0)))

    def step(self) -> bool:
        st, env = self.st, self.env
        on_land = (env.floor.is_on(st.anchor) or env.ceiling.is_on(st.anchor)
                   or env.work_area.is_on(st.anchor))
        if self.real_elapsed > 0:      # C++: don't consider IE on the first tick
            on_land = on_land or env.active_ie.is_on(st.anchor)
        if on_land:
            return False

        if self.velocity.x != 0:
            st.looking_right = self.velocity.x > 0

        res_x = float(self.vars.get_num("RegistanceX", 0.05))
        res_y = float(self.vars.get_num("RegistanceY", 0.1))
        gravity = float(self.vars.get_num("Gravity", 2.0))
        self.velocity.x -= (self.velocity.x * res_x)
        self.velocity.y += (gravity - self.velocity.y * res_y)

        before = Vec2(st.anchor.x, st.anchor.y)
        st.anchor.x += self.velocity.x
        st.anchor.y += self.velocity.y

        near_floor = abs(st.anchor.y - env.floor.y) < BORDER_TOL
        if st.anchor.x > env.work_area.right:
            st.anchor.x = env.work_area.right
            if near_floor:
                st.anchor.y = env.floor.y - 1.1
        elif st.anchor.x < env.work_area.left:
            st.anchor.x = env.work_area.left
            if near_floor:
                st.anchor.y = env.floor.y - 1.1
        if st.anchor.y < env.ceiling.y:
            st.anchor.y = env.ceiling.y
        elif st.anchor.y > env.floor.y:
            st.anchor.y = env.floor.y

        # IE_STICK (C++ macro): if this step crossed a tracked-window border from outside to
        # inside (staying within the window's range on the other axis), snap the anchor onto
        # that border — how falling "lands" on window tops/sides/bottom.
        ie = env.active_ie
        if ie.visible:
            y_in_range = ie.top - BORDER_TOL <= st.anchor.y <= ie.bottom + BORDER_TOL
            x_in_range = ie.left - BORDER_TOL <= st.anchor.x <= ie.right + BORDER_TOL
            if before.x < ie.left and st.anchor.x >= ie.left and y_in_range:
                st.anchor.x = ie.left                       # crossed the window's left wall
            elif before.x > ie.right and st.anchor.x <= ie.right and y_in_range:
                st.anchor.x = ie.right                      # ...right wall
            elif before.y < ie.top and st.anchor.y >= ie.top and x_in_range:
                st.anchor.y = ie.top                        # ...top edge (window-top landing)
            elif before.y > ie.bottom and st.anchor.y <= ie.bottom and x_in_range:
                st.anchor.y = ie.bottom                     # ...bottom edge

        # Wall grip (C++ Fall.hasNext(): the fall ENDS when the mascot reaches a wall,
        # leaving it ON the wall so the Fall's GrabWall branch — and the ambient wall
        # pool — runs. This is how a sideways throw "grabs" a wall/ceiling instead of
        # sliding down to the floor.) The work-area clamp above already pins the anchor to
        # a side wall, so the 1px border check never skips even at high velocity.
        a = st.anchor
        if (env.work_area.left_border().is_on(a) or env.work_area.right_border().is_on(a)
                or (ie.visible and (ie.left_border().is_on(a) or ie.right_border().is_on(a)))):
            return False

        anim = self._current_anim()
        if anim is not None:
            pose = anim.get_pose(self.st.time - getattr(self, "_anim_t0", 0)) \
                if hasattr(self, "_anim_t0") else anim.poses[0]
            if not hasattr(self, "_anim_t0"):
                self._anim_t0 = self.st.time
            self.core.set_active_frame(pose)
        return True


class JumpAction(AnimationAction):   # C++ `jump.cc` — aim toward TargetX/TargetY with an arc
    def step(self) -> bool:
        if not self.tick_ok():
            return False
        st = self.st
        tx = float(self.vars.get_num("TargetX", st.anchor.x))
        ty = float(self.vars.get_num("TargetY", st.anchor.y))
        st.looking_right = st.anchor.x < tx
        dxv, dyv = tx - st.anchor.x, ty - st.anchor.y - abs(tx - st.anchor.x)
        speed = float(self.vars.get_num("VelocityParam", 20.0))
        dist = (dxv * dxv + dyv * dyv) ** 0.5
        if dist > 1e-6:
            st.anchor.x += speed * dxv / dist
            st.anchor.y += speed * dyv / dist
        anim = self._current_anim()
        if anim is not None and anim.poses:
            self.core.set_active_frame(anim.get_pose(self.st.time - getattr(self, "_anim_t0", 0))
                                       if hasattr(self, "_anim_t0") else anim.poses[0])
            if not hasattr(self, "_anim_t0"):
                self._anim_t0 = st.time
        if dist <= speed:
            st.anchor.x, st.anchor.y = tx, ty
            return False
        return True


class InstantAction(Action):         # Offset / Look — one effect, then advance (C++ `instant`)
    def step(self) -> bool:
        if not self.tick_ok():
            return False
        st = self.st
        kind = getattr(self, "kind", "")
        if kind == "offset":
            dxv = float(self.vars.get_num("X", 0.0))
            dyv = float(self.vars.get_num("Y", 0.0))
            st.anchor.x += dxv
            st.anchor.y += dyv
        elif kind == "look":
            lr = self.vars.get_bool("LookRight", not st.looking_right)
            st.looking_right = bool(lr)
        return False                 # always finishes after one tick


class SequenceAction(Action):        # runs child actions in order (Loop attr optional)
    def __init__(self, attrs, core, children: list[Action]):
        super().__init__(attrs, core)
        self.children = children or []
        self._idx = -1

    def init(self, ctx: ActionCtx):
        # Sequence/Select force Loop=false for Select via overlay (C++ select::init)
        super().init(ctx)
        self._idx = -1
        self.next_child()

    def finalize(self):
        for c in self.children:
            if c.active:
                c.finalize()
        super().finalize()

    def next_child(self):
        if 0 <= self._idx < len(self.children) and self.children[self._idx].active:
            self.children[self._idx].finalize()
        self._idx += 1
        if self._idx >= len(self.children):
            loop = bool(self.vars.get_bool("Loop", False))
            if not loop:
                return
            self._idx = 0
        if self._idx < len(self.children):
            child = self.children[self._idx]
            try:
                child.init(ActionCtx(self.core, None))
            except MalformedAction as exc:   # bad child → skip it (stay alive)
                logger.warning("skipping malformed action %s", exc)
                self.next_child()

    def step(self) -> bool:
        if not self.tick_ok():
            return False
        attempts = 0
        max_attempts = len(self.children) + 1
        while True:
            child = self._child()
            if child is None:
                # sequence ended; try next (handles a run of instant actions)
                self.next_child()
                child = self._child()
                if child is None:
                    return False
                attempts += 1
                continue
            ok = child.subtick(0)
            if self.st.queued_behavior or attempts >= max_attempts:
                break
            if ok:
                return True           # still running this tick
            self.next_child()         # child finished → advance
            attempts += 1
        return not (self._child() is None and not self.st.queued_behavior)

    def _child(self):
        if 0 <= self._idx < len(self.children):
            c = self.children[self._idx]
            if c.active:
                return c
            # finalize stale child then re-init (sequence that ended mid-run)
            c.finalize()
            try:
                c.init(ActionCtx(self.core, None))
                return c if c.active else None
            except MalformedAction as exc:
                logger.warning("child action failed to restart: %s", exc)
        return None


class SelectAction(SequenceAction):
    """First child whose Condition holds wins; re-evaluated periodically while running; ends
    when the selected branch finishes (C++ `select` — no re-picking after completion)."""

    _recheck_ticks = 20          # ~0.8 s at mascot.tick_ms 40

    def __init__(self, attrs, core, children):
        super().__init__(attrs, core, children)
        self._cond_vars: list[Optional[ActionVars]] = []

    def _cond(self, i: int) -> Optional[ActionVars]:
        while len(self._cond_vars) <= i:
            self._cond_vars.append(None)
        if self._cond_vars[i] is None:
            self._cond_vars[i] = ActionVars(
                self.children[i].init_attrs, lambda: core_view(self.core), self.core.rng)
        return self._cond_vars[i]

    def _matches(self, i: int) -> bool:
        try:
            av = self._cond(i)
            if not av.has("Condition"):
                return True
            return bool(av.resolve("Condition"))
        except Exception:
            return False

    def _select(self) -> int:
        for i in range(len(self.children)):
            if self._matches(i):
                return i
        return -1

    def _start(self, i: int) -> bool:
        if 0 <= i < len(self.children):
            try:
                self.children[i].init(ActionCtx(self.core, None))
            except MalformedAction as exc:
                logger.warning("skipping malformed action %s", exc)
                return False
        return True

    def next_child(self):
        if 0 <= self._idx < len(self.children) and self.children[self._idx].active:
            self.children[self._idx].finalize()
        self._idx = self._select()
        self._start(self._idx)

    def step(self) -> bool:
        if not self.tick_ok():
            return False
        if self.real_elapsed > 0 and self.real_elapsed % self._recheck_ticks == 0:
            chosen = self._select()
            if chosen != self._idx:
                if 0 <= self._idx < len(self.children) and self.children[self._idx].active:
                    self.children[self._idx].finalize()
                self._idx = chosen
                self._start(self._idx)
        if not (0 <= self._idx < len(self.children)):
            return True                    # no branch matches yet → keep waiting
        child = self.children[self._idx]
        if not child.active:
            return False                   # selected branch finished → Select is done
        ok = child.subtick(0)
        if self.st.queued_behavior:
            return False                   # pre-empted by an external force
        if not ok:
            # The selected branch finished — end the Select (C++ ComplexAction.hasNext()
            # is false once the current child's hasNext() is false). Do NOT re-init it:
            # re-initing the finished branch here is what kept the pet looping
            # Bouncing/Stand/Fall forever instead of returning to ambient behavior.
            return False
        return True


class ReferenceAction(Action):       # <ActionReference Name=... TargetX=.../> indirection
    def __init__(self, attrs, core):
        super().__init__(attrs, core)
        self.target: Optional[Action] = None

    def link(self, target: Action):
        if self.target is not None and self.target is not target:
            raise MalformedAction(self.name(), "reference already linked")
        self.target = target

    def init(self, ctx: ActionCtx):
        super().init(ctx)
        if self.target is None:
            raise MalformedAction(self.name(), f"unlinked reference (action {self.init_attrs.get('Name')!r} missing)")
        # The reference's OWN attributes are the overlay for the target (C++ ReferenceAction
        # passes its vars to the target's init). Before, we forwarded `ctx.extra_attr` — which
        # is None for a top-level sequence child — so every reference overlay (InitialVX/VY,
        # TargetX/Y, Duration, Gap, LookRight, ...) was silently dropped. That made Thrown's
        # InitialVX=${cursor.dx} launch with zero horizontal velocity and broke target-based
        # Walk/Move/Jump references.
        try:
            self.target.init(ActionCtx(self.core, self.init_attrs))
        except MalformedAction:
            raise

    def finalize(self):
        if self.target is not None:
            try:
                self.target.finalize()
            except Exception:      # noqa: BLE001 - finalize must never block a transition
                pass
        super().finalize()

    def step(self) -> bool:
        return self.target.subtick(0) if self.target else False


# --------------------------------------------------------------------------- helpers ----
def core_view(core: "MascotCore") -> JSMascot:
    return core.js_view


_STRIP_EXPR = "${"


def is_true_js(s: str) -> bool:
    s = (s or "").strip().lower()
    return s in ("true", "")


def strip_js_expr(js: str) -> str:
    js = (js or "").strip()
    for pre, post in (("${", "}"), ("#{", "}")):
        if js.startswith(pre) and js.endswith(post):
            return js[len(pre):-len(post)]
    return js


# --------------------------------------------------------------------------- the core ----
class MascotCore:
    """Pure-Python behavior engine. Call :meth:`tick` each animation tick; feed it a fresh
    environment snapshot beforehand (screen geometry + tracked window). Everything is in
    logical screen px, anchor = image anchor point's world position (C++ convention)."""

    #: action type names we refuse to run solo (breeding family) — no-op advances
    UNIMPLEMENTED_TYPES = {"Breed", "BreedJump", "BreedMove", "ScanMove", "ScanInteract",
                           "ScanJump", "Interact", "SelfDestruct", "Transform", "Mute"}

    def __init__(self, env: MascotEnvironment, rng: Optional[random.Random] = None,
                 excluded_behaviors: Optional[list[str]] = None):
        self.env = env
        self.rng = rng or random.Random()
        self.state = MascotState()
        self.js_view = JSMascot(lambda: self.state, lambda: self.env)
        self.excluded = {str(n) for n in (excluded_behaviors or [])}

        self.actions: dict[str, Action] = {}          # named actions from actions.xml
        self._refs: list[ReferenceAction] = []        # inline <ActionReference>s, linked post-parse
        self.behavior_defs: dict[str, BehaviorDef] = {}   # behaviors + their next-pool sources
        self.initial_pool: "_PoolSource" = _FlatSources([])
        self._active_behavior_node: Optional[BehaviorNode] = None
        self.active_action: Optional[Action] = None
        self._init_count = 0                          # C++ reached_init_limit guard

    # -- parsing ---------------------------------------------------------------
    def parse(self, actions_xml_path: Union[str, Path], behaviors_xml_path: Union[str, Path]):
        """Parse the mascot's two XML files (community-pack compatible)."""
        actions_doc = _load(actions_xml_path)
        self._parse_actions(actions_doc, from_pack=True)
        for ref in self._refs:
            target = self.actions.get(str(ref.init_attrs.get("Name", "") or "").strip())
            if target is None:
                logger.warning("unlinked action reference %r", ref.name())
                continue
            try:
                ref.link(target)
            except MalformedAction as exc:
                logger.warning("skipping action reference %r: %s", ref.name(), exc)

        beh_doc = _load(behaviors_xml_path)
        pool_children: list[_PoolSource] = []
        for top in _iter_elements(beh_doc):
            if local_name(top.tag) == "BehaviorList":
                for child in list(top):
                    src = self._parse_behavior_node(child, pack=True)
                    if src is not None:
                        pool_children.append(src)
        self.initial_pool = _FlatSources(pool_children)
        # reserved built-ins that must exist (C++ behaviors.xml ALWAYS REQUIRED section)
        for builtin in ("Fall", "Dragged", "Thrown"):
            defn = self.behavior_defs.get(builtin)
            if defn is not None:
                defn.node.frequency = 0
                defn.node.hidden = True
        self._link_breed_gags()

    #: solo-pet enhancement — breed behaviors whose own action is empty are remapped to play
    #: their Breed action's animation as a *visual-only* flourish (frames 38-46), no spawn.
    BREED_GAG_ACTIONS = {
        "SplitIntoTwo": "Divide1",
        "PullUpShimeji": "PullUpShimeji1",
    }

    def _link_breed_gags(self):
        for beh_name, act_name in self.BREED_GAG_ACTIONS.items():
            defn = self.behavior_defs.get(beh_name)
            act = self.actions.get(act_name)
            if defn is None or act is None or not isinstance(defn.node.action, _NoOpInline):
                continue
            ref = ReferenceAction({"Name": act_name}, self)
            try:
                ref.link(act)
            except MalformedAction as exc:      # noqa: BLE001
                logger.warning("breed gag %r skipped: %s", beh_name, exc)
                continue
            defn.node.action = ref

    def _parse_actions(self, doc: ET.ElementTree, from_pack: bool):
        for el in _iter_elements(doc):
            ln = local_name(el.tag)
            if ln == "ActionList":
                for a in list(el):
                    parsed = self._parse_action(a, child=False)
                    if parsed is None:
                        continue
                    name = str(parsed.init_attrs.get("Name", "") or "").strip()
                    if not name:
                        continue
                    existing = self.actions.get(name)
                    if existing is not None and from_pack:      # first definition wins (C++ overwrites; keep ours on merge)
                        continue
                    self.actions[name] = parsed

    def _parse_action(self, el: ET.Element, child: bool) -> Optional[Action]:
        ln = local_name(el.tag)
        if ln == "ActionReference":
            ref = ReferenceAction(dict(_attrs(el)), self)
            self._refs.append(ref)          # linked after all actions are parsed
            return ref
        if ln != "Action" and not child:
            return None
        if child and ln not in ("Action",):
            return None

        attrs = dict(_attrs(el))
        name = str(attrs.get("Name", "") or "").strip()
        action_type = str(attrs.pop("Type", "") or "").strip()
        cls_attr = str(attrs.pop("Class", "") or "").strip()

        # Embedded class → canonical type (C++ parser strips the com.group_finity prefix)
        if action_type == "Embedded":
            for known in ("com.group_finity.mascot.action.",):
                if cls_attr.startswith(known):
                    action_type = cls_attr[len(known):]
                    break
            else:
                logger.warning("unknown embedded class %r — treating as no-op", cls_attr)
                return None

        # collect <Animation> blocks (ordered; each may carry its own Condition)
        anims: list[AnimList] = []
        child_actions: list[Action] = []
        for sub in list(el):
            sln = local_name(sub.tag)
            if sln == "Animation":
                poses = [self._parse_pose(p) for p in sub.findall(f"{ns_el(sub.tag)}Pose")]
                if poses:
                    anims.append(AnimList(poses, str(_attr_of(sub, "Condition", "true") or "true")))
            elif sln == "Action":
                ca = self._parse_action(sub, child=True)
                if ca is not None:
                    child_actions.append(ca)
            elif sln == "ActionReference":
                ra = self._parse_action(sub, child=True)
                if ra is not None:
                    child_actions.append(ra)

        # unknown/solo-excluded types → parse ok but run as one-tick no-op (safe degrade)
        # (match case-insensitively: the pack's Embedded classes use `FallWithIE`/`WalkWithIE`)
        if action_type in self.UNIMPLEMENTED_TYPES or \
                action_type.lower().startswith(("fallwithie", "walkwithie", "throwie",
                                                "broadcast", "hotspot")):
            # FallWithIE/WalkWithIE/ThrowIE need an IE prop we don't render solo; degrade to plain stays/moves
            if action_type.lower() == "fallwithie":
                action_type = "Stay"
            elif action_type.lower() == "walkwithie":
                action_type = "Move"
            elif action_type.lower().startswith("throwie"):
                action_type = "Stay"   # plays its own windup pose (shime37) then advances; no IE to toss solo
            elif action_type.lower().startswith("breed"):
                # visual-only flourish: play the Breed animation (frames 38-46) in place, but
                # never spawn — env.allows_breeding is False for the solo pet. Frames stay used.
                action_type = "Stay"
            else:
                return _NoOpAction(attrs, self, name)

        anim_action_types = {"Stay", "Move", "Animate", "Jump", "Fall", "Dragged",
                             "Regist", "Turn", "MoveWithTurn"}
        try:
            if action_type in ("Sequence", "Select"):
                cls = SelectAction if action_type == "Select" else SequenceAction
                return cls(attrs, self, child_actions)
            if action_type in ("Offset",):
                act = InstantAction(attrs, self); act.kind = "offset"; return act
            if action_type in ("Look",):
                act = InstantAction(attrs, self); act.kind = "look"; return act
            if action_type in anim_action_types:
                cls = {"Stay": StayAction, "Move": MoveAction, "Jump": JumpAction,
                       "Fall": FallAction, "Animate": AnimateAction}.get(
                           action_type, AnimationAction)
                if action_type == "Dragged" or action_type == "Regist":
                    # while dragged the UI pins the anchor; pose set animates in place
                    return _DraggableAction(attrs, self, anims)
                return cls(attrs, self, anims)
        except Exception as exc:                     # noqa: BLE001 - report & skip action
            logger.warning("failed to build action %r (%s): %s", name or action_type, action_type, exc)
        return None

    def _parse_pose(self, el: ET.Element) -> Optional[Pose]:
        img = str(_attr_of(el, "Image", "")).lstrip("/")
        if not img:
            return None
        anchor_s = str(_attr_of(el, "ImageAnchor", "0,128"))
        try:
            ax, ay = (float(x) for x in anchor_s.split(","))
        except ValueError:
            ax, ay = 64.0, 128.0
        vel_s = str(_attr_of(el, "Velocity", "0,0"))
        try:
            vx, vy = (float(x) for x in vel_s.split(","))
        except ValueError:
            vx, vy = 0.0, 0.0
        dur = int(float(str(_attr_of(el, "Duration", "250")) or 1))
        return Pose(image=img, anchor=Vec2(ax, ay), velocity=Vec2(vx, vy), duration=max(1, dur))

    def _parse_behavior_node(self, el: ET.Element, pack: bool) -> Optional[_PoolSource]:
        """Parse a <Behavior> or <Condition> group under a BehaviorList."""
        ln = local_name(el.tag)
        if ln == "Condition":
            cond_js = str(_attr_of(el, "Condition", "true") or "true")
            sub: list[_PoolSource] = []
            for child in list(el):
                src = self._parse_behavior_node(child, pack=pack)
                if src is not None:
                    sub.append(src)
            return _GroupSources(cond_js, sub)

        if ln != "Behavior":
            return None
        name = str(_attr_of(el, "Name", "") or "").strip()
        if not name:
            return None
        try:
            freq = int(float(str(_attr_of(el, "Frequency", "100")) or 100))
        except ValueError:
            freq = 100
        hidden = _attr_of(el, "Hidden", "") is not None and \
            str(_attr_of(el, "Hidden")).strip().lower() == "true"

        # next pool: <NextBehavior Add=...> with BehaviorReference children (cond-gated)
        next_children: list[_PoolSource] = []
        add_next = False
        for sub in el.findall(f"{ns_el(el.tag)}NextBehavior"):
            add_next = str(_attr_of(sub, "Add", "")).strip().lower() == "true"
            for ref in list(sub):
                if local_name(ref.tag) == "BehaviorReference":
                    rname = str(_attr_of(ref, "Name", "") or "").strip()
                    if not rname:
                        continue
                    try:
                        rfreq = int(float(str(_attr_of(ref, "Frequency", "100")) or 100))
                    except ValueError:
                        rfreq = 100
                    next_children.append(_NamedSource(rname, str(_attr_of(ref, "Condition", "") or ""),
                                                      rfreq))

        # the behavior's own action content (inline <Action> children) — required for pack behaviors
        inline_action: Optional[Action] = None
        for sub in el:
            if local_name(sub.tag) == "Action":
                inline_action = self._parse_action(sub, child=True)
                break

        node = BehaviorNode(name=name, action=inline_action or _NoOpInline(core=self), frequency=freq, hidden=hidden)
        # reference behaviors point at named actions (e.g. behavior "StandUp" → action "StandUp")
        if inline_action is None and name in self.actions:
            ref = ReferenceAction({"Name": name}, self)
            ref.link(self.actions[name])
            node.action = ref

        defn = BehaviorDef(node=node, next_children=next_children, add_next=add_next)
        # don't clobber an earlier definition of the same name (later packs are reference only)
        if pack and name not in self.behavior_defs:
            self.behavior_defs[name] = defn
        elif not pack:
            self.behavior_defs[name] = defn
        return _NamedSource(name, str(_attr_of(el, "Condition", "") or ""), freq)

    # -- runtime ---------------------------------------------------------------
    def set_active_frame(self, pose: Pose):
        if getattr(self.state.active_frame, "image", None) != pose.image:
            self.state.active_frame = pose       # record the current pose (for rendering/telemetry)
            self.on_frame_changed(pose)          # hook for signals/logging (default no-op)

    on_frame_changed: Callable[[Pose], None] = lambda self, p: None

    def force_behavior(self, name: str):
        """App-side entry point: drag→Dragged, flick→Thrown, nudge/reply→custom."""
        self.state.queued_behavior = name

    def update_environment(self, *, cursor_pos=None, tracked_window=None):
        """Feed per-tick platform data (logical px). `tracked_window` = rect dict or None."""
        if cursor_pos is not None:
            x, y, dx, dy = cursor_pos
            self.env.cursor.x, self.env.cursor.y = x, y
            self.env.cursor.dx, self.env.cursor.dy = dx, dy
        if tracked_window is None:
            # all-negative sentinel → visible()==False → its borders never match (no window)
            self.env.active_ie = DArea.invisible()
        else:
            w = tracked_window
            top = float(w.get("top", 0)); bottom = float(w.get("bottom", 0))
            left = float(w.get("left", 0)); right = float(w.get("right", 0))
            prev = self.env.active_ie
            new = DArea(top, right, bottom, left)
            if hasattr(prev, "dx"):
                new.dx = (new.left - getattr(prev, "left", 0.0)) or 0.0
                new.dy = (new.top - getattr(prev, "top", 0.0)) or 0.0
            self.env.active_ie = new

    # -- tick + the recovery ladder (C++ mascot::manager::tick) -----------------
    def pre_tick(self):
        st = self.state
        if not st.dead:
            st.time += 1

    def _pick_behavior(self, queued_name: Optional[str]) -> BehaviorNode:
        """Roulette (C++ behavior::manager::next). Returns the chosen node; sets next pool."""
        defs = self.behavior_defs

        def find(name):
            d = defs.get(name)
            if d is None:
                raise KeyError(f"unknown behavior {name!r}")
            return d

        if queued_name:
            node = find(queued_name).node          # forced → pool becomes that behavior's next list
            self._next_pool_srcs = _PoolSourcesOf(find(queued_name))
            return node

        sources: list[_PoolSource] = list(getattr(self, "_current_pool", self.initial_pool))
        flat = []                                   # (BehaviorDef-ish source) with conditions evaluated
        for src in sources:
            if isinstance(src, _GroupSources):
                if not self._cond_true(src.cond_js):
                    continue
                flat.extend(s for s in src.sub if self._ref_ok(s))
            elif isinstance(src, _NamedSource):
                if not self._cond_true(src.cond_js):
                    continue
                if self._ref_ok(src):
                    flat.append(src)

        # drop excluded (user toggles / solo-pet breeding list) + hidden (not ambient-selectable)
        def keep(s: _PoolSource) -> bool:
            nm = s.name
            return nm not in self.excluded and not find(nm).node.hidden
        flat = [s for s in flat if keep(s)]

        # single candidate → take it (C++); else weighted pick; no candidates → Fall fallback
        def src_freq(s: _PoolSource) -> int:
            return int(s.freq) if getattr(s, "freq", 0) else find(s.name).node.frequency

        if len(flat) == 1:
            chosen = flat[0]
        elif flat:
            total = sum(src_freq(s) for s in flat) or 1
            dice = self.rng.randint(0, total - 1)
            acc = 0
            chosen = flat[-1]
            for s in flat:
                acc += src_freq(s)
                if acc > dice:
                    chosen = s
                    break
        else:
            return self._fallback_fall()

        chdef = find(chosen.name)
        node = chdef.node
        add_next = chdef.add_next
        self._next_pool_srcs = _PoolSourcesOf(chdef, initial=self.initial_pool, add_next=add_next)
        return node

    def _ref_ok(self, src: "_PoolSource") -> bool:
        """A named ref is valid if the behavior exists AND (for refs into action lists) the action parses."""
        d = self.behavior_defs.get(src.name)
        return d is not None and d.node.action is not None

    def _cond_true(self, cond_js: str) -> bool:
        js = strip_js_expr((cond_js or "").strip())
        if not js:
            return True
        try:
            v = ExpressionCompiler(js).eval_value(self.js_view, self.rng)
        except parse_error as exc:
            logger.warning("behavior condition failed to compile (%r): %s", cond_js[:80], exc)
            return False      # fail closed → behavior skipped (never crash the pet)
        except Exception:
            return False
        if isinstance(v, _UNDEFINED_TYPE2):
            return False
        return bool(_js_truthy(v))

    def _fallback_fall(self) -> BehaviorNode:
        d = self.behavior_defs.get("Fall")
        if d is not None:
            return d.node
        # no Fall defined at all → build a synthetic one so the pet always drops
        fall_action = self.actions.get("Falling")
        if fall_action is None:
            fall_action = StayAction({}, self,
                                     [AnimList([Pose("shime1.png", Vec2(64, 128), Vec2(), 1)])])
        elif isinstance(fall_action, ReferenceAction):
            fall_action = fall_action.target or _NoOpInline(core=self)
        elif isinstance(fall_action, SequenceAction):
            fall_action = _NoOpInline(core=self)
        node = BehaviorNode(name="Fall", action=fall_action, frequency=0, hidden=True)
        self.behavior_defs["Fall"] = BehaviorDef(node=node, next_children=[], add_next=False)
        return node

    def detach_from_borders(self):
        st, env = self.state, self.env
        a = st.anchor
        if env.active_ie.right_border().is_on(a) or env.work_area.left_border().is_on(a):
            a.x += 1.1
        elif env.active_ie.left_border().is_on(a) or env.work_area.right_border().is_on(a):
            a.x -= 1.1
        if env.active_ie.bottom_border().is_on(a) or env.work_area.top_border().is_on(a):
            a.y += 1.1
        elif env.active_ie.top_border().is_on(a) or env.work_area.bottom_border().is_on(a):
            a.y -= 1.1

    def reset_position(self):
        scr = self.env.screen
        if scr.width >= 100 and scr.height >= 100:
            st.anchor.x = scr.left + 50 + self.rng.random() * (scr.width - 100)
            st.anchor.y = scr.top + 50 + self.rng.random() * (scr.height - 100)
        else:
            st.anchor.x, st.anchor.y = scr.left + scr.width / 2, scr.top + scr.height / 2

    def _start_behavior(self, node: BehaviorNode):
        if self.active_action is not None and self.active_action.active:
            try:
                self.active_action.finalize()
            except Exception:      # noqa: BLE001 - finalize must never block a transition
                pass
        self.state.behavior_name = node.name
        act = node.action
        try:
            act.init(ActionCtx(self))
        except (MalformedAction, parse_error) as exc:
            # count CONSECUTIVE failures; only a genuinely broken behavior trips the limit
            self._init_count += 1
            if self._init_count >= 20:      # C++ reached_init_limit → stop trying, let caller reset
                self._init_count = 0
                raise MalformedAction(node.name, "reached init limit") from exc
            logger.warning("behavior %r failed to start (%s); re-picking", node.name, exc)
            return False
        self._init_count = 0                # success resets the failure streak
        self.active_action = act
        self._active_behavior_node = node
        self.on_behavior_changed(node.name)   # hook (no-op by default; App wires logging/sounds)
        return True

    on_behavior_changed: Callable[[str], None] = lambda self, name: None

    def _tick_once(self) -> bool:
        st = self.state
        if st.queued_behavior:
            node = self._pick_behavior(st.queued_behavior or None)
            st.queued_behavior = ""
            if not self._start_behavior(node):
                return False
        ok = self.active_action.subtick(0) if (self.active_action and self.active_action.active) else False
        if ok:
            return True
        # action ended → choose the next one (pool from the finished behavior's NextBehavior)
        srcs = getattr(self, "_next_pool_srcs", None) or self.initial_pool
        node = self._pick_from(srcs)
        st.queued_behavior = ""
        return self._start_behavior(node)

    def _pick_from(self, sources: list["_PoolSource"]) -> BehaviorNode:
        """Same roulette but over an explicit source list (used for next-pool selection)."""
        save_current = getattr(self, "_current_pool", None)
        self._current_pool = sources
        try:
            return self._pick_behavior(None)
        finally:
            if save_current is not None:
                self._current_pool = save_current

    def tick(self):
        """One engine tick — the 4-level recovery ladder (a bad action can't wedge us)."""
        st = self.state
        if st.dead:
            return
        self.pre_tick()

        for attempt in range(4):
            try:
                if self._tick_once():
                    return
            except MalformedAction as exc:
                logger.warning("tick recovery (attempt %d): %s", attempt + 1, exc)
            # escalate: force Fall → detach from borders → reset position (C++ manager::tick)
            self._init_count = 0
            st.queued_behavior = "Fall" if attempt >= 1 else ""
            if attempt == 2:
                self.detach_from_borders()
            if attempt == 3:
                self.reset_position()

        # last resort (should be unreachable): force Fall and let the next tick handle it
        st.queued_behavior = "Fall"

    # -- test/inspection ---------------------------------------------------------
    @property
    def active_behavior_name(self) -> str:
        return self.state.behavior_name


# --------------------------------------------------------------------------- support ----
class _NoOpAction(Action):     # solo-pet degradation target for unrunnable types (one-tick advance)
    def __init__(self, attrs, core, name=""):
        super().__init__(attrs, core)
        if name:
            self.init_attrs.setdefault("Name", name)

    def step(self) -> bool:
        return False


class _NoOpInline(Action):
    def __init__(self, attrs=None, core=None):
        super().__init__(dict(attrs or {}), core)

    def init(self, ctx: ActionCtx):
        self.active = True
        self.real_start = getattr(self, "real_start", 0)
        self.vars = ActionVars({}, lambda: None, None)   # no keys → condition_ok/duration_ok True

    def step(self) -> bool:
        return False


class _DraggableAction(AnimationAction):   # Dragged / Regist pose sets (in-place while held)
    def init(self, ctx: ActionCtx):
        super().init(ctx)
        st = self.core.state
        st.foot_x = self.env.cursor.x        # pendulum starts on the cursor (C++ Dragged.init)
        st.foot_dx = 0.0

    def step(self) -> bool:
        if not self.tick_ok():
            return False
        # Pendulum (C++ Dragged): footDx = (footDx + (cursorX - footX)*0.1)*0.8 — a damped
        # oscillator that keeps FootX swinging back and forth around the cursor even after it
        # stops moving. The Pinched lean conditions (FootX < cursor.x - N) then alternate, so
        # the pet visibly sways like a pendulum instead of holding one lean pose.
        st = self.st
        base = st.foot_x if st.foot_x is not None else self.env.cursor.x
        st.foot_dx = (st.foot_dx + (self.env.cursor.x - base) * 0.1) * 0.8
        st.foot_x = base + st.foot_dx
        # `_current_anim()` picks the AnimList whose #{Condition} holds (C++ Pinched picks
        # the pose by FootX-vs-cursor distance); play its pose in place — x follows the cursor (UI).
        anim = self._current_anim()
        if anim is None or not anim.poses:
            return False
        if not st.dragging:           # released mid-Dragged behavior → physics via Thrown/Fall
            return False
        pose = anim.get_pose(self.real_elapsed)
        st.anchor.y += pose.velocity.y        # vertical component only; x follows the cursor (UI)
        self.core.set_active_frame(pose)
        return True


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


def _js_truthy(v) -> bool:
    if v is None or isinstance(v, _UNDEFINED_TYPE2):
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return len(v) > 0
    try:
        return len(v) > 0
    except TypeError:
        return True


# -- tiny XML helpers (namespace-agnostic; shimeji files use the group-finity namespace) --
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
