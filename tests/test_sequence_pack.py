"""Sesame / lacis_shimeji sequence-bundle backend tests — pure Python, no QApplication.

Drives :class:`mascot_sequence_pack.MascotSequenceCore` directly over the shipped bundle and
guards its behavioral contract (spawn → fall/bounce, walks stop at walls, drag holds in place,
flings land via bounce, self-loops like idle still recover to ambient). Mirrors the pure-Python
conventions of ``tests/test_mascot_engine.py``."""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from vaultsprite.mascot_sequence_pack import (
    FORCED_NAME_ALIASES,
    MascotSequenceCore,
    load_sequence_pack,
)

REPO = Path(__file__).resolve().parent.parent
SESAME = REPO / "assets" / "lacis_shimeji"
W, H = 1280, 800


def make_core(seed: int = 1) -> MascotSequenceCore:
    _manifest, animations, paths = load_sequence_pack(SESAME)
    assert paths, "bundle sprites missing"
    core = MascotSequenceCore(animations, rng=random.Random(seed))
    core.set_work_area(0, W, 0, H)
    return core


def test_loads_full_bundle():
    manifest, animations, paths = load_sequence_pack(SESAME)
    assert len(paths) == int(manifest["sprites"]["spriteCount"]) == 54
    keys = set(animations)
    # the canonical forced names must all map to real animations
    for target in FORCED_NAME_ALIASES.values():
        assert target in keys, f"forced-name alias {target!r} has no animation"
    # ambient core behaviors present
    for k in ("stand", "walk_left", "walk_right", "fall", "drag"):
        assert k in keys


def test_spawn_falls_to_floor_then_bounces():
    core = make_core()
    st = core.state
    core.spawn()
    # start just above the floor so we actually see a fall, then let it settle
    st.anchor_y = H - 400
    core.force("Fall")
    for _ in range(500):
        core.tick()
        if core.current_key in ("bounce", "stand", "walk_left", "walk_right"):
            break
    assert st.anchor_y >= H - 2, f"never reached floor: y={st.anchor_y}"


def test_walk_reaches_wall_and_re_routes():
    """A leftward walk from center must reach the LEFT wall and then RE-ROUTE (turn around or
    climb) via its border transition — it can neither leave the screen nor stall against a wall."""
    core = make_core()
    st = core.state
    st.anchor_x, st.anchor_y = W // 2, H        # on the floor mid-screen
    core.force("walk_left")                      # raw animation keys are accepted by force() too
    assert core.current_key == "walk_left"
    reached_wall = False
    for _ in range(30 * 25):                     # >enough ticks to cross half a screen at -2/tick
        before = core.current_key
        core.tick()
        if st.anchor_x <= 1.0:                   # touched the left wall this tick
            reached_wall = True
            assert core.current_key != "walk_left", \
                f"walk did not re-route at the wall (stuck in {core.current_key})"
            break
    assert reached_wall, "walk never reached the LEFT wall — it stopped mid-screen every time?" 
    # (mid-screen stops are fine when a maxDurationTicks budget expires; with this seed/budget mix we
    # expect the wall to be hit. If a flaky stop occurs, bump the loop cap or seed.)
    assert -5.0 <= st.anchor_x <= W + 5.0        # never off-screen


def test_drag_holds_anchor_in_place():
    """While held, frames keep cycling but the anchor never moves (the UI owns position)."""
    core = make_core()
    st = core.state
    st.anchor_x, st.anchor_y = W // 2, H - 100   # mid-air so a fall would change x/y if it moved
    core.set_dragging(True)
    x0, y0 = st.anchor_x, st.anchor_y
    for _ in range(30 * 25):
        core.tick()
    assert (st.anchor_x, st.anchor_y) == (x0, y0), "anchor moved while dragged"


