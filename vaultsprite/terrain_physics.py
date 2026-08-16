"""Desktop terrain physics & taskbar walking (Module 4).

Computes the desktop floor line (work-area bottom = taskbar-excluded), simulates
the post-drag-release fall with gravity/terminal velocity/wall bounce, and — on
Windows — lets the pet land on top of visible windows. Ported from koishi's
``gravity.py`` template + Shimeji floor resolution; all Win32 imports are
guarded so the module runs (and unit-tests) headless on Linux.

Coordinate contract: everything here is **logical** Qt pixels. Win32 rects come
back in physical pixels and are divided by ``devicePixelRatio()`` at the bridge.
"""
from __future__ import annotations

import logging
import math
import sys
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from .config import Config, load_config

logger = logging.getLogger(__name__)

try:  # Windows-only; must not break Linux imports/tests
    if sys.platform == "win32":
        import win32gui as _win32gui
        from ctypes import Structure, byref, wintypes

        class POINT(Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    else:
        _win32gui = None
except ImportError as exc:  # pragma: no cover - pywin32 missing
    logger.debug("pywin32 unavailable (%s); win32 terrain disabled", exc)
    _win32gui = None


class TerrainPhysics(QObject):
    """Floor query + fall simulation; repositions the pet via injected callbacks."""

    falling_started = Signal()          # entered a fall
    landed = Signal(int, int)           # (x, y) exact landing position
    standing_lost = Signal(str)         # window we stood on vanished (title)

    def __init__(self, config: Optional[Config] = None):
        super().__init__()
        self.config = config or load_config()
        c = self.config.section("physics")

        self._tick_ms = int(c.get("tick_ms", 30))
        self._gravity_accel = float(c.get("gravity_accel", 1.5))
        self._friction = float(c.get("friction", 0.99))
        self._max_speed = float(c.get("max_speed", 25.0))
        self._fall_terminal = float(c.get("fall_terminal", 8.0))
        self._wall_bounce = float(c.get("wall_bounce", -0.4))
        self._impulse_scale = float(c.get("impulse_scale", 0.05))
        self._stand_on_windows = bool(
            c.get("stand_on_windows", True) and _win32gui is not None
        )

        # injected by main(): (x, y) getter + (x, y) setter in logical px
        self._position: Callable[[], tuple[int, int]] | None = None
        self._move_to: Callable[[int, int], None] | None = None
        self._pet_size: Callable[[], tuple[int, int]] | None = None

        self._vx = 0.0
        self._vy = 0.0
        self._falling = False
        self._enabled = True
        self._in_flick = False
        # standee tracking (Windows only)
        self._standee: dict | None = None
        self._alive_tick = 0

        self._timer = QTimer(self)
        self._timer.setInterval(self._tick_ms)
        self._timer.timeout.connect(self._tick)

    # -- wiring ------------------------------------------------------------------
    def set_mover(
        self,
        position: Callable[[], tuple[int, int]],
        move_to: Callable[[int, int], None],
        pet_size: Callable[[], tuple[int, int]] = lambda: (96, 96),
    ):
        self._position = position
        self._move_to = move_to
        self._pet_size = pet_size

    def start(self):
        if not self._timer.isActive():
            self._timer.start()

    def stop(self):
        self._timer.stop()

    def enable(self, enabled: bool = True):
        """Drag pauses physics (overlay calls with False while dragging)."""
        self._enabled = enabled
        if enabled:
            self.reset_fall_state()
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()

    def reset_fall_state(self):
        self._falling = False
        self._in_flick = False
        self._vx = 0.0
        self._vy = 0.0
        self._standee = None

    @property
    def falling(self) -> bool:
        return self._falling

    def snap_to_ground(self):
        """Clamp the pet onto its current floor (startup / after resume)."""
        if self._position is None or self._move_to is None:
            return
        x, y = int(self._position()[0]), int(self._position()[1])
        h = int((self._pet_size() or (96, 96))[1])
        floor = self.get_floor_line(x)
        target_y = floor - h
        if abs(y - target_y) > 2:
            self._move_to(x, target_y)

    # -- public physics API --------------------------------------------------------
    def release(self, vx_px_s: float = 0.0, vy_px_s: float = 0.0):
        """Overlay released the pet after a drag (called by main).

        Re-enables the world (dragging had disabled it), then either injects a
        flick impulse (speed past ``window.flick_speed_threshold``) or — for slow
        releases — lets the gravity re-arm path start a natural fall so drops
        always settle.
        """
        self.enable(True)   # resume timer + clear stale fall/standee state
        threshold = float(self.config.get("window.flick_speed_threshold", 80.0)) \
            if self.config is not None else 80.0
        if math.hypot(vx_px_s, vy_px_s) > threshold:   # only genuine flicks throw
            self.apply_impulse(vx_px_s, vy_px_s)

    def _dpr(self) -> float:
        screen = QApplication.primaryScreen()
        return screen.devicePixelRatio() if screen else 1.0

    def get_floor_line(self, x: int | None = None) -> int:
        """Y (logical px) of the desktop surface at column ``x``.

        Work-area bottom is the safe default — it already excludes the taskbar
        and is DPI-correct from Qt's side. On Windows an optional Shell_TrayWnd
        rect would let us mirror exotic dock positions; work area covers all of
        those without extra Win32 surface area, so we stick to it here plus the
        visible-window sweep during falls.
        """
        screen = QApplication.primaryScreen()
        if screen is None:  # headless fallback for unit tests
            return int(self._get_fallback_floor())
        return screen.availableGeometry().bottom()

    def _get_fallback_floor(self) -> int:
        if self.config is not None:
            v = self.config.get("physics.test_floor")
            if v is not None:
                return int(v)
        return 720

    # -- public physics API -----------------------------------------------------------
    def apply_impulse(self, vx_px_s: float, vy_px_s: float):
        """Inject release-flick velocity (px/s) and begin falling."""
        if not self._enabled or self._position is None:
            return
        self._vx = max(-self._max_speed, min(vx_px_s * self._impulse_scale, self._max_speed))
        vy = vy_px_s * self._impulse_scale
        if vy >= 0:
            vy = -2.0        # guarantee liftoff on a pure-horizontal flick
        self._vy = max(-self._max_speed, min(vy, self._max_speed))
        self._in_flick = True
        self._standee = None
        if not self._falling:
            self._falling = True
            logger.info("apply_impulse vx=%.1f vy=%.1f px/tick", self._vx, self._vy)
            self.falling_started.emit()

    # -- tick loop ---------------------------------------------------------------------
    def _tick(self):
        if not self._enabled:
            return

        # standee lifecycle: is the window we're standing on still alive? (Windows only)
        if self._standee is not None and _win32gui is not None:
            self._alive_tick += 1
            if self._alive_tick >= 15:
                self._alive_tick = 0
                if not self._check_standee_alive():
                    title = (self._standee or {}).get("title", "")
                    self._standee = None
                    logger.info("standing window lost: %r", title[:60])
                    self.standing_lost.emit(title)
                    self.falling_started.emit()

        pos = self._position()
        x, y = int(pos[0]), int(pos[1])
        w, h = (self._pet_size() or (96, 96))[:2]

        # gravity re-arm: if we're floating above the current surface (e.g. after a
        # plain drop with no flick), start a natural fall so drops always settle.
        floor_now = self.get_floor_line(x)
        if not self._falling and y + h < floor_now - 2:
            self._falling = True
            logger.info("pet is floating above floor at x=%d; falling", x)
            self.falling_started.emit()

        if not self._falling:
            return
        old_bottom = y + h
        self._vy = min(self._vy + self._gravity_accel, self._fall_terminal)
        self._vx *= self._friction
        new_y = y + self._vy
        new_x = x + self._vx

        # walls (clamp within work area + bounce)
        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            if new_x <= geo.left():
                new_x, self._vx = geo.left(), -self._vx * self._wall_bounce
            elif new_x >= geo.right() - w:
                new_x, self._vx = geo.right() - w, -self._vx * self._wall_bounce

        floor = self.get_floor_line(new_x)          # desktop surface (work-area bottom)
        effective_bottom = floor

        # sweep test against visible window tops (Windows only): highest surface wins.
        # Only a surface at/below the pet's starting bottom is landable, otherwise
        # we would "land" on windows we are already passing above in mid-air.
        if self._stand_on_windows:
            landing = self._window_landing(x, new_x, old_bottom, int(new_y + h))
            if landing is not None:
                top_y, meta = landing
                if top_y >= old_bottom - 1:
                    effective_bottom = min(effective_bottom, top_y)
                    self._standee = {"top": top_y, "hwnd": meta["hwnd"],
                                     "title": meta.get("title", "")}

        at_bottom = new_y + h >= effective_bottom
        if at_bottom:
            landed_y = int(effective_bottom - h)
            self._move_to(int(new_x), landed_y)
            self._falling = False
            self._vy = 0.0
            # horizontal drift keeps the pet walking a little after touchdown
            self.landed.emit(int(new_x), landed_y)
        else:
            self._move_to(int(new_x), int(new_y))

    def _window_landing(self, x0: int, x1: int, old_bottom: int, new_bottom: int):
        """Return (top_y, meta) for a visible window the pet's feet crossed."""
        wins = self._get_visible_windows()
        w, h = (self._pet_size() or (96, 96))[:2]
        feet_l = min(x0, x1) + w // 3
        feet_r = max(x0, x1) - w // 3
        best: tuple[int, dict] | None = None
        for win in wins:
            top = win["top"]
            if old_bottom <= top <= new_bottom and not (feet_l >= win["right"] or feet_r <= win["left"]):
                if best is None or top < best[0]:
                    best = (top, {"hwnd": win["hwnd"], "title": win.get("title", "")})
        return best

    def _check_standee_alive(self) -> bool:
        if self._standee is None or not _win32gui:
            return True
        hwnd = self._standee.get("rect")
        try:
            alive = _win32gui.IsWindow(hwnd) if isinstance(hwnd, int) else False
            visible = _win32gui.IsWindowVisible(hwnd) if alive else False
        except Exception:
            alive, visible = True, True   # probe errors keep us standing (safe side)
        return bool(alive and visible)

    def _get_visible_windows(self) -> list[dict]:
        """Top edges of visible top-level windows in logical px (Windows only)."""
        if not _win32gui:
            return []
        out: list[dict] = []
        dpr = self._dpr()

        def cb(hwnd, _):
            try:
                if not _win32gui.IsWindowVisible(hwnd) or not _win32gui.IsWindow(hwnd):
                    return True
                l, t, r, b = _win32gui.GetWindowRect(hwnd)  # physical px
                w = (r - l) / dpr
                hgt = (b - t) / dpr
                if w < 60 or hgt < 60:                      # ignore slivers
                    return True
                out.append({
                    "hwnd": hwnd,
                    "left": int(l / dpr), "top": int(t / dpr),
                    "right": int(r / dpr), "bottom": int(b / dpr),
                    "title": _win32gui.GetWindowText(hwnd) or "",
                })
            except Exception:
                pass
            return True

        try:
            _win32gui.EnumWindows(cb, 0)
        except Exception as exc:  # pragma: no cover - win32 hiccups
            logger.debug("EnumWindows failed: %s", exc)
        return out
