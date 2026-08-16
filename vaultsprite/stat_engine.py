"""Needs & stat decay engine (Module 3).

QTimer-driven decay of Hunger / Energy / Boredom with edge-triggered
critical-threshold signals (port of DyberPet's tier-crossing idiom, stripped
of shop/inventory/GUI per the extraction doc). Stats only tick while the pet
considers itself "active" — main wires that flag to the context detector so
decay pauses in PLAY/UNKNOWN contexts.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Mapping, Union

from PySide6.QtCore import QObject, QTimer, Signal

from .config import Config, load_config

logger = logging.getLogger(__name__)


class StatKind(str, Enum):
    HUNGER = "hunger"
    ENERGY = "energy"
    BOREDOM = "boredom"


#: stats whose critical state is a LOW value (fire when dropping below)
LOW_CRITICAL = (StatKind.HUNGER, StatKind.ENERGY)
#: stats whose critical state is a HIGH value (fire when climbing above)
HIGH_CRITICAL = (StatKind.BOREDOM,)

REARM_MARGIN = 10   # hysteresis: stat must clear the threshold by this much to re-arm


class StatEngine(QObject):
    """Decay ticker + threshold signal hub."""

    stat_changed = Signal(str, int)      # (stat name, new value) on any change
    signal_hungry = Signal()             # hunger dropped below critical
    signal_tired = Signal()              # energy dropped below critical
    signal_bored = Signal()              # boredom climbed above critical

    def __init__(self, config: Union[Config, None] = None):
        super().__init__()
        self.config = config or load_config()
        c = self.config.section("stats")

        lo, hi = (int(v) for v in c.get("bounds", [0, 100]))
        self.bounds: dict[StatKind, tuple[int, int]] = {k: (lo, hi) for k in StatKind}
        self._initial: dict[StatKind, int] = {
            k: int(c["initial"].get(k.value, lo + 5)) for k in StatKind
        }
        decay_raw: Mapping[str, Any] = c.get("decay_per_tick", {})
        self.decay: dict[StatKind, int] = {k: int(decay_raw.get(k.value, 0)) for k in StatKind}
        critical_raw: Mapping[str, Any] = c.get("critical", {})
        self.critical: dict[StatKind, int] = {
            k: int(critical_raw[k.value]) for k in StatKind if k.value in critical_raw
        }
        self.pause_when_inactive = bool(c.get("pause_when_inactive", True))

        self._values: dict[StatKind, int] = dict(self._initial)
        self._in_critical: dict[StatKind, bool] = {k: False for k in StatKind}
        self._active = True
        self._paused = False

        self._timer = QTimer(self)
        self._timer.setInterval(int(c.get("tick_ms", 60_000)))
        self._timer.timeout.connect(self._tick)

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        if not self._timer.isActive():
            self._timer.start()

    def stop(self):
        self._timer.stop()

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    def pause(self):
        """Suspend ticks (e.g. while the overlay is being dragged)."""
        self._paused = True

    def resume(self):
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    def set_active(self, active: bool):
        """Gate decay on real user presence (M5 context feeds this)."""
        if self._active == active:
            return
        self._active = active
        if not active:
            # reset edge state so the next work session fires fresh signals
            for kind in StatKind:
                self._in_critical[kind] = False

    @property
    def active(self) -> bool:
        return self._active

    # -- introspection ---------------------------------------------------------
    def get_stat(self, kind: Union[StatKind, str]) -> int:
        if isinstance(kind, str):
            kind = StatKind(kind)
        return self._values[kind]

    def stats(self) -> dict[str, int]:
        return {k.value: v for k, v in self._values.items()}

    @property
    def tick_ms(self) -> int:
        return self._timer.interval()

    # -- mutation ----------------------------------------------------------------
    def adjust(self, kind: Union[StatKind, str], delta: int):
        """Manually change a stat (feeding, petting...). Clamped + re-evaluated."""
        if isinstance(kind, str):
            kind = StatKind(kind)
        changed, new = self._apply_delta(kind, delta)
        if changed:
            self.stat_changed.emit(kind.value, new)
            self._evaluate(kind)

    def _tick(self):
        if self._paused:
            return
        if not self._active and self.pause_when_inactive:
            return
        for kind in StatKind:
            delta = self.decay.get(kind, 0)
            if not delta:
                continue
            changed, new = self._apply_delta(kind, delta)
            if changed:
                self.stat_changed.emit(kind.value, new)
            self._evaluate(kind)

    def _apply_delta(self, kind: StatKind, delta: int) -> tuple[bool, int]:
        """Apply a clamped change; returns (changed, new_value)."""
        lo, hi = self.bounds[kind]
        value = min(hi, max(lo, self._values[kind] + delta))
        changed = value != self._values[kind]
        if changed:
            self._values[kind] = value
        return changed, value

    # -- threshold logic (DyberPet tier-crossing port) ----------------------------
    def _evaluate(self, kind: StatKind):
        if kind not in self.critical:
            return
        threshold = self.critical[kind]
        value = self._values[kind]
        is_critical = value < threshold if kind in LOW_CRITICAL else value > threshold

        signal = {
            StatKind.HUNGER: self.signal_hungry,
            StatKind.ENERGY: self.signal_tired,
            StatKind.BOREDOM: self.signal_bored,
        }[kind]

        if is_critical and not self._in_critical[kind]:
            logger.info("stat %s crossed critical at %d", kind.value, value)
            self._in_critical[kind] = True
            signal.emit()
        elif not is_critical:
            # re-arm with hysteresis so a borderline value doesn't chatter
            cleared = (value > threshold + REARM_MARGIN
                       if kind in LOW_CRITICAL else
                       value < threshold - REARM_MARGIN)
            if self._in_critical[kind] and cleared:
                self._in_critical[kind] = False
