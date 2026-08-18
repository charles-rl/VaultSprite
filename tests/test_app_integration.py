"""App-level wiring: the 8 modules cooperate via signals in one process."""
from __future__ import annotations

import pytest

from tests.conftest import FakeConfig, spin


def _test_config(tmp_path, mascot=False):
    return FakeConfig({
        "obsidian.vault_root": str(tmp_path / "IntegrationVault"),
        "remote.ask_interval_ms": 0,                       # vision loop off in CI
        "health.work_threshold_min": 1,
        "stats.tick_ms": 30_000,                           # don't tick during tests
        "mascot.enabled": mascot,                          # legacy-FSM tests exercise M2; boots exercise M9
    })


def test_app_boots_and_wires(qapp, tmp_path):
    from vaultsprite.main import App

    app = App(_test_config(tmp_path, mascot=True))   # exercise the M9 mascot wiring
    app.start()                                   # drives initial mascot tick + timers
    app.window.show()

    # every module constructed and cross-referenced
    assert isinstance(app.fsm.current_state, str)
    assert app.physics._position is not None               # mover injected
    assert app.agent.model == "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q8_K_XL"                # from config
    assert app.health.threshold_minutes == 1

    # initial animation state played through the window
    assert app.window.player.is_playing or \
        not app.window.pet_label.pixmap().isNull()

    # drag lifecycle wiring: pause stats on drag, resume on release
    stats = app.stats
    app._on_drag_started()
    assert stats.paused
    app._on_drag_released(0.0, 0.0)                        # plain drop → physics.release
    assert not stats.paused

    app.shutdown()


def test_context_change_drives_stats_and_health(qapp, tmp_path):
    from vaultsprite.main import App

    app = App(_test_config(tmp_path))
    emitted = []
    app.context.context_changed.connect(lambda c: emitted.append(c))

    # simulate the detector reporting a WORK→PLAY swing (as its thread would)
    app._on_context_changed("WORK")
    assert app.stats.active and app.health._active is True

    app._on_context_changed("PLAY")
    assert not app.stats.active                            # decay paused in play
    assert not app.health._active                          # work clock reset
    app.shutdown()


def test_vault_events_written_to_temp_root(qapp, tmp_path):
    from vaultsprite.main import App

    cfg = _test_config(tmp_path)
    app = App(cfg)
    app.vault.record_event("integration boot check", source="test")
    events_dir = app.vault.events_dir
    files = list(events_dir.glob("*/*.md")) if events_dir.exists() else []
    assert files, "record_event should have created Memory/Events/<day>/<id>.md"

    app._say("hello from integration test")               # bubble path must not raise
    app.shutdown()


def test_stretch_nudge_forces_fsm_state(qapp, tmp_path):
    from vaultsprite.main import App

    app = App(_test_config(tmp_path))
    played = []
    original_play = app.window.play_state
    app.window.play_state = lambda t: (played.append(t.name), original_play(t))

    # window is not dragging/falling in this fresh state → nudge should take effect
    assert not app.window.dragging and not app.physics.falling
    app._trigger_stretch_nudge()
    assert "stretch_nudge" in played
    # FSM must be one-shot: its next weighted pick returns to a normal state
    tr = app.fsm.force_state("stretch_nudge")
    nxt = app.fsm.get_next_state(tr.name)
    assert nxt.name == "idle" or nxt.name in ("walking", "talking", "sleeping")

    app.shutdown()


def test_smoke_boot_exit_zero(qapp, tmp_path):
    """--smoke runs a short headless loop and returns 0."""
    from vaultsprite import main as main_mod

    code = main_mod.main(["--smoke"])                      # real config; ~1.5s run
    assert code == 0


# -- hide / show (walk to nearest edge, pause autonomy) ---------------------------------
def _walk_until(app, done, max_steps=400):
    steps = 0
    while not done() and steps < max_steps:
        app._hide_walk_step()
        steps += 1
    assert steps < max_steps, "hide/show walk never reached its target"


