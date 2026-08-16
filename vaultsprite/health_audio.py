"""Chiptune sound & health nudge engine (Module 8).

Two concerns in one file per the outline:
- :class:`SoundBank` — preloads small local ``.wav`` files into RAM via
  ``pygame.mixer`` and plays them non-blocking from the Qt main thread. The
  whole mixer init is guarded so a headless box (no audio device) degrades to a
  no-op stub instead of crashing at import/startup.
- :class:`WorkTimer` — accumulates continuous WORK minutes (fed by M5 context)
  and, after the configured threshold (outline: 45–60 min), emits ``stretch_nudge``
  once and pauses until main resolves it (skip / stretch / postpone).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Union

from PySide6.QtCore import QObject, QTimer, Signal

from .config import Config, load_config

logger = logging.getLogger(__name__)

try:  # pygame is imported lazily-guarded; headless boxes get a no-op SoundBank
    import pygame
except ImportError as exc:  # pragma: no cover - optional dep present in pyproject
    logger.debug("pygame unavailable (%s); audio disabled", exc)
    pygame = None


class SoundBank:
    """RAM-preloaded SFX with non-blocking playback and a safe headless fallback."""

    def __init__(self, config: Union[Config, None] = None):
        self.config = config or load_config()
        section = self.config.section("health")
        sounds_dir = Path(self.config.get("health.sounds_dir", "assets/sounds"))
        if not sounds_dir.is_absolute():
            sounds_dir = (Path(__file__).resolve().parent.parent / sounds_dir).resolve()
        self.sounds_dir = sounds_dir
        self.volumes: dict[str, float] = {
            name: float(v) for name, v in (section.get("volume", {}) or {}).items()
        }
        self._disabled = True
        self._sounds: dict = {}

        if pygame is None:
            logger.info("audio disabled (pygame not installed)")
            return
        try:
            # low-latency SFX pre-init MUST run before pygame.init()
            pygame.mixer.pre_init(44100, -16, 2, 1024)
            pygame.init()
            if pygame.mixer.get_init() is None:
                logger.info("audio disabled (no mixer / headless)")
                return
        except Exception as exc:  # pragma: no cover - environment specific
            logger.info("audio init failed (%s); disabled", exc)
            return

        self._disabled = False
        for name in ("step", "chirp", "yawn"):
            path = sounds_dir / f"{name}.wav"
            if not path.exists():
                logger.warning("missing sound asset %s; %s will be silent", path, name)
                continue
            try:
                self._sounds[name] = pygame.mixer.Sound(str(path))
                vol = self.volumes.get(name, 1.0)
                self._sounds[name].set_volume(vol)
            except Exception as exc:  # pragma: no cover - decode errors
                logger.warning("failed to load %s (%s)", path.name, exc)

    @property
    def disabled(self) -> bool:
        return self._disabled or not self._sounds

    def has(self, name: str) -> bool:
        return (not self._disabled) and name in self._sounds

    def play(self, name: str, volume: Optional[float] = None):
        """Non-blocking one-shot playback (overlapping plays are fine)."""
        if self.disabled or name not in self._sounds:
            return
        sound = self._sounds[name]
        if volume is not None:
            sound.set_volume(max(0.0, min(1.0, volume)))
        else:
            sound.set_volume(self.volumes.get(name, 1.0))
        sound.play()

    def stop(self, name: str):
        if self.disabled or name not in self._sounds:
            return
        try:
            self._sounds[name].stop()
        except Exception:
            pass

    def play_loop(self, name: str):
        """Repeated playback (e.g. footsteps); returns immediately."""
        if self.disabled or name not in self._sounds:
            return
        self._sounds[name].play(loops=-1)


class WorkTimer(QObject):
    """Continuous-work accumulator → stretch nudge state machine."""

    stretch_nudge = Signal()      # fired once per threshold, pauses until resolved
    work_minutes_changed = Signal(int)   # coarse progress for a status bar (optional)

    def __init__(self, config: Union[Config, None] = None):
        super().__init__()
        self.config = config or load_config()
        section = self.config.section("health")
        self._threshold_min = int(section.get("work_threshold_min", 50))   # 45–60
        tick_ms = int(section.get("nudge_tick_ms", 5000))

        self._timer = QTimer(self)
        self._timer.setInterval(tick_ms)
        # work-time credited per tick; default is real time (tick length), the key
        # exists so tests can fast-forward without waiting for a full hour.
        override_s = section.get("tick_work_seconds") if isinstance(section, dict) \
            else self.config.get("health.tick_work_seconds")
        self._tick_s = float(override_s) if override_s is not None else tick_ms / 1000.0
        self._timer.timeout.connect(self._tick)

        self._work_seconds = 0.0
        self._active = True       # set by M5: only accumulate in WORK context
        self._nudge_pending = False   # paused until main resolves the nudge

    # -- tuning / introspection -------------------------------------------------
    @property
    def threshold_minutes(self) -> int:
        return self._threshold_min

    @property
    def work_seconds(self) -> float:
        return self._work_seconds

    @property
    def nudge_pending(self) -> bool:
        return self._nudge_pending

    # -- lifecycle -----------------------------------------------------------------
    def start(self):
        if not self._timer.isActive():
            self._timer.start()

    def stop(self):
        self._timer.stop()

    def set_active(self, active: bool):
        """WORK=True accumulates; PLAY/UNKNOWN zeroes progress (reset on switch)."""
        if self._active == active:
            return
        self._active = active
        if not active:
            self._work_seconds = 0.0
            self._nudge_pending = False   # leaving work cancels a pending nudge

    def reset(self):
        self._work_seconds = 0.0
        self._nudge_pending = False

    def resolve_nudge(self, mode: str = "stretch", credit_minutes: int = 25):
        """Main calls this when the health prompt is dismissed.

        - ``"stretch"`` / ``"skip"`` → full reset (next nudge after a full cycle).
        - ``"postpone"`` → partial credit back: next nudge comes sooner, after
          re-accumulating just ``credit_minutes`` less than the threshold.
        """
        self._nudge_pending = False
        if mode == "postpone":
            # next nudge comes after `credit_minutes` more continuous work,
            # i.e. start the counter pre-credited to (threshold - credit).
            self._work_seconds = max(0, self._threshold_min - credit_minutes) * 60
        else:
            self._work_seconds = 0.0

    # -- tick ---------------------------------------------------------------------
    def _tick(self):
        if not self._active or self._nudge_pending:
            return
        self._work_seconds += self._tick_s
        threshold_s = self._threshold_min * 60
        if self._work_seconds >= threshold_s:
            logger.info("health nudge: %d min continuous work reached",
                        self._threshold_min)
            self._nudge_pending = True
            self._work_seconds = 0.0
            self.stretch_nudge.emit()
