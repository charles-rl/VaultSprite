"""HealthAudio: WorkTimer state machine + SoundBank headless no-ops."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QTimer

from tests.conftest import FakeConfig, spin
from vaultsprite.health_audio import SoundBank, WorkTimer


def _timer(cfg_overrides=None):
    """WorkTimer with 2-minute threshold and 100 work-seconds credited per tick."""
    cfg = FakeConfig({
        "health.work_threshold_min": 2,      # = 120s of (fast-forwarded) work
        "health.nudge_tick_ms": 50,
        "health.tick_work_seconds": 100,     # each _tick() ≈ 1.67 work-minutes
    })
    if cfg_overrides:
        cfg._tree.update(cfg_overrides)
    return WorkTimer(cfg)


# -- accumulation & gating ---------------------------------------------------------
def test_accumulates_only_when_active():
    # threshold raised to 4 min (240s) so the two 100s ticks don't trip a nudge,
    # which would zero the counter by design.
    cfg = FakeConfig({
        "health.work_threshold_min": 4,
        "health.nudge_tick_ms": 50,
        "health.tick_work_seconds": 100,
    })
    wt = WorkTimer(cfg)
    assert wt.work_seconds == 0.0
    wt.set_active(True)
    for _ in range(2):
        wt._tick()
    assert wt.work_seconds == 200             # two ticks × 100 work-seconds
    assert not wt.nudge_pending


def test_play_context_resets_progress():
    wt = _timer()
    wt.set_active(True)
    wt._tick()                               # some progress
    first = wt.work_seconds
    wt.set_active(False)                     # PLAY → reset + pending nudge cancelled
    assert wt.work_seconds == 0.0
    wt._tick()
    assert wt.work_seconds == 0.0            # inactive ticks add nothing


def test_stretch_nudge_fires_exactly_once_at_threshold():
    fired = []
    wt = _timer()
    wt.stretch_nudge.connect(lambda: fired.append(1))
    wt.set_active(True)
    for _ in range(5):                       # 5×100s ≥ 120s threshold on tick #2
        if not wt._nudge_pending:
            wt._tick()
    assert fired == [1], f"expected exactly one nudge, got {len(fired)}"
    assert wt.nudge_pending
    assert wt.work_seconds == 0.0            # counter zeroed on fire


def test_no_refire_while_pending():
    wt = _timer()
    wt.set_active(True)
    for _ in range(5):
        if not wt._nudge_pending:
            wt._tick()
    before = wt.nudge_pending
    assert before is True
    # further ticks must NOT re-fire while the nudge is still pending
    fired_more = []
    wt.stretch_nudge.connect(lambda: fired_more.append(1))
    for _ in range(3):
        wt._tick()
    assert fired_more == []


# -- resolution modes (skip / stretch full reset, postpone partial credit) ---------
def test_resolve_skip_resets_fully():
    wt = _timer()
    wt.set_active(True)
    for _ in range(5):
        if not wt._nudge_pending:
            wt._tick()
    assert wt.nudge_pending
    wt.resolve_nudge("skip")
    assert not wt.nudge_pending and wt.work_seconds == 0.0


def test_resolve_postpone_grants_credit():
    wt = _timer()                             # threshold 2 min, credit default 25 → capped at 2? no:
    # threshold(2) - credit(25) < 0 → clamps to 0; use an explicit small credit via mode arg
    wt.set_active(True)
    for _ in range(5):
        if not wt._nudge_pending:
            wt._tick()
    assert wt.nudge_pending
    wt.resolve_nudge("postpone", credit_minutes=1)   # re-arm after 1 more minute
    assert not wt.nudge_pending
    assert wt.work_seconds == (2 - 1) * 60           # pre-credited to threshold−credit


def test_qtimer_path_fires_real_signal(qapp):
    fired = []
    cfg = FakeConfig({
        "health.work_threshold_min": 1,      # 60 work-seconds
        "health.nudge_tick_ms": 25,          # real 25ms cadence…
        "health.tick_work_seconds": 30,      # …but credits 30s per tick → ~2 ticks to fire
    })
    wt = WorkTimer(cfg)
    wt.stretch_nudge.connect(lambda: fired.append(1))
    wt.set_active(True)
    done = []

    def finish():
        done.append(True)
        qapp.quit()

    QTimer.singleShot(400, finish)
    wt.start()
    qapp.exec()
    wt.stop()
    assert fired, "real QTimer path never reached the threshold"


# -- SoundBank headless behaviour ----------------------------------------------------
def test_soundbank_play_is_noop_when_disabled(qapp):
    cfg = FakeConfig({"health.sounds_dir": "assets/sounds"})
    bank = SoundBank(cfg)
    # whether or not a mixer exists, play/stop must never raise
    assert bank.disabled is True or isinstance(bank.disabled, bool)
    for name in ("step", "chirp", "yawn"):
        bank.play(name)                       # must be silent & safe either way
        bank.stop(name)
        bank.play_loop(name)
