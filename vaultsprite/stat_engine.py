"""Needs & stat decay engine (Module 3).

QTimer-driven decay of Hunger / Energy / Boredom with edge-triggered
critical-threshold signals (port of DyberPet's tier-crossing idiom, stripped
of shop/inventory/GUI per the extraction doc). Stats only tick while the pet
considers itself "active" — main wires that flag to the context detector so
decay pauses in PLAY/UNKNOWN contexts.
"""
from __future__ import annotations

import json
import logging
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Union

from PySide6.QtCore import QObject, QTimer, Signal

from .config import Config, load_config

logger = logging.getLogger(__name__)


def _atomic_write_json(path: "Path | str", obj: dict) -> None:
    """Best-effort atomic JSON write (dot-temp + rename), never raises.

    Deliberately independent of M7's vault writer: this is the engine's own scratch
    state, not Obsidian memory — so no sandbox guard applies here and a full disk can
    at most lose this session's stats, never crash the pet."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort by contract
        logger.debug("stat state write failed (%s): %s", p, exc)


def _load_stat_state(path: "Path | str") -> dict[str, int] | None:
    """Read a prior stat snapshot keyed by StatKind value.

    Returns {} on a missing file (first launch), the loaded values when valid, and None
    when corrupt/unusable — callers then fall back to config initial values."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {k.value: int(raw[k.value]) for k in StatKind}   # raises on any bad key/value
    except Exception as exc:  # noqa: BLE001 - corrupt file tolerated (DyberPet idiom)
        logger.warning("stat state %s unreadable (%s); resetting to config initial", p, exc)
        return None


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

        # -- cross-launch persistence (config-driven; off by default) ---------------
        # Snapshot lives at `stats.state_path` — deliberately OUTSIDE the Obsidian vault
        # so pet memory stays clean engine state, not journal pollution.
        self.persist_enabled = bool(c.get("persist", False))
        raw_path = c.get("state_path") or "state/stats.json"
        self._state_path: Path = (Path(self.config.root) / raw_path
                                  if not Path(raw_path).is_absolute() else Path(raw_path))

        self._values: dict[StatKind, int] = dict(self._initial)
        loaded = _load_stat_state(self._state_path) if self.persist_enabled else {}
        if loaded is None:                      # corrupt snapshot → keep config initial
            pass
        elif loaded:                            # non-empty → hydrate (clamped to bounds)
            for k in StatKind:
                lo, hi = self.bounds[k]
                v = min(hi, max(lo, int(loaded.get(k.value, self._initial[k]))))
                if v != self._values[k]:
                    logger.info("stat %s restored from state file: %d → %d", k.value,
                                self._values[k], v)
                    self._values[k] = v
        self._in_critical: dict[StatKind, bool] = {k: False for k in StatKind}
        # re-arm edge state to the HYDRATED values so a boot straight from a saved
        # critical stat doesn't fire a spurious threshold signal on the first tick.
        for k in StatKind:
            if k in self.critical:
                thr = self.critical[k]
                self._in_critical[k] = (self._values[k] < thr) if k in LOW_CRITICAL \
                    else (self._values[k] > thr)
        self._active = True
        self._paused = False

        self._timer = QTimer(self)
        self._timer.setInterval(int(c.get("tick_ms", 60_000)))
        self._timer.timeout.connect(self._tick)

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        if not self._timer.isActive():
            self._timer.start()

    def flush(self):
        """Persist the current snapshot (graceful shutdown hook; also called on change)."""
        if self.persist_enabled:
            _atomic_write_json(
                self._state_path,
                {k.value: v for k, v in self._values.items()},
            )

    def stop(self):
        self._timer.stop()
        self.flush()

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
            self.flush()                     # manual changes persist immediately

    def _tick(self):
        if self._paused:
            return
        if not self._active and self.pause_when_inactive:
            return
        changed_any = False
        for kind in StatKind:
            delta = self.decay.get(kind, 0)
            if not delta:
                continue
            changed, new = self._apply_delta(kind, delta)
            if changed:
                changed_any = True
                self.stat_changed.emit(kind.value, new)
            self._evaluate(kind)
        if changed_any:
            self.flush()                     # save-on-mutation (DyberPet idiom, atomic here)

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
