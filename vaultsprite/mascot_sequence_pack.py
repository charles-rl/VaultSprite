"""Sesame / lacis_shimeji backend — a *second*, additive Shimeji runtime for Android
"shimeji bundle" packs (``manifest.json`` + ``animation.json`` + sequential sprite frames).

These bundles are **not** group-finity XML: the data is a lightweight named-animation FSM with
per-frame sprite index, integer per-tick velocity (``dx``/``dy``), tick durations, weighted
``onFinish`` choices, ``borderTransitions`` (floor/wall/ceiling contact) and an event table.
The M9 XML engine (`MascotCore`) cannot parse it, so this module ports the *same* behavior model
to pure Python over JSON.

Design constraints (kept to protect the PC/XML path — see ``docs/09_mascot_engine/README.md``):
  * Pure Python, **no Qt** — driven by an external tick clock, exactly like `MascotCore`.
  * Isolated: imports nothing from ``mascot_engine`` / ``mascot_actions``; the XML packs
    (steve/kazeem/dieter) run through `MascotEngine` untouched. Backend selection is a single
    branch in App based on pack content — never a change to either engine's internals.
  * Same conceptual surface as `MascotCore`: forced canonical names (Dragged/Thrown/SitDown/Fall…),
    an anchor point, per-tick movement, and frame changes the UI renders.

Documented compromises vs PC packs:
  * No window (IE) interaction — only screen edges; no mouse-tracking face (TAP maps to idle).
  * Throws use a fling pose plus simple gravity integration of the flick velocity (no IE physics).
  * No per-pose ImageAnchor in the format — feet anchor synthesized at image bottom-center by UI.
  * ``idle`` is a self-loop with no auto-timeout in the data; we synthesize one so a pet tapped
    into idle always returns to ambient motion. The Android app exits it via tap; nothing taps here,
    so without this the pet would sit idling forever (documented divergence).
"""
from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SeqFrame:
    sprite: int           # index into the sprites/ %04d pattern (0-based)
    dx: float = 0.0       # per-tick horizontal velocity while this frame shows (signed px/tick)
    dy: float = 0.0       # per-tick vertical velocity (positive = down, screen coords)
    duration_ticks: int = 1

    @classmethod
    def from_json(cls, f: dict) -> "SeqFrame":
        return cls(
            sprite=int(f.get("sprite", 0)),
            dx=float(f.get("dx", 0.0)),
            dy=float(f.get("dy", 0.0)),
            duration_ticks=max(1, int(f.get("durationTicks", 1))),
        )


@dataclass
class SeqChoice:
    to: str
    weight: float = 1.0
    set_facing: Optional[str] = None      # LEFT | RIGHT | RANDOM | KEEP | EVENT_VELOCITY

    @classmethod
    def from_json(cls, c: dict) -> "SeqChoice":
        return cls(to=str(c.get("to", "")), weight=float(c.get("weight", 1.0)),
                   set_facing=c.get("setFacing"))


@dataclass
class SeqAnimation:
    key: str
    type: str                                  # GROUND | WALL | CEILING | AIR | USER
    subtype: Optional[str] = None
    loop_mode: str = "LOOP"                    # LOOP | ONESHOT
    direction: Optional[str] = None            # LEFT | RIGHT | ANY
    frames: list[SeqFrame] = field(default_factory=list)
    on_finish: list[SeqChoice] = field(default_factory=list)
    max_ticks: Optional[tuple[int, int]] = None   # (min,max) ticks before a LOOP may end
    border_transitions: dict[str, list[SeqChoice]] = field(default_factory=dict)

    @property
    def is_ceiling(self) -> bool:
        return self.type.upper() == "CEILING"

    @classmethod
    def from_json(cls, a: dict) -> "SeqAnimation":
        auto = a.get("auto", {}) or {}
        on_finish = [SeqChoice.from_json(c) for c in (auto.get("onFinish") or [])]
        maxd = auto.get("maxDurationTicks")
        max_ticks = None
        if isinstance(maxd, dict):
            try:
                lo, hi = int(maxd.get("minTicks", 0)), int(maxd.get("maxTicks", 0))
                if hi > 0 and lo <= hi:
                    max_ticks = (lo, hi)
            except (TypeError, ValueError):
                max_ticks = None
        borders: dict[str, list[SeqChoice]] = {}
        for bt in a.get("borderTransitions", []) or []:
            when = str(bt.get("when", "")).upper()
            if not when:
                continue
            borders.setdefault(when, []).extend(SeqChoice.from_json(c) for c in (bt.get("choices") or []))
        return cls(
            key=str(a["key"]),
            type=str(a.get("type", "GROUND")),
            subtype=a.get("subtype"),
            loop_mode=str(a.get("loop", "LOOP")).upper(),
            direction=a.get("direction"),
            frames=[SeqFrame.from_json(f) for f in (a.get("frames") or [])],
            on_finish=on_finish,
            max_ticks=max_ticks,
            border_transitions=borders,
        )


