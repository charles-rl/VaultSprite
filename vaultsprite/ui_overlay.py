"""Transparent GUI & drag overlay (Module 1) + sprite playback host.

A frameless, always-on-top, fully translucent PySide6 window that renders the
mascot and supports click-vs-drag discrimination with a flick-velocity release.
Ported from koishi-ai-pet's ``base_window``/``pet_window`` per the extraction
doc — only window construction + drag repositioning survive; particles, context
actions, feed/chat/music bubbles are stripped (a minimal right-click menu keeps
the frameless pet controllable).

Cross-module signals:
- ``drag_started()``                  → main pauses physics + stat decay
- ``drag_released(vx, vy)``           → main forwards flick velocity to TerrainPhysics.release
- ``clicked()``                       → FSM/health reaction (petting / attention)
- ``ask_vision_requested(str)``       → "Ask what I see" menu item prompt
- ``stretch_requested()``             → menu-triggered health nudge

GIF playback lives in :class:`SpritePlayer` (QImageReader frame slicing at the
configured per-frame interval); *deciding* which state plays next is the pure
:class:`~vaultsprite.animation_fsm.AnimationFSM`. The window re-renders frames
through a single slot so it can apply the left/right walk flip.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QPoint, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import (QColor, QCursor, QFont, QFontMetrics, QIcon, QImage,
                           QImageReader, QMouseEvent, QMovie, QPainter, QPainterPath,
                           QPixmap, QTransform)
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMenu,
                               QSystemTrayIcon, QVBoxLayout, QWidget)

from .animation_fsm import StateTransition
from .config import Config, load_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GIF playback host — a native QMovie drives frames (correct timing + disposal +
# transparency); a single-shot hold timer fires ``state_finished`` after the
# state's configured duration. This is the documented M2 rendering path and sid-
# steps this PySide6 build's QImageReader multi-frame limitation entirely.
# ---------------------------------------------------------------------------
class SpritePlayer(QObject):

    frame_changed = Signal(QPixmap)      # render this frame now
    position_delta = Signal(int, int)    # per-frame movement (walking states)
    state_finished = Signal(str)         # hold duration elapsed → pick next state

    def __init__(self, default_frame_ms: int = 100, parent=None):
        super().__init__(parent)
        self._default_frame_ms = max(16, int(default_frame_ms))
        self._transition: Optional[StateTransition] = None
        self._movie: "QMovie | None" = None
        self._started = False

        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(
            lambda: self.state_finished.emit(self.current_state))

    # -- public -------------------------------------------------------------
    @property
    def current_state(self) -> str:
        return self._transition.name if self._transition else ""

    @property
    def is_playing(self) -> bool:
        # Track our own flag rather than the exact Qt MovieState enum constant,
        # whose spelling varies across PySide versions.
        return (self._movie is not None and self._started) or self._hold_timer.isActive()

    def play(self, transition: StateTransition):
        """Start (or restart) a state's animation from frame 0."""
        self.stop()
        self._transition = transition

        first_frame = self._read_first_frame(transition.sprite_path)
        if first_frame is None:   # missing/corrupt asset → still honor the hold
            logger.warning("no decodable frames for %s (%s)",
                           transition.name, transition.sprite_path)
            self.frame_changed.emit(QPixmap())          # blank; holds duration
            self._hold_timer.start(max(50, int(transition.duration_ms)))
            return

        self.frame_changed.emit(first_frame)

        movie = QMovie(str(transition.sprite_path))
        if movie.isValid() and movie.frameCount() > 1:   # animated sprite
            movie.frameChanged.connect(self._on_movie_frame)
            self._movie = movie
            self._started = True
            movie.start()
        # else: static single-frame asset — hold the still for its duration.
        self._hold_timer.start(max(50, int(transition.duration_ms)))

    def stop(self):
        if self._movie is not None:
            try:
                self._movie.stop()
            except RuntimeError:      # C++ object already destroyed
                pass
            self._movie = None
        self._hold_timer.stop()

    # -- internals --------------------------------------------------------------
    @staticmethod
    def _read_first_frame(path: Path) -> Optional[QPixmap]:
        """Decode the first frame (reliably, incl. its transparency) for an
        immediate paint before the QMovie's first async callback lands."""
        if not path.exists():
            return None
        reader = QImageReader(str(path))
        image = reader.read()
        if image.isNull():
            return None
        return QPixmap.fromImage(
            image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied))

    def _on_movie_frame(self, frame_no: int):
        movie = self._movie
        if movie is None:
            return
        image = movie.currentImage()
        if image.isNull():
            return
        self.frame_changed.emit(QPixmap.fromImage(
            image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)))
        tr = self._transition
        if tr is not None and (tr.dx or tr.dy):
            self.position_delta.emit(tr.dx, tr.dy)

