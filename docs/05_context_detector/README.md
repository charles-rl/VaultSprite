# Module 5 — Contextual Focus Detector (Work vs. Play)

## 1. Module Overview & Objective

Detects which application currently has focus and classifies it as **WORK** or **PLAY** based on keyword lists, emitting a `context_changed(str)` signal from a lightweight background thread. The stat engine (M3) and health module (M8) use the classification (e.g. only decay/accumulate work-time in a WORK context).

Maps to **Module 5** of `Implementation Outline.md`; produces `context_detector.py`.

Extraction source: **`Kalmat/PyWinCtl`** (cross-platform window control; Win32 backend under the hood). Key files:
- `src/pywinctl/_pywinctl_win.py` — `getActiveWindow()` / `getActiveWindowTitle()`, `.title`, `.isActive` (the `GetForegroundWindow`/`GetWindowText` call sites).
- `src/pywinctl/_main.py` — platform re-export switch + `_WatchDog` (event-driven alternative to polling).

> The outline's original reference `lethee/get_active_window` is a **dead 404 repo**; replaced (approved) with `Kalmat/PyWinCtl`.

## 2. Minimum Required Libraries

| Package | Why |
|---|---|
| `PyWinCtl` (`pywinctl`) | Unified API: `getActiveWindow()`, `.title`, `.isActive` — wraps `GetForegroundWindow`/`GetWindowText` on Win32 |
| Platform deps of PyWinCtl (auto-installed) | `pywin32` (Windows), `python-xlib`+`ewmhlib` (Linux), `pyobjc` (macOS) |
| `PySide6` | `QObject`, `Signal`, `QThread` for the polling worker |

> **Linux dev-box constraint**: importing `pywinctl` on an unsupported platform raises `NotImplementedError` (see §4.6) — and even on Linux its `getActiveWindow()` needs X11/Wayland quirks. `context_detector.py` must **not import pywinctl unconditionally**; replicate the guard pattern (§5).

## 3. Source Code Extraction (Verbatim)

### 3.1 Foreground window — `src/pywinctl/_pywinctl_win.py` (lines 49–72)

```python
def getActiveWindow() -> Win32Window | None:
    """
    Get the currently active (focused) Window

    :return: Window object or None
    """
    hWnd = win32gui.GetForegroundWindow()
    if hWnd:
        return Win32Window(hWnd)
    else:
        return None


def getActiveWindowTitle() -> str:
    """
    Get the title of the currently active (focused) Window

    :return: window title as string or empty
    """
    hWnd = getActiveWindow()
    if hWnd:
        return hWnd.title
    else:
        return ""
```

### 3.2 Window `.title` / `.isActive` properties — `_pywinctl_win.py` (lines 956–975)

```python
@property
def isActive(self) -> bool:
    """
    Check if current window is currently the active, foreground window
    """
    return bool(win32gui.GetForegroundWindow() == self._hWnd)

@property
def title(self) -> str:
    """
    Get the current window title, as string
    """
    name = win32gui.GetWindowText(self._hWnd)
    if isinstance(name, bytes):
        name = name.decode()
    return name or ""
```

### 3.3 Platform backend selection — `src/pywinctl/_main.py` (lines 1009–1056, excerpt)

```python
if sys.platform == "darwin":
    from ._pywinctl_macos import MacOSWindow as Window
    from ._pywinctl_macos import getActiveWindow as getActiveWindow
    from ._pywinctl_macos import getActiveWindowTitle as getActiveWindowTitle
    ...
elif sys.platform == "win32":
    from ._pywinctl_win import Win32Window as Window
    from ._pywinctl_win import getActiveWindow as getActiveWindow
    from ._pywinctl_win import getActiveWindowTitle as getActiveWindowTitle
    ...
elif sys.platform == "linux":
    from ._pywinctl_linux import LinuxWindow as Window
    from ._pywinctl_linux import getActiveWindow as getActiveWindow
    ...
else:
    raise NotImplementedError(
        "PyWinCtl currently does not support this platform. "
        + "If you think you can help, please contribute! https://github.com/Kalmat/PyWinCtl"
    )
```

Also `src/pywinctl/_pywinctl_win.py` lines 6–7 — the module-level guard that keeps pywin32 imports Windows-only:

```python
if sys.platform != "win32":
    raise OSError(f"Cannot import {__name__} on {sys.platform}")
```

### 3.4 Watchdog (event-driven alternative to polling) — `src/pywinctl/_main.py` (lines 456–509, excerpt)

```python
def start(
    self,
    isAliveCB: Callable[[bool], None] | None = None,
    isActiveCB: Callable[[bool], None] | None = None,
    ...
    changedTitleCB: Callable[[str], None] | None = None,
    interval: float = 0.3
):
    if self._watchdog is None:
        self._watchdog = _WatchDogWorker(self._parent, isAliveCB, isActiveCB, isVisibleCB,
                                         isMinimizedCB, isMaximizedCB, resizedCB, movedCB,
                                         changedTitleCB, changedDisplayCB, interval)
        self._watchdog.daemon = True
        self._watchdog.start()
    else:
        self._watchdog.restart(isAliveCB, isActiveCB, isVisibleCB, isMinimizedCB,
                               isMaximizedCB, resizedCB, movedCB, changedTitleCB,
                               changedDisplayCB, interval)
```

