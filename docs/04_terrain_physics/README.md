# Module 4 — Desktop Terrain Physics & Taskbar Walking

## 1. Module Overview & Objective

Computes the desktop "terrain" the pet walks on: the floor line (taskbar top / monitor work-area bottom), optional standing on the active window's top edge, wall/ceiling bounds, and **Y-axis gravity** applied when a drag is released above the floor. Drives the sprite's `Y` position during fall states until `Y_pet ≥ Y_floor`.

Maps to **Module 4** of `IMPLEMENTATION_OUTLINE.md`; produces `terrain_physics.py` (a lightweight PyWin32 wrapper + fall simulation). Because the Linux dev box has no Win32, all win32 imports must be guarded/mocked.

Extraction sources:
- **`akitak1290/desktop-pets`** (`pets.py`, tkinter): work-area bounds query + tick-based move/clamp/floor-snap loop. **Lacks** `Shell_TrayWnd` and gravity acceleration.
- **Shimeji-EE family** (`DalekCraft2/Shimeji-Desktop`, classic `TigerHix/shimeji-ee`): the canonical taskbar/work-area detection, per-tick gravity math, and exact-equality landing (documented in §3.3 as the reference formulas).
- **`Koishi007/koishi-ai-pet`** (`pet/action/gravity.py`): the best Python/PySide6 model — explicit terminal velocity, sweep test, and landing snap. Primary port template.

## 2. Minimum Required Libraries

| Package | Why |
|---|---|
| `pywin32` (`pywin32` package) | `win32gui.FindWindow`/`GetWindowRect`/`GetForegroundWindow`/`EnumWindows`, `win32api.GetMonitorInfo`/`MonitorFromPoint` — **Windows only** |
| `ctypes` (stdlib) | Optional `SHAppBarMessage` fallback and low-level monitor structs |
| `PySide6` | `QTimer` tick + `QWidget.move()` repositioning |

**Guarding**: `terrain_physics.py` must do `try: import win32gui ... except ImportError: win32gui = None` (or branch on `sys.platform`) so the module imports on Linux; the physics falls back to the Qt screen `availableGeometry()` as floor.

## 3. Source Code Extraction (Verbatim)

### 3.1 Work-area floor — `akitak1290/desktop-pets/pets.py` (lines 59–62)

```python
monitor_info = GetMonitorInfo(MonitorFromPoint((0, 0)))
work_area = monitor_info.get("Work")
self.screen_width = work_area[2]
self.screen_height = work_area[3]
```

