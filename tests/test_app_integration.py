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