# ---------------------------------------------------------------------------
# Speech bubble — small auto-hiding rounded panel above the pet
# ---------------------------------------------------------------------------
class SpeechBubble(QWidget):
    MAX_W = 280

    def __init__(self, parent=None):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._text = ""
        self._lines: list[str] = []            # pre-wrapped; drawn line-by-line below
        self._font = QFont()
        self._font.setPixelSize(13)
        self._fm = QFontMetrics(self._font)
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self.hide)

    def show_text(self, text: str, duration_ms: int = 5000):
        if not (text or "").strip():
            return
        self._text = " ".join(text.split())[:400]
        fm = self._fm
        # word-wrap ourselves and draw each line at an explicit baseline (paintEvent) —
        # Qt's own re-wrap inside a padded rect can produce MORE lines than our advance-
        # based wrap predicts (font rounding), which is what clipped the last line before.
        words, lines, cur = self._text.split(" "), [], ""
        for word in words:
            trial = (cur + " " + word).strip()
            if fm.horizontalAdvance(trial) > self.MAX_W - 24 and cur:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        if cur:
            lines.append(cur)
        self._lines = lines
        width = min(self.MAX_W,
                    max(90, max((fm.horizontalAdvance(l) for l in lines), default=60)) + 24)
        # Multi-line-safe height: full line boxes (lineSpacing each), plus the last
        # line's descent. The old fm.height()*n under-sized wrapped text by a partial
        # line, clipping its lower half (looked like "text just stops mid-sentence").
        top_pad, bot_pad = 9, 13
        height = (fm.ascent() + (len(lines) - 1) * fm.lineSpacing()
                  + fm.descent() + top_pad + bot_pad)
        self.setFixedSize(int(width), int(height))
        self.raise_()
        self.show()
        if duration_ms > 0:
            self._timeout.start(duration_ms)

    def position_above(self, pet_rect: QRectF):
        x = int(pet_rect.center().x() - self.width() / 2)
        y = int(pet_rect.top() - self.height() - 14)
        screen = QApplication.primaryScreen()
        if screen is not None:   # keep the bubble on-screen (near top edge)
            geo = screen.availableGeometry()
            x = max(int(geo.left()), min(x, int(geo.right()) - self.width()))
            y = max(int(geo.top()), y)
        self.move(x, y)

    def paintEvent(self, event):  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        rect = QRectF(1.5, 1.5, self.width() - 3, self.height() - 3)
        path.addRoundedRect(rect, 12, 12)
        cx = min(max(self.width() / 2, 18), self.width() - 18)
        tail = QPainterPath()
        tail.moveTo(cx - 9, rect.bottom() - 1)
        tail.lineTo(cx + 9, rect.bottom() - 1)
        tail.lineTo(cx, rect.bottom() + 8)
        tail.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(250, 251, 253, 236))
        painter.drawPath(path)
        painter.drawPath(tail)
        painter.setPen(QColor(30, 47, 88))
        painter.setFont(self._font)
        # draw each pre-wrapped line at an explicit baseline — deterministic layout that
        # matches show_text()'s geometry exactly (no in-rect re-wrap to surprise us)
        fm = self._fm
        base_x = rect.left() + 12
        base_y = rect.top() + 9 + fm.ascent()     # mirrors show_text()'s top pad
        for line in self._lines:
            painter.drawText(int(base_x), int(base_y), line)
            base_y += fm.lineSpacing()


