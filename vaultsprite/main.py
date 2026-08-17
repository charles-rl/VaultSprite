"""VaultSprite main entry point.

Assembles the eight decoupled modules and wires them together with PySide6
signals/slots (see ``IMPLEMENTATION_OUTLINE.md`` "System Architecture Flow").
The :class:`App` class is the single owner of animation-state transitions: it is
the only thing that calls ``fsm.get_next_state`` / ``force_state``, driven by the
sprite player's ``state_finished`` plus external forces (drag/flick, stat
thresholds, health nudges, LLM replies).

Run::

    uv run vaultsprite                # real display
    QT_QPA_PLATFORM=offscreen uv run vaultsprite --smoke   # headless boot check
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from typing import Optional

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication

from .animation_fsm import AnimationFSM, StateTransition
from .config import Config, load_config
from .context_detector import CONTEXT_PLAY, ContextDetector
from .health_audio import SoundBank, WorkTimer
from .obsidian_vault import ObsidianVault
from .remote_agent import RemoteAgent
from .stat_engine import StatEngine
from .terrain_physics import TerrainPhysics
from .ui_overlay import PetOverlayWindow

logger = logging.getLogger("vaultsprite")


class App(QObject):
    """Wires the 8 modules; owns animation-state decisions."""

    def __init__(self, config: Optional[Config] = None):
        super().__init__()
        self.config = config or load_config()

        # --- build modules -----------------------------------------------------
        self.window = PetOverlayWindow(config)
        self.fsm = AnimationFSM(self.config.asset_config_path)
        self.stats = StatEngine(config)
        self.physics = TerrainPhysics(config)
        self.context = ContextDetector(config)
        self.agent = RemoteAgent(config)
        self.vault = ObsidianVault(config)
        self.sounds = SoundBank(config)
        self.health = WorkTimer(config)

        # keep the terrain callbacks pointed at the live window geometry
        self.physics.set_mover(
            position=self.window.position,
            move_to=self.window.move_to,
            pet_size=self.window.size_px,
        )
        self.agent.set_overlay_winid(self.window.winId())
        self._context_now: str = "UNKNOWN"

        # --- wiring ------------------------------------------------------------
        w = self.window
        w.drag_released.connect(self._on_drag_released)
        w.drag_started.connect(self._on_drag_started)
        w.clicked.connect(self._on_pet_clicked)
        w.ask_vision_requested.connect(lambda p: self.agent.ask(p, window_context=self._context_now))
        w.stretch_requested.connect(self._trigger_stretch_nudge)

        player = w.player
        player.state_finished.connect(self._advance_fsm)

        self.physics.falling_started.connect(self._on_falling_started)
        self.physics.landed.connect(self._on_landed)
        self.physics.standing_lost.connect(
            lambda t: logger.info("standing window lost: %r", (t or "")[:60]))

        self.stats.signal_hungry.connect(lambda: self._stat_nudge("hunger"))
        self.stats.signal_tired.connect(lambda: self._stat_nudge("energy"))
        self.stats.signal_bored.connect(lambda: self._stat_nudge("boredom"))

        self.context.context_changed.connect(self._on_context_changed)

        self.health.stretch_nudge.connect(self._trigger_stretch_nudge)

        self.agent.response_ready.connect(self._on_agent_reply)
        self.agent.error.connect(
            lambda m: logger.info("vision note: %s", (m or "")[:120]))

        # --- vault storage watchdog (M7; warn, never crash on a full disk) ---------
        self.vault.vault_size_warning.connect(self._on_vault_size_warning)
        size_ms = int(self.config.get("obsidian.size_check_ms", 300000))
        self._size_timer = QTimer(self)
        self._size_timer.setInterval(size_ms)
        self._size_timer.timeout.connect(lambda: self.vault.check_storage())

        # --- autonomous vision loop (config-gated; 0 disables) --------------------
        ask_ms = int(self.config.get("remote.ask_interval_ms", 0))
        self._vision_timer = QTimer(self)
        self._vision_timer.setInterval(ask_ms)
        self._vision_timer.timeout.connect(self._vision_tick)

    # -- lifecycle --------------------------------------------------------------
    def start(self):
        logger.info("starting VaultSprite (model=%s, vault=%s)",
                    self.agent.model, self.vault.root)
        t = self.fsm.force_state(self.fsm.current_state)
        self.window.play_state(t)
        self.physics.start()
        self.stats.start()
        self.context.start()          # no-ops on Linux (pwc unavailable); still fine
        self.health.start()

        ask_ms = int(self.config.get("remote.ask_interval_ms", 0))
        if ask_ms > 0 and self.agent.enabled:
            self._vision_timer.start()
            logger.info("autonomous vision loop every %d ms (model=%s)",
                        ask_ms, self.agent.model)
        else:
            reason = "disabled in config" if ask_ms <= 0 else "LLM client unavailable"
            logger.info("autonomous vision loop off (%s)", reason)

        size_ms = int(self.config.get("obsidian.size_check_ms", 300000))
        if size_ms > 0:
            self._size_timer.start()
            first = self.vault.check_storage()       # immediate baseline audit
            logger.info("vault storage watchdog on (limit %s MB, %.1f KB used)",
                        float(self.config.get("obsidian.max_size_mb", 50) or 0),
                        first / 1024)

    def _on_vault_size_warning(self, size_bytes: float):
        logger.warning(
            "VAULT SIZE LIMIT EXCEEDED: %.1f MB in %s — prune journal/events; pet keeps running",
            size_bytes / 1048576, self.vault.root)

    def shutdown(self):
        for stopper in (self._size_timer.stop, self._vision_timer.stop, self.physics.stop,
                        self.stats.stop, self.context.stop, self.health.stop):
            try:
                stopper()
            except Exception as exc:  # pragma: no cover - teardown best-effort
                logger.debug("stop failed: %s", exc)

    # -- animation ownership (the single FSM driver) -------------------------------
    def _play(self, transition: StateTransition):
        self.window.play_state(transition)
        if transition.name == "walking":
            pass  # per-frame walk drift handled by SpritePlayer.position_delta → window

    def _advance_fsm(self, _finished_name: str = ""):
        """Current state's hold elapsed → draw the next weighted state."""
        t = self.fsm.get_next_state(self.window.player.current_state or None)
        self._play(t)

    # -- drag / flick (M1 -> M4) ---------------------------------------------------
    def _on_drag_started(self):
        self.physics.enable(False)     # freeze gravity while held
        self.stats.pause()             # don't decay mid-interaction

    def _on_pet_clicked(self):
        """Petting: a little energy + a friendly blip."""
        from .stat_engine import StatKind
        self.stats.adjust(StatKind.ENERGY, 4)
        self.sounds.play("chirp", volume=0.5)

    def _on_drag_released(self, vx: float, vy: float):
        """Drag ended (flick or plain drop). The window already cleared its own
        dragging flag; physics decides between an impulse and a natural fall."""
        self.stats.resume()
        self.physics.release(vx, vy)

    # -- terrain events (M4 -> M2/M1) -----------------------------------------------
    def _on_falling_started(self):
        if self.window.dragging:
            return
        t = self.fsm.force_state("falling")
        self._play(t)

    def _on_landed(self, x: int, y: int):
        logger.debug("landed at (%d, %d)", x, y)
        if not self.window.dragging:
            t = self.fsm.force_state("idle")
            self._play(t)

    # -- stat thresholds (M3 -> bubble + memory) -------------------------------------
    def _stat_nudge(self, kind: str):
        lines = {
            "hunger": "I'm hungry... click me for a snack!",
            "energy": "So sleepy... need a break from you?",
            "boredom": "Boring! Do something with me.",
        }
        self._say(lines.get(kind, kind))
        try:
            self.vault.append_journal(f"stat critical: {kind}={self.stats.get_stat(kind)}")
            self.vault.write_fact("stats", f"last_{kind}_critical", str(self.stats.get_stat(kind)))
        except Exception as exc:  # pragma: no cover - memory is best-effort
            logger.debug("vault write failed: %s", exc)

    # -- context (M5 -> stats/health/memory) -------------------------------------------
    def _on_context_changed(self, context: str):
        self._context_now = context
        if context == CONTEXT_PLAY:
            self.stats.set_active(False)     # decay pauses in play
            self.health.set_active(False)    # and the work clock resets
        elif context == "WORK":
            self.stats.set_active(True)
            self.health.set_active(True)
        else:                                # UNKNOWN → keep last-known (already set above)
            pass
        logger.info("context -> %s", context)
        try:
            self.vault.record_event(f"context switched to {context}")
        except Exception as exc:  # pragma: no cover
            logger.debug("vault event failed: %s", exc)

    # -- health nudge (M8 -> FSM + sound + bubble) ---------------------------------------
    def _trigger_stretch_nudge(self):
        if self.window.dragging or self.physics.falling:
            return     # defer if the pet is mid-air / held; re-arms next tick window
        t = self.fsm.force_state("stretch_nudge")
        self._play(t)
        self.sounds.play("chirp")
        self._say("Stretch break! Stand up and move.")
        try:
            from datetime import datetime, timezone
            self.vault.write_fact("health", "last_stretch",
                                  datetime.now(timezone.utc).isoformat(timespec="seconds"))
            self.vault.append_journal("stretch nudge fired after continuous work")
        except Exception as exc:  # pragma: no cover
            logger.debug("vault health write failed: %s", exc)

    # -- remote vision (M6 -> bubble + FSM talking + memory) -----------------------------
    def _vision_tick(self):
        if self.window.dragging or not self.agent.enabled:
            return
        prompt = ("Look at my screen and, in one short sentence, tell me what I appear "
                  "to be doing right now.")
        logger.debug("autonomous vision ask (context=%s)", self._context_now)
        self.agent.ask(prompt, window_context=self._context_now)

    def _on_agent_reply(self, text: str):
        if not (text or "").strip():
            return
        self._say(text.strip())
        # animate the mouth for a moment while the reply shows
        if not (self.window.dragging or self.physics.falling):
            t = self.fsm.force_state("talking")
            self._play(t)
        try:
            self.vault.append_journal(f"said: {text.strip()[:120]}")
        except Exception as exc:  # pragma: no cover
            logger.debug("vault journal failed: %s", exc)

    def _say(self, text: str):
        if not (self.window and self.window.isVisible()):
            return
        self.window.show_bubble(text)


# ---------------------------------------------------------------------------
def setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="[%(asctime)s] [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="vaultsprite", description="VaultSprite desktop pet")
    parser.add_argument("--smoke", action="store_true",
                        help="boot all modules, run ~1.5s headless, then exit 0/1")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    config = load_config()
    setup_logging(str(config.get("app.log_level", "INFO")))

    # Headless fallback (dev boxes, CI): use the offscreen platform when no display
    # server env vars are present at all. Never overrides an explicit QT_QPA_PLATFORM.
    if os.name != "nt" and args.smoke and not any(
            v in os.environ for v in ("DISPLAY", "WAYLAND_DISPLAY")):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    brain = App(config)
    brain.window.show()
    if args.smoke:
        QTimer.singleShot(1500, app.quit)
    else:
        brain.start()

    code = app.exec()
    try:
        brain.shutdown()
    finally:
        return int(code or 0)


if __name__ == "__main__":
    sys.exit(main())
