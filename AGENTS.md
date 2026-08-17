# VaultSprite

PySide6 desktop pet: transparent always-on-top overlay, sprite FSM, stat decay, taskbar terrain physics, screen vision via remote Ollama, Obsidian vault memory. Implemented (see `BUILD_NOTES.md` for what was built, wiring rules, known gaps, and PySide6 build traps).

**Read `BUILD_NOTES.md` before modifying code** — it records the decisions that diverge from this file/`docs/` (e.g. QMovie playback instead of QImageReader slicing, `App` as sole FSM owner) plus verified platform quirks for this PySide6 6.11 build.

## Architecture
- Follow the 8-module split in `IMPLEMENTATION_OUTLINE.md`: `ui_overlay.py`, `animation_fsm.py`, `stat_engine.py`, `terrain_physics.py`, `context_detector.py`, `remote_agent.py`, `obsidian_vault.py`, `health_audio.py`. One concern per file; PySide6 signals for cross-module events.
- **`main.py:App` is the single owner of animation-state transitions** — only it calls `fsm.get_next_state()`/`force_state()`. The sprite player emits `state_finished`; external forces (drag/flick, stats, health nudge, LLM reply) all route through App. Don't scatter FSM decisions into other modules.
- Reference repos (koishi-ai-pet, Shirros/desktop-pet, DyberPet, PyWinCtl, obsidian-memory-for-ai, StretchBreak, Shimeji) are extraction sources only — port the named logic into standalone modules; do not copy their GUIs, shops, or inventories wholesale.
- **`docs/` is the self-contained implementation source.** Each module folder (`docs/01_ui_overlay/` … `docs/08_health_audio/`) has a 5-section `README.md` (Overview, Min Libraries, Verbatim Extraction, Data Flow, Refactoring/Integration) + a `source/` subfolder of verbatim reference files. `docs/INDEX.md` is the master tracker and lists key divergences from the outline. Implement from `docs/` alone — do not re-clone the reference repos.
- Key divergences (full list in `docs/INDEX.md`): M3 DyberPet uses APScheduler not QTimer (refactor to `QTimer`); M5 reference is `Kalmat/PyWinCtl` (outline updated; `lethee/get_active_window` is dead); M4's `Shell_TrayWnd`/gravity came from Shimeji + koishi `gravity.py`; M6 reference uses sync `openai` SDK in a QThread; M7 spec has no daily-journal type (`append_journal()` is hand-built); M8 StretchBreak is Rust (port patterns only, use `pygame.mixer`); M2 config has no per-state durations (add `duration_ms`).

## Platform gotchas
- Target OS is Windows: `terrain_physics.py` (PyWin32) and `context_detector.py` use win32 APIs that fail on the Linux dev box — guard those imports for local runs. Note PyWinCtl itself raises `NotImplementedError` on unsupported platforms, so `context_detector.py` must guard the `import pywinctl`. As implemented, terrain uses Qt `availableGeometry().bottom()` as its floor (work area already excludes the taskbar) rather than a `Shell_TrayWnd` lookup; window-standing sweep and self-screenshot skip are Windows-only code paths behind `_win32gui`, untested off-Linux.
- Headless GUI smoke tests require `QT_QPA_PLATFORM=offscreen`; `--smoke` sets it automatically when no display env vars exist (never overrides an explicit choice, never runs on Windows).
- Win32 APIs return physical pixels while Qt geometry is logical — divide by `devicePixelRatio()` when bridging (`terrain_physics.py`).

## Non-obvious contracts (from the outline)
- `animation_fsm.py`: config-driven states (JSON or YAML; the bundled matrix is `assets/config.yaml` — Shirros list schema also accepted) (`idle`, `walking`, `sleeping`, `talking`) with weighted `transitions_to`; `get_next_state()` returns sprite asset, duration, position offsets. Pure Python (no Qt) so it unit-tests without a QApplication.
- `remote_agent.py`: OpenAI-compatible Ollama endpoint `http://<H100_IP>:11434/v1/chat/completions` (Qwen 27B) — IP via env/config, never hardcoded. Screenshots downscaled to ~1024×768 JPEG before sending.
- `obsidian_vault.py`: atomic Markdown with YAML frontmatter (a QObject — emits edge-triggered `vault_size_warning`); fixed vault paths `Memory/Events/`, `Memory/Facts/`, `Journal/YYYY-MM-DD.md`; API is `write_fact(category, key, value)` + `append_journal(entry)`; no Obsidian plugin dependency. **All writes are sandboxed**: targets must realpath-inside the vault root or raise `PermissionError("WRITE DENIED: …")` — keep that guard on the public API, and never add a write path that bypasses it (`_atomic_write` is internal + unguarded by design for test fixtures).
- `health_audio.py`: preload small local `.wav` files (`step.wav`, `chirp.wav`, `yawn.wav`) into RAM; 45–60 min continuous-work timer forces FSM state `stretch_nudge`.

## Tooling
- `pyproject.toml` (hatchling). Deps: PySide6, PyYAML, Pillow, mss, openai, httpx, pygame-ce; Windows-only `pywin32`/`pywinctl` via `sys_platform == "win32"` markers. Dev extras: pytest, pytest-env, pytest-qt.
- Python 3.13 (`uv`). Headless GUI tests use `QT_QPA_PLATFORM=offscreen` (set by `[tool.pytest.ini_options].env` and auto in `--smoke`).
- Run the suite: `QT_QPA_PLATFORM=offscreen uv run pytest`. Boot check: `uv run vaultsprite --smoke` (~1.5s headless, exits 0/1). Regenerate placeholder assets: `uv run python tools/generate_assets.py`. P1 sandbox/vault/Ollama-vision runner (direct-run; talks to a real Ollama): `uv run python tests/test_vault_and_ai.py`.
- Offscreen render/transparency probe (writes `/tmp/vaultsprite_render.png`, asserts alpha): `QT_QPA_PLATFORM=offscreen uv run python tools/render_check.py`.