def test_hide_walks_off_screen_and_freezes_engine(qapp, tmp_path):
    from vaultsprite.main import App

    app = App(_test_config(tmp_path, mascot=True))
    app.start()
    app.window.show()
    before = app.window.position()

    app._begin_hide()
    assert app._hidden
    # the engine stays alive during the walk so the pet visibly walks off-screen
    assert app.mascot._timer.isActive() is True
    assert app.mascot._hide_walking is True
    assert app.mascot.core.state.queued_behavior == "HideWalk"  # walk frames queued

    _walk_until(app, lambda: not app.window.isVisible())
    assert not app.window.isVisible()                     # fully off-screen + hidden
    assert app.mascot._timer.isActive() is False          # ambient engine now frozen
    assert app.mascot._hide_walking is False
    app.shutdown()


def test_hidden_suppresses_bubbles_and_nudges(qapp, tmp_path):
    from vaultsprite.main import App

    app = App(_test_config(tmp_path, mascot=True))
    app.start()
    app.window.show()
    bubbles = []
    app.window.show_bubble = lambda text, *a, **k: bubbles.append(text)

    app._begin_hide()
    app._say("should not appear while hidden")
    app._trigger_stretch_nudge()
    assert bubbles == []                                  # no bubble, no nudge while hidden
    app.shutdown()