@dataclass
class SeqState:
    """Live per-pet state. ``anchor`` is the feet point; the UI moves the window from it."""
    anchor_x: float = 0.0
    anchor_y: float = 0.0
    facing_right: bool = True
    dragging: bool = False
    vx: float = 0.0          # per-tick velocity, used while flung (px/tick)
    vy: float = 0.0
    in_fling: bool = False


# Canonical (App / C++-style) forced names -> bundle animation keys. App always speaks the English
# Shimeji vocabulary; the bundle uses its own keys. Unknown canonical names are ignored (logged once)
# so a missing animation can never wedge the pet — mirrors MascotCore's fallback contract (C7).
FORCED_NAME_ALIASES = {
    "Dragged": "drag",
    "Thrown": "fling",
    "Fall": "fall",
    "SitDown": "idle",            # no sitting art in this bundle; idle is its resting pose
    "StandUp": "stand",
    "LieDown": "idle",
    "SitAndFaceMouse": "idle",    # compromise: no mouse-facing art (documented)
}

# Liveness budget for self-trapping loops, expressed as a fraction of a "long stand" pose.
_SELF_LOOP_TICKS = (300, 900)     # @ tick_ms 40 => ~12-36 s


class MascotSequenceCore:
    """Pure-Python runtime for an animation.json bundle. Driven externally by :meth:`tick`."""

    GRAVITY_PER_TICK = 0.8       # px/tick^2 added to vy while flung (feel-tuned, not physical)
    TERMINAL_VY = 14.0           # cap downward speed so a fling can't tunnel through the floor

    def __init__(self, animations: dict[str, SeqAnimation], rng=None):
        self.anims = animations
        self.rng = rng or random.Random()
        self.state = SeqState()
        self.current_key: Optional[str] = None
        # per-animation clocks (reset by _switch)
        self.frame_index = 0
        self.tick_in_frame = 0         # ticks into the current frame's duration
        self.frames_done = 0           # frames fully shown of the current cycle (ONESHOT end)
        self.tick_elapsed = 0          # raw ticks since this animation started (budget unit)
        self._chosen_max_ticks = 0
        # work area (logical px), updated by the UI each tick: left, right, top, bottom
        self.wa_left = 0.0; self.wa_right = 1.0; self.wa_top = 0.0; self.wa_bottom = 1.0
        self._warned_missing: set[str] = set()
        # hide/show walk-off: poses play in place while App owns the window position (the UI calls
        # sync_anchor each step). Mirrors the XML pack's synthetic HideWalk / InPlaceAction.
        self.hide_walk_active = False

    # -- geometry ---------------------------------------------------------------
    def set_work_area(self, left: float, right: float, top: float, bottom: float):
        self.wa_left, self.wa_right, self.wa_top, self.wa_bottom = \
            float(left), float(right), float(top), float(bottom)

    # -- public control (mirrors the App-facing surface of MascotEngine) --------
    def spawn(self):
        """Place the pet on the floor at center; it drops in via its default 'fall' animation."""
        st = self.state
        st.anchor_x = (self.wa_left + self.wa_right) / 2.0
        st.anchor_y = self.wa_bottom
        st.facing_right = bool(self.rng.random() < 0.5)
        st.vx = st.vy = 0.0; st.in_fling = False
        default = "fall" if "fall" in self.anims else next(iter(self.anims), None)
        self._switch(default, keep_anchor=True)

    def set_dragging(self, dragging: bool):
        st = self.state
        st.dragging = bool(dragging)
        if dragging and "drag" in self.anims and self.current_key != "drag":
            self._switch("drag", keep_anchor=True)

    def inject_fling_velocity(self, vx_per_tick: float, vy_per_tick: float):
        """Feed a flick's release velocity (px/tick). Arms the fling integrator + pose."""
        st = self.state
        st.vx = float(vx_per_tick); st.vy = float(vy_per_tick)
        if "fling" in self.anims:
            st.in_fling = True
            self._switch("fling", keep_anchor=True)

    def force(self, name: str):
        """App-side entry point. Accepts a canonical Shimeji name or a raw animation key."""
        target = FORCED_NAME_ALIASES.get(name, name)
        if not target or target not in self.anims:
            known = set(FORCED_NAME_ALIASES.values()) | set(self.anims)
            if name and name not in known and name not in self._warned_missing:
                logger.warning("sequence pack: forced behavior %r has no animation; ignoring", name)
                self._warned_missing.add(name)
            return
        st = self.state
        # entering the fling without an explicit velocity means "just released": drift down;
        # leaving it (any other forced behavior) clears residual inertia so a later ambient
        # fall is a clean descent, not an inherited throw.
        if target == "fling" and st.vy == 0.0:
            st.in_fling = True
        elif target != "fling":
            st.in_fling = False
            st.vx = st.vy = 0.0
        self._switch(target, keep_anchor=True)

    def set_hide_walk(self, active: bool, moving_right: bool = True):
        """In-place walk pose while the UI walks the window off/on-screen (mirrors the XML
        pack's synthetic HideWalk). The anchor is owned by the UI; frames just play."""
        if not active:
            self.hide_walk_active = False
            return
        st = self.state
        st.facing_right = bool(moving_right)
        key = "walk_right" if moving_right else "walk_left"
        self.hide_walk_active = True
        if key in self.anims and self.current_key != key:
            self._switch(key, keep_anchor=True)

    # -- per-tick engine ----------------------------------------------------------
    def tick(self):
        st = self.state
        a = self.anims.get(self.current_key) if self.current_key else None
        if a is None or not a.frames:
            return

        if st.dragging or (self.hide_walk_active and not st.in_fling):
            # position is owned by the App (drag or walk-off/on-screen): cycle the pose in place,
            # never move/re-route — mirrors the XML pack's InPlaceAction for HideWalk.
            self.tick_elapsed += 1
            self._step_display(a)
            return

        self.tick_elapsed += 1                   # one tick of this animation (budget unit)

        # -- movement ------------------------------------------------------------
        if st.in_fling or a.key == "fling":
            mvx, mvy = st.vx, st.vy           # fling integrates injected velocity + gravity
            st.vy += self.GRAVITY_PER_TICK
            if st.vy > self.TERMINAL_VY:
                st.vy = self.TERMINAL_VY
        else:
            frame = a.frames[self.frame_index]
            mvx, mvy = frame.dx, frame.dy     # 'fall' bakes its own descent rate (dy=10)
        if self.hide_walk_active and not st.in_fling:
            mvx = mvy = 0.0    # HideWalk plays in place; App owns the window (InPlaceAction parity)

        st.anchor_x += mvx
        st.anchor_y += mvy

        # keep the solo pet on-screen; touching a wall zeroes that axis's velocity
        if st.anchor_x < self.wa_left:
            st.anchor_x = self.wa_left; st.vx = 0.0
        elif st.anchor_x > self.wa_right:
            st.anchor_x = self.wa_right; st.vx = 0.0
        if st.anchor_y > self.wa_bottom:
            st.anchor_y = self.wa_bottom
            if st.vy > 0:
                st.vy = 0.0
        elif st.anchor_y < self.wa_top:
            st.anchor_y = self.wa_top; st.vy = 0.0

        # a fling that has reached the floor (or a wall) ends in its bounce chain
        if st.in_fling and (st.anchor_y >= self.wa_bottom - 1.0 or st.anchor_x <= self.wa_left + 1.0
                            or st.anchor_x >= self.wa_right - 1.0):
            st.in_fling = False; st.vx = st.vy = 0.0
            target = None
            if "bounce" in self.anims and st.anchor_y >= self.wa_bottom - 1.0:
                target = "bounce"
            else:
                choices = (a.border_transitions.get("BOTTOM") or a.border_transitions.get("LEFT")
                           or a.border_transitions.get("RIGHT"))
                choice = self._pick(choices) if choices else None
                if choice is not None:
                    self._apply_facing(choice.set_facing); target = choice.to
            self._switch(target or "stand", keep_anchor=True)
            return

        # -- border transitions (contact-driven, like the XML recovery ladder) ----
        for when in ("BOTTOM", "TOP", "LEFT", "RIGHT"):
            if not self._touches(when):
                continue
            choices = a.border_transitions.get(when)
            if not choices:
                continue
            choice = self._pick(choices)
            if choice is None:
                continue
            self._apply_facing(choice.set_facing)
            self._switch(choice.to, keep_anchor=True)
            return

        # -- advance frames / finish -----------------------------------------------
        self._step_display(a)
        if self._should_end(a):
            target, facing = self._exit_target(a)
            if not target or target not in self.anims:
                return
            if target != a.key:
                self._apply_facing(facing)
                self._switch(target, keep_anchor=True)
            else:
                # onFinish loops back to itself (a walk re-rolling its own direction): restart the
                # frame cycle WITHOUT resetting the budget or chosen max — otherwise a bounded loop
                # would never actually end.
                if not self.hide_walk_active:      # HideWalk keeps looping until App stops it
                    self.frame_index = 0
                    self.frames_done = 0

    # -- internals ------------------------------------------------------------------
    def _touches(self, when: str) -> bool:
        st = self.state
        if when == "BOTTOM":
            return st.anchor_y >= self.wa_bottom - 1.0
        if when == "TOP":
            return st.anchor_y <= self.wa_top + 1.0 and not (st.anchor_y >= self.wa_bottom - 1.0)
        if when == "LEFT":
            return st.anchor_x <= self.wa_left + 1.0
        if when == "RIGHT":
            return st.anchor_x >= self.wa_right - 1.0
        return False

    def _step_display(self, a: SeqAnimation):
        """Advance the per-frame display clock (poses only; movement already applied this tick)."""
        n = len(a.frames)
        if not n:
            return
        self.tick_in_frame += 1
        frame = a.frames[self.frame_index]
        if self.tick_in_frame < frame.duration_ticks:
            return
        self.tick_in_frame = 0
        self.frames_done += 1
        at_last = self.frame_index == n - 1
        if a.loop_mode == "ONESHOT" and at_last:
            return                        # hold on the last pose; _should_end switches us out
        self.frame_index = (self.frame_index + 1) % n

    def _should_end(self, a: SeqAnimation) -> bool:
        """Whether the current animation has run its course.

        ONESHOT ends once every frame was shown exactly once; bounded LOOPs end after their
        ``maxDurationTicks`` budget (raw ticks); unbounded loops only self-end for a *synthesized*
        liveness budget — and only if that loop's onFinish exits nowhere but itself (see
        :meth:`_exit_target`). Everything else (e.g. 'fall') runs until a border/event switches it,
        exactly as the Android app keeps such animations alive."""
        n = len(a.frames)
        if a.loop_mode == "ONESHOT":
            return self.frames_done >= n
        if a.max_ticks is not None:
            return self.tick_elapsed >= self._chosen_max_ticks
        return self._is_self_only_loop(a) and self.tick_elapsed >= self._self_budget(a)

    def _is_self_only_loop(self, a: SeqAnimation) -> bool:
        """True when the animation's only exit is itself (an ambient 'trapped' loop like idle)."""
        return bool(a.on_finish) and all(c.to == a.key for c in a.on_finish)

    def _self_budget(self, a: SeqAnimation) -> int:
        budget = getattr(a, "_auto_budget", 0)
        if not budget:
            lo, hi = _SELF_LOOP_TICKS
            a._auto_budget = int(self.rng.randint(lo, hi))   # cached per animation instance
            budget = a._auto_budget
        return budget

    def _exit_target(self, a: SeqAnimation):
        """Resolve where a finished animation goes next (target key, facing spec). A self-only
        loop re-rolls from the pack's ambient pool instead of playing its no-op exit."""
        if not a.on_finish:
            return None, None
        if self._is_self_only_loop(a):
            stand = self.anims.get("stand")
            pool = [c for c in (stand.on_finish if stand else [])
                    if c.to in self.anims and c.to != a.key and c.weight > 0]
            choice = self._pick(pool) if pool else None
            return (choice.to, choice.set_facing) if choice else (None, None)
        choice = self._pick(a.on_finish)
        return (choice.to, choice.set_facing) if choice else (None, None)

    def _pick(self, choices: list[SeqChoice]) -> Optional[SeqChoice]:
        total = sum(c.weight for c in choices if c.to and c.weight > 0)
        if total <= 0:
            valid = [c for c in choices if c.to]
            return self.rng.choice(valid) if valid else None
        r = self.rng.random() * total
        acc = 0.0
        for c in choices:
            if not c.to or c.weight <= 0:
                continue
            acc += c.weight
            if r <= acc:
                return c
        return [c for c in choices if c.to][-1]

    def _apply_facing(self, spec: Optional[str]):
        st = self.state
        if not spec:
            return
        s = str(spec).upper()
        if s == "LEFT":
            st.facing_right = False
        elif s == "RIGHT":
            st.facing_right = True
        elif s == "RANDOM":
            st.facing_right = bool(self.rng.random() < 0.5)
        # KEEP / EVENT_VELOCITY: leave unchanged (fling facing is set by the UI from velocity sign)

    def _switch(self, key: Optional[str], keep_anchor: bool):
        if not key or key not in self.anims:
            return                                # no such animation -> stay as-is, never wedge
        a = self.anims[key]
        st = self.state
        st.anchor_x = max(self.wa_left, min(st.anchor_x, self.wa_right))
        st.anchor_y = max(self.wa_top, min(st.anchor_y, self.wa_bottom))
        if key == "fling" and not st.in_fling:
            st.in_fling = True                    # entering the fling via force(): drift down
        self.current_key = key
        self.frame_index = 0
        self.tick_in_frame = 0
        self.frames_done = 0
        self.tick_elapsed = 0
        a._auto_budget = 0                        # re-roll any synthetic liveness budget next time it's needed
        if a.max_ticks is not None:
            lo, hi = a.max_ticks
            self._chosen_max_ticks = int(self.rng.randint(lo, hi))


