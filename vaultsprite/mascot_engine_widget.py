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
from .mascot_engine import MascotCore
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

        self._pixmap_cache: dict[str, QPixmap] = {}
        self._cursor = None
        self._dragging = False
        self._rendered_frame = False

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
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def set_dragging(self, dragging: bool):
        self._dragging = dragging
        if self.core:
            self.core.state.dragging = dragging

    def set_hidden(self, hidden: bool):
        """Freeze the ambient engine while the pet is hidden off-screen.

        ``hidden=True`` stops the tick timer so the core stays at its current anchor
        and never moves the window or changes behavior while the pet is away;
        ``hidden=False`` restarts ambient animation WITHOUT re-seeding the anchor
        (unlike :meth:`start`, which puts a fresh pet at floor center)."""
        if hidden:
            self._timer.stop()
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
        self.core.env.cursor.dx = vx_px_s / ticks
        self.core.env.cursor.dy = vy_px_s / ticks

    def set_tracked_window(self, rect: Optional[dict] = None):
        if self.core:
            self.core.update_environment(tracked_window=rect)

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
            px, py = self._cursor if self._cursor is not None else (cx, cy)
            dx, dy = cx - px, cy - py
            self._cursor = (cx, cy)
            self.core.update_environment(cursor_pos=(cx, cy, dx, dy))
            self.core.tick()
            # move the window to follow the core anchor (unless the user is dragging it)
            if not self._dragging:
                st = self.core.state
                x = int(st.anchor.x) - self._px // 2
                y = int(st.anchor.y) - self._px
                self.position_changed.emit(x, y)
        except Exception as exc:   # noqa: BLE001 - a bad tick must never kill the app
            logger.warning("mascot tick skipped: %s", exc)

    def _on_behavior(self, name: str):
        self.behavior_changed.emit(name)

    def _on_frame(self, pose):
        pm = self._load_pixmap(pose.image)
        if pm is None or self.core is None:
            return
        if not self.core.state.looking_right:
            pm = pm.transformed(QTransform().scale(-1, 1))
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
