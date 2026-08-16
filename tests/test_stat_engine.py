"""StatEngine: decay math, clamping, edge-triggered threshold signals."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QEventLoop, QTimer

from tests.conftest import FakeConfig
from vaultsprite.stat_engine import StatKind, StatEngine


@pytest.fixture()
def engine(qapp):
    cfg = FakeConfig({"stats.tick_ms": 50})   # fast ticks for tests
    eng = StatEngine(cfg)
    eng.set_active(True)
    return eng


def test_initial_values(engine):
    assert engine.get_stat(StatKind.HUNGER) == 80
    assert engine.get_stat("energy") == 90          # string form works too
    assert engine.get_stat(StatKind.BOREDOM) == 10
    assert not engine.running


def test_decay_applies_per_tick(engine, qapp):
    events = []
    engine.stat_changed.connect(lambda k, v: events.append((k, v)))
    # three ticks: hunger 80→77, energy 90→87, boredom 10→16 (clamped to >=0)
    for _ in range(3):
        engine._tick()
    assert engine.get_stat(StatKind.HUNGER) == 77
    assert engine.get_stat(StatKind.ENERGY) == 87
    assert engine.get_stat(StatKind.BOREDOM) == 16
    assert ("hunger", 79) in events and ("boredom", 12) in events


def test_clamping_at_bounds(engine):
    for _ in range(300):                            # far past the floor/ceiling
        engine._tick()
    assert engine.get_stat(StatKind.HUNGER) == 0     # lower bound
    assert engine.get_stat(StatKind.ENERGY) == 0
    assert engine.get_stat(StatKind.BOREDOM) == 100  # upper bound


def test_threshold_crossing_fires_once_per_episode(engine):
    fired = []
    engine.signal_bored.connect(lambda: fired.append("bored"))
    # push boredom straight above critical (80), tick a few times at the edge
    engine._values[StatKind.BOREDOM] = 79
    for _ in range(5):                              # stays >=80 → single crossing
        if engine.get_stat(StatKind.BOREDOM) < 100:
            engine.adjust(StatKind.BOREDOM, 2)
        else:
            break
    assert fired == ["bored"]


def test_hysteresis_rearm(engine):
    fired = []
    engine.signal_bored.connect(lambda: fired.append("x"))
    engine._values[StatKind.BOREDOM] = 95
    engine._evaluate(StatKind.BOREDOM)              # crossing → fire
    assert len(fired) == 1
    engine._values[StatKind.BOREDOM] = 70           # cleared below 80-10 margin
    engine._in_critical[StatKind.BOREDOM] = False   # emulate re-arm path check
    engine._evaluate(StatKind.BOREDOM)
    assert len(fired) == 1
    # climb back above → fires again (re-armed earlier)
    engine._values[StatKind.BOREDOM] = 85
    engine._evaluate(StatKind.BOREDOM)
    assert len(fired) == 2


def test_paused_and_inactive_skip_ticks(engine):
    engine.pause()
    before = engine.stats()
    for _ in range(3):
        engine._tick()
    assert engine.stats() == before

    engine.resume()
    engine.set_active(False)                        # PLAY context → decay off
    before2 = engine.stats()
    for _ in range(3):
        engine._tick()
    assert engine.stats() == before2


def test_adjust_emits_and_clamps(engine):
    events = []
    engine.stat_changed.connect(lambda k, v: events.append((k, v)))
    engine.adjust(StatKind.ENERGY, -999)            # hard floor at 0
    assert engine.get_stat(StatKind.ENERGY) == 0
    assert ("energy", 0) in events


def test_qtimer_tick_loop(qapp):
    """The real QTimer path fires (not just the manual _tick)."""
    cfg = FakeConfig({"stats.tick_ms": 25})
    eng = StatEngine(cfg)
    seen = []
    eng.stat_changed.connect(lambda k, v: seen.append(1))
    eng.start()
    loop_done = []

    def done():
        loop_done.append(True)
        qapp.quit()

    QTimer.singleShot(120, done)
    qapp.exec()
    eng.stop()
    assert seen, "QTimer never ticked"
