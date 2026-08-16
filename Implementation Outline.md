n### Module 1: Transparent GUI & Drag Overlay

* **Target Repo:** `https://github.com/Koishi007/koishi-ai-pet`
* **Core Feature:** Frameless, always-on-top, transparent PySide6 overlay window.
* **Files/Functions to Inspect:** `main.py` / `gui/window.py` (Look for `QMainWindow` setup and mouse event handlers).
* **Extraction Objective:**
* Extract PySide6 window flags: `Qt.FramelessWindowHint`, `Qt.WindowStaysOnTopHint`, and `Qt.WA_TranslucentBackground`.
* Extract `mousePressEvent`, `mouseMoveEvent`, and `mouseReleaseEvent` to enable click-and-drag desktop positioning.


* **OpenCode Directive:** *"Extract only the transparent PySide6 window instantiation and mouse-dragging handlers from Koishi-AI-Pet. Refactor into a standalone `ui_overlay.py` module exposing a single `PetOverlayWindow` class."*

---

### Module 2: Sprite Animation FSM Engine

* **Target Repo:** `https://github.com/Shirros/desktop-pet`
* **Core Feature:** Probabilistic Finite State Machine (FSM) driven by a `config.json` schema.
* **Files/Functions to Inspect:** `config.json` and the main animation loop parser.
* **Extraction Objective:**
* Extract the JSON parser that maps states (`idle`, `walking`, `sleeping`, `talking`) to specific sprite frames/GIFs, frame delays, and horizontal movement vectors `[x, y]`.
* Extract the weighted random transition logic (`transitions_to`) that decides state changes when idle.


* **OpenCode Directive:** *"Port Shirros's JSON FSM parsing logic into PySide6. Create `animation_fsm.py` with a method `get_next_state()` that accepts current state and returns the next sprite asset, duration, and position offsets."*

---

### Module 3: Needs & Stat Decay Engine

* **Target Repo:** `https://github.com/ChaozhongLiu/DyberPet`
* **Core Feature:** `QTimer`-driven stat decay (Hunger, Energy, Boredom counters).
* **Files/Functions to Inspect:** `core/pet.py` or background ticker logic handling `satiety` and `mood`.
* **Extraction Objective:**
* Extract the background timer loop that decrements stat counters over time.
* Extract threshold event triggers (e.g., when `boredom > 80`, emit a `signal_bored` event).


* **OpenCode Directive:** *"Strip out DyberPet's shop, inventory, and GUI menus. Extract purely the underlying `QThread` / `QTimer` stat decay math into `stat_engine.py`, emitting PySide6 signals when stats cross critical levels."*

---

### Module 4: Desktop Terrain Physics & Taskbar Walking

* **Target Repo / Reference:** `https://github.com/akitak1290/desktop-pets` / `Shimeji-EE`
* **Core Feature:** Windows taskbar bounding-box collision, dragging release gravity, and window-top landing.
* **Files/Functions to Inspect:** Win32 API calls querying `Shell_TrayWnd` and window rectangles.
* **Extraction Objective:**
* Extract `win32gui.FindWindow("Shell_TrayWnd", None)` and `win32gui.GetWindowRect()` to identify taskbar coordinates.
* Implement simple $Y$-axis gravity: when dragging releases the pet above the taskbar, apply a downward $Y$-velocity vector until $Y_{pet} \ge Y_{taskbar}$.


* **OpenCode Directive:** *"Create a lightweight PyWin32 wrapper in `terrain_physics.py` that calculates the current desktop floor line (taskbar or top of active window) and updates the sprite's $Y$ position during fall states."*

---

### Module 5: Contextual Focus Detector (Work vs. Play)

* **Target Repo / Reference:** `https://github.com/Kalmat/PyWinCtl`
* **Core Feature:** Foreground window handle title detection for active app sensing.
* **Files/Functions to Inspect:** `src/pywinctl/_pywinctl_win.py` (Win32 backend using `win32gui.GetForegroundWindow()` / `win32gui.GetWindowText()`) and `src/pywinctl/_main.py` (`getActiveWindow()`, `getActiveWindowTitle()`).
* **Extraction Objective:**
* Implement a 5-second polling loop calling `pwc.getActiveWindow()` / `win.title` and checking active window titles.
* Map keyword lists (`VS Code`, `Terminal`, `Obsidian`) to `Context.WORK` and (`YouTube`, `Steam`, `Reddit`) to `Context.PLAY`.
* Optional: use `win.watchdog.start(isActiveCB=...)` as an event-driven alternative to polling.