# ---------------------------------------------------------------------------
# M9 telemetry overlay + system-tray manager (mirror Shimeji-Desktop's
# DebugWindow / TrayMenu; live coords/behavior/frame + global controls).
# ---------------------------------------------------------------------------
class TelemetryOverlay(QWidget):
    """Small always-on-top panel showing live M9 state (coords/behavior/frame)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(
            "background: rgba(20,20,30,200); color: #dfe6ff;"
            "font: 10px monospace; border-radius:6px; padding:4px;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        self._label = QLabel("mascot telemetry: n/a")
        self._label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self._label)
        self.setFixedSize(260, 84)
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.refresh)

    def start(self, getter):
        """`getter() -> dict` returns live fields each refresh tick."""
        self._getter = getter
        self.refresh()
        self.move(8, 8)
        self.show()
        self._timer.start()

    def refresh(self):
        g = getattr(self, "_getter", None)
        if g is None:
            return
        d = g()
        text = (f"pos=({d.get('x', 0)},{d.get('y', 0)})\n"
                f"behavior={d.get('behavior', '')}\n"
                f"frame={d.get('frame', '')}")
        self._label.setText(text)

    def closeEvent(self, event):  # noqa: N802
        self._timer.stop()
        super().closeEvent(event)


class SystemTray(QSystemTrayIcon):
    """Global manager: scale, per-behavior toggles (exclude-by-name), dismiss/quit."""

    scale_changed = Signal(float)          # scale factor applied by App
    dismiss_requested = Signal()
    quit_requested = Signal()

    def __init__(self, icon_path: str, behaviors: list[str], parent=None):
        super().__init__(parent)
        icon = QIcon(icon_path) if icon_path and Path(icon_path).exists() else QIcon()
        self.setIcon(icon)
        self.setToolTip("VaultSprite")
        self._behaviors = behaviors

        menu = QMenu()
        scale_menu = menu.addMenu("Scale")
        for label, factor in (("Small (0.7x)", 0.7), ("Default (1.0x)", 1.0),
                              ("Large (1.3x)", 1.3)):
            act = scale_menu.addAction(label)
            act.triggered.connect(lambda _=False, f=factor: self.scale_changed.emit(f))

        self._beh_actions: dict[str, object] = {}
        beh_menu = menu.addMenu("Behaviors (exclude)")
        for name in behaviors:
            a = beh_menu.addAction(name)
            a.setCheckable(True)
            a.setChecked(False)
            a.toggled.connect(lambda checked, n=name: self._beh_toggled(n, checked))
            self._beh_actions[name] = a

        menu.addSeparator()
        menu.addAction("Dismiss").triggered.connect(self.dismiss_requested.emit)
        menu.addAction("Quit VaultSprite").triggered.connect(self.quit_requested.emit)
        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)

    def _beh_toggled(self, name: str, checked: bool):
        self.behavior_toggled.emit(name, checked)

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            menu = self.contextMenu()
            if menu is not None:
                menu.popup(QCursor.pos())

    behavior_toggled = Signal(str, bool)


# ---------------------------------------------------------------------------
# The overlay window itself
# ---------------------------------------------------------------------------
class PetOverlayWindow(QWidget):
    drag_started = Signal()
    clicked = Signal()
    drag_released = Signal(float, float)            # px/s at release; ~0 for a plain drop
    ask_vision_requested = Signal(str)              # prompt from right-click menu
    stretch_requested = Signal()                    # "Stretch break" menu item

    def __init__(self, config: Optional[Config] = None):
        super().__init__()
        self.config = config or load_config()
        c = self.config.section("window")
        self._w = int(c.get("width", 96))
        self._h = int(c.get("height", 96))

        # transparent always-on-top frameless overlay (base_window.py flags)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFixedSize(self._w, self._h)

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(0, 0, 0, 0)
        self.pet_label: QLabel = QLabel()
        self.pet_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vbox.addWidget(self.pet_label)

        # drag/click discrimination (koishi port: 200ms timer + 5px threshold)
        self._drag_threshold_px = int(c.get("drag_threshold_px", 5))
        self._press_pos: Optional[QPoint] = None
        self._grab_local: Optional[QPoint] = None   # click point in window coords
        self._drag_history: list[tuple[QPoint, float]] = []
        self.max_drag_samples = int(c.get("max_drag_samples", 10))
        self.flick_speed_threshold = float(c.get("flick_speed_threshold", 80.0))
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(int(c.get("click_timeout_ms", 200)))
        self._click_timer.timeout.connect(self._on_click_confirmed)

        self.player: SpritePlayer = SpritePlayer(
            default_frame_ms=int(self.config.get("animation.default_frame_ms", 100)),
            parent=self)
        self.player.frame_changed.connect(self._render_frame)
        self.player.position_delta.connect(self._apply_position_delta)

        self.bubble = SpeechBubble()
        self.bubble.hide()

        self._flipped = False          # walk direction mirror
        self._transition_cache: Optional[StateTransition] = None

        self._place_initially()

    # -- drag state exposed to main (guards FSM/physics during a hold) -----------
    _dragging = False

    @property
    def dragging(self) -> bool:
        return self._dragging

    # -- Qt event overrides ------------------------------------------------------
    @staticmethod
    def _now_ms() -> float:
        # Monotonic clock in ms — a relative reference is exactly what release-
        # velocity needs (no wall-clock jumps, no timezone import).
        return time.monotonic() * 1000.0

    def mousePressEvent(self, event: QMouseEvent):  # noqa: N802 (Qt naming)
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.globalPosition().toPoint()
            self._grab_local = None
            self._drag_history.clear()
            self._click_timer.start()
        elif event.button() == Qt.MouseButton.RightButton:
            self._show_menu(event.globalPosition().toPoint())

    def mouseMoveEvent(self, event: QMouseEvent):  # noqa: N802 (Qt naming)
        if (self._click_timer.isActive() and self._press_pos is not None
                and self._grab_local is None):
            delta = event.globalPosition().toPoint() - self._press_pos
            if abs(delta.x()) + abs(delta.y()) >= self._drag_threshold_px:
                self._click_timer.stop()
                self._start_drag(event)
        # koishi parity (§3.3): the grab-offset guard alone gates movement — a
        # hover event can never enter this branch because _grab_local is None then.
        if self._grab_local is not None:
            new_pos = event.globalPosition().toPoint() - self._grab_local
            self.move(self._clamp(new_pos))
            self._drag_history.append((new_pos, self._now_ms()))
            if len(self._drag_history) > self.max_drag_samples:
                self._drag_history.pop(0)

    def mouseReleaseEvent(self, event: QMouseEvent):  # noqa: N802 (Qt naming)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        was_click = self._click_timer.isActive()
        if was_click:
            self._click_timer.stop()
            self._on_click_confirmed()
            return
        # only a real in-progress drag reaches here. A still-press whose timer already
        # confirmed the click has _dragging False → release nothing (no bogus drop).
        if not self._dragging:
            return
        vx, vy = 0.0, 0.0
        now = self._now_ms()
        recent = [(p, t) for p, t in self._drag_history if now - t <= 150]
        if len(recent) >= 2:   # only samples from the last ~150ms (koishi §3.3.6)
            (p1, t1), (p2, t2) = recent[0], recent[-1]
            dt = (t2 - t1) / 1000.0
            if dt > 0.005:     # ignore sub-5ms jitter clusters
                vx, vy = (p2.x() - p1.x()) / dt, (p2.y() - p1.y()) / dt
        self._drag_history.clear()
        speed = (vx * vx + vy * vy) ** 0.5
        # main treats |v| > flick_speed_threshold as an impulse; slower releases
        # become a plain drop handled by physics' gravity re-arm path.
        if not self.dragging:      # safety: no _grab_local but drag flag set (odd seq)
            vx = vy = 0.0
        logger.info("drag release v=(%.0f, %.0f) px/s", vx, vy)
        self._grab_local = None
        self._dragging = False
        self.drag_released.emit(vx, vy)

    # -- drag state ------------------------------------------------------------------
    def _start_drag(self, event: QMouseEvent):
        """Confirmed as a drag (moved past threshold before the click timer)."""
        was_held = self._dragging
        self._grab_local = event.position().toPoint()   # click-relative grab offset
        self._dragging = True
        if not was_held:
            self.player.stop()   # freeze mid-animation while held
            logger.debug("drag started at %s", self._grab_local)
            self.drag_started.emit()

    def resume_after_drag(self):
        """Main calls once the pet has landed after a drag release."""
        if self._transition_cache is not None and not self.player.is_playing:
            self.player.play(self._transition_cache)

    # -- animation entry points (driven by main from FSM decisions) --------------------
    def play_state(self, transition: StateTransition):
        """Play one FSM-decided state; remembers it for post-drag resume."""
        self._transition_cache = transition
        self.player.play(transition)

    def set_flipped(self, flipped: bool):
        """Mirror horizontally for walk direction; safe to call repeatedly."""
        self._flipped = flipped
        base = getattr(self, "_base_pixmap", None)
        if base is None or base.isNull():
            return
        transform = QTransform().scale(-1 if flipped else 1, 1)
        self.pet_label.setPixmap(base.transformed(transform))

    def render_mascot_frame(self, pixmap: QPixmap):
        """M9 Shimeji frames arrive already scaled + mirrored (MascotEngine)."""
        if pixmap and not pixmap.isNull():
            self.pet_label.setPixmap(pixmap)

    def set_scale(self, factor: float):
        """Resize the overlay (tray scale control); sprite rescales on next frame."""
        self._w = max(16, int(self._w * factor))
        self._h = max(16, int(self._h * factor))
        self.setFixedSize(self._w, self._h)

    def _render_frame(self, frame: QPixmap):
        """Scale to the window, then apply the walk-direction mirror. This single
        slot is where every animation frame lands (first + QMovie frames)."""
        if not frame.isNull():
            screen = QApplication.primaryScreen()
            dpr = screen.devicePixelRatio() if screen is not None else 1.0
            fit = max(16, int(min(self._w, self._h) * dpr))
            scaled = frame.scaled(fit, fit, Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
            if dpr > 1:
                scaled.setDevicePixelRatio(dpr)
            frame = scaled
        self._base_pixmap = frame
        self.set_flipped(self._flipped)

    # -- click & menu --------------------------------------------------------------------
    def _on_click_confirmed(self):
        self._press_pos = None
        logger.debug("pet clicked")
        self.clicked.emit()

    def _show_menu(self, global_pos: QPoint):
        menu = QMenu(self)
        ask_action = menu.addAction("Ask what I see")
        stretch_action = menu.addAction("Stretch break")
        menu.addSeparator()
        quit_action = menu.addAction("Quit VaultSprite")
        chosen = menu.exec(global_pos)
        if chosen is ask_action:
            self.ask_vision_requested.emit(
                "Look at my screen and tell me, in one sentence, what I appear to be doing.")
        elif chosen is stretch_action:
            self.stretch_requested.emit()
        elif chosen is quit_action:
            app = QApplication.instance()
            if app is not None:
                app.quit()

    # -- geometry helpers (callbacks injected by main/terrain) ------------------------------
    def move_to(self, x: int, y: int):
        self.move(QPoint(int(x), int(y)))

    def position(self) -> tuple[int, int]:
        p = self.pos()
        return int(p.x()), int(p.y())

    def size_px(self) -> tuple[int, int]:
        return self._w, self._h

    def rect_f(self) -> QRectF:
        g = self.geometry()
        return QRectF(g.x(), g.y(), g.width(), g.height())

    def show_bubble(self, text: str, duration_ms: int = 5000):
        self.bubble.position_above(self.rect_f())
        self.bubble.show_text(text, duration_ms)

    # -- walking movement (per-frame dx from the FSM config) ---------------------------------
    _walk_dir = 1   # ±1; flipped at screen walls so the pet turns around

    def reverse_walk(self):
        self._walk_dir *= -1
        self.set_flipped(not self._flipped)

    def _apply_position_delta(self, dx: int, dy: int):
        """Walk drift from moving states; bounces the sprite at screen walls."""
        if self._grab_local is not None or getattr(self.player, "_transition", None) is None:
            return
        x, y = self.position()
        step_x = dx * self._walk_dir
        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            if step_x < 0 and x + step_x <= geo.left():
                self.reverse_walk(); step_x = 0
            elif step_x > 0 and x + step_x >= geo.right() - self._w:
                self.reverse_walk(); step_x = 0
        self.move_to(x + step_x, y)

    # -- misc -------------------------------------------------------------------------------
    def _clamp(self, pos: QPoint) -> QPoint:
        screen = QApplication.primaryScreen()
        if screen is None:
            return pos
        geo = screen.availableGeometry()
        x = max(geo.left(), min(pos.x(), geo.right() - self._w))
        y = max(geo.top(), min(pos.y(), geo.bottom() - self._h))
        return QPoint(x, y)

    def _place_initially(self):
        """Start standing on the taskbar line at screen center (doc §3.2)."""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x = (geo.width() - self._w) // 2 + geo.left()
        y = geo.bottom() - self._h      # feet on the work-area bottom
        self.move(self._clamp(QPoint(x, y)))