The **`Work`** rect of the monitor containing (0,0) is the screen minus the taskbar — this is the "floor" source of truth (matches Shimeji's `rcWork`, §3.3).

### 3.2 Tick-based move, clamp, and floor snap — `desktop-pets/pets.py` `Pet.draw_loop` (lines 1256–1305)

```python
# ... (speeds_list indexed by current action, read as (x_speed, y_speed) px per 100ms frame)
if self.y >= self.screen_height - self.pet_height and not self.is_drag:
    self.falling = False
    if self.y > screen_height - pet_height:
        y_dist = (screen_height - pet_height) - self.y
    else:
        y_dist = 0
else:
    ...
self.x += x_dist
self.y += y_dist
self.canvas.move(self.image_container, x_dist, y_dist)
...
# release handler:
def release_pet(...):
    self.falling = True
# run loop:
self.after_id = self.canvas.after(100, self.draw_loop)   # fixed 100ms tick
```

The fall here is **constant per-frame velocity** (from config, e.g. `[0, 5]` = 5 px/100 ms ≈ 50 px/s) with a floor test `y >= screen_height - pet_height` that snaps the Y back to exactly the floor. No acceleration/terminal velocity — that comes from Shimeji/koishi below.

### 3.3 Shimeji-EE gravity & floor reference (Java → formulas)

**Tick cadence**: `Manager.TICK_INTERVAL = 40` ms.

**Constants** (`action/Fall.java`):

| Name | Default | Meaning |
|---|---|---|
| `InitialVX` / `InitialVY` | 0 / 0 | starting velocity (px/tick) |
| `ResistanceX` | 0.05 | horizontal drag factor |
| `ResistanceY` | 0.10 | vertical drag factor |
| `Gravity` | 2 | px/tick² downward acceleration |

**Per-tick update** (damped velocity + gravity, with a sub-pixel accumulator and substep sweep):

```java
vx = vx - (vx * ResistanceX);                       // 0.95 * vx
vy = vy - (vy * ResistanceY) + Gravity;             // 0.90 * vy + 2

modX += (vx % 1);   int dx = (int)vx + (int)modX;   modX %= 1;
modY += (vy % 1);   int dy = (int)vy + (int)modY;   modY %= 1;

// substep along the segment to catch fast walls/floors
dev = max(1, max(|dx|, |dy|));
for (i in 0..=dev):
    x = start.x + dx*i/dev; y = start.y + dy*i/dev
    mascot.setAnchor(x, y)
    if (dy > 0):
        // HACK-IE: scan up to 80px above for the top of the active window
        for j in -80..=0:  setAnchor(x, y+j); if floor(true).isOn(anchor): break OUTER;
    if wall(true).isOn(anchor): break
```

**Landing = exact equality**: `FloorCeiling.isOn(location)` returns true only when `getY() == location.y` (plus an x-span check). `Fall.hasNext()` ends the fall once grounded:

```java
boolean onBorder = false;
if (environment.getFloor().isOn(pos))  onBorder = true;   // work-area/taskbar line OR active-window top
if (environment.getWall().isOn(pos))   onBorder = true;
return super.hasNext() && !onBorder;
```

**Effective terminal velocity**: fixed point of `vy = 0.9·vy + 2` ⇒ `vy∞ = 2/(1−0.9) = 20 px/tick ≈ 500 px/s`. Shimeji relies on resistance as the cap — no explicit clamp.

**Floor resolution** (which boundary is the ground — `MascotEnvironment`):
1. `activeIE.getTopBorder().isOn(anchor)` — top edge of the **active window** (stand-on-window);
2. `workArea.getBottomBorder().isOn(anchor)` — work-area bottom (= taskbar line), gated by the "only one monitor seam" check `isScreenTopBottom`.

**Multi-monitor**: per-monitor work areas from `GetMonitorInfo(...).rcWork` (JNA: `MonitorFromPoint(point, MONITOR_DEFAULTTOPRIMARY)` + `GetMonitorInfo`); floor = `rcWork.bottom`. Never special-cases `Shell_TrayWnd`; the work area already excludes the bar.

**Win32 recipe (Python/pywin32)** — `Shell_TrayWnd` bounding box and active-window top:

```python
import win32gui

# A. Primary taskbar rect
hwnd = win32gui.FindWindow("Shell_TrayWnd", None)
if hwnd:
    l, t, r, b = win32gui.GetWindowRect(hwnd)      # (left, top, right, bottom) PHYSICAL px

# B. Secondary/extra-monitor taskbars (one per extra monitor)
def secondary_taskbars():
    out = []
    def cb(h, _):
        if win32gui.GetClassName(h) == "Shell_SecondaryTrayWnd":
            out.append(win32gui.GetWindowRect(h))
        return True
    win32gui.EnumWindows(cb, 0)
    return out

# C. Landing on the ACTIVE window's top
fg = win32gui.GetForegroundWindow()
if fg and win32gui.IsWindowVisible(fg):
    fl, ft, fr, fb = win32gui.GetWindowRect(fg)    # pet stands at y == ft (top edge)

# D. Fallback floor via monitor work area (DPI-safe, recommended)
from ctypes import wintypes, Structure, byref
import ctypes
class POINT(Structure): _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]
user32 = ctypes.windll.user32
def work_area_at(x, y):
    MONITOR_DEFAULTTOPRIMARY = 2
    hmon = user32.MonitorFromPoint(POINT(int(x), int(y)), MONITOR_DEFAULTTOPRIMARY)
    mi = wintypes.MONITORINFO(); mi.cbSize = 40
    user32.GetMonitorInfoW(hmon, byref(mi))
    wa = mi.rcWork                               # taskbar excluded
    return wa.left, wa.top, wa.right, wa.bottom
```

> **DPI gotcha**: `GetWindowRect`/`SHAppBarMessage` return **physical** pixels; Qt geometry is **logical**. Divide physical values by `QApplication.primaryScreen().devicePixelRatio()`. This is why work-area is preferred for a DPI-aware Qt pet.

**SHAppBarMessage alternative** (`ABM_GETTASKBARPOS = 5` via `windll.shell32`): returns the *primary* taskbar rect + edge, but in physical px and primary-monitor only — use only when you need the literal bar geometry.

### 3.4 Python/PySide6 gravity port template — `koishi-ai-pet/pet/action/gravity.py` (key parts)

```python
class GravitySystem(QObject):
    falling_started = Signal()  # 进入下落状态时发出
    landed = Signal()           # 落地时发出
    standing_lost = Signal(str) # 站立的窗口消失/被遮挡

    _GRAVITY_ACCEL  = 1.5   # px/tick²
    _FRICTION       = 0.99
    _MAX_SPEED      = 25.0  # px/tick
    _FALL_TERMINAL  = 8.0   # px/tick (hard cap — cleaner than Shimeji's implicit cap)
    _WALL_BOUNCE    = -0.4
    _IMPULSE_SCALE  = 0.05  # px/s → px/tick (30ms tick)

    def __init__(self, window, animator, win_anims, parent=None):
        ...
        self._interval = 30                      # 30 ms tick (vs Shimeji 40 ms)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(self._interval)
        ...

    def apply_impulse(self, vx: float, vy: float):
        self._vx = max(-self._MAX_SPEED, min(vx * self._IMPULSE_SCALE, self._MAX_SPEED))
        scaled_vy = vy * self._IMPULSE_SCALE
        if scaled_vy >= 0:
            scaled_vy = min(scaled_vy, -2.0)     # guarantee liftoff
        self._vy = max(-self._MAX_SPEED, min(scaled_vy, self._MAX_SPEED))
        self._in_flick = True
        ...
        self.falling_started.emit()

    def _tick(self):                             # (simplified)
        old_y = self._window.y()
        self._vy = min(self._vy + self._GRAVITY_ACCEL, self._FALL_TERMINAL)
        new_y = old_y + self._vy
        screen_bottom = screen.availableGeometry().bottom() - h
        ...
        # sweep test across visible windows:
        old_pet_bottom = old_y + h
        new_pet_bottom = new_y + h
        for win in get_visible_windows():
            if feet_l >= right or feet_r <= left:      # x-overlap of the pet's "feet"
                continue
            if old_pet_bottom <= top <= new_pet_bottom:  # crossed the window's top this tick
                if not occluded(win["hwnd"]):
                    landing = top - h
                    if landing < effective_bottom:
                        effective_bottom = landing      # nearest (highest) surface wins
        ...
        at_bottom = new_y >= effective_bottom
        if at_bottom:
            new_y = effective_bottom                   # snap exactly to floor
        self._window.move(...)
        if at_bottom and self._falling:
            self._falling = False
            self._vy = 0.0
            self.landed.emit()
```

## 4. Logic & Data Flow Breakdown

1. **Floor resolution** (§3.1/3.3): at tick time the module resolves `effective_bottom = min(screen_work_area_bottom, top_of_any_window_under_feet)`. The work-area bottom is the safe default (taskbar already excluded, DPI-safe). `Shell_TrayWnd`/`GetWindowRect` is used only when you need the *bar's literal rect* (side/top-docked bars) or when explicitly mirroring the outline's `FindWindow("Shell_TrayWnd", None)` + `GetWindowRect()` call.
2. **Velocity model**: `vy = min(vy + GRAVITY_ACCEL, FALL_TERMINAL)` each tick (koishi's explicit terminal = cleaner than Shimeji's implicit resistance cap). Horizontal flick velocity decays by `FRICTION` per tick and bounces off walls with `_WALL_BOUNCE`. Sub-pixel accumulation (Shimeji §3.3) keeps slow falls smooth.
3. **Sweep/crossing test** (`old_pet_bottom <= top <= new_pet_bottom`): instead of Shimeji's pixel-perfect `==`, the port checks whether the pet's bottom **crossed** a surface during this tick, then snaps `y = top − height`. This prevents tunneling at high terminal velocity. The pet's "feet" are the middle third of its width (`feet_l/feet_r = x + w//3 .. x + 2w//3`).
4. **Occlusion & standee-lifecycle** (koishi `_tick`): only visible, non-cloaked windows are landable; once standing, the module periodically re-checks that the window still exists/moved — otherwise emits `standing_lost(title)` and resumes falling.
5. **Landing**: on contact, `vy = 0`, position snaps to the surface, `falling = False`, and `landed` is emitted (the overlay/FSM plays the "idle" animation). If the tick leaves the pet above the floor with no surface, it transitions to falling and plays the "falling" animation.
6. **Frame loop**: `QTimer` at 30 ms (koishi) or 100 ms (desktop-pets) calls `_tick`, which ends with `self._window.move(x, y)`. Dragging pauses the timer (`enable(False)`), release re-enables it and injects `apply_impulse(vx, vy)` from the overlay's flick handler.

## 5. Refactoring & Integration Notes

Target: `terrain_physics.py` exposing **`class TerrainPhysics(QObject)`** that (a) queries the floor and (b) simulates falls, without touching GUI code directly.

Step-by-step:

1. **Guard all win32 imports**:
   ```python
   try:
       import win32gui, win32api, win32con
   except ImportError:
       win32gui = win32api = win32con = None   # Linux dev box / headless tests
   ```
   When `win32gui is None`, `get_floor()` falls back to `QApplication.primaryScreen().availableGeometry()`.
2. **Split into two public concerns**:
   - `get_floor_line(x) -> int` — pure query: work-area bottom (default) with optional `Shell_TrayWnd`/`Shell_SecondaryTrayWnd` rects for exotic dock positions; returns logical px (divide by DPR).
   - `class FallSimulation` — the tick loop: `start()/stop()`, `apply_impulse(vx, vy)`, internal `_vy`, emits `landed`/`falling_started`/`standing_lost`. Port the koishi sweep/snap logic §3.4 directly.
3. **Interface to the overlay (M1)**: subscribe to the overlay's `released_with_velocity(float, float)` signal → `apply_impulse`. Move the window via an injected callback `move_to(x, y)` (avoids reaching into Qt widgets from physics).
4. **Interface to the FSM (M2)**: on `falling_started`/`landed`, trigger the FSM `falling`/`idle` states via the module signal bus; return `position_delta` if the FSM wants to own walking movement.
5. **Drop from the reference**: the tkinter canvas/renderer (desktop-pets), shop/inventory (N/A here), and koishi's action-queue coupling. Keep only math + signals.
6. **Preserve the constants as module-level tuning knobs**: `GRAVITY_ACCEL`, `FALL_TERMINAL`, `FRICTION`, `WALL_BOUNCE`, `TICK_MS` (30), `FEET_RATIO` (1/3).
7. **Testing**: with win32 mocked, drive `FallSimulation` with a fake `move_to` recorder and a fake floor; assert: fall accelerates to terminal velocity, snaps exactly on the floor, never tunnels through a window top (sweep test), and emits `landed` exactly once. All headless (QCoreApplication + manual tick calls).

**Implementation divergences from this spec (as shipped — see BUILD_NOTES §9):**
- `impulse_scale` ships at **0.1** (config), not 0.05 — a user-requested ≈2× stronger throw. Caps (`max_speed`, `fall_terminal`) are untouched.
- Wall bounce is applied as a **single multiplication** by the negative constant (`v * wall_bounce`); an early port double-negated it and left thrown pets glued to side walls (fixed 2026-08-17).
- The tick loop also clamps flight at the **top edge** of the work area (same bounce) so an up-flick can't eject the pet above the screen; the "floating above floor" re-arm log fires once per airborne episode.

## 6. Source Files (Reference Copies)

Full verbatim copies, kept locally:

| File | Origin | Purpose |
|---|---|---|
| `source/desktop_pets.py` | `akitak1290/desktop-pets/pets.py` | Single-file tkinter pet: work-area bounds query (L59–62), tick move/clamp/floor-snap (L1256–1305) |
| `source/gravity.py` | koishi `pet/action/gravity.py` | **Primary port template** — `GravitySystem`: gravity/terminal velocity/friction constants, sweep landing test, occlusion/standee lifecycle, flick physics, signals |
| `source/window_detector.py` | koishi `pet/brain/window_detector.py` | `get_visible_windows`/`get_window_rect`/`is_window_occluded` — the Win32 window-scan helpers gravity.py depends on |
| `source/win_detector.py` | koishi `pet/brain/win_detector.py` | Alternate `GetWindowRect` + `DWMWA_CLOAKED` occlusion detection (ctypes-based; cited by research as the occlusion source) |

> Shimeji-EE `Fall.java` formulas are quoted verbatim in README §3.3 (research-sourced; repos not cloned here).
