# VaultSprite

PySide6 desktop pet: a transparent always-on-top mascot that walks your taskbar, decays in needs, senses work-vs-play context, watches your screen via a remote Ollama endpoint (Qwen), and journals its life into an Obsidian vault. Built per `IMPLEMENTATION_OUTLINE.md` from the extraction docs in `docs/`.

## Quick start — getting it running on your PC

One prerequisite: install [uv](https://docs.astral.sh/uv/) (`powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`). Everything else is in these three commands:

```bash
git clone https://github.com/charles-rl/VaultSprite.git && cd VaultSprite
uv sync              # one-time: downloads Python 3.13 + all libraries into .venv
uv run vaultsprite   # the mascot appears on your desktop, standing on your taskbar
```

That's it — placeholder sprites and sounds ship in `assets/`, nothing else to install. **Stop:** right-click the mascot → *Quit VaultSprite* (or Ctrl+C in the terminal).

Optional, do later when you want:
- **Have it watch your screen** — set its Ollama server IP: `$env:OLLAMA_BASE_URL = "http://<H100_IP>:11434/v1"` then launch. Until you do this, the pet works fully; only vision is idle.
- **Choose where memory goes** — by default it writes `Memory/` + `Journal/` inside a `Vault/` folder in the project root. To use your real Obsidian vault: `$env:VAULT_ROOT = "C:\path\to\your-vault"`.
- Everything else (sizes, decay speed, stretch-break interval, keyword lists) lives in `config/config.yaml`.

### Linux / headless development

```bash
uv sync --extra dev                          # + test deps
QT_QPA_PLATFORM=offscreen uv run pytest      # 100 tests, no display needed
uv run python tests/test_vault_and_ai.py     # P1 runner: sandboxing / size watch / vault I/O / Ollama vision probe (live)
uv run vaultsprite --smoke                   # ~1.5 s boot check, exits 0/1
```

On Windows every capability is real (taskbar physics, window-standing falls, context detection, screenshots); on Linux those paths fall back gracefully so the code still runs and tests here.

## Configuring VaultSprite (`config/config.yaml`)

Everything about how the pet *behaves* is in one YAML file, `config/config.yaml`. You do **not** need to touch code to change its size, speed, feel, or memory. Below are the knobs that affect the pet most — each is already present in the file with a sensible default; edit the number/word, save, and relaunch `uv run vaultsprite`.

**Size & look**
- `window.width` / `window.height` — the on-screen box in logical px (default `89`). Bigger = bigger pet. The mascot art scales to fit, so this is the master "how big is it" control.

**Motion & physics (how it walks, throws, and settles)**
- `window.fps` — legacy animation rate. Note: when `mascot.enabled` is `true` (the default) this is **not** used; the Shimeji engine clock below drives everything instead.
- `mascot.tick_ms` — the Shimeji behavior/action clock (default `40` = 25 ticks/s). Lower = faster, snappier pet; higher = calmer. Pose `Duration`s in the pack are in these ticks, so this also scales animation length.
- `mascot.smooth_motion` (`true`) — interpolates the window between engine ticks so throws look smooth instead of "frame-by-frame" on high-refresh monitors. Set `false` to disable.
- `mascot.time_scale` (`1.0`) — multiplies pose durations; `< 1` = livelier, `> 1` = slower.
- `physics.*` — the legacy (non-mascot) fall/gravity path: `impulse_scale` (throw strength, `0.1`), `max_speed` (`25` px/tick cap), `wall_bounce` (`-0.4`, how it bounces off screen edges), `gravity_accel`, `fall_terminal`. The pet is clamped on-screen in both modes.
- `window.flick_speed_threshold` (`80.0`) — release speed (px/s) needed to count as a *throw*; below this it's just a drop.

**Mascot pack behaviors**
- `mascot.excluded_behaviors` — behavior names the ambient roulette never picks (defaults disable breeding/divide `SplitIntoTwo`, `PullUpShimeji` for a solo pet).

**Needs & work clock**
- `stats.*` — `initial` (starting hunger/energy/boredom), `decay_per_tick` (how fast), `critical` (thresholds that make it complain).
- `health.work_threshold_min` (default `50`) — minutes of continuous WORK before the stretch-break nudge. Use `HEALTH_WORK_MIN` to override at runtime.

**Context (WORK vs PLAY)**
- `context.work_keywords` / `context.play_keywords` — whole-word (case-insensitive) lists matched against the foreground window title to decide WORK vs PLAY. Add your editor to `work_keywords` (e.g. `rust-analyzer`) so the pet keeps "working"; add games/streaming to `play_keywords` so it rests.

**Vision (screen watching)**
- `remote.ollama_base_url` / `remote.ollama_model` — the Ollama endpoint + model; usually best set via `OLLAMA_BASE_URL` / `OLLAMA_MODEL` env vars instead.
- `remote.ask_interval_ms` (default `300000`) — how often it asks "what am I doing?" autonomously; `0` disables the loop.
- `remote.vision_enabled` — master on/off for the whole screen-watching feature.

**Memory (Obsidian vault)**
- `obsidian.vault_root` (or env `VAULT_ROOT`) — which folder it journals to.
- `obsidian.max_size_mb` (default `50`) — storage warning threshold; `<= 0` disables the monitor.

**Misc**
- `hide.enabled` — on/off for the "Hide pet" walk-to-edge feature.
- `debug.vault_logging` (`true`) — writes rolling diagnostic trails to `Memory/Debug/` (state changes, context switches, mascot position/behavior). Keep on to make future "why did it do X" reports diagnosable.

Machine-specific secrets (Ollama URL/model, vault path, LLM timeout) are **env vars**, not this file — see "Useful commands" below.

## How to navigate this repo (what's in each file)

| If you want… | Read / touch |
|---|---|
| All settings: sizes, decay rates, thresholds, Ollama URL/model, vault path | `config/config.yaml` — one YAML file controls everything; machine-specific overrides via env vars (see "Useful commands" below) |
| Swap in your own sprites or sounds | Regenerate or edit `assets/`; the state matrix is `assets/config.yaml` (states, frame files, per-state durations, movement vectors, transition weights). No code changes needed — repoint the `sprite:` paths |
| How it's wired: every signal between modules, design decisions that diverge from the docs, known gaps, PySide6 6.11 build traps | **`BUILD_NOTES.md`** (read before modifying code) |
| Module contracts & platform gotchas for agents | `AGENTS.md`; original extraction specs + verbatim reference sources in `docs/01_…`–`docs/08_…` and `docs/INDEX.md` |
| The 9 modules themselves | `vaultsprite/`: `ui_overlay`, `animation_fsm`, `stat_engine`, `terrain_physics`, `context_detector`, `remote_agent`, `obsidian_vault`, `health_audio`, `mascot_engine` (+ `mascot_environment`; M9 engine, not yet wired into the overlay); assembled + wired in `main.py:App` (the single FSM owner) |
| Tests | `tests/` — one file per module + app-level integration; all run offscreen |

## What it does, at a glance

- **Overlay** (`ui_overlay.py`) — frameless, always-on-top, translucent window. Click = petting reaction; drag-and-release with speed = flick → the pet is thrown and falls under gravity onto the taskbar (or on top of visible windows, on Windows). Right-click menu: *Ask what I see*, *Stretch break*, *Hide pet*, *Quit*.
- **Hide** — toggle *Hide pet* in the system tray (or right-click the sprite). The pet walks to the nearest screen edge, slips fully off-screen, and pauses all autonomy (no bubbles, no ambient walking, no vision asks, no nudges) — handy for meetings. Unchecking the tray item walks it back to the exact spot it hid from and resumes. Config: `hide.*` in `config/config.yaml`.
- **Brain** (`animation_fsm.py`) — weighted probabilistic state machine over `idle / walking / talking / sleeping` plus forced states `falling` and `stretch_nudge`. Pure Python; the overlay plays each state's animated GIF for its configured duration, then asks the FSM what's next.
- **Needs** (`stat_engine.py`) — Hunger/Energy decay and Boredom climbs once per minute while you're in a WORK context. Crossing critical levels makes the pet complain (speech bubble) and write it to your vault.
- **Context** (`context_detector.py`) — polls the foreground window title every 5 s, whole-word keyword classification into WORK vs PLAY; ~30 s in an unclassified app decays to UNKNOWN. Gates stat decay and the health timer (paused in PLAY *and* UNKNOWN), resets work-time when you switch away from work.
- **Vision** (`remote_agent.py`) — downscaled screenshot (~1024×768 JPEG) + prompt dispatched asynchronously — capture *and* LLM call both run off-thread, so asking never freezes the pet; the reply appears in a speech bubble and is journaled. Prompts carry the real foreground window title (not just "WORK"). An autonomous loop asks "what am I doing?" every 5 min when enabled (`remote.ask_interval_ms`).
- **Memory** (`obsidian_vault.py`) — atomic Markdown with YAML frontmatter: facts under `Memory/Facts/`, events under `Memory/Events/YYYY-MM-DD/`, daily journal at `Journal/YYYY-MM-DD.md`; rolling debug trails (state changes, context switches) land in `Memory/Debug/` for troubleshooting. No Obsidian plugin required; point `obsidian.vault_root` (or `VAULT_ROOT`) at any vault folder. All writes are hard-sandboxed inside that folder (`PermissionError` on escape), and a storage watcher warns when the vault exceeds `obsidian.max_size_mb` (default 50 MB).
- **Mascot engine** (`mascot_engine.py` + `mascot_environment.py`, M9) — plays the bundled Shimeji community pack (`assets/steve_shimeji/`) natively in Python: ambient behavior roulette, drag sway, throws/falls, wall & ceiling climbs. This is the default renderer when `mascot.enabled: true`; `mascot.tick_ms`/`mascot.smooth_motion`/`mascot.time_scale` in `config/config.yaml` tune its feel.
- **Health & sound** (`health_audio.py`) — after 45–60 min of continuous work the pet forces a stretch pose, chirps, and asks you to move; preloaded 8-bit SFX play non-blocking via pygame (no-op where no audio device exists).

## Useful commands (optional, for later)

```powershell
uv run python tools/render_check.py        # proves transparency + live animation; prints PASS and saves PNGs to the temp folder (path printed)
uv run vaultsprite --smoke                 # ~1.5 s boot check, exits 0/1
```

Env-var overrides (PowerShell shown): `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `LLM_TIMEOUT_S`, `VISION_ENABLED`, `VAULT_ROOT`, `HEALTH_WORK_MIN` — e.g. `$env:OLLAMA_BASE_URL = "http://192.168.1.50:11434/v1"`. Everything else lives in `config/config.yaml`.
