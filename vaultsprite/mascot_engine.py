"""Mascot behavior engine (Module 9) — plays standard Shimeji packs natively in Python.

Pure-Python port of the Shimeji-ee runtime (pattern source: ``DalekCraft2/Shimeji-Desktop``,
see ``docs/09_mascot_engine/README.md``): parses ``actions.xml`` + ``behaviors.xml``, runs the
weighted behavior roulette with per-tick pose animation over raw PNG frames, and evaluates
``${...}``(once)/`#{...}`(per-tick) conditions through the safe evaluator in
:mod:`vaultsprite.mascot_environment`. No Qt import — unit-tested by calling ``core.tick()``
directly (the same style as terrain/stat tests).

This module is the **facade + orchestrator** of the M9 split: it holds :class:`MascotCore`
(the parser, behavior roulette, tick loop and 4-level recovery ladder) and re-exports the
leaf-module types so existing imports keep working. The rest of the split lives in:

- :mod:`vaultsprite.mascot_xml`    — namespace-agnostic XML helpers
- :mod:`vaultsprite.mascot_data`   — pose/animation + behavior-pool data types
- :mod:`vaultsprite.mascot_vars`   — ``ActionVars`` (``${once}`` / ``#{per-tick}`` store)
- :mod:`vaultsprite.mascot_actions` — the action runners (Stay/Move/Fall/Jump/Sequence/…)

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
import math
import random
from pathlib import Path
from typing import Any, Callable, Optional, Union

from .mascot_actions import (Action, ActionCtx, AnimationAction, AnimateAction,
                             FallAction, InPlaceAction, InstantAction, JumpAction,
                             MalformedAction, MoveAction, ReferenceAction, SelectAction,
                             SequenceAction, StayAction, _DraggableAction, _NoOpAction,
                             _NoOpInline, core_view, is_true_js, strip_js_expr)
from .mascot_data import (AnimList, BehaviorDef, BehaviorNode, MascotState, Pose,
                          _BehaviorList, _FlatSources, _GroupSources, _NamedSource,
                          _PoolEntry, _PoolSource, _PoolSourcesOf)
from .mascot_environment import (BORDER_TOL, DArea, DVec2, ExpressionCompiler, JSMascot,
                                 MascotEnvironment, Vec2, is_undefined, parse_error)
from .mascot_vars import ActionVars, _DynOnce, _UNDEFINED, _UNDEFINED_TYPE2, _coerce_literal
from .mascot_xml import _attr_of, _attrs, _iter_elements, _load, local_name, ns_el

logger = logging.getLogger(__name__)


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
        # C++ reached_init_limit guard, tracked PER BEHAVIOR so an unrelated successful
        # fallback (e.g. forcing Fall after a broken pick) can't reset the streak.
        self._init_fail: dict[str, int] = {}
        self._broken: set[str] = set()                # behaviors excluded after repeated init failure

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
                # NOTE: must be "Animate" (play once then advance), NOT "Stay" — a Stay loops
                # its poses forever, so the PullUpShimeji/SplitIntoTwo gag would never advance
                # and the pet would sit in the flourish indefinitely (reported scale-change
                # loop). Animate plays the breed frames once and moves on.
                action_type = "Animate"
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
            d = defs.get(queued_name)
            if d is None:
                # A forced name that isn't defined must not wedge the pet: log and fall
                # through to the ambient roulette/fallback instead of raising KeyError
                # every tick (review finding C7).
                logger.warning("forced behavior %r not defined; falling back to ambient", queued_name)
            else:
                self._next_pool_srcs = _PoolSourcesOf(d)
                return d.node

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

        # drop excluded (user toggles / solo-pet breeding list) + broken (repeated init
        # failure) + hidden (not ambient-selectable)
        def keep(s: _PoolSource) -> bool:
            nm = s.name
            return nm not in self.excluded and nm not in self._broken and not find(nm).node.hidden
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
        """A named ref is valid if the behavior exists, hasn't been excluded after
        repeated init failure, AND (for refs into action lists) the action parses."""
        d = self.behavior_defs.get(src.name)
        return d is not None and src.name not in self._broken and d.node.action is not None

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
            # count consecutive failures PER BEHAVIOR; only a genuinely broken one trips the
            # limit. C++ reached_init_limit → stop trying: exclude it so the ambient roulette
            # stops re-picking it (review finding C8). Never raise — the ladder still
            # recovers, and raising would kill the tick loop. An unrelated success (e.g. the
            # forced-Fall fallback) must NOT reset another behavior's streak.
            n = self._init_fail.get(node.name, 0) + 1
            self._init_fail[node.name] = n
            if n >= 20:
                self._init_fail[node.name] = 0
                self._broken.add(node.name)
                logger.warning("behavior %r failed to start 20x; excluding it", node.name)
                return False
            logger.warning("behavior %r failed to start (%s); re-picking", node.name, exc)
            return False
        self._init_fail[node.name] = 0      # success clears this behavior's streak
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
            # escalate: force Fall → detach from borders → reset position (C++ manager::tick).
            # NOTE: we intentionally do NOT reset _init_count here — consecutive init
            # failures must accumulate across ticks so the exclusion guard (C8) can fire.
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


def _js_truthy(v) -> bool:
    # Fail CLOSED (falsy) for unknown/undefined/NaN — matching js_truthy in
    # mascot_environment and the documented "exotic behaviors degrade safely"
    # contract. The old version mishandled the env `_UNDEFINED` sentinel (no
    # __len__ → TypeError → True) and NaN (v != 0 → True) — review finding B6.
    if v is None or isinstance(v, _UNDEFINED_TYPE2):
        return False
    if is_undefined(v):                     # the environment's _UndefinedType sentinel
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, float) and math.isnan(v):
        return False
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return len(v) > 0
    try:
        return len(v) > 0
    except TypeError:
        return False


# --------------------------------------------------------------------------- facade ----
# Re-export the leaf-module public API so consumers (tests + mascot_engine_widget) can keep
# importing from `vaultsprite.mascot_engine` unchanged. Everything above is already in this
# namespace via the imports at the top; this block just documents the intended public surface.
__all__ = [
    # core + state
    "MascotCore", "MascotState", "Pose", "AnimList",
    # data / pool
    "BehaviorNode", "BehaviorDef", "_PoolSource", "_NamedSource", "_GroupSources",
    "_FlatSources", "_PoolSourcesOf", "_BehaviorList", "_PoolEntry",
    # vars
    "ActionVars", "_UNDEFINED", "_UNDEFINED_TYPE2", "_DynOnce", "_coerce_literal",
    # actions
    "Action", "ActionCtx", "MalformedAction", "AnimationAction", "StayAction",
    "AnimateAction", "InPlaceAction", "MoveAction", "FallAction", "JumpAction",
    "InstantAction", "SequenceAction", "SelectAction", "ReferenceAction",
    "_NoOpAction", "_NoOpInline", "_DraggableAction", "core_view", "is_true_js",
    "strip_js_expr", "_js_truthy",
    # environment re-exports commonly used by callers
    "BORDER_TOL", "DArea", "DVec2", "ExpressionCompiler", "JSMascot",
    "MascotEnvironment", "Vec2", "parse_error",
]