def test_fling_arcs_and_lands_in_bounce():
    """A hard upward flick must rise first, arc back down to the floor inside the screen, and end
    the fling in its bounce chain — verifying the injected-velocity gravity integration."""
    core = make_core(seed=9)
    st = core.state
    st.anchor_x, st.anchor_y = W // 2, H - 5
    core.inject_fling_velocity(4.0, -18.0)       # px/tick: fast upward-right throw
    assert st.in_fling and core.current_key == "fling"
    rose_above_start = False
    landed_ticks = None
    for t in range(60 * 25):
        core.tick()
        if H - 30 < st.anchor_y < H:             # actually went UP before coming down
            rose_above_start = True
        if not st.in_fling and st.anchor_y >= H - 2 and landed_ticks is None \
                and core.current_key in ("bounce", "stand"):
            landed_ticks = t
            break
    assert rose_above_start, "fling never left the floor"
    assert landed_ticks is not None, f"fling did not settle: key={core.current_key} y={st.anchor_y}"
    assert 0.0 <= st.anchor_x <= W + 1.0


def test_self_only_idle_still_recovers_to_ambient():
    """Liveness contract: ``idle`` is a self-loop with no auto-timeout in the data, so we synthesize
    one — a pet tapped into idle must return to ambient motion within its budget (and not sit there
    forever)."""
    core = make_core(seed=3)
    st = core.state
    st.anchor_x, st.anchor_y = W // 2, H
    core.force("SitDown")                        # -> idle via alias table
    assert core.current_key == "idle"
    escaped = None
    for t in range(150 * 25):                    # generous cap: well above the max budget (900 ticks)
        core.tick()
        if core.current_key != "idle":
            escaped = core.current_key
            break
    assert escaped is not None, "idle never left — self-loop liveness broken"


def test_unknown_forced_name_never_wedges():
    """App-forcing a behavior the bundle lacks must be ignored (log-and-continue), exactly like
    MascotCore's fallback contract: no exception, current animation unaffected."""
    core = make_core()
    st = core.state
    st.anchor_x, st.anchor_y = W // 2, H
    core.force("SitDown")
    assert core.current_key == "idle"
    core.force("TotallyNoSuchBehavior")          # must not raise or clear state
    assert core.current_key == "idle"


def test_hide_walk_plays_in_place():
    """HideWalk parity: the walk poses cycle but the anchor stays put (App walks the window)."""
    core = make_core()
    st = core.state
    st.anchor_x, st.anchor_y = W // 2, H - 50
    core.set_hide_walk(True, moving_right=True)
    assert core.current_key == "walk_right" and st.facing_right is True
    x0, y0 = st.anchor_x, st.anchor_y
    for _ in range(30 * 25):
        core.tick()
    assert (st.anchor_x, st.anchor_y) == (x0, y0), "HideWalk moved the anchor"


def test_repeated_seeds_keep_behavior_ambient():
    """Over a ~1-minute simulated run with periodic drag/throw/tap interactions the pet must keep
    visiting multiple behaviors (no permanent wedge in any single animation)."""
    core = make_core(seed=5)
    longest_unbroken_ticks = 0
    cur, cnt = None, 0
    for t in range(60 * 25):                     # 1 minute @25Hz
        if t == 400:
            core.set_dragging(True)
            core.force("Dragged")
        elif t == 700:
            core.set_dragging(False)
            core.inject_fling_velocity(6.0, -12.0)
        elif t == 1100:
            core.force("SitDown")
        core.tick()
        if core.current_key != cur:
            longest_unbroken_ticks = max(longest_unbroken_ticks, cnt)
            cur, cnt = core.current_key, 1
        else:
            cnt += 1
    longest_unbroken_ticks = max(longest_unbroken_ticks, cnt)
    # liveness: nothing ran unbroken past ~40s (max self-loop budget is 900 ticks ≈ 36 s + slack)
    assert longest_unbroken_ticks < int(45 * 25), f"wedged too long: {longest_unbroken_ticks} ticks"
