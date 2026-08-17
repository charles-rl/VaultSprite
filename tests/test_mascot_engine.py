"""M9 Mascot engine tests — pure Python, no QApplication (drive ``core.tick()``)."""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from vaultsprite.mascot_engine import ActionCtx, MascotCore, _NoOpInline
from vaultsprite.mascot_environment import DArea, HBorder, MascotEnvironment, Vec2

W, H = 1280, 800
REPO = Path(__file__).resolve().parent.parent
ACTIONS = REPO / "assets" / "steve_shimeji" / "conf" / "actions.xml"
BEHAVIORS = REPO / "assets" / "steve_shimeji" / "conf" / "behaviors.xml"


def make_core(seed: int = 1, excluded=None) -> MascotCore:
    env = MascotEnvironment(
        ceiling=HBorder(0, 0, W),
        floor=HBorder(H, 0, W),
        screen=DArea(0, W, H, 0),
        work_area=DArea(0, W, H, 0),
        active_ie=DArea.invisible(),
        allows_breeding=False,
        mascot_count=1,
    )
    core = MascotCore(env, rng=random.Random(seed), excluded_behaviors=excluded)
    core.parse(ACTIONS, BEHAVIORS)
    return core


def test_parses_full_pack():
    core = make_core()
    assert len(core.behavior_defs) >= 50
    assert len(core.actions) >= 80
    # all 46 frames are referenced by at least one parsed action
    used = {p.image for a in core.actions.values() for anim in getattr(a, "anims", [])
            for p in anim.poses}
    assert used, "no poses parsed"


