"""Qt driver for sequence (Android bundle) packs — the Sesame/lacis backend of M9.

Exposes the **same signal + method surface** as :class:`mascot_engine_widget.MascotEngine` so
``App`` can use either backend interchangeably; only construction differs (pack content decides,
see ``main.py``). Rendering loads the bundle's sequential sprites directly and drives them through
:meth:`MascotSequenceCore.tick`, reusing the same window-move interpolation / clamping conventions
as the XML backend so motion looks identical across packs.

Nothing here touches the XML path: the two backends share no mutable state.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QTransform, QImage, QPixmap
from PySide6.QtWidgets import QApplication

from .config import Config, load_config
from .mascot_sequence_pack import MascotSequenceCore, SeqState, load_sequence_pack

logger = logging.getLogger(__name__)


class MascotSequenceWidget(QObject):
    """QObject wrapper around :class:`MascotSequenceCore` with a `MascotEngine`-shaped API.

    Signals: ``frame_changed(QPixmap)``, ``behavior_changed(str)``,
    ``position_changed(int, int)``, ``debug_log(str)`` — identical to `MascotEngine`.
    """

    frame_changed = Signal(QPixmap)          # type: ignore[misc]  (defined below via import guard)
    behavior_changed = Signal(str)           # type: ignore[misc]
    position_changed = Signal(int, int)      # type: ignore[misc]
    debug_log = Signal(str)                  # type: ignore[misc]

    def __init__(self, config: Optional[Config] = None, parent=None):
        super().__init__(parent)
        self.config = config or load_config()
        m = self.config.section("mascot")
        self.tick_ms = max(10, int(m.get("tick_ms", 40) or 40))
        self._px = max(16, int(self.config.get("window.width", 96) or 96))

        pack_dir = self.config.mascot_pack_dir
        core: Optional[MascotSequenceCore] = None
        sprite_paths: list[Path] = []
        try:
            _manifest, animations, sprite_paths = load_sequence_pack(pack_dir)
            core = MascotSequenceCore(animations)
        except Exception as exc:   # noqa: BLE001 - never let a bad pack kill the app
            logger.warning("Mascot sequence engine failed to load %s: %s", pack_dir, exc)
        self.core = core
        self._sprite_paths = sprite_paths

        # timers exist even when disabled so stop()/set_dragging() stay safe on a dead backend
        self._pixmap_cache: dict[int, QPixmap] = {}
        self._smooth = bool(m.get("smooth_motion", True))
        self._interp_from = (0, 0)
        self._interp_to: Optional[tuple[int, int]] = None
        self._interp_elapsed = 0
        self._interp_total = max(1, self.tick_ms)
        self._pos_cur: Optional[tuple[int, int]] = None
        self._hide_walking = False

        # interpolate timer runs faster than the engine clock (throws must read as smooth),
        # same approach as MascotEngine.
        self._interp_timer = QTimer(self)
        self._interp_timer.setInterval(max(5, self.tick_ms // 4))
        self._interp_timer.timeout.connect(self._interp_step)

        self._timer = QTimer(self)
        self._timer.setInterval(self.tick_ms)
        self._timer.timeout.connect(self._tick)

    # -- introspection (MascotEngine-compatible names) ----------------------------
    @property
    def enabled(self) -> bool:
        return self.core is not None

    @property
    def behavior_names(self):
        return sorted(self.core.anims) if self.core else []

    @property
    def active_behavior(self) -> str:
        return (self.core.current_key or "") if self.core else ""

    # -- lifecycle -----------------------------------------------------------------
    def start(self):
        if not self.core or self._timer.isActive():
            return
        self._update_env_geometry()
        self.core.spawn()
        for _ in range(8):                       # land + render a frame before showing
            self._tick_core(render=True)
        st = self.core.state
        x, y = self._window_pos_for_anchor(st.anchor_x, st.anchor_y)
        self._pos_cur = (x, y)
        self.position_changed.emit(x, y)
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self._stop_interp()

    # -- interaction plumbing ------------------------------------------------------
    def set_dragging(self, dragging: bool):
        if not self.core:
            return
        self.core.set_dragging(dragging)
        if dragging:
            self._stop_interp()
        else:
            # release: window is at the drop point; reset lerp origin so a throw launches in place
            self._pos_cur = None
            self._stop_interp()

    def set_hide_walk(self, active: bool, moving_right: bool = True):
        if not self.core:
            return
        self._hide_walking = bool(active)
        if active:
            self._stop_interp()                  # App owns the window while walking
            if not self._timer.isActive():
                self._timer.start()
            self.core.set_hide_walk(True, moving_right)

    def set_hidden(self, hidden: bool):
        """Freeze / resume ambient (mirrors MascotEngine; no re-seed on reveal)."""
        if hidden:
            self._timer.stop()
            self._stop_interp()
            return
        if self._timer.isActive() or not self.core:
            return
        for _ in range(8):
            self._tick_core(render=True)
        self._timer.start()

    def force_behavior(self, name: str):
        if self.core:
            self.core.force(name)

    def sync_anchor(self, x: float, y: float):
        """Snap the core anchor to a real screen position (drag release)."""
        if not self.core:
            return
        st = self.core.state
        st.anchor_x, st.anchor_y = float(x), float(y)

    def inject_throw(self, vx_px_s: float, vy_px_s: float):
        """Feed a flick's release velocity (px/s). Convert to px/tick and arm the fling."""
        if not self.core:
            return
        ticks = 1000.0 / max(1, self.tick_ms)
        st = self.core.state
        st.vx = vx_px_s / ticks
        st.vy = vy_px_s / ticks
        st.facing_right = vx_px_s >= 0          # face the throw direction (event-velocity rule)
        if "fling" in self.core.anims:
            st.in_fling = True

    def respawn(self):
        """Re-settle after a scale change: recentre on the floor + drop in."""
        if not self.core:
            return
        self._update_env_geometry()
        self.core.spawn()
        for _ in range(8):
            self._tick_core(render=True)

    def anchor(self) -> tuple[int, int]:
        if self.core:
            st = self.core.state
            return int(st.anchor_x), int(st.anchor_y)
        return 0, 0

    def current_frame(self) -> str:
        if not self.core:
            return ""
        a = self.core.anims.get(self.core.current_key)
        if not a or not a.frames:
            return ""
        return f"{a.frames[self.core.frame_index].sprite}"

    def toggle_excluded(self, name: str, exclude: bool):
        pass     # sequence packs have no behavior-frequency pool to gate (documented)

    def set_tracked_window(self, rect=None):
        pass     # compromise: the Android bundle format has no window/IE interaction

    # -- internals -------------------------------------------------------------------
    def _update_env_geometry(self):
        if not self.core:
            return
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        left, top = float(geo.left()), float(geo.top())
        right, bottom = float(geo.left() + geo.width()), float(geo.top() + geo.height())
        self.core.set_work_area(left, right, top, bottom)

    def _tick(self):
        if not self.core:
            return
        try:
            self._update_env_geometry()
            self._tick_core(render=True)
        except Exception as exc:   # noqa: BLE001 - a bad tick must never kill the app
            logger.warning("mascot sequence tick skipped: %s", exc)

    def _tick_core(self, render: bool = True):
        core = self.core
        if core is None or core.state is None:
            return
        st: SeqState = core.state
        core.tick()

        a = core.anims.get(core.current_key)
        if a and a.frames:
            frame = a.frames[core.frame_index]
            pm = self._load_pixmap(frame.sprite)
            if pm is not None:
                emit_pm = pm
                if st.facing_right:              # base art faces left (bundle convention)
                    emit_pm = emit_pm.transformed(QTransform().scale(-1, 1))
                if a.is_ceiling:                 # hang frames are authored upside-down → flip
                    emit_pm = emit_pm.transformed(QTransform().scale(1, -1))
                scaled = self._scaled(emit_pm)
                self.frame_changed.emit(scaled)

        if not st.dragging and not self._hide_walking:
            x, y = self._window_pos_for_anchor(st.anchor_x, st.anchor_y)
            self._set_target(x, y)

    def _scaled(self, pm):
        return pm.scaled(self._px, self._px, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)

    def _window_pos_for_anchor(self, ax: float, ay: float) -> tuple[int, int]:
        """Anchor = feet at bottom-center of the square frame; keep the whole window on-screen.

        Mirrors MascotEngine._clamp_pos (2026-08-21): clamps in window space so full-canvas
        bundle art sits flush against a side wall instead of being clipped by the screen."""
        px = self._px
        screen = QApplication.primaryScreen()
        if screen is None:
            return int(ax) - px // 2, int(ay) - px
        geo = screen.availableGeometry()
        hi = max(float(geo.left()), float(geo.left()) + geo.width() - px)
        wx = max(float(geo.left()), min(int(ax) - px // 2, hi))
        wy = max(float(geo.top()), min(int(ay) - px, float(geo.top()) + geo.height() - px))
        return int(wx), int(wy)

    def _set_target(self, x: int, y: int):
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
        if hasattr(self, "_interp_timer"):
            self._interp_timer.stop()

    def _interp_step(self):
        """Ease the window from the previous engine target to the current one (smoothstep)."""
        if self._interp_to is None:
            self._interp_timer.stop()
            return
        self._interp_elapsed += self._interp_timer.interval()
        t = min(1.0, float(self._interp_elapsed) / max(1, self._interp_total))
        t = t * t * (3.0 - 2.0 * t)                       # smoothstep ease
        fx, fy = self._interp_from
        tx, ty = self._interp_to
        x = int(fx + (tx - fx) * t)
        y = int(fy + (ty - fy) * t)
        self._pos_cur = (x, y)
        self.position_changed.emit(x, y)
        if t >= 1.0:
            self._interp_to = None
            self._interp_timer.stop()

    def _load_pixmap(self, sprite_index: int) -> Optional[QPixmap]:
        if not (0 <= sprite_index < len(self._sprite_paths)):
            return None
        pm = self._pixmap_cache.get(sprite_index)
        if pm is None:
            path = self._sprite_paths[sprite_index]
            try:
                img = QImage(str(path))
            except Exception as exc:  # noqa: BLE001 - a bad frame must not crash the app
                logger.warning("sequence sprite load failed %s: %s", path, exc)
                return None
            if img.isNull():
                return None
            pm = QPixmap.fromImage(img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied))
            self._pixmap_cache[sprite_index] = pm
        return pm
