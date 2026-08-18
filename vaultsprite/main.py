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
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication

from .animation_fsm import AnimationFSM, StateTransition
from .config import Config, load_config
from .context_detector import CONTEXT_PLAY, ContextDetector
from .health_audio import SoundBank, WorkTimer
from .mascot_engine_widget import MascotEngine
from .obsidian_vault import ObsidianVault
from .remote_agent import RemoteAgent
from .stat_engine import StatEngine
from .terrain_physics import TerrainPhysics
from .ui_overlay import PetOverlayWindow, SystemTray, TelemetryOverlay

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
        self._mascot_on = bool(self.config.get("mascot.enabled", False))
        self.mascot = MascotEngine(config, parent=self) if self._mascot_on else None

        # keep the terrain callbacks pointed at the live window geometry
        self.physics.set_mover(
            position=self.window.position,
            move_to=self.window.move_to,
            pet_size=self.window.size_px,
        )
        self.agent.set_overlay_winid(self.window.winId())
        self._context_now: str = "UNKNOWN"
        self._prev_context: str = "UNKNOWN"

        # --- wiring ------------------------------------------------------------
        w = self.window
        w.drag_released.connect(self._on_drag_released)
        w.drag_started.connect(self._on_drag_started)
        w.clicked.connect(self._on_pet_clicked)
        w.ask_vision_requested.connect(
            lambda p: self._ask_vision(p, window_context=self._vision_window_context()))
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

        if self.mascot is not None:
            self.mascot.frame_changed.connect(self.window.render_mascot_frame)
            self.mascot.position_changed.connect(self.window.move_to)
            self.mascot.behavior_changed.connect(self._on_mascot_behavior)
            if bool(self.config.get("debug.telemetry_overlay", False)):
                self._overlay = TelemetryOverlay()
                self._overlay.start(self._mascot_telemetry)

        # The tray is the reveal path for a hidden pet, so it must exist whenever the
        # hide feature is enabled — even in legacy (non-mascot) mode.
        if bool(self.config.get("hide.enabled", True)) or self.mascot is not None:
            self._setup_tray()

        self.agent.response_ready.connect(self._on_agent_reply)
        self.agent.error.connect(
            lambda m: logger.info("vision note: %s", (m or "")[:120]))

        w.hide_requested.connect(self._on_hide_requested)

        # --- hide/show behavior (walk to nearest edge, pause autonomy) -------------
        self._hidden = False
        self._hide_restore: Optional[tuple[int, int]] = None
        self._hide_target: Optional[tuple[int, int]] = None
        self._hide_timer = QTimer(self)
        self._hide_timer.setInterval(int(self.config.get("hide.step_ms", 20)))
        self._hide_timer.timeout.connect(self._hide_walk_step)

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
        logger.info("starting VaultSprite (model=%s, vault=%s, mascot=%s)",
                    self.agent.model, self.vault.root, self._mascot_on)
        if self._mascot_on and self.mascot is not None:
            self.mascot.start()       # M9 owns ambient animation + position
        else:
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
        if self.mascot is not None:
            try:
                self.mascot.stop()
            except Exception as exc:  # pragma: no cover
                logger.debug("mascot stop failed: %s", exc)
        overlay = getattr(self, "_overlay", None)
        if overlay is not None:
            try:
                overlay.close()
            except Exception:  # pragma: no cover
                pass
        tray = getattr(self, "_tray", None)
        if tray is not None:
            try:
                tray.hide()
            except Exception:  # pragma: no cover
                pass
        for stopper in (self._size_timer.stop, self._vision_timer.stop, self._hide_timer.stop,
                        self.physics.stop, self.stats.stop, self.context.stop, self.health.stop):
            try:
                stopper()
            except Exception as exc:  # pragma: no cover - teardown best-effort
                logger.debug("stop failed: %s", exc)

    # -- animation ownership (the single FSM driver) -------------------------------
    def _play(self, transition: StateTransition):
        self._log_state_transition(transition)   # every state change funnels through here
        self.window.play_state(transition)
        if transition.name == "walking":
            pass  # per-frame walk drift handled by SpritePlayer.position_delta → window

    def _debug_log(self, category: str, entry: str):
        """Best-effort rolling debug trail in the Vault (user-facing debugging aid)."""
        if not self.config.get("debug.vault_logging", True):
            return
        try:
            self.vault.append_debug_log(category, entry)
        except Exception as exc:  # pragma: no cover - never break the pet over logging
            logger.debug("vault debug log failed: %s", exc)

    def _log_state_transition(self, transition: StateTransition):
        x, y = self.window.position()
        self._debug_log(
            "pet-states",
            f"state -> {transition.name} (dur={int(getattr(transition,'duration_ms',0))}ms, pos=({x},{y}))")

    def _advance_fsm(self, _finished_name: str = ""):
        """Current state's hold elapsed → draw the next weighted state."""
        if self._hidden:
            return
        t = self.fsm.get_next_state(self.window.player.current_state or None)
        self._play(t)

    # -- drag / flick (M1 -> M4) ---------------------------------------------------
    def _on_drag_started(self):
        self.physics.enable(False)     # freeze gravity while held
        self.stats.pause()             # don't decay mid-interaction
        x, y = self.window.position()
        self._debug_log("mascot", f"drag started at ({x},{y})")
        if self.mascot is not None:
            self.mascot.set_dragging(True)
            self.mascot.force_behavior("Dragged")

    def _on_pet_clicked(self):
        """Petting: a little energy + a friendly blip."""
        from .stat_engine import StatKind
        self.stats.adjust(StatKind.ENERGY, 4)
        self.sounds.play("chirp", volume=0.5)

    def _on_drag_released(self, vx: float, vy: float):
        """Drag ended (flick or plain drop). The window already cleared its own
        dragging flag; physics decides between an impulse and a natural fall."""
        self.stats.resume()
        if self.mascot is not None:
            self.mascot.set_dragging(False)
            self.mascot.inject_throw(vx, vy)
            # anchor the throw at the window's actual feet so it launches from where the
            # user let go (the core anchor was frozen at the grab point during the drag)
            wx, wy = self.window.position()
            px = getattr(self.mascot, "_px", 0)
            self.mascot.sync_anchor(wx + px // 2, wy + px)
            self.mascot.force_behavior("Thrown")
            self._debug_log("mascot", f"throw released v=({vx:.0f},{vy:.0f}) px/s at ({wx},{wy})")
        else:
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
        if context == CONTEXT_PLAY or context == "UNKNOWN":
            # PLAY and decayed-UNKNOWN both stop crediting "work" — the old code kept
            # WORK active on UNKNOWN, which is why stretch nudges fired while idle.
            self.stats.set_active(False)     # decay pauses in play / unknown
            self.health.set_active(False)    # and the work clock resets
        elif context == "WORK":
            self.stats.set_active(True)
            self.health.set_active(True)
        title = (getattr(self.context, "last_title", "") or "").strip()
        logger.info("context -> %s (title=%r)", context, title[:60])
        self._debug_log("context", f"{self._prev_context or 'UNKNOWN'} -> {context} | title={title!r}")
        self._prev_context = context
        try:
            title = getattr(self.context, "last_title", "") or "?"
            if context == "UNKNOWN":
                self.vault.record_event(
                    f"context decayed to UNKNOWN after {title} stopped matching keywords")
            else:
                self.vault.record_event(f"context switched to {context}", title=title)
        except Exception as exc:  # pragma: no cover
            logger.debug("vault event failed: %s", exc)

    # -- health nudge (M8 -> FSM + sound + bubble) ---------------------------------------
    def _trigger_stretch_nudge(self):
        if self._hidden or self.window.dragging or self.physics.falling:
            return     # defer if the pet is mid-air / held / hidden; re-arms next tick window
        if self.mascot is not None:
            self.mascot.force_behavior("SitDown")
        else:
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
    def _vision_window_context(self) -> str:
        """The REAL foreground window title for the LLM — never a bare 'WORK'/'PLAY'
        bucket string. The old code passed self._context_now, so when no real title was
        available the prompt said 'Active window context:\nWORK' and the model happily
        described a window titled WORK instead of what it saw (see your Vault journal)."""
        title = (getattr(self.context, "last_title", "") or "").strip()
        bucket = self._context_now or "UNKNOWN"
        if not title:
            return f"[no foreground window title captured; detected context: {bucket}]"
        return f"{title}  [detected context: {bucket}]"

    def _ask_vision(self, prompt: str, window_context: str = ""):
        """Dispatch a vision ask with immediate on-screen feedback.

        The LLM call runs off-thread (RemoteAgent.ask), but a remote 27B model can take
        many seconds (cold start / dev-tunnel), which reads as a frozen pet with no
        indicator. Show an instant "thinking…" bubble so the pet visibly responds the
        moment you ask; ``_on_agent_reply`` replaces it with the actual reply.
        """
        self._say("Thinking\u2026")
        self.agent.ask(prompt, window_context=window_context)

    def _vision_tick(self):
        if self._hidden or self.window.dragging or not self.agent.enabled:
            return
        prompt = ("Look at my screen and, in one short sentence, tell me what I appear "
                  "to be doing right now.")
        logger.debug("autonomous vision ask (context=%s)", self._context_now)
        self._ask_vision(prompt, window_context=self._vision_window_context())

    def _on_agent_reply(self, text: str):
        if not (text or "").strip():
            return
        self._say(text.strip())
        # animate the pet for a moment while the reply shows
        if not (self.window.dragging or self.physics.falling):
            if self.mascot is not None:
                self.mascot.force_behavior("SitAndFaceMouse")
            else:
                t = self.fsm.force_state("talking")
                self._play(t)
        try:
            self.vault.append_journal(f"said: {text.strip()[:120]}")
        except Exception as exc:  # pragma: no cover
            logger.debug("vault journal failed: %s", exc)

    def _on_mascot_behavior(self, name: str):
        """M9 telemetry → Vault debug trail (debug.vault_logging; default on)."""
        x, y = self.window.position()
        self._debug_log("mascot", f"behavior -> {name} (pos=({x},{y}))")

    def _setup_tray(self):
        icon_path = str(Path(self.config.resolve_path("assets/steve_shimeji/img/icon.png")))
        names = self.mascot.behavior_names if self.mascot is not None else []
        self._tray = SystemTray(icon_path, names)
        self._tray.scale_changed.connect(self._on_scale_changed)
        self._tray.behavior_toggled.connect(self._on_behavior_toggled)
        self._tray.hide_toggled.connect(self._on_hide_toggled)
        self._tray.quit_requested.connect(self._on_quit_requested)
        self._tray.show()

    # -- hide / show (walk to nearest edge, pause autonomy) ---------------------------
    def _on_hide_requested(self):
        """Sprite right-click 'Hide pet' → hide (the tray checkbox follows)."""
        if not self._hidden and bool(self.config.get("hide.enabled", True)):
            self._begin_hide()
            tray = getattr(self, "_tray", None)
            if tray is not None:
                tray.set_hidden(True)

    def _on_hide_toggled(self, want_hidden: bool):
        if want_hidden and not self._hidden:
            self._begin_hide()
        elif not want_hidden and self._hidden:
            self._begin_show()

    def _hide_edge_target(self) -> tuple[int, int]:
        """Fully-off-screen position at the nearest screen edge, keeping the sprite's y."""
        screen = QApplication.primaryScreen()
        w, h = self.window.size_px()
        if screen is None:
            x, y = self.window.position()
            return x, y
        geo = screen.availableGeometry()
        wx, wy = self.window.position()
        center = wx + w // 2
        go_left = center < (geo.left() + geo.width() / 2)
        if go_left:
            target_x = geo.left() - w            # window fully off the left edge
        else:
            target_x = geo.right()               # window fully off the right edge
        return target_x, wy

    def _begin_hide(self):
        if self._hidden:
            return
        self._hidden = True
        self._hide_restore = self.window.position()
        self._hide_target = self._hide_edge_target()
        if self.mascot is not None:
            # the engine anchor is the sprite's FEET — restore that, not the window
            # top-left, or the reveal sync would jump the pet by ~half its size.
            self._hide_anchor = self.mascot.anchor()
            # walk off using the engine's walk frames (position-locked; App owns the walk)
            self.mascot.set_hide_walk(True, moving_right=self._hide_target[0] > self._hide_restore[0])
        else:
            self.physics.stop()                  # legacy mode: freeze movement
            self.window.player.stop()
        self.window.bubble.hide()                # no lingering bubble while away
        self._debug_log("mascot", f"hide started from {self._hide_restore} -> target {self._hide_target}")
        self._hide_timer.start()

    def _begin_show(self):
        if not self._hidden:
            return
        self._hidden = False
        self.window.show()
        self._hide_target = self._hide_restore   # return to the pre-hide spot
        if self.mascot is not None:
            cur_x = self.window.position()[0]
            self.mascot.set_hide_walk(True, moving_right=self._hide_target[0] > cur_x)
        self._debug_log("mascot", f"show started, returning to {self._hide_target}")
        self._hide_timer.start()

    def _hide_walk_step(self):
        """One step toward the hide/show target; the timer drives repeated steps."""
        if self._hide_target is None:
            self._hide_timer.stop()
            return
        x, y = self.window.position()
        tx, ty = self._hide_target
        step = int(self.config.get("hide.step_px", 6))
        dx = tx - x
        dy = ty - y
        nx, ny = x, y
        if abs(dx) <= step:
            nx = tx
        else:
            nx = x + (step if dx > 0 else -step)
        if abs(dy) <= step:
            ny = ty
        else:
            ny = y + (step if dy > 0 else -step)
        self.window.move_to(nx, ny)
        if self.mascot is not None:
            w, h = self.window.size_px()
            self.mascot.sync_anchor(nx + w // 2, ny + h)   # keep walk frames grounded under the window
        if (nx, ny) == (tx, ty):
            self._hide_timer.stop()
            self._hide_target = None
            self._hide_walk_done()

    def _hide_walk_done(self):
        """Reached the off-screen hide spot or the restore point."""
        if self._hidden:
            if self.mascot is not None:
                self.mascot.set_hide_walk(False)
                self.mascot.set_hidden(True)     # fully freeze while off-screen
            self.window.hide()                   # fully invisible
            return
        # back on screen: hand the anchor back to the engine and resume autonomy
        if self.mascot is not None:
            rx, ry = getattr(self, "_hide_anchor", None) or self.window.position()
            self.mascot.sync_anchor(rx, ry)
            self.mascot.set_hide_walk(False)     # stop the position-locked walk
            self.mascot.set_hidden(False)        # resume ambient (no re-seed)
            self.mascot.force_behavior("SitDown")  # leave HideWalk → return to idle
        else:
            self.physics.start()
            t = self.fsm.force_state(self.fsm.current_state)
            self.window.play_state(t)

    def _on_scale_changed(self, factor: float):
        w, h = self.window.size_px()
        new_px = max(16, int(w * factor))
        self.window.set_scale(factor)
        if self.mascot is not None:
            self.mascot._px = new_px

    def _on_behavior_toggled(self, name: str, exclude: bool):
        if self.mascot is not None:
            self.mascot.toggle_excluded(name, exclude)
            self._debug_log("mascot", f"exclude behavior {name}={exclude}")

    def _on_quit_requested(self):
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _mascot_telemetry(self) -> dict:
        x, y = self.window.position()
        return {"x": x, "y": y,
                "behavior": self.mascot.active_behavior if self.mascot else "",
                "frame": self.mascot.current_frame() if self.mascot else ""}

    def _say(self, text: str):
        if self._hidden or not (self.window and self.window.isVisible()):
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