* **OpenCode Directive:** *"Write `context_detector.py` running a lightweight background thread that emits a `context_changed(str)` signal containing current work/play classification, powered by PyWinCtl's cross-platform window API."*

---

### Module 6: Screen Vision & Remote Ollama Client

* **Target Repo:** `https://github.com/Koishi007/koishi-ai-pet`
* **Core Feature:** Screen capture thread and multimodal LLM payload generation.
* **Files/Functions to Inspect:** `vision.py` / `llm_api.py`.
* **Extraction Objective:**
* Extract PIL/PyAutoGUI downscaled screenshot capture ($1024 \times 768$, compressed JPEG).
* Extract the async HTTP client, re-pointing the base URL to your remote H100 Ollama endpoint (`http://<H100_IP>:11434/v1/chat/completions`) with Qwen 27B payload formatting.


* **OpenCode Directive:** *"Extract Koishi's screen grabber and HTTP payload builder. Build `remote_agent.py` configured to dispatch visual context and prompt strings asynchronously to a remote OpenAI-compatible Ollama endpoint."*

---

### Module 7: Obsidian Atomic Memory File Engine

* **Target Repo:** `https://github.com/jrcruciani/obsidian-memory-for-ai`
* **Core Feature:** Atomic Markdown file creation and append I/O with YAML frontmatter.
* **Files/Functions to Inspect:** `SPEC-v4.md` / Python file writer examples.
* **Extraction Objective:**
* Extract file I/O helper functions that parse/write Markdown files inside designated vault subdirectories (`/Vault/Memory/Events/` and `/Vault/Memory/Facts/`).
* Implement an append method for daily journal logs (`/Vault/Journal/YYYY-MM-DD.md`).


* **OpenCode Directive:** *"Build `obsidian_vault.py` providing `write_fact(category, key, value)` and `append_journal(entry)` methods adhering to atomic Markdown memory patterns without external Obsidian plugin dependencies."*

---

### Module 8: Chiptune Sound & Health Nudge Engine

* **Target Repo / Reference:** `pygame.mixer` & `pieterdd/StretchBreak`
* **Core Feature:** Non-blocking 8-bit sound triggers and active work timer nudges.
* **Files/Functions to Inspect:** `pygame.mixer.Sound` initialization and continuous user activity timer.
* **Extraction Objective:**
* Load tiny local `.wav` files (`step.wav`, `chirp.wav`, `yawn.wav`) into RAM for instant async playback.
* Set a 45–60 minute continuous work timer. When triggered, force the pet FSM to state `stretch_nudge`, play `chirp.wav`, and display a health prompt.


* **OpenCode Directive:** *"Create `health_audio.py` combining `pygame.mixer` sound playback methods with a background timer that triggers posture/stretch events when active work thresholds are met."*

---

### System Architecture Flow

```
                         +-----------------------------------+
                         |       ui_overlay.py (PySide6)     |
                         |   (Transparent Window & Drag)     |
                         +-----------------+-----------------+
                                           |
       +-----------------------------------+-----------------------------------+
       |                                   |                                   |
+------v------------------+     +----------v---------------+        +----------v---------------+
|   animation_fsm.py      |     |     stat_engine.py       |        |   terrain_physics.py     |
| (Sprite JSON Matrix)    |     | (Hunger/Boredom Tickers) |        |  (Taskbar & Gravity)     |
+--------------+----------+     +----------+---------------+        +----------+---------------+
               |                           |                                   |
               +---------------------------+-----------------------------------+
                                           |
                         +-----------------v-----------------+
                         |       health_audio.py             |
                         | (Chiptunes & Stretch Nudges)      |
                         +-----------------+-----------------+
                                           |
       +-----------------------------------+-----------------------------------+
       |                                   |                                   |
+------v------------------+     +----------v---------------+        +----------v---------------+
|   context_detector.py   |     |     remote_agent.py      |        |    obsidian_vault.py     |
| (Work vs Play Window)   |     | (Screen Vision -> H100)  |        | (Atomic Markdown Memory) |
+-------------------------+     +--------------------------+        +--------------------------+

```