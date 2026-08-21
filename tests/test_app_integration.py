"""App-level wiring: the 8 modules cooperate via signals in one process."""
from __future__ import annotations

import time

import pytest

from tests.conftest import FakeConfig, spin


def _test_config(tmp_path, mascot=False):
    return FakeConfig({
        "obsidian.vault_root": str(tmp_path / "IntegrationVault"),
        "remote.ask_interval_ms": 0,                       # vision loop off in CI
        "health.work_threshold_min": 1,
        "stats.tick_ms": 30_000,                           # don't tick during tests
        "stats.persist": False,                            # integration App.stop() must not write repo state
        "mascot.enabled": mascot,                          # legacy-FSM tests exercise M2; boots exercise M9
    })


class _FakeVisionAgent:
    """Test double for RemoteAgent — simulates the QThread round-trip by calling the
    app's slots directly (the real agent's Qt signal wiring is covered in its own file)."""

    enabled = True
    model = "test-model"
    last_error_at = 0.0

    def __init__(self, fail=None):
        self.fail = fail                # exception class to raise on ask()
        self.in_flight_state = False
        self.asked: list[str] = []

    @property
    def in_flight(self):
        return self.in_flight_state

    def ask(self, prompt, window_context=""):      # mirrors the real agent: dispatch ⇒ in flight
        if self.fail is not None:
            raise self.fail("boom")
        self.asked.append(prompt)
        self.in_flight_state = True

    def cancel_inflight(self):
        self.in_flight_state = False


def _swap_agent(app, fake=None):
    """Replace App.agent with the stub (real one is a 120s-remote client — CI must not touch it)."""
    app.agent = fake if fake is not None else _FakeVisionAgent()


def _spin_until(qapp, cond, timeout_s=3.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


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
    ``_clamp_pos`` pins the ANCHOR so the WHOLE sprite window stays in the work area (2026-08-21:
    full-canvas packs like kazeem/dieter/sesame were half-clipped at the side walls when the feet
    could reach them) — always drawn UPRIGHT — while a Dash toward ``cursor.x``/a big
    ``Math.random`` TargetX can no longer walk the whole pet off-screen. The vertical clamps are
    unchanged: head touches the top edge, feet touch the floor."""
    from PySide6.QtWidgets import QApplication
    from vaultsprite.mascot_engine_widget import MascotEngine
    from tests.conftest import FakeConfig

    eng = MascotEngine(FakeConfig({}))
    geo = QApplication.primaryScreen().availableGeometry()
    px = eng._px

    # Qt's right()/bottom() are INCLUSIVE (last pixel); the work area ends at left+width.
    # far past the right / bottom → the window sits flush against the right wall and its last
    # row on the floor line (no runaway; nothing overhangs a side wall)
    x, y = eng._clamp_pos(geo.right() + 9999, geo.bottom() + 9999)
    assert x + px == geo.left() + geo.width()   # whole sprite window inside at the right wall
    assert y + px == geo.top() + geo.height()   # feet pinned to the floor line

    # far past the left / top → upright and fully on-screen: left edge at geo.left(), top at
    # geo.top() (the head touches the screen top, nothing overhangs the left side)
    x, y = eng._clamp_pos(geo.left() - 9999, geo.top() - 9999)
    assert x == geo.left()                     # whole sprite window inside at the left wall
    assert y == geo.top()                      # sprite top at the screen top (still visible)


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
    (PullUpShimeji gag + fall) so the resized pet visibly settles instead of glitching.

    Pinned to the ``steve`` pack: PullUpShimeji is its breed flourish, and other packs (e.g.
    kazeem) don't define that behavior name — respawn must then degrade to a plain Fall."""
    from vaultsprite.mascot_engine_widget import MascotEngine
    from tests.conftest import FakeConfig

    eng = MascotEngine(FakeConfig({"mascot.pack": "steve"}))
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


# -- A2: vision error/timeout UX + autonomous fail-cooldown --------------------------------
def test_vision_error_shows_fallback_and_clears_pending(qapp, tmp_path):
    from vaultsprite.main import App

    app = App(_test_config(tmp_path))
    app.window.show()                       # _say only bubbles when the window is visible
    bubbles = []
    app.window.show_bubble = lambda text, *a, **k: bubbles.append(text)
    fake = _FakeVisionAgent()
    _swap_agent(app, fake)

    app._ask_vision("what am I doing?")
    assert app._vision_pending and "Thinking\u2026" in bubbles[-1]
    # worker thread fails (queued error signal arrives on the next event-loop pass)
    spin(qapp, lambda: True, timeout_s=0.05)   # pump so queued signals can't sneak in first
    app._on_agent_error("connection reset by peer")
    assert not app._vision_pending             # App state cleared…
    assert bubbles[-1] == "Sorry — I can't see clearly right now."     # …and bubble replaced

    # late reply after the error must be ignored (no stale overwrite)
    app._on_agent_reply("stale answer from 2 minutes ago")
    assert bubbles[-1] == "Sorry — I can't see clearly right now."
    app.shutdown()


def test_vision_timeout_releases_gate_and_shows_fallback(qapp, tmp_path):
    from vaultsprite.main import App

    cfg = _test_config(tmp_path)
    cfg._tree["remote"]["llm_timeout_s"] = 0.5   # small → a short watchdog margin for the test
    app = App(cfg)
    app.window.show()                       # _say only bubbles when the window is visible
    bubbles = []
    app.window.show_bubble = lambda text, *a, **k: bubbles.append(text)

    class _HungFake(_FakeVisionAgent):
        def ask(self, prompt, window_context=""):
            self.in_flight_state = True          # hang forever — never delivers either signal

    fake = _HungFake()
    _swap_agent(app, fake)

    app._ask_vision("what am I doing?")
    assert app.agent.in_flight and app._vision_pending
    assert "Thinking\u2026" in bubbles[-1]

    # simulate the watchdog firing (its timer wiring is a plain 2-line connect; here we invoke
    # the slot directly, as timeout would once its interval elapses):
    app._on_vision_timeout()
    assert not fake.in_flight                    # gate released for the next ask…
    assert not app._vision_pending               # …and the stale-hung reply can no longer take over UX
    assert bubbles[-1] == "Sorry — I can't see clearly right now."

    # a late reply that finally arrives must be ignored, not shown:
    app._on_agent_reply("way too late answer")
    assert bubbles[-1] == "Sorry — I can't see clearly right now."
    app.shutdown()


def test_autonomous_vision_tick_respects_fail_cooldown(qapp, tmp_path):
    from vaultsprite.main import App

    app = App(_test_config(tmp_path))
    fake = _FakeVisionAgent()
    _swap_agent(app, fake)
    # fresh window: not dragging, app._hidden False → the tick guard is open

    # a recent failure puts autonomous asks into cooldown…
    fake.last_error_at = time.monotonic()
    app._vision_tick()
    assert not fake.asked                        # suppressed during the 300s default window
    # …but once stale they run again.
    fake.last_error_at = time.monotonic() - 400
    app._vision_tick()
    assert len(fake.asked) == 1 and "screen" in fake.asked[0]
    app.shutdown()


# -- A3: stretch-nudge resolution paths -----------------------------------------------------
def _armed_nudge_app(qapp, tmp_path, threshold_min=None):
    """App whose WorkTimer is genuinely at-threshold pending (fast-forwarded credits).

    The firing `stretch_nudge` signal travels the REAL wiring (WorkTimer → App), so when
    this returns, App._stretch_nudge_active is already set — production behavior, not a stub.
    """
    from vaultsprite.main import App

    cfg = _test_config(tmp_path)                          # default: health.work_threshold_min=1
    if threshold_min is not None:
        cfg._tree["health"]["work_threshold_min"] = threshold_min
    cfg._tree["health"]["tick_work_seconds"] = 40         # fast-forward: each tick credits 40 work-seconds
    app = App(cfg)
    app.health.set_active(True)                           # as _on_context_changed("WORK") would do
    while not app.health.nudge_pending:                   # ticks until the real signal fires…
        app.health._tick()
    return app


def test_stretch_nudge_resolved_by_pet_click(qapp, tmp_path):
    from vaultsprite.main import App   # noqa: F401 (imported for parity with sibling tests)

    app = _armed_nudge_app(qapp, tmp_path)
    assert app.health.nudge_pending
    assert app._stretch_nudge_active             # set by the real WorkTimer→App signal path

    # pet click while the nudge is active → full resolution, NOT the +4 energy petting path
    energy_before = app.stats.get_stat("energy")
    app._on_pet_clicked()
    assert not app.health.nudge_pending           # timer re-armed for the next cycle
    assert not app._stretch_nudge_active          # App-level flag cleared in lockstep
    assert app.stats.get_stat("energy") == energy_before   # no +4 petting happened


def test_stretch_nudge_postponed_by_tray_dismiss(qapp, tmp_path):
    from vaultsprite.main import App

    # 30-min threshold so postpone's default 25-min partial credit is observable (>0);
    # with the tiny 1-min CI threshold it clamps to a full reset by design.
    app = _armed_nudge_app(qapp, tmp_path, threshold_min=30)
    app.window.show()                             # bubbles only appear while visible
    assert app._stretch_nudge_active              # arming already fired via the signal path
    bubbles = []
    app.window.show_bubble = lambda text, *a, **k: bubbles.append(text)

    app._on_dismiss()                             # tray 'Dismiss' while pending → postpone
    assert not app.health.nudge_pending           # resolve_nudge("postpone") ran…
    assert app.health.work_seconds == (30 - 25) * 60   # …with partial credit back (next nudge sooner)
    assert not app._stretch_nudge_active
    assert any("soon" in b for b in bubbles)


def test_leaving_work_context_cancels_stretch_nudge(qapp, tmp_path):
    from vaultsprite.main import App

    app = _armed_nudge_app(qapp, tmp_path)
    assert app._stretch_nudge_active              # already armed via the signal path

    app._on_context_changed("PLAY")               # user walked away from work → auto-cancel
    assert not app._stretch_nudge_active, "pending nudge must clear when leaving WORK"


def test_pet_click_without_pending_nudge_is_plain_petting(qapp, tmp_path):
    from vaultsprite.main import App

    app = App(_test_config(tmp_path))
    assert not app._stretch_nudge_active
    energy_before = app.stats.get_stat("energy")
    app._on_pet_clicked()
    assert app.stats.get_stat("energy") == min(100, energy_before + 4)   # original behavior intact


# -- T1: ambient SFX wiring (M8 doc §5.4 — walking→step loop, resting→yawn once) ---------
def _stub_sounds(app):
    class _S:
        def __init__(self):
            self.played = []      # one-shots by name

        def play(self, name, *a, **k):
            self.played.append(name)

        def stop(self, name, *a, **k):
            self.played.append(f"stop:{name}")

        def play_loop(self, name, *a, **k):
            self.played.append(f"loop:{name}")

    app.sounds = _S()
    return app.sounds


def test_walk_states_start_step_loop_and_others_stop_it(qapp, tmp_path):
    from vaultsprite.main import App   # noqa: F401 (parity with sibling tests)

    app = App(_test_config(tmp_path))
    s = _stub_sounds(app)
    app._hidden = False
    app._ambient_sounds_for("WalkAlongWorkAreaFloor")      # M9 walk behavior
    assert "loop:step" in s.played
    app._ambient_sounds_for("RunAlongIECeiling")           # also a walk (run) — re-assert, idempotent
    assert s.played.count("loop:step") >= 2
    app._ambient_sounds_for("SitDown")                     # any non-walk state stops the loop
    assert "stop:step" in s.played[-3:]


def test_legacy_fsm_state_names_drive_the_same_hooks(qapp, tmp_path):
    from vaultsprite.main import App   # noqa: F401

    app = App(_test_config(tmp_path))
    s = _stub_sounds(app)
    app._hidden = False
    app._ambient_sounds_for("walking")          # legacy transition name (assets/config.yaml state key)
    assert "loop:step" in s.played
    app._ambient_sounds_for("sleeping")         # legacy resting state → yawn, and stops the loop
    assert "yawn" in s.played and "stop:step" in s.played


def test_yawn_fires_once_per_rest_episode_then_cooldown(qapp, tmp_path):
    from vaultsprite.main import App   # noqa: F401

    cfg = _test_config(tmp_path)
    cfg._tree["health"]["yawn_cooldown_s"] = 300
    app = App(cfg)
    s = _stub_sounds(app)
    app._hidden = False
    app._ambient_sounds_for("LieDown")          # M9 rest behavior → one yawn
    assert s.played.count("yawn") == 1
    app._ambient_sounds_for("SitDown")          # left resting (no second yawn source)
    app._ambient_sounds_for("LieDown")          # back to rest within cooldown → NO repeat yawn
    assert s.played.count("yawn") == 1, "cooldown must gate a second yawn in the same episode window"


# -- T2: telemetry inspector shows live stat values -----------------------------------------
def test_telemetry_overlay_includes_live_stats(qapp, tmp_path):
    """T2: the stat values (previously emitted but consumed nowhere) now show in the inspector."""
    from vaultsprite.main import App

    app = App(_test_config(tmp_path))            # legacy mode; _mascot_telemetry handles mascot=None
    d = app._mascot_telemetry()                  # getter now carries stat_* keys alongside pos/behavior/frame
    assert any(str(k).startswith("stat_") for k in d), f"telemetry dict lacks stat fields: {d.keys()}"

    from vaultsprite.ui_overlay import TelemetryOverlay
    ov = TelemetryOverlay()
    try:
        ov.start(lambda: d)                      # one deterministic refresh (500 ms timer also fires; harmless)
        text = ov._label.text()
        assert "hunger=" in text and "energy=" in text, \
            f"telemetry label must show live stat values, got:\n{text}"
    finally:
        ov.close()                               # closes our overlay (App's own would be closed by shutdown)
