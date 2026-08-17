# VaultSprite — Module Extraction Documentation Index

Master tracker for the per-module extraction documentation. One folder per module, each with a `README.md` following the standard 5-section structure (Overview, Min Required Libraries, Verbatim Source Extraction, Logic & Data Flow, Refactoring & Integration Notes).

| # | Module | Target Repo(s) | Doc Folder | Status |
|---|--------|----------------|------------|--------|
| 1 | Transparent GUI & Drag Overlay | `Koishi007/koishi-ai-pet` | `docs/01_ui_overlay/README.md` | **Done** |
| 2 | Sprite Animation FSM Engine | `Shirros/desktop-pet` | `docs/02_animation_fsm/README.md` | **Done** |
| 3 | Needs & Stat Decay Engine | `ChaozhongLiu/DyberPet` | `docs/03_stat_engine/README.md` | **Done** |
| 4 | Desktop Terrain Physics | `akitak1290/desktop-pets`, Shimeji-EE family, koishi `gravity.py` | `docs/04_terrain_physics/README.md` | **Done** |
| 5 | Contextual Focus Detector | `Kalmat/PyWinCtl` | `docs/05_context_detector/README.md` | **Done** |
| 6 | Screen Vision & Remote Ollama Client | `Koishi007/koishi-ai-pet` | `docs/06_remote_agent/README.md` | **Done** |
| 7 | Obsidian Atomic Memory File Engine | `jrcruciani/obsidian-memory-for-ai` | `docs/07_obsidian_vault/README.md` | **Done** |
| 8 | Chiptune Sound & Health Nudge Engine | `pieterdd/StretchBreak` + pygame.mixer | `docs/08_health_audio/README.md` | **Done** |

All temporary clones have been deleted. **Each module folder is self-contained**: it contains its `README.md` (5-section extraction doc) plus a `source/` subfolder holding verbatim copies of the reference-repo files needed for implementation. The build agent should need nothing outside `docs/`.

## Bundled Source Files

| Module | `source/` contents |
|---|---|
| `docs/01_ui_overlay/source/` | koishi `base_window.py`, `pet_window.py`, `config.py`, `app.py` |
| `docs/02_animation_fsm/source/` | shirros `util.py`, `pet.py`, `main.py`, `config_cave_chaos.json`, `config_bonzi.json` + koishi `pet_animations.py` (PySide6 GIF rendering) |
| `docs/03_stat_engine/source/` | dyberpet `settings.py`, `modules.py`, `DyberPet.py`, `conf.py`, `run_DyberPet.py`, `bubbleManager.py` |
| `docs/04_terrain_physics/source/` | `desktop_pets.py`, koishi `gravity.py` (primary template), `window_detector.py`, `win_detector.py` |
| `docs/05_context_detector/source/` | pywinctl `_pywinctl_win.py`, `_main.py`, `__init__.py` |
| `docs/06_remote_agent/source/` | koishi `screen_reader.py`, `llm_client.py`, `context_builder.py`, `pet_agent.py`, `behavior.py`, `prompts.py`, `config.py` |
| `docs/07_obsidian_vault/source/` | `SPEC-v4.md`, `SPEC-v3.md`, `compact.py`, `lint.py`, `transact.py`, `propose.py`, `review.py`, `compact_v3.py`, `ops_v3.py`, `fact/event/transaction.schema.yaml` |
| `docs/08_health_audio/source/` | stretchbreak `main.rs`, `idle_monitoring.rs` (Rust — pattern reference only) |

> Shimeji-EE gravity formulas (Module 4) are quoted in its README §3.3; those repos were not re-cloned.

## Key Findings & Divergences From The Outline (read before implementing)

1. **Module 5 reference changed** (approved 2026-08-16): dead repo `lethee/get_active_window` → `Kalmat/PyWinCtl`. `IMPLEMENTATION_OUTLINE.md` updated accordingly. PyWinCtl raises `NotImplementedError` on unsupported platforms — `context_detector.py` must guard the import.
2. **Module 3**: DyberPet has no `core/pet.py` and uses **APScheduler** (not `QTimer`/`QThread`); only 2 stats (HP/FV) with a 4-tier ladder. Refactor maps to outline's `QTimer` + 3-stat contract in `stat_engine.py`.
3. **Module 4**: `akitak1290/desktop-pets` has **no** `Shell_TrayWnd`/gravity — that logic came from the Shimeji-EE family + koishi `pet/action/gravity.py` (research-sourced, URLs in doc). Work-area (`rcWork`) is the recommended floor source.
4. **Module 6**: koishi uses the **sync `openai` SDK in a `QThread`**, not an async HTTP client; no aiohttp/httpx client exists in the repo. Doc covers both keep-sync and write-async options.
5. **Module 7**: the spec has **no daily-journal file type** — `append_journal()` is hand-built in the doc from the atomic-write pattern. No true append helper exists in the reference (append-only is by convention).
6. **Module 8**: **StretchBreak is Rust** (rodio/GTK), not pygame. Only its timer state-machine + non-blocking-sound *patterns* are extracted; the pygame.mixer recipe is canonical (from official pygame docs). StretchBreak's default break is 20 min — VaultSprite should use 45–60 min per outline.
7. **Module 2**: Shirros config has **no per-state durations** (global 100 ms frame). The `get_next_state()` contract in the outline needs a `duration_ms` field added to the config schema.