def test_show_returns_to_pre_hide_position_and_resumes_engine(qapp, tmp_path):
    from vaultsprite.main import App

    app = App(_test_config(tmp_path, mascot=True))
    app.start()
    app.window.show()
    app.window.move_to(220, 300)
    app.mascot.sync_anchor(220 + app.mascot._px // 2, 300 + app.mascot._px)
    restore = app.window.position()

    app._begin_hide()
    _walk_until(app, lambda: app._hide_target is None)

    app._begin_show()
    assert not app._hidden
    _walk_until(app, lambda: app._hide_target is None)

    assert app._hide_restore == restore                   # reveal targets the pre-hide spot
    assert app.window.isVisible()
    assert app.mascot._timer.isActive() is True           # autonomy resumed
    app.shutdown()


def test_nearest_edge_selection(qapp, tmp_path):
    from vaultsprite.main import App

    app = App(_test_config(tmp_path, mascot=True))
    app.start()
    app.window.show()
    geo = qapp.primaryScreen().availableGeometry()
    w, _ = app.window.size_px()

    app.window.move_to(geo.left() + 20, 300)              # left half → hide left
    tx, _ = app._hide_edge_target()
    assert tx == geo.left() - w

    app.window.move_to(geo.right() - 20 - w, 300)         # right half → hide right
    tx, _ = app._hide_edge_target()
    assert tx == geo.right()
    app.shutdown()


def test_injected_throw_survives_tick_refresh(qapp):
    """A1 regression: the flick velocity injected by inject_throw() must survive the next
    engine tick, whose per-tick cursor refresh used to overwrite cursor.dx/dy with the
    (tiny) live delta before the queued Thrown read ${cursor.dx} — so every flick
    degraded to a near-zero drop."""
    from vaultsprite.mascot_engine_widget import MascotEngine
    from tests.conftest import FakeConfig

    eng = MascotEngine(FakeConfig({}))
    assert eng.core is not None
    eng.inject_throw(500.0, -300.0)
    ticks = 1000.0 / eng.tick_ms
    eng._tick()                                  # the tick that consumes the queued Thrown
    assert eng.core.env.cursor.dx == pytest.approx(500.0 / ticks)
    assert eng.core.env.cursor.dy == pytest.approx(-300.0 / ticks)
    assert eng._pending_throw is False           # consume-once flag cleared


def test_mascot_engine_clamps_off_screen_targets(qapp):
    """M9 feedback: a throw/run toward an off-screen target must never leave the monitor.
    ``_clamp_pos`` now pins the ANCHOR (feet) to the work-area borders so the pet reaches the
    ceiling and side walls to grip them (no on-screen margin), while a Dash toward
    ``cursor.x``/a big ``Math.random`` TargetX can no longer walk the whole pet off-screen."""
    from PySide6.QtWidgets import QApplication
    from vaultsprite.mascot_engine_widget import MascotEngine
    from tests.conftest import FakeConfig

    eng = MascotEngine(FakeConfig({}))
    geo = QApplication.primaryScreen().availableGeometry()
    px = eng._px

    # far past the right / bottom → feet clamp onto the right wall / floor (no runaway)
    x, y = eng._clamp_pos(geo.right() + 9999, geo.bottom() + 9999)
    assert x + px // 2 == geo.right()          # feet pinned to the right wall
    assert y + px == geo.bottom()              # feet pinned to the floor

    # far past the left / top while STANDING → feet clamp onto the borders; the sprite may
    # overhang the side by half its size so it can grip, but never leaves the area entirely
    x, y = eng._clamp_pos(geo.left() - 9999, geo.top() - 9999)
    assert x + px // 2 == geo.left()           # feet pinned to the left wall
    assert y + px == geo.top()                 # standing just below the ceiling

    # gripping the ceiling: the body hangs BELOW the feet (upside down), so the window starts
    # at the anchor — fully visible, not "vanished above the ceiling"
    eng._on_ceiling = True
    x, y = eng._clamp_pos(geo.right() + 9999, geo.top() - 9999)
    assert x + px // 2 == geo.right()
    assert y == geo.top()                      # window top == anchor → body visible below


def test_mascot_interpolation_smooths_window_moves(qapp):
    """M9 feedback: throws read as 'frame-by-frame' judder because the window moved once
    per 25 Hz engine tick in large steps. The interpolation layer subdivides each engine
    target into small eased moves so successive positions never jump the full distance."""
    from vaultsprite.mascot_engine_widget import MascotEngine
    from tests.conftest import FakeConfig

    eng = MascotEngine(FakeConfig({}))
    eng._smooth = True
    eng._pos_cur = (0, 0)
    moves: list[tuple[int, int]] = []
    eng.position_changed.connect(lambda x, y: moves.append((x, y)))

    eng._set_target(400, 300)
    guard = 0
    while eng._interp_timer.isActive() and guard < 1000:
        eng._interp_step()
        guard += 1

    assert moves and moves[-1] == (400, 300)          # arrives at the target
    assert len(moves) >= 3                            # subdivided into multiple steps
    max_delta = max(abs(moves[i][0] - moves[i - 1][0]) + abs(moves[i][1] - moves[i - 1][1])
                    for i in range(1, len(moves)))
    assert max_delta < 400                            # no single full-distance teleport


def test_mascot_respawn_recentres_and_forces_breed_gag(qapp):
    """M9 feedback: changing the scale mid-animation left the pet floating/jumping. respawn()
    recentres the anchor on the floor and replays the breed 'spawned a new version' flourish
    (PullUpShimeji gag + fall) so the resized pet visibly settles instead of glitching."""
    from vaultsprite.mascot_engine_widget import MascotEngine
    from tests.conftest import FakeConfig

    eng = MascotEngine(FakeConfig({}))
    eng.core.state.anchor.x = 10.0
    eng.core.state.anchor.y = 10.0
    eng.respawn()

    wa = eng._env.work_area
    assert eng.core.state.anchor.x == pytest.approx((wa.left + wa.right) / 2)
    assert eng.core.state.anchor.y == pytest.approx(eng._env.floor.y)
    assert eng.core.state.queued_behavior == "PullUpShimeji"


def test_scale_change_triggers_respawn_unless_hidden(qapp, tmp_path, monkeypatch):
    """The tray scale control routes through _on_scale_changed → MascotEngine.respawn()
    so a resize always re-settles the pet; while hidden the respawn is skipped."""
    from vaultsprite.main import App

    app = App(_test_config(tmp_path, mascot=True))
    calls = []
    monkeypatch.setattr(app.mascot, "respawn", lambda: calls.append(1))
    app._on_scale_changed(1.2)              # a size change re-settles the pet
    assert calls == [1]
    app._hidden = True
    app._on_scale_changed(1.5)
    assert calls == [1]                     # hidden → no respawn
    app.shutdown()
