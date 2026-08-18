"""Qt adapter for the M9 Shimeji engine (Module 9).

A thin :class:`MascotEngine` (QObject) owns the ``MascotCore`` tick ``QTimer`` and
turns the pure-Python engine's pose events into ``QPixmap`` frames for the overlay
window. It also bridges the engine's environment to the live Qt screen geometry and
cursor, and emits telemetry/behavior signals for App wiring.

Kept separate from :mod:`vaultsprite.mascot_engine` so the core stays pure Python
(unit-testable without a QApplication).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QImageReader, QPixmap, QCursor, QTransform
from PySide6.QtWidgets import QApplication

from .config import Config, load_config
from .mascot_engine import BehaviorDef, BehaviorNode, InPlaceAction, MascotCore
from .mascot_environment import DArea, HBorder, MascotEnvironment

logger = logging.getLogger(__name__)


class MascotEngine(QObject):
    """Drives the Shimeji core on a QTimer and renders its frames for the overlay.

    Signals:
    - ``frame_changed(QPixmap)`` — already scaled + mirrored for the overlay window.
    - ``behavior_changed(str)``  — a new behavior became active (telemetry/logging).
    - ``position_changed(x, y)`` — the core's anchor moved; App moves the window here.
    """

    frame_changed = Signal(QPixmap)
    behavior_changed = Signal(str)
    position_changed = Signal(int, int)
    debug_log = Signal(str)

    def __init__(self, config: Optional[Config] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.config = config or load_config()
        m = self.config.section("mascot")
        self.tick_ms = max(10, int(m.get("tick_ms", 40) or 40))
        self.excluded = set(m.get("excluded_behaviors", []) or [])
        self._px = max(16, int(self.config.get("window.width", 96) or 96))
        self._mascot_dir = self._resolve_img_dir(
            self.config.resolve_path(str(m.get("actions_xml", ""))))

        self._env = MascotEnvironment(
            ceiling=HBorder(0, 0, 1),
            floor=HBorder(1, 0, 1),
            screen=DArea(0, 1, 1, 0),
            work_area=DArea(0, 1, 1, 0),
            active_ie=DArea.invisible(),
            allows_breeding=False,
            mascot_count=1,
        )
        self.core: Optional[MascotCore] = None
        self._enabled = False
        try:
            core = MascotCore(self._env, excluded_behaviors=self.excluded)
            core.parse(
                self.config.resolve_path(str(m.get("actions_xml", ""))),
                self.config.resolve_path(str(m.get("behaviors_xml", ""))),
            )
            core.on_frame_changed = self._on_frame
            core.on_behavior_changed = self._on_behavior
            self.core = core
        except Exception as exc:   # noqa: BLE001 - never let a bad pack kill the app
            logger.warning("Mascot engine failed to load pack; disabled: %s", exc)
            self.core = None
        if self.core is not None:
            self._install_hide_walk()

        self._pixmap_cache: dict[str, QPixmap] = {}
        self._cursor = None
        self._dragging = False
        self._hide_walking = False
        self._rendered_frame = False
        # tracked foreground window rect (logical px) or None → the engine's "no window"
        # invisible-sentinel. Fed to core.update_environment every tick (D10). The caller
        # must divide any Win32 physical px by devicePixelRatio() at the bridge.
        self._tracked_window: Optional[dict] = None
        # consume-once throw velocity: inject_throw() feeds the launch tick, and _tick()
        # must NOT overwrite it with the live cursor delta before the queued Thrown reads
        # ${cursor.dx}/${cursor.dy} (review finding A1).
        self._pending_throw = False
        self._pending_dx = 0.0
        self._pending_dy = 0.0

        # Smooth on-screen motion: the engine tick moves the window in discrete
        # multi-pixel steps (25 Hz at tick_ms 40) which reads as "frame-by-frame"
        # judder on high-refresh monitors. A fast interpolation timer lerps the
        # window between successive engine targets so motion looks continuous
        # while the physics/behavior clock stays on its own tick. Config
        # ``mascot.smooth_motion`` (default true) turns this off if ever needed.
        self._smooth = bool(m.get("smooth_motion", True))
        self._interp_from = (0, 0)
        self._interp_to: Optional[tuple[int, int]] = None
        self._interp_elapsed = 0
        self._interp_total = max(1, self.tick_ms)
        self._pos_cur: Optional[tuple[int, int]] = None
        self._interp_timer = QTimer(self)
        self._interp_timer.setInterval(max(5, self.tick_ms // 4))
        self._interp_timer.timeout.connect(self._interp_step)
        self._debug_counter = 0
        # the grip surface for the current tick: True = anchor is on the ceiling, so the
        # sprite is rendered upside-down hanging below the feet (see _clamp_pos/_on_frame).
        self._on_ceiling = False

        self._timer = QTimer(self)
        self._timer.setInterval(self.tick_ms)
        self._timer.timeout.connect(self._tick)

    @property
    def enabled(self) -> bool:
        return self.core is not None

    @property
    def behavior_names(self) -> list[str]:
        return sorted(self.core.behavior_defs) if self.core else []

    @property
    def active_behavior(self) -> str:
        return self.core.active_behavior_name if self.core else ""

    def anchor(self) -> tuple[int, int]:
        if self.core:
            return int(self.core.state.anchor.x), int(self.core.state.anchor.y)
        return 0, 0

    def current_frame(self) -> str:
        f = self.core.state.active_frame if self.core else None
        return f.image if f else ""

    def toggle_excluded(self, name: str, exclude: bool):
        if not self.core:
            return
        if exclude:
            self.core.excluded.add(name)
        else:
            self.core.excluded.discard(name)

    # -- lifecycle -------------------------------------------------------------
    def start(self):
        if not self.core or self._timer.isActive():
            return
        self._update_env_geometry()
        st = self.core.state
        if st.anchor.x == 0 and st.anchor.y == 0:   # seed to floor center on first start
            st.anchor.x = (self._env.floor.xstart + self._env.floor.xend) / 2
            st.anchor.y = self._env.floor.y
        self._rendered_frame = False
        for _ in range(6):          # first tick starts the behavior; a later one renders a frame
            self._tick()
            if self._rendered_frame:
                break
        st = self.core.state
        self._pos_cur = (int(st.anchor.x) - self._px // 2, int(st.anchor.y) - self._px)
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self._stop_interp()

    def set_dragging(self, dragging: bool):
        self._dragging = dragging
        if dragging:
            self._stop_interp()
        if not dragging:
            # Release: the window is already at the drop point (the overlay followed the
            # mouse), but _pos_cur still holds the pre-drag position. If we kept it, the
            # next _set_target would lerp the window back to the grab point then forward
            # to the throw target — the visible snap-back. Resetting it makes the next
            # engine target the interpolation origin, so the throw launches in place.
            self._pos_cur = None
            self._stop_interp()
        if self.core:
            self.core.state.dragging = dragging

    def set_hide_walk(self, active: bool, moving_right: bool = True):
        """Enter/leave the hide/show walk: the engine animates the walk frames in place
        while App owns the window position (see :meth:`sync_anchor` per step).

        ``active=True`` keeps the tick timer running and forces the synthetic
        ``HideWalk`` behavior (an in-place walk loop) so the pet visibly walks toward
        the edge; ``position_changed`` is suppressed so the engine never fights App's
        manual stepping. ``active=False`` stops the walk and leaves the timer state to
        the caller (usually a subsequent :meth:`set_hidden`)."""
        self._hide_walking = bool(active)
        if active:
            self._stop_interp()          # App owns the window while walking; no lerp
        if not self.core:
            return
        self.core.state.looking_right = bool(moving_right)
        if active:
            if not self._timer.isActive():
                self._timer.start()
            self.core.force_behavior("HideWalk")

    def _install_hide_walk(self):
        """Register a hidden ``HideWalk`` behavior reusing the pack's ``Walk`` pose set
        (shime1/2/3) but playing in place, so App can walk the pet off-screen."""
        if self.core is None:
            return
        walk = self.core.actions.get("Walk")
        anims = getattr(walk, "anims", None)
        if not anims:
            logger.warning("HideWalk unavailable: pack has no Walk animation")
            return
        act = InPlaceAction({}, self.core, list(anims))
        node = BehaviorNode(name="HideWalk", action=act, frequency=0, hidden=True)
        self.core.behavior_defs["HideWalk"] = BehaviorDef(
            node=node, next_children=[], add_next=False)

    def set_hidden(self, hidden: bool):
        """Freeze the ambient engine while the pet is hidden off-screen.

        ``hidden=True`` stops the tick timer so the core stays at its current anchor
        and never moves the window or changes behavior while the pet is away;
        ``hidden=False`` restarts ambient animation WITHOUT re-seeding the anchor
        (unlike :meth:`start`, which puts a fresh pet at floor center)."""
        if hidden:
            self._timer.stop()
            self._stop_interp()
            return
        if self._timer.isActive() or not self.core:
            return
        self._rendered_frame = False
        for _ in range(6):          # warmup ticks render a frame + start a behavior
            self._tick()
            if self._rendered_frame:
                break
        self._timer.start()

    def force_behavior(self, name: str):
        if self.core:
            self.core.force_behavior(name)

    def sync_anchor(self, x: float, y: float):
        """Snap the core anchor to a real screen position (the sprite's feet).

        Called by App on drag release: during a drag the overlay moves the window with
        the mouse while the core anchor stays at the grab point, so without this the
        ``Thrown``/``Falling`` launch would originate from the stale anchor and the window
        would snap back there. ``x``/``y`` are the desired anchor coords (logical px)."""
        if self.core:
            self.core.state.anchor.x = float(x)
            self.core.state.anchor.y = float(y)

    def inject_throw(self, vx_px_s: float, vy_px_s: float):
        """Feed a flick's release velocity (px/s) into the core's cursor delta so
        the ``Thrown`` action's InitialVX/VY (``cursor.dx/dy``) launch the pet."""
        if not self.core:
            return
        ticks = 1000.0 / max(1, self.tick_ms)
        # Stage the release velocity for the single tick that consumes the queued
        # Thrown. Writing cursor.dx/dy directly here is not enough: _tick() refreshes
        # them from the live QCursor delta every tick (see _tick below).
        self._pending_dx = vx_px_s / ticks
        self._pending_dy = vy_px_s / ticks
        self._pending_throw = True

    def respawn(self):
        """Re-settle the pet after an external resize (tray scale control).

        The scale change only updates ``_px``; the engine keeps its old anchor/behavior,
        so a resized pet can float mid-air or jump off-screen. This recentres the anchor
        on the floor and replays the pack's ``PullUpShimeji`` "spawned a new version"
        flourish (breed gag frames 38-41 + jump + fall + bounce) — a visible drop-and-
        settle instead of a physics glitch. Falls back to a plain ``Fall`` for packs
        without the breed behavior."""
        if not self.core:
            return
        wa = self._env.work_area
        self.core.state.anchor.x = (wa.left + wa.right) / 2
        self.core.state.anchor.y = self._env.floor.y
        name = "PullUpShimeji" if "PullUpShimeji" in self.core.behavior_defs else "Fall"
        self.core.force_behavior(name)
        self.debug_log.emit(f"respawn: recentred on floor, forcing {name}")

    def set_tracked_window(self, rect: Optional[dict] = None):
        """Track the foreground window rect (logical px: left/top/right/bottom) so the
        engine can land on / interact with windows (activeIE). Pass None for "no window".
        The caller divides Win32 physical px by devicePixelRatio() at the bridge."""
        self._tracked_window = rect

    # -- internals --------------------------------------------------------------
    @staticmethod
    def _resolve_img_dir(actions_xml: Path) -> Optional[Path]:
        """Derive the image set dir (<pack>/img/<Name>) from the actions.xml path."""
        pack_root = actions_xml.parent.parent
        img_root = pack_root / "img"
        if img_root.is_dir():
            dirs = [d for d in img_root.iterdir() if d.is_dir()]
            if dirs:
                return dirs[0]
        return None

    def _update_env_geometry(self):
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        left, top = float(geo.left()), float(geo.top())
        right, bottom = float(geo.left() + geo.width()), float(geo.top() + geo.height())
        self._env.screen = DArea(top, right, bottom, left)
        self._env.work_area = DArea(top, right, bottom, left)
        self._env.ceiling = HBorder(top, left, right)
        self._env.floor = HBorder(bottom, left, right)

    def _tick(self):
        if self.core is None:
            return
        try:
            self._update_env_geometry()
            c = QCursor.pos()          # may be a null QPoint offscreen; guard defensively
            cx, cy = (c.x(), c.y()) if c is not None else (0, 0)
            if self._pending_throw:
                # the tick that launches the queued Thrown must see the injected flick
                # velocity, not the (tiny) live cursor delta of this same tick.
                dx, dy = self._pending_dx, self._pending_dy
                self._pending_throw = False
            else:
                px, py = self._cursor if self._cursor is not None else (cx, cy)
                dx, dy = cx - px, cy - py
            self._cursor = (cx, cy)
            self.core.update_environment(cursor_pos=(cx, cy, dx, dy),
                                         tracked_window=self._tracked_window)
            # grip surface for THIS tick's rendering (upside-down ceiling flip): read the
            # anchor before the tick so _on_frame flips the frame as it is drawn.
            self._on_ceiling = self._anchor_on_ceiling()
            self.core.tick()
            # move the window to follow the core anchor (unless the user is dragging it
            # or App is walking the pet off-screen during hide/show). The anchor is clamped
            # to the work-area borders so the pet actually grips the ceiling and side walls
            # (no on-screen margin) while never running off-screen — a Dash toward an
            # off-screen cursor.x or a huge Math.random TargetX still can't escape; the
            # interpolator then smooths the move between engine ticks.
            if not self._dragging and not self._hide_walking:
                st = self.core.state
                x, y = self._clamp_pos(int(st.anchor.x), int(st.anchor.y))
                self._set_target(x, y)
            else:
                self._stop_interp()
            self._maybe_debug(self.core.state)
        except Exception as exc:   # noqa: BLE001 - a bad tick must never kill the app
            logger.warning("mascot tick skipped: %s", exc)

    # -- motion helpers --------------------------------------------------------
    def _anchor_on_ceiling(self) -> bool:
        """True when the core anchor (feet) is on the ceiling, so the sprite hangs
        upside-down below it instead of standing above it."""
        if self.core is None:
            return False
        screen = QApplication.primaryScreen()
        if screen is None:
            return False
        return self.core.state.anchor.y <= screen.availableGeometry().top() + 1

    def _clamp_pos(self, ax: int, ay: int) -> tuple[int, int]:
        """Clamp the ANCHOR (feet) to the work-area borders and derive the window's
        top-left. Keeping the anchor on the borders lets the pet grip the ceiling and
        side walls (no visible margin), while the border clamp still stops off-screen
        runaway — a Dash/Throw target outside the work area pins the feet on the edge
        instead of pushing the whole window off the monitor. Returns (x, y) window pos."""
        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            ax = max(geo.left(), min(ax, geo.right()))
            ay = max(geo.top(), min(ay, geo.bottom()))
        px = self._px
        if self._on_ceiling:
            return ax - px // 2, ay          # body hangs BELOW the feet (upside down)
        return ax - px // 2, ay - px         # body rises ABOVE the feet

    def _set_target(self, x: int, y: int):
        """Route an engine position to the window — directly or through the lerp timer."""
        if not self._smooth:
            self._pos_cur = (x, y)
            self.position_changed.emit(x, y)
            return
        self._interp_from = self._pos_cur if self._pos_cur is not None else (x, y)
        self._interp_to = (x, y)
        self._interp_elapsed = 0
        self._interp_total = max(1, self.tick_ms)
        if not self._interp_timer.isActive():
            self._interp_timer.start()

    def _stop_interp(self):
        self._interp_to = None
        self._interp_timer.stop()

    def _interp_step(self):
        """Ease the window from the previous engine target to the current one."""
        if self._interp_to is None:
            self._interp_timer.stop()
            return
        self._interp_elapsed += self._interp_timer.interval()
        t = min(1.0, self._interp_elapsed / self._interp_total)
        t = t * t * (3.0 - 2.0 * t)                       # smoothstep ease
        fx, fy = self._interp_from
        tx, ty = self._interp_to
        x = fx + (tx - fx) * t
        y = fy + (ty - fy) * t
        self._pos_cur = (int(x), int(y))
        self.position_changed.emit(int(x), int(y))
        if t >= 1.0:
            self._interp_to = None
            self._interp_timer.stop()

    def _maybe_debug(self, st):
        """Throttled (~1 Hz) telemetry line so Vault trails show where the pet is and
        why (the reported 'logs are missing stuff'): anchor, work-area, behavior, frame."""
        self._debug_counter += 1
        if self._debug_counter % 25 != 0:
            return
        frame = getattr(getattr(st, "active_frame", None), "image", "") or ""
        self.debug_log.emit(
            f"anchor=({int(st.anchor.x)},{int(st.anchor.y)}) "
            f"behavior={st.behavior_name or st.queued_behavior or ''} frame={frame} "
            f"wa=({int(self._env.work_area.left)},{int(self._env.work_area.top)},"
            f"{int(self._env.work_area.right)},{int(self._env.work_area.bottom)})")

    def _on_behavior(self, name: str):
        self.behavior_changed.emit(name)

    def _on_frame(self, pose):
        pm = self._load_pixmap(pose.image)
        if pm is None or self.core is None:
            return
        # The pack's shime*.png are the LEFT-facing image (Shimeji-ee loads the file as the
        # left image and mirrors it for the right — see ImagePairs.load). So we mirror when
        # looking RIGHT; the old `if not looking_right` showed the left art while facing right
        # and vice-versa, which made the walk look flipped.
        if self.core.state.looking_right:
            pm = pm.transformed(QTransform().scale(-1, 1))
        # Gripping the ceiling: the anchor (feet) is at the top and the body hangs below, so
        # flip vertically too. The anchor is the bottom-center of the standing image; after a
        # vertical flip it's the top-center, which is where the feet grip the ceiling.
        if getattr(self, "_on_ceiling", False):
            pm = pm.transformed(QTransform().scale(1, -1))
        pm = pm.scaled(self._px, self._px, Qt.AspectRatioMode.KeepAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
        self._rendered_frame = True
        self.frame_changed.emit(pm)

    def _load_pixmap(self, image: str) -> Optional[QPixmap]:
        if not image or self._mascot_dir is None:
            return None
        if image not in self._pixmap_cache:
            path = self._mascot_dir / image.lstrip("/")
            if not path.exists():
                logger.debug("missing mascot frame %s", path)
                return None
            reader = QImageReader(str(path))
            img = reader.read()
            if img.isNull():
                return None
            pm = QPixmap.fromImage(
                img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied))
            self._pixmap_cache[image] = pm
        return self._pixmap_cache[image]