# -- loader -------------------------------------------------------------------------
def load_sequence_pack(pack_dir: Path) -> tuple[dict, dict[str, SeqAnimation], list[Path]]:
    """Load a bundle's ``manifest.json`` + ``animation.json``.

    Returns ``(manifest, animations_by_key, sprite_paths)`` where ``sprite_paths[i]`` is the on-disk
    path for sprite index i. Raises FileNotFoundError / ValueError for malformed bundles — the UI
    layer catches these and degrades exactly like a bad XML pack (engine off, app alive)."""
    pack_dir = Path(pack_dir)
    manifest_path = pack_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"not a sequence bundle: missing {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    anim_ref = (manifest.get("animationSchema") or {}).get("path", "animation.json")
    data = json.loads((pack_dir / str(anim_ref)).read_text(encoding="utf-8"))

    sp = manifest.get("sprites", {}) or {}
    base_rel = str(sp.get("basePath", "sprites/")).strip("/") + "/"
    pattern = str(sp.get("filePattern", "%04d.webp"))
    count = int(sp.get("spriteCount") or 0)
    sprites_root = pack_dir / base_rel

    def sprite_path(i: int) -> Path:
        name = pattern.replace("%04d", f"{i:04d}").replace("{n}", str(i))
        return sprites_root / name

    paths: list[Path] = []
    for i in range(count or 1):
        p = sprite_path(i)
        if not p.exists():
            raise FileNotFoundError(f"missing sprite {i}: {p}")
        paths.append(p)

    animations: dict[str, SeqAnimation] = {}
    for a in data.get("animations", []) or []:
        key = str(a.get("key", "")).strip()
        if not key:
            continue
        animations[key] = SeqAnimation.from_json(a)
    if not animations:
        raise ValueError(f"no animations parsed from {anim_ref}")

    max_ref = max((f.sprite for an in animations.values() for f in an.frames), default=-1)
    if max_ref >= len(paths):
        raise ValueError(f"animation references sprite {max_ref} but only {len(paths)} present")
    return manifest, animations, paths
