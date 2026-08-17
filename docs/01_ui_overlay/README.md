# Module 1 — Transparent GUI & Drag Overlay

## 1. Module Overview & Objective

Provides the desktop-pet surface: a frameless, always-on-top, fully transparent PySide6 window that renders the sprite and can be repositioned by click-and-drag. Maps to **Module 1** of `IMPLEMENTATION_OUTLINE.md` and the `ui_overlay.py` node in the system-architecture diagram (the top-level entry point every other module talks to).

Extraction source: **`Koishi007/koishi-ai-pet`** (PySide6, pinned `PySide6==6.11.1`).

Targets extracted:
- Window flags: `FramelessWindowHint`, `WindowStaysOnTopHint`, `WA_TranslucentBackground` (plus `Tool` to hide from taskbar).
- Drag handlers: `mousePressEvent`, `mouseMoveEvent`, `mouseReleaseEvent` for desktop positioning, including a click-vs-drag discriminator and release-velocity flick detection.

## 2. Minimum Required Libraries

| Package | Why |
|---|---|
| `PySide6` | Qt6 GUI framework — window flags, `QWidget`, `QTimer`, mouse events |
| `QtGui` / `QtCore` / `QtWidgets` | Bundled with PySide6; `QMouseEvent`, `QPoint`, `QTimer`, `QDateTime` |
| `pywin32` (Windows only, *optional*) | `WS_EX_TRANSPARENT` click-through (`set_mouse_penetration`) — guard the import |

No system drivers required. On the Linux dev box run headless smoke tests with `QT_QPA_PLATFORM=offscreen`.

## 3. Source Code Extraction (Verbatim)

### 3.1 Transparent window base — `pet/ui/base_window.py`

```python
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt


class TransparentWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
```

### 3.2 Window setup & initial centering — `pet/ui/pet_window.py` (`PetWindow.__init__` + `_setup_ui`, lines 68–176)

```python
class PetWindow(TransparentWindow):
    def __init__(self):
        super().__init__()
        self._setup_ui()
        self._grab_local: QPoint | None = None
        ...
        self._drag_history: list = []  # [(坐标点, 时间戳毫秒), ...]
        self._press_pos: QPoint | None = None  # 按下时的全局坐标
        self._click_timer = QTimer(self)       # 单击检测定时器
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(200)      # 200ms 内无移动 → 判定为单击
        self._click_timer.timeout.connect(self._on_click_confirmed)
        ...

    def _setup_ui(self):
        self.setFixedSize(config.PET_WIDTH, config.PET_HEIGHT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.pet_label = QLabel()
        self.pet_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.pet_label)
        ...
        # 初始位置：屏幕中央
        from PySide6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            x = (geo.width() - config.PET_WIDTH) // 2
            y = (geo.height() - config.PET_HEIGHT) // 2
            self.move(x, y)
```

### 3.3 Mouse drag handlers — `pet/ui/pet_window.py`, lines 184–263

```python
def mousePressEvent(self, event: QMouseEvent):
    if event.button() == Qt.MouseButton.LeftButton:
        self._press_pos = event.globalPosition().toPoint()
        self._drag_history.clear()
        ...
        self._click_timer.start()
    elif event.button() == Qt.MouseButton.RightButton:
        self._show_context_menu(event.globalPosition().toPoint())

def _start_drag(self):
    """确认为拖拽：激活抓取状态。"""
    self._grab_local = QPoint(62, 20)
    self.pet_actions.gravity.enable(False)
    self.action_queue.pause()
    self.action_queue.clear()
    self.pet_actions.grabbed()

def _on_click_confirmed(self):
    """200ms 内无移动，判定为单击，并提升心理状态。"""
    self._press_pos = None
    self.particles.spawn("hearts")
    ...

def mouseMoveEvent(self, event: QMouseEvent):
    # 若单击定时器还在跑，检查是否已移动足够距离以判定为拖拽
    if self._click_timer.isActive() and self._press_pos is not None:
        delta = event.globalPosition().toPoint() - self._press_pos
        if delta.manhattanLength() >= 5:
            self._click_timer.stop()
            self._start_drag()
    if self._grab_local is not None:
        new_pos = event.globalPosition().toPoint() - self._grab_local
        self.move(new_pos)
        now = QDateTime.currentMSecsSinceEpoch()
        self._drag_history.append((new_pos, now))
        if len(self._drag_history) > 10:
            self._drag_history.pop(0)

def mouseReleaseEvent(self, event: QMouseEvent):
    if event.button() != Qt.MouseButton.LeftButton:
        return
    if self._click_timer.isActive():
        self._click_timer.stop()
        self._on_click_confirmed()
        return
    if self._grab_local is None:
        return
    self._grab_local = None
    self.action_queue.resume()
    vx, vy = 0.0, 0.0
    # 只使用最近 100ms 内的采样帧，过期帧视为停顿（避免释放前停顿导致速度为 0）
    now = QDateTime.currentMSecsSinceEpoch()
    recent = [(p, t) for p, t in self._drag_history if now - t <= 150]
    if len(recent) >= 2:
        p1, t1 = recent[0]
        p2, t2 = recent[-1]
        dt = (t2 - t1) / 1000.0
        if dt > 0.005:
            vx = (p2.x() - p1.x()) / dt
            vy = (p2.y() - p1.y()) / dt
    self._drag_history.clear()
    speed = (vx ** 2 + vy ** 2) ** 0.5
    self.pet_actions.gravity.enable(True)
    if speed > 80:
        self.pet_actions.gravity.apply_impulse(vx, vy)
```

