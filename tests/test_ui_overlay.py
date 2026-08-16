"""PetOverlayWindow: transparent window flags + click-vs-drag discrimination.

Mouse events are built by hand (``QMouseEvent``) so ``globalPosition()`` is
exactly the value we assert on — QTest re-maps positions against the widget's
*current* geometry, which shifts as a drag moves the window.
"""
from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QEvent, QPointF, QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from tests.conftest import FakeConfig
from vaultsprite.ui_overlay import PetOverlayWindow


@pytest.fixture()
def window(qapp):
    cfg = FakeConfig({"window.click_timeout_ms": 120, "window.drag_threshold_px": 5})
    w = PetOverlayWindow(cfg)
    yield w
    w.close()


def _evt(type_, local: QPoint, scene: QPoint, g: QPoint,
         button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.NoButton):
    return QMouseEvent(type_, QPointF(local.x(), local.y()), QPointF(scene.x(), scene.y()),
                       g, button, buttons, Qt.KeyboardModifier.NoModifier)


def _press(window, local: tuple[int, int]):
    origin = QPoint(*window.position())
    lp = QPoint(*local)
    window.mousePressEvent(_evt(QEvent.Type.MouseButtonPress, lp, lp, origin + lp))


def _move_held(window, global_pos: QPoint):
    """Move with the left button held. Local/scene are derived from the live origin
    (``mapFromGlobal(global)``), so the grab offset captured at drag-start is real."""
    local = global_pos - QPoint(*window.position())
    window.mouseMoveEvent(_evt(QEvent.Type.MouseMove, local, local, global_pos,
                               button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.LeftButton))


def _release(window, global_pos: QPoint):
    local = global_pos - QPoint(*window.position())
    window.mouseReleaseEvent(_evt(QEvent.Type.MouseButtonRelease, local, local, global_pos))


# -- the module-1 contract: flags & translucency ------------------------------------
def test_transparent_flags(window):
    flags = window.windowFlags()
    assert Qt.WindowType.FramelessWindowHint in flags
    assert Qt.WindowType.WindowStaysOnTopHint in flags
    # Tool keeps it out of the taskbar / Alt-Tab (required for a frameless overlay)
    assert window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)


def test_fixed_size_matches_config(window):
    assert window.width() == 128 and window.height() == 128   # config.yaml defaults
    assert window.pet_label is not None
    x, y = window.position()
    assert (x, y) >= (0, 0)


# -- click vs drag via hand-built global-position events ------------------------------
def test_plain_click_emits_clicked(window):
    clicks = []
    releases = []
    window.clicked.connect(lambda: clicks.append(1))
    window.drag_released.connect(lambda vx, vy: releases.append((vx, vy)))

    start = QPoint(*window.position())   # nothing moves the window during a still press
    _press(window, (30, 30))
    app = QApplication.instance()

    # release ~40ms later — still inside the click-confirmation window (<120ms)
    deadline = time.time() + 0.04
    while time.time() < deadline:
        app.processEvents(); time.sleep(0.005)
    assert not clicks, "click must not confirm before release"

    _release(window, start + QPoint(30, 30))   # same point → still a click
    deadline = time.time() + 0.6
    while clicks == [] and time.time() < deadline:
        app.processEvents(); time.sleep(0.01)
    assert clicks == [1]
    assert releases == [], "a plain click must not count as a drag release"


def test_drag_moving_window_and_emits_release(window):
    """Press at local (30,30); the first move past 5px sets grab=(48,49) — then each
    cursor position places the origin at ``cursor_global − grab``. A final cursor at
    (start+78, start+82) therefore yields an origin shifted by exactly (30, 33)."""
    releases = []
    window.drag_released.connect(lambda vx, vy: releases.append((vx, vy)))

    # The pet starts standing on the taskbar line; any downward drag would be
    # clamped there (correct behaviour). Lift it 150px first so a +33px drop has room.
    window.move(QPoint(*window.position()) - QPoint(0, 150))
    start = QPoint(*window.position())           # fixed reference for all globals
    _press(window, (30, 30))
    _move_held(window, start + QPoint(32, 31))   # manhattan 2 < 5 → still a click candidate
    app = QApplication.instance(); app.processEvents()
    assert window._grab_local is None

    _move_held(window, start + QPoint(48, 49))   # crosses threshold → drag starts here
    _move_held(window, start + QPoint(78, 82))
    app.processEvents()
    assert window._grab_local == QPoint(48, 49)   # grab = triggering move's local point

    cur = QPoint(*window.position())
    assert (cur.x(), cur.y()) == (start.x() + 30, start.y() + 33)

    # release cursor one grab-offset past the origin (same point as last move + offset)
    _release(window, cur + QPoint(48, 49))
    app.processEvents()
    assert releases, "drag release should emit drag_released(vx, vy)"
    vx, vy = releases[-1]
    assert isinstance(vx, float) and isinstance(vy, float)


def test_state_playback_and_flip(window):
    """play_state drives the player; set_flipped mirrors without crashing."""
    from pathlib import Path
    from vaultsprite.animation_fsm import AnimationFSM

    fsm = AnimationFSM(Path(__file__).resolve().parent.parent / "assets" / "config.yaml")
    tr = fsm.force_state("idle")
    window.play_state(tr)
    assert not window.pet_label.pixmap().isNull()
    assert window.player.is_playing

    window.set_flipped(True)
    window.set_flipped(False)   # back to normal; must stay non-null
    assert not window.pet_label.pixmap().isNull()
