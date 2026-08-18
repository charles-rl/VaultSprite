"""M9 action runners — the per-tick motion/animation engine.

Pure Python (no Qt). Each ``<Action Type=...>`` from the pack XML maps to a runner
below, mirroring the ``com.group_finity.mascot.action.*`` classes of
``DalekCraft2/Shimeji-Desktop``. Actions talk to the engine only through the
duck-typed ``self.core`` instance (``.state`` / ``.env`` / ``.rng`` /
``.js_view`` / ``.set_active_frame``), so this module never imports ``MascotCore``.

Key reference-fidelity notes (see ``docs/09_mascot_engine/README.md`` §3):
- ``Animate`` plays its effective animation **once** then ends.
- ``Select`` picks the first effective branch, runs it to completion, then ends.
- ``Dragged`` sways via the reference damped oscillator (``FootX``/``FootDX``).
"""
from __future__ import annotations

import logging
from typing import Any, Optional, TYPE_CHECKING, Union

from .mascot_data import AnimList, MascotState, Pose
from .mascot_environment import (BORDER_TOL, DArea, ExpressionCompiler, JSMascot,
                                 MascotEnvironment, Vec2, parse_error)
from .mascot_vars import ActionVars, _UNDEFINED

if TYPE_CHECKING:                      # type-checking only — no runtime circular import
    from .mascot_engine import MascotCore

logger = logging.getLogger(__name__)


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
                # Evaluate against the action's own attributes so conditions can
                # reference bare action vars (e.g. ClimbWall's #{TargetY < ...});
                # see review finding B5.
                scope = self.vars.as_scope() if getattr(self, "vars", None) else None
                v = ExpressionCompiler(strip_js_expr(cond)).eval_value(
                    core_view(self.core), self.core.rng, scope)
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
        pose = anim.get_pose(self.real_elapsed)
        self.core.set_active_frame(pose)
        # Apply the pose's velocity so velocity-carrying Animate actions actually move
        # (e.g. Tripping tumbles forward; Bouncing is 0-velocity so it stays put) —
        # Java `animation.apply` integrates pose velocity (review finding C9).
        v = pose.velocity
        st, lr = self.st, self.st.looking_right
        st.anchor.x += (-1 if lr else 1) * v.x
        st.anchor.y += v.y
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

    def init(self, ctx: ActionCtx):
        # Re-resolve branch conditions once per action RUN. _cond_vars caches the
        # ActionVars (whose ${...} conditions are _DynOnce = cached forever), so a
        # shared Select instance would otherwise latch the first run's branch choice
        # for the life of the process (review finding A2). #{} stays per-tick.
        self._cond_vars = []
        super().init(ctx)

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
            # No branch matches (Java `ComplexAction.hasNext()` is false once the
            # current action has no next). END the Select so the parent Sequence
            # advances; blocking here froze ChaseMouse's trailing actions when its
            # IE-only branches couldn't match (review finding A3).
            return False
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
        # the pet visibly sways like a pendulum instead of holding one lean pose. When the
        # cursor comes to rest within a small dead-zone we LOCK FootX onto it (foot_dx = 0) so
        # the sway actually settles instead of re-exciting on cursor jitter forever.
        st = self.st
        cur_x = self.env.cursor.x
        if st.foot_x is None:
            st.foot_x = cur_x
        if abs(cur_x - st.foot_x) < 2.0:
            st.foot_x = cur_x
            st.foot_dx = 0.0
        else:
            base = st.foot_x
            st.foot_dx = (st.foot_dx + (cur_x - base) * 0.1) * 0.8
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
