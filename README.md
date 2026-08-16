# VaultSprite

PySide6 desktop pet: a transparent always-on-top mascot that walks your taskbar, decays in needs, senses work-vs-play context, watches your screen via a remote Ollama endpoint (Qwen), and journals its life into an Obsidian vault. Built per `Implementation Outline.md` from the extraction docs in `docs/`.

## Quick start

```bash
uv sync --extra dev            # creates .venv (Python 3.13), installs deps
.venv/bin/python tools/generate_assets.py   # one-time: placeholder mascot + sounds
QT_QPA_PLATFORM=offscreen uv run pytest     # test suite — 72 tests, headless-safe
uv run vaultsprite             # launch the pet (real display required for visuals)
```

Headless boot check (no display needed, ~1.5 s): `uv run vaultsprite --smoke`.

On Windows everything is real: taskbar floor physics, window-standing falls, foreground-window context detection, and screenshot capture. On Linux dev those paths fall back gracefully (Qt work-area floor, disabled context detector, audio no-op) so the whole system still runs and tests.

## How to navigate this repo (what's in each file)

| If you want… | Read / touch |
|---|---|
| All settings: sizes, decay rates, thresholds, Ollama URL/model, vault path | `config/config.yaml` — one YAML file controls everything; machine-specific overrides via env vars (see "Useful commands" below) |
| Swap in your own sprites or sounds | Regenerate or edit `assets/`; the state matrix is `assets/config.yaml` (states, frame files, per-state durations, movement vectors, transition weights). No code changes needed — repoint the `sprite:` paths |
| How it's wired: every signal between modules, design decisions that diverge from the docs, known gaps, PySide6 6.11 build traps | **`BUILD_NOTES.md`** (read before modifying code) |
| Module contracts & platform gotchas for agents | `AGENTS.md`; original extraction specs + verbatim reference sources in `docs/01_…`–`docs/08_…` and `docs/INDEX.md` |
| The 8 modules themselves | `vaultsprite/`: `ui_overlay`, `animation_fsm`, `stat_engine`, `terrain_physics`, `context_detector`, `remote_agent`, `obsidian_vault`, `health_audio`; assembled + wired in `main.py:App` (the single FSM owner) |
| Tests | `tests/` — one file per module + app-level integration; all run offscreen |

## What it does, at a glance

- **Overlay** (`ui_overlay.py`) — frameless, always-on-top, translucent window. Click = petting reaction; drag-and-release with speed = flick → the pet is thrown and falls under gravity onto the taskbar (or on top of visible windows, on Windows). Right-click menu: *Ask what I see*, *Stretch break*, *Quit*.
- **Brain** (`animation_fsm.py`) — weighted probabilistic state machine over `idle / walking / talking / sleeping` plus forced states `falling` and `stretch_nudge`. Pure Python; the overlay plays each state's animated GIF for its configured duration, then asks the FSM what's next.
- **Needs** (`stat_engine.py`) — Hunger/Energy decay and Boredom climbs once per minute while you're in a WORK context. Crossing critical levels makes the pet complain (speech bubble) and write it to your vault.
- **Context** (`context_detector.py`) — polls the foreground window title every 5 s, keyword-classifies WORK vs PLAY; gates stat decay and the health timer, resets work-time when you switch to play.
- **Vision** (`remote_agent.py`) — downscaled screenshot (~1024×768 JPEG) + prompt dispatched asynchronously (sync `openai` SDK in a worker QThread) to your remote Ollama; the reply appears in a speech bubble and is journaled. An autonomous loop asks "what am I doing?" every 5 min when enabled (`remote.ask_interval_ms`).
- **Memory** (`obsidian_vault.py`) — atomic Markdown with YAML frontmatter: facts under `Memory/Facts/`, events under `Memory/Events/YYYY-MM-DD/`, daily journal at `Journal/YYYY-MM-DD.md`. No Obsidian plugin required; point `obsidian.vault_root` (or `VAULT_ROOT`) at any vault folder.
- **Health & sound** (`health_audio.py`) — after 45–60 min of continuous work the pet forces a stretch pose, chirps, and asks you to move; preloaded 8-bit SFX play non-blocking via pygame (no-op where no audio device exists).

## Useful commands

```bash
uv run python tools/render_check.py        # offscreen: proves transparency + live animation
uv run python -m vaultsprite.main --smoke  # headless boot check, exits 0/1
.venv/bin/python -c "from vaultsprite.config import load_config; print(load_config().get('remote.ollama_model'))"
```

Env-var overrides: `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `LLM_TIMEOUT_S`, `VISION_ENABLED`, `VAULT_ROOT`, `HEALTH_WORK_MIN`. Everything else lives in `config/config.yaml`.