### 3.4 Optional click-through — `pet/ui/pet_window.py` `set_mouse_penetration` (lines 455–476, abbreviated)

Uses Win32 `WS_EX_TRANSPARENT` via ctypes on Windows (`GWL_EXSTYLE = -20`), falling back to `Qt.WA_TransparentForMouseEvents` elsewhere. Guarded by `sys.platform == "win32"`.

## 4. Logic & Data Flow Breakdown

1. **Flag combination** (`base_window.py:8–12`): `FramelessWindowHint` removes the title bar/borders; `WindowStaysOnTopHint` keeps the pet above other windows; `Tool` demotes it from a normal top-level window so it never appears in the taskbar or Alt-Tab. Without `Tool` the frameless overlay would still occupy a taskbar slot.
2. **Alpha** (`base_window.py:13–14`): `WA_TranslucentBackground` makes the widget's background alpha-blended through the compositor; `WA_NoSystemBackground` prevents the system from painting an opaque backdrop behind it. Together they render the window fully transparent so only the sprite pixmap drawn on the `QLabel` is visible.
3. **Fixed size + centering** (`_setup_ui`): the window is sized to `PET_WIDTH × PET_HEIGHT` and initially centered on the primary screen's `availableGeometry()` (screen minus taskbar). A `QVBoxLayout` with zero margins hosts a single `QLabel` that the FSM module drives via `setPixmap`.
4. **Press** (`mousePressEvent`): records the global press point, clears drag history, and starts a 200 ms single-shot `_click_timer`. If released before moving, it counts as a click (here: spawn "hearts" particles + mood boost).
5. **Move threshold** (`mouseMoveEvent`): if the pointer travels ≥ 5 px (Manhattan distance) before the 200 ms timer fires, the timer is stopped and drag begins (`_start_drag`). While dragging, the window is moved so that a **fixed grab offset** `QPoint(62, 20)` sits under the cursor. A rolling 10-sample history of `(pos, timestamp)` is kept.
6. **Release** (`mouseReleaseEvent`): recomputes flick velocity from only the samples in the last 150 ms (so a deliberate pause before release doesn't zero the speed), clamps to `dt > 5 ms`, and if release speed exceeds `80 px/s` hands `(vx, vy)` to the gravity module as an impulse. Otherwise the pet just falls straight down.
7. **Cross-module contract**: the handlers call out to `pet_actions.gravity.enable()` / `apply_impulse()` (terrain module, M4) and `action_queue.pause()/resume()` (FSM module, M2) — the decoupling boundary we reproduce via signals.

## 5. Refactoring & Integration Notes

Target: a standalone `ui_overlay.py` exposing **`class PetOverlayWindow`** (a `QWidget`, not necessarily `QMainWindow` — the reference uses `QWidget`, which is lighter and correct for a single-content overlay).

Step-by-step:

1. **Keep only the two concerns**: transparent window construction + drag repositioning. Drop the context menu, chat/feed/music bubbles, particles, action queue, debug/log windows, and mouse-penetration UI.
2. **Reconstruct the class**:
   - `__init__`: apply the 3 flags + 2 attributes; build the single `QLabel` in a zero-margin `QVBoxLayout`; `setFixedSize(width, height)`; center on `QApplication.primaryScreen().availableGeometry()`.
   - Add a `sprite_changed = Signal(QPixmap)`-driven slot `set_sprite(pixmap)` → `self.pet_label.setPixmap(pixmap)` so the FSM module (M2) never touches the widget directly.
   - Expose `move_to(x, y)` and `pos()` so terrain physics (M4) can reposition the window without reaching into Qt internals.
3. **Port the drag handlers as-is** but replace the hardcoded `QPoint(62, 20)` grab offset with a click-relative one (`event.position() - rect.center()`) unless you specifically want the koishi "grab by the head" feel.
4. **Decouple the flick release** via a signal instead of calling gravity directly: emit `released_with_velocity = Signal(float, float)` (px/s) when `speed > 80`, and `drag_started = Signal()` / `clicked = Signal()` on their respective paths. `terrain_physics.py` (M4) subscribes to `released_with_velocity` and handles falling.
5. **Keep the click discriminator** (200 ms single-shot `QTimer` + 5 px Manhattan threshold) — it is essential so accidental micro-drags don't fight the sprite's idle animation.
6. **DPI awareness**: on high-DPI displays use `Qt.AA_EnableHighDpiScaling` at app creation (PySide6.4+ handles this by default) and be consistent about logical vs physical pixels when M4 passes coordinates.
7. **Testing**: `QApplication` requires an event loop; use `QT_QPA_PLATFORM=offscreen` for smoke tests that construct the window, `move()` it, and simulate events via `QTest.mouseMove`/`QTest.mousePress`. Verify `windowFlags()` contains all three flags and the window is translucent (`testAttribute(WA_TranslucentBackground)`).

## 6. Source Files (Reference Copies)

Full verbatim copies from `Koishi007/koishi-ai-pet`, kept locally so the build agent only needs this folder:

| File | Purpose |
|---|---|
| `source/base_window.py` | `TransparentWindow` — the 3 window flags + 2 attributes (the entire M1 core) |
| `source/pet_window.py` | `PetWindow` — full 481-line window: `_setup_ui`, centering, drag handlers, click discriminator, flick-velocity release, mouse penetration, context menu (strip most) |
| `source/config.py` | App config — `PET_WIDTH`/`PET_HEIGHT`, screen/dpi constants referenced by `_setup_ui` |
| `source/app.py` | `main()` startup wiring — `QApplication` + `setQuitOnLastWindowClosed(False)`, signal wiring, system tray, shutdown — a template for our own entry point |