The `_WatchDogWorker.run()` polls each watched property every `interval` seconds (default 0.3 s) in a daemon thread and invokes the callback only on a state diff — including `isActiveCB` and `changedTitleCB`. It is polling-in-a-thread, not an OS event hook.

## 4. Logic & Data Flow Breakdown

1. **Get the focused window** (§3.1): `getActiveWindow()` calls `win32gui.GetForegroundWindow()` and wraps the returned HWND (accepts hex-string handles) in a `Win32Window`. `getActiveWindowTitle()` delegates and returns the `.title` string (or `""` when no foreground window).
2. **Title extraction** (§3.2): the `.title` property is `win32gui.GetWindowText(self._hWnd)`, decoded from bytes if needed, empty-string when absent. `.isActive` compares the window's handle to the current foreground window — useful to filter your own overlay.
3. **Platform switch** (§3.3): `_main.py` binds the backend at **import time** via `sys.platform` — so on the Linux dev box, importing the package hits the `else: raise NotImplementedError` branch. This is a *porting hazard*: our `context_detector.py` must replicate this guard itself and degrade gracefully.
4. **Polling loop** (our design, per outline): every **5 s**, read `getActiveWindowTitle()` (or `pwc.getActiveWindow().title`), lowercase, then scan the keyword lists:
   - WORK: `vs code`, `terminal`, `obsidian`, `pycharm`, `intellij`, `sublime`, `notepad++`, `word`, `excel`, `slack`, `teams` …
   - PLAY: `youtube`, `steam`, `reddit`, `netflix`, `twitch`, `discord`, `spotify`, `chrome`-browsing … 
   If the bucket changed since the last poll, emit `context_changed("WORK"/"PLAY")`.
5. **Event-driven alternative** (§3.4): instead of a 5 s poll, `pwc.getActiveWindow().watchdog.start(isActiveCB=..., changedTitleCB=...)` fires callbacks only on change (default 0.3 s cadence). This is more responsive but ties us to PyWinCtl; the polling approach is the safer primary design.
6. **Edge cases**: ignore our own overlay window (`skip if title empty or hwnd == our winId`); treat unknown apps by last-known context (or a `Context.NEUTRAL`).

## 5. Refactoring & Integration Notes

Target: `context_detector.py` exposing **`class ContextDetector(QObject)`** with `context_changed = Signal(str)` and `current_context` property, running a lightweight `QThread` (or `threading.Thread` + queued signal).

Step-by-step:

1. **Guard the dependency** — never import pywinctl unconditionally (it raises on unsupported platforms):
   ```python
   try:
       import pywinctl as pwc
   except (ImportError, NotImplementedError):
       pwc = None   # headless/dev fallback → context stays UNKNOWN
   ```
   Optionally, when `pwc is None`, use `win32gui` directly inside the same guard for Windows-only runs.
2. **Keyword classification** — module-level constants:
   ```python
   WORK_KEYWORDS = ["vs code", "terminal", "obsidian", "pycharm", "notepad", ...]
   PLAY_KEYWORDS = ["youtube", "steam", "reddit", "netflix", "twitch", "discord", "spotify", ...]
   def classify(title: str) -> str: ...
   ```
   Case-insensitive substring match (PyWinCtl's `Re.CONTAINS`/`IGNORECASE` exist for exact/partial matching if you use `getWindowsWithTitle` instead).
3. **Worker**: `class _Poller(QThread)` whose `run()` loops every 5 s, calls `pwc.getActiveWindowTitle()` (wrapped in try/except — the call can raise on some platforms), classifies, and on change `self.context_changed.emit(...)` — use a **queued connection** (default for cross-thread) so the GUI thread receives it.
4. **Signal contract**: emit the outline's `context_changed(str)` containing `"WORK"`/`"PLAY"` (or `"UNKNOWN"` when the detector is unavailable). Expose `is_available()` for the stat/health modules to gate on.
5. **Start/stop lifecycle**: `start()` spawns the thread (daemon), `stop()` sets a stop flag + `wait()`; ensure no leaked threads on app exit. Tie `active()` into the stat engine (M3) so decay only runs during `WORK` if desired.
6. **Testability**: inject a `probe` callable (default `pwc.getActiveWindowTitle`) so tests can feed fake titles without a window system; assert classification boundaries (e.g. `"Visual Studio Code - main.py"` → WORK, `"Steam - Store"` → PLAY) and that no signal is re-emitted when the context doesn't change.
7. **Cleanups**: don't drag in the full PyWinCtl surface (`getAllWindows`, menus, geometry control) — only the three primitives (getActiveWindow/getActiveWindowTitle/.title) are needed.

## 6. Source Files (Reference Copies)

Full verbatim copies from `Kalmat/PyWinCtl`, kept locally:

| File | Purpose |
|---|---|
| `source/_pywinctl_win.py` | Win32 backend: `getActiveWindow`/`getActiveWindowTitle` (L49–72), `.title`/`.isActive` (L956–975), `Win32Window` class, `GetForegroundWindow`/`GetWindowText` call sites |
| `source/_main.py` | `BaseWindow` ABC, `_WatchDog`/`_WatchDogWorker` (event-driven alternative), platform re-export switch (L1009–1056 — the `NotImplementedError` hazard) |
| `source/__init__.py` | Public `pwc.*` API surface (what to import vs. avoid) |