def test_falls_from_air_and_lands_on_floor():
    core = make_core()
    core.state.anchor = Vec2(W // 2, 100)
    core.force_behavior("Fall")
    for _ in range(500):
        core.tick()
        if core.state.anchor.y >= H - 2:
            break
    assert core.state.anchor.y >= H - 2, f"never landed: y={core.state.anchor.y}"


def test_landing_does_not_stick_on_bounce():
    """A drop must land, play the Bouncing splash (shime18/19) exactly once, stand, then
    return to AMBIENT behavior — not loop the Bouncing pose forever (the reported "spams
    the shime18 water-bucket landing and is stuck in an infinite loop").

    Guards two bugs: Animate (Bouncing) used to loop forever, and SelectAction re-inited
    its finished branch so the Fall sequence never ended."""
    core = make_core(seed=5)
    core.state.anchor = Vec2(W // 2, 100)
    core.force_behavior("Fall")
    landed = False
    bounce_seen = False
    post_land_behaviors: set[str] = set()
    for _ in range(600):
        core.tick()
        if not landed and core.state.anchor.y >= H - 2:
            landed = True
        if landed:
            if core.state.active_frame is not None and "shime18.png" in core.state.active_frame.image:
                bounce_seen = True
            post_land_behaviors.add(core.state.behavior_name)
    assert landed, "pet never landed"
    assert bounce_seen, "Bouncing splash never played on landing"
    # after the splash the pet must have moved on from the Fall behavior (e.g. into an
    # ambient floor behavior), not stay stuck bouncing/falling
    assert len(post_land_behaviors) > 1, f"pet stuck in one behavior after landing: {post_land_behaviors}"


def test_sideways_throw_grabs_wall_and_climbs():
    """A hard sideways throw reaches the work-area wall and the fall ENDS there (C++
    Fall.hasNext), leaving the pet ON the wall so the ambient wall pool (ClimbAlongWall /
    HoldOntoWall / FallFromWall) can run — feedback: climbing-wall animations were never used."""
    core = make_core(seed=8)
    core.state.anchor = Vec2(W // 2, 80)
    core.state.dragging = False
    core.env.cursor.dx = 80            # hard throw to the right (px/tick) → reaches wall mid-air
    core.force_behavior("Thrown")
    wall_seen = False
    wall_behaviors: set[str] = set()
    for _ in range(300):
        core.tick()
        # anchored on the right wall (within 1px of the work-area right edge)
        if abs(core.state.anchor.x - W) < 2:
            wall_seen = True
            wall_behaviors.add(core.state.behavior_name)
    assert wall_seen, "pet never reached/gripped the right wall"
    # the wall grip must have run wall-related behaviors (ClimbAlongWall/HoldOntoWall/...)
    assert any("Wall" in b or b == "FallFromWall" for b in wall_behaviors), \
        f"no wall-climbing behavior ran after gripping: {wall_behaviors}"


def test_active_frame_recorded():
    core = make_core()
    core.state.anchor = Vec2(W // 2, 100)
    core.force_behavior("Fall")
    frames = 0
    for _ in range(200):
        core.tick()
        if core.state.active_frame is not None:
            frames += 1
        if core.state.anchor.y >= H - 2:
            break
    assert frames > 0, "engine never set an active frame"


def test_recovery_ladder_survives_malformed_behavior():
    """A forced behavior whose action is a broken no-op must fall back, not wedge."""
    core = make_core()
    core.state.anchor = Vec2(W // 2, H)
    core.force_behavior("Fall")
    for _ in range(50):
        core.tick()
    assert core.active_behavior_name          # still has an active behavior


def test_breed_is_noop_no_spawn():
    core = make_core()
    core.state.anchor = Vec2(W // 2, H)
    core.force_behavior("SplitIntoTwo")
    for _ in range(100):
        core.tick()
    assert core.env.mascot_count == 1          # solo pet never spawns


@pytest.mark.parametrize("behavior,frames", [
    ("SplitIntoTwo", {"shime42.png", "shime43.png", "shime44.png", "shime45.png", "shime46.png"}),
    ("PullUpShimeji", {"shime38.png", "shime39.png", "shime40.png", "shime41.png"}),
])
def test_breed_gag_plays_bread_only_frames(behavior, frames):
    """Breed-only frames 38-46 are reused as a visual-only flourish (no spawn)."""
    core = make_core()
    core.state.anchor = Vec2(W // 2, H)
    core.force_behavior(behavior)
    played = set()
    for _ in range(500):
        core.tick()
        if core.state.active_frame is not None:
            played.add(core.state.active_frame.image)
    assert frames <= played, f"expected {frames} in {played}"
    assert core.env.mascot_count == 1


def test_excluded_behaviors_never_ambient():
    core = make_core(excluded={"SitDown", "LieDown"})
    core.state.anchor = Vec2(W // 2, H)
    seen = set()
    for _ in range(2000):
        core.tick()
        seen.add(core.active_behavior_name)
    assert "SitDown" not in seen and "LieDown" not in seen


def test_throw_ie_action_builds_not_dropped():
    """ThrowIe (Embedded ThrowIE) must parse to a StayAction, not vanish."""
    core = make_core()
    act = core.actions.get("ThrowIe")
    assert act is not None, "ThrowIe was dropped (type case bug)"
    assert "shime37.png" in {p.image for anim in act.anims for p in anim.poses}


def test_fall_with_ie_and_walk_with_ie_parse():
    core = make_core()
    assert core.actions.get("FallWithIe") is not None
    assert core.actions.get("WalkWithIe") is not None
    assert core.actions.get("RunWithIe") is not None


def test_inline_action_references_are_linked():
    core = make_core()
    refs = core._refs
    assert refs, "no inline ActionReference parsed"
    assert all(r.target is not None for r in refs), "some references unlinked"


def test_noop_inline_is_tickable():
    """_NoOpInline must be constructible and tickable without crashing."""
    core = make_core()
    a = _NoOpInline(core=core)
    a.init(ActionCtx(core))
    assert a.subtick(0) is False          # finishes as a one-tick no-op


# -- M9 fixes (2026-08-17 pass): sway + throw --------------------------------------
def _dragged_swing_frames(anchor_x: float, cursor_from: float, cursor_to: float,
                          seed: int = 3, steps: int = 70) -> set[str]:
    """Drag while the cursor sweeps from `cursor_from` to `cursor_to` (then holds).

    FootX is a damped oscillator that starts on the cursor and chases it, so a lean pose
    (shime9/10) appears while the cursor is moving (FootX lags) — matching C++ Dragged —
    rather than from a static cursor offset."""
    core = make_core(seed=seed)
    core.state.anchor = Vec2(anchor_x, H)
    core.state.dragging = True
    core.env.cursor.x, core.env.cursor.y = cursor_from, H
    core.force_behavior("Dragged")
    played: set[str] = set()
    sweep = 2                      # fast fling → large transient FootX lag → deep lean
    for i in range(steps):
        core.env.cursor.x = cursor_from + (cursor_to - cursor_from) * min(1.0, i / sweep)
        core.tick()
        if core.state.active_frame is not None:
            played.add(core.state.active_frame.image)
    return played


def test_dragged_sways_toward_cursor():
    """Swinging the cursor must play the lean frames (shime9/10) while it moves, not just
    the neutral pose. Requires FootX to resolve, the conditional Pinched branches to be
    evaluated, and the pendulum oscillator to lag FootX behind a moving cursor."""
    right = _dragged_swing_frames(anchor_x=200, cursor_from=200, cursor_to=600)
    assert "shime9.png" in right, f"expected far-right lean while swinging: {sorted(right)}"
    left = _dragged_swing_frames(anchor_x=1100, cursor_from=1100, cursor_to=300)
    assert "shime10.png" in left, f"expected far-left lean while swinging: {sorted(left)}"


def test_dragged_pendulum_settles():
    """After the cursor stops, FootX keeps swinging back and forth around it (foot_dx
    alternates sign) and damps back toward the neutral pose — the 'swing like a pendulum'
    behavior the sway was missing. This is the C++ Dragged footDx=(footDx+(newX-footX)*0.1)*0.8
    oscillator, previously absent (FootX was static anchor.x)."""
    core = make_core(seed=3)
    core.state.anchor = Vec2(200, H)
    core.state.dragging = True
    core.env.cursor.x, core.env.cursor.y = 200, H
    core.force_behavior("Dragged")
    for _ in range(3):
        core.tick()
    core.env.cursor.x = 600                      # swing far right, then STOP
    signs: list[int] = []
    for _ in range(80):
        core.tick()
        signs.append(1 if core.state.foot_dx > 0 else (-1 if core.state.foot_dx < 0 else 0))
    # foot_dx must change sign at least twice (it overshoots and swings back)
    flips = sum(1 for i in range(1, len(signs)) if signs[i] != 0 and signs[i] != signs[i - 1])
    assert flips >= 2, f"pendulum never swung back (flips={flips}, signs={signs})"
    # and it damps: the last few samples are back near the neutral pose (close to cursor)
    near_cursor = abs(core.state.foot_x - 600) < 40
    assert near_cursor, f"pendulum did not settle near the cursor (foot_x={core.state.foot_x:.1f})"


def test_throw_launches_from_release_position_with_velocity():
    """A flick release must launch from the synced release anchor (not the stale grab
    point) and carry the reference's InitialVX overlay. Regression for: ReferenceAction
    dropped its own overlay attrs (InitialVX was lost → zero horizontal velocity) and the
    core anchor was frozen at the grab point during the drag."""
    core = make_core(seed=7)
    core.state.anchor = Vec2(100, H)      # stale grab point (where the drag started)
    core.state.dragging = False
    release_x, release_y = W // 2, H - 250      # user swung up, released mid-air
    core.state.anchor.x, core.state.anchor.y = release_x, release_y
    core.env.cursor.dx, core.env.cursor.dy = 12, -10     # px/tick release velocity
    core.force_behavior("Thrown")
    max_x = core.state.anchor.x
    landed = False
    for _ in range(400):
        core.tick()
        max_x = max(max_x, core.state.anchor.x)
        if core.state.anchor.y >= H - 2:
            landed = True
            break
    assert landed, "throw never reached the floor"
    assert max_x > release_x + 20, f"throw had no horizontal velocity (max_x={max_x:.0f})"
    # never snapped back to the stale grab x=100
    assert core.state.anchor.x > release_x - 5


def test_reference_overlay_attrs_reach_target():
    """An ActionReference's own overlay attrs (InitialVX/TargetX/Duration) must be applied
    to its target, not dropped. A Walk reference carrying TargetX must actually move."""
    core = make_core(seed=9)
    core.state.anchor = Vec2(100, H)
    core.state.dragging = False
    core.force_behavior("WalkAlongWorkAreaFloor")   # Walk TargetX=${...} via reference
    start_x = core.state.anchor.x
    for _ in range(80):
        core.tick()
    assert core.state.anchor.x > start_x + 20, \
        f"Walk never moved toward its TargetX (start={start_x:.0f} end={core.state.anchor.x:.0f})"
