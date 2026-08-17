# BUILD NOTES — VaultSprite implementation details for agents

Companion to `AGENTS.md` (contracts/gotchas) and `docs/INDEX.md` (extraction tracker).
This file records **what was actually built, how it wires together, the decisions
that diverge from the docs, the known gaps, and platform-specific traps** discovered
while implementing. Read this before touching code — several items here only make
sense because of PySide6 6.11 build quirks found empirically on this box.

Built: Python 3.13 (uv), Linux dev box, offscreen Qt validation. Target: Windows 11.
Status at time of writing: **91 tests passing, `--smoke` exit 0, render check PASS.**
(2026-08-17 P1 hardening pass added vault sandboxing/size-watcher/concurrency + the standalone
`tests/test_vault_and_ai.py` runner; see §9. The earlier maintenance pass added the two M4
airborne-clamp tests.) Nothing is committed yet — the entire tree is untracked in git (greenfield repo).

---

## 1. Project map

```
pyproject.toml            hatchling; PySide6/PyYAML/Pillow/mss/openai/httpx/pygame-ce;
                          pywin32+pywinctl behind sys_platform=="win32"; [tool.uv] package=true
config/config.yaml        ALL app settings (dotted sections) — the single config source
vaultsprite/
  config.py               load_config() → Config with .get('a.b'), .section(), env overrides,
                          path resolution vs repo root. Caches in _CACHED; pass reload=True.
  ui_overlay.py           M1: PetOverlayWindow + SpritePlayer (QMovie) + SpeechBubble
  animation_fsm.py        M2: AnimationFSM — PURE python (no Qt), WeightedRandomMap verbatim
  stat_engine.py          M3: StatEngine(QObject) QTimer decay + edge-trigger signals
  terrain_physics.py      M4: TerrainPhysics(QObject) floor query + fall sim, win32 guarded
  context_detector.py     M5: ContextDetector(QObject) poller thread, pywinctl guarded
  remote_agent.py         M6: RemoteAgent(QObject) mss capture → openai-in-QThread dispatch
  obsidian_vault.py       M7: ObsidianVault(QObject) — sandboxed atomic dot-temp writes, size watcher
  health_audio.py         M8: SoundBank (pygame no-op fallback) + WorkTimer(QObject)
  main.py                 App class = the SINGLE FSM owner; assembles/wires all 8 modules
config/… assets/config.yaml is separate:
assets/config.yaml        sprite state matrix (dict schema, not Shirros list) — repoint this
                          to swap in real sprites/sheets without touching code
assets/sprites/*.gif      generated placeholder mascot, 96×96, 4 frames/state, transparent
assets/sounds/{step,chirp,yawn}.wav   synthesized 8-bit mono WAVs (22 kHz)
tools/generate_assets.py  deterministic-ish asset generator (RNG seed 42 for drawing; the
                          shared RNG makes regenerated .wavs vary between runs — cosmetic only)
tools/render_check.py     offscreen boot → composites live frame, asserts transparency by
                          checking corners stay pure magenta, pixel-diffs two captures ~1.4 s
                          apart to prove QMovie is advancing; writes /tmp/vaultsprite_render.png
tests/                    91 tests, all offscreen-safe (conftest: qapp fixture + FakeConfig); plus
                           tests/test_vault_and_ai.py — a standalone direct-run runner for the P1 pass
```

Run commands (see AGENTS.md for the canonical list): `uv run vaultsprite`,
`QT_QPA_PLATFORM=offscreen uv run pytest`, `uv run python tools/generate_assets.py`.
On this Linux box Qt additionally needs system libs (`libxkbcommon0 libegl1 libgl1
libglib2.0-0 libdbus-1-3`) — already apt-installed; a fresh container will need them again.

---

## 2. Signal graph (who talks to whom)

Everything crosses through PySide6 signals; no module imports another's internals.
`main.py:App.__init__` holds the whole wiring table. The one rule that keeps it sane:

**`App` is the only thing that calls `fsm.get_next_state()` / `force_state()`.**
The sprite player never decides state — it just emits `state_finished(state_name)`
when a hold expires, and `App._advance_fsm` draws the next weighted state. External
forces (drag, flick fall, stat thresholds, health nudge, LLM reply) all route into
`force_state(...)` + `_play(transition)`.

```
PetOverlayWindow                        App                         modules
  drag_started ───────────────────────► _on_drag_started → physics.enable(False), stats.pause()
  drag_released(vx,vy) ───────────────► _on_drag_released → stats.resume(), physics.release(vx,vy)
    (window clears its own dragging flag inside mouseReleaseEvent before emitting)
  clicked ────────────────────────────► energy +4, chirp blip
  ask_vision_requested(prompt) ───────► agent.ask(prompt, window_context=context_now)
  stretch_requested ──────────────────► _trigger_stretch_nudge

SpritePlayer (lives on the window)       App
  state_finished(name) ───────────────► _advance_fsm → fsm.get_next_state → window.play_state(t)
  frame_changed(QPixmap) ─────────────► window._render_frame (scale + flip mirror)
  position_delta(dx,dy) ──────────────► window._apply_position_delta (walk drift; wall bounce
                                               reverses _walk_dir and mirrors via set_flipped)

TerrainPhysics                            App
  falling_started ────────────────────► fsm.force_state("falling")      [guarded by not dragging]
  landed(x,y) ────────────────────────► fsm.force_state("idle")         [same guard]
  standing_lost(title) ───────────────► log (Windows standee lifecycle; inert on Linux)

StatEngine                                App
  stat_changed(kind,val)               → (not wired to UI yet — introspection only)
  signal_hungry/tired/bored ──────────► bubble line + vault.append_journal + vault.write_fact

ContextDetector                           App
  context_changed(WORK|PLAY) ─────────► stats.set_active, health.set_active, record_event

WorkTimer                                 App
  stretch_nudge ──────────────────────► force_state("stretch_nudge"), chirp, bubble,
                                         vault.write_fact(health,last_stretch), journal

RemoteAgent                               App + window menu
  response_ready(text) ───────────────► _on_agent_reply → bubble, force_state("talking"), journal
  error(str) ─────────────────────────► log "vision note: …"

Vision loop: QTimer in App (interval = remote.ask_interval_ms; 0 disables). Starts only if
agent.enabled. Each tick calls agent.ask with a fixed prompt + current context string.
```

`main()` also auto-sets `QT_QPA_PLATFORM=offscreen` **only when `--smoke` is passed AND no
`DISPLAY`/`WAYLAND_DISPLAY` exists** — it never shadows an explicit platform choice, and it's a
no-op on Windows (guarded by `os.name != "nt"`).

---

## 3. Key decisions that diverge from / extend the docs

### M2 animation_fsm + sprite playback
- **`SpritePlayer` is QMovie-driven, not QImageReader frame-slicing.** This PySide6 6.11 build's
  `QImageReader.jumpToNextImage()` returns *only frame 0* for Pillow-generated animated GIFs
  (verified empirically; RGB and RGBA variants both fail). Live `QMovie` playback delivers all
  frames with correct disposal and transparency — verified by compositing over magenta. This is
  also what the M2 doc recommends ("no Pillow needed, QMovie handles GIF natively").
  Consequences: per-frame interval comes from the GIF's own timing (Pillow wrote `duration=96 ms`);
  the config `frame_ms` is still loaded and passed in `StateTransition.frame_ms` but only used as
  a fallback hint — don't rely on it for exact frame pacing. If you replace GIFs, any per-frame
  timing in your sheets will be respected natively.
- **Per-state hold = single-shot QTimer (`duration_ms`).** This is the doc's "M5/outline wants
  explicit durations" ask: `SpritePlayer.play()` starts a one-shot hold timer for
  `StateTransition.duration_ms`; when it fires, `state_finished` goes to App. `QMovie` loops in
  the meantime so short states (e.g. stretch_nudge 4200 ms) still show motion after frame 4 wraps.
- **The FSM is pure Python** (no Qt imports at all — testable with zero QApplication).
  `WeightedRandomMap` keeps Shirros' cumulative-distribution negative-index trick verbatim.
  `get_next_state(current)` and `force_state(name)` both return the state's declared `move` vector
  (`dx, dy`) for a uniform contract (the doc sketched forced states as zero; we kept it uniform —
  walking is never *forced*, so this only matters if you force a moving state in tests).
- **`assets/config.yaml` uses a dict schema** `{name: {sprite, duration_ms, move?, one_shot?, transitions_to?}}`,
  not Shirros' list-of-objects. `AnimationFSM.__init__` accepts BOTH shapes (list items may use
  `state_name` or `file_name`). This is the file you edit to add/retune states; the FSM validates
  transition targets fail-fast at load.
- **One-shot states** (`one_shot: true`: stretch_nudge, falling) exit deterministically to `idle`
  in `_pick_next()` instead of rolling weights — they must always return control.

### M1 ui_overlay (the one with the most hidden traps)
- **Drag/click discrimination is a koishi port**: left press starts a single-shot click timer
  (`window.click_timeout_ms`=200 default; 120 in tests); first move with Manhattan travel ≥
  `drag_threshold_px` (5) stops it and calls `_start_drag`; the click path on release calls
  `_on_click_confirmed()`. A still-press whose timer already fired then releasing is a **no-op**
  (guarded by `_dragging`) — that guard was a real bug fix, don't remove it.
- **Grab offset = triggering move's `event.position().toPoint()`** (doc §5.3 "click-relative" note),
  not koishi's hardcoded head point. Release velocity uses only samples in the last 150 ms and is
  emitted as `drag_released(vx, vy)` **for every real drag** — slow drops emit `(≈0, ≈0)`. App
  doesn't threshold; `TerrainPhysics.release()` does (see M4).
- **Movement gate during a drag is `_grab_local is not None` only** (koishi parity, doc §3.3),
  deliberately NOT also checking `event.buttons()`. Real Qt hover events can't reach that branch
  because `_grab_local` is None; synthetic test events without the button flag do reach it — this
  is what makes hand-built-event UI tests behave like real held-button drags. Documented in code.
- **Sprite rendering pipeline**: `QMovie.frameChanged → _on_movie_frame` and an eager first frame
  via `_read_first_frame()` (the *first* QImageReader read alone works fine — only multi-frame
  iteration is broken) both funnel into one slot, `window._render_frame(pixmap)`, which scales to
  the window size × devicePixelRatio then applies the walk mirror. The flip is stored as a
  `_base_pixmap` + `set_flipped(bool)`; note `set_flipped` must NOT early-return on
  "no change" — that was a bug (first frame never painted because the label only receives frames
  through this slot). `reverse_walk()` flips direction AND mirror at screen walls.
- **QEvent enum spellings in this build**: `QEvent.Type.MouseButtonPress` / `.MouseButtonRelease`
  / `.MouseMove` (NOT `…PressEvent`), and `QMouseEvent` requires the 7-arg constructor
  `(type, localPos, scenePos, globalPos, button, buttons, modifiers)` — see
  `tests/test_ui_overlay.py::_evt` for a working template.
- **SpeechBubble height is lineSpacing-based** (2026-08-17): `ascent + (n-1)*lineSpacing + descent +
  pads`. The old `fm.height()*lines + 22` under-sized wrapped text by up to a partial line, so the
  last line's lower half was clipped at the widget edge — on-screen it looked like the sentence just
  stopped mid-word (reported as "roll your" with nothing after). `paintEvent`'s draw rect uses the
  same pads; keep them in sync if you touch either side.

### M3 stat_engine
- `_tick()` applies decay **then** evaluates thresholds per stat (order matters: the first version
  compared before applying and never emitted). `adjust(kind, delta)` is the manual API
  (feeding/petting route; App uses it on click with ENERGY +4).
- Edge-triggered signals use hysteresis (`REARM_MARGIN=10`): a stat oscillating at its critical
  value fires once per episode, re-arming only after clearing the threshold by the margin.
- `set_active(False)` (PLAY context) **resets edge state** so the next WORK session fires fresh;
  while inactive `_tick` is a full no-op (`stats.pause_when_inactive: true`). `pause()/resume()`
  are separate (used during drag), independent of the active gate.

### M4 terrain_physics
- **Plain drops fall via a "gravity re-arm" check at the top of `_tick()`**: if not falling and the
  pet's bottom is above the floor by more than 2 px, it starts a natural fall. This was needed
  because the window only emits `drag_released` (no separate plain-drop signal). It means *any*
  time you position() the window mid-air with physics enabled, it settles — convenient for tests
  and drag-to-reposition, harmless otherwise.
- **Flight is clamped to the work area**: `_tick()` bounces the pet off side walls *and* the top
  edge (`wall_bounce`, single multiplication — see fix below) and never lets it leave the screen
  while airborne. Without this, an up-flick launched the pet above the display; it then fell back
  from thousands of px at terminal velocity (perceived "infinite falling" + a re-arm log line every
  tick). The re-arm `logger.info` now fires once per airborne episode (`_fall_announced`, cleared on
  landing/grounded). **Bug fix 2026-08-17**: the wall bounce was `-vx * -0.4 = +0.4 vx` — same sign,
  so a throw into a side wall left the pet glued to / pushing through it forever; tests
  `test_wall_bounce_reverses_velocity` and `test_upward_flick_clamps_at_screen_top_and_lands` pin this.
- **`release(vx, vy)` re-enables the world** (`enable(True)`, since `drag_started` disabled it) and
  calls `apply_impulse` **only when speed > `window.flick_speed_threshold` (80 px/s)**. Slower
  releases are plain drops. An earlier version applied a minimum liftoff impulse to *any* non-zero
  velocity — that made gentle drops pop upward; fixed by the threshold check. Don't regress it.
- **Floor = Qt `availableGeometry().bottom()`** (work area, taskbar-excluded) per doc §3.1's
  "recommended" source. The `Shell_TrayWnd` recipe exists in docs/04 §3.3 but was intentionally not
  coded — work-area covers all dock positions without extra Win32 surface and is DPI-correct from
  Qt's side (the physical-vs-logical px trap, AGENTS.md line ~15). If you later need the bar's
  literal rect (side/top-docked bars), port §3.3.A and divide by devicePixelRatio().
- Window-standing (`stand_on_windows: true`) is implemented **Windows-only** behind `_win32gui`:
  sweep crossing test against visible window tops (feet = middle third of width, koishi style),
  standee liveness re-check every ~15 ticks → `standing_lost(title)`. Inert on Linux; no tests for
  it exist because the win32 surface can't run here. `snap_to_ground()` is available but main
  doesn't call it (the re-arm check supersedes it).

### M5 context_detector
- **`classify(title)` returns `None` for unknown/empty titles** (not "UNKNOWN"); the poller keeps
  its last-known bucket on None. Only real bucket switches emit `context_changed`. Initial state is
  `"UNKNOWN"`. App maps WORK→active, PLAY→inactive+reset; UNKNOWN falls through (keeps previous).
- Keyword lists are **case-insensitive substring** matches from `config.yaml`. Note: the outline's
  literal "vs code" keyword would NOT match a window titled "Visual Studio Code — file.py", so the
  config additionally ships `"visual studio"` and `"studio code"`. If you add IDEs, test with the
  *actual* title string (see `tests/test_context_detector.py` boundary table).
- Poller is a daemon `threading.Thread`; `poll_once()` runs one cycle synchronously — that's the
  path tests use (the thread variant is also covered end-to-end with an injected probe, since
  pywinctl can't be imported here: on Linux `_pwc` is None and `default_title_probe()` returns "").

### M6 remote_agent
- **Transport = sync openai SDK inside a per-call QThread** (`_BrainWorker`), exactly the koishi
  pattern you chose. `ask(prompt, window_context=..., screenshot=True)` returns immediately; reply
  lands on `response_ready(str)` via queued signal; failures land on `error(str)`.
- **Base URL gets a trailing slash appended in `__init__`** (`self.base_url`) — Ollama's OpenAI-compat
  wants `<host>/v1/`; if you change the config to include one, it won't double up (it only appends
  when missing). Dummy API key `"ollama"` is mandatory for the SDK constructor.
- **Capture skips itself**: `_self_is_foreground()` compares `GetForegroundWindow()` hwnd against the
  overlay's winId (Windows-only; main injects the live handle via `set_overlay_winid`). Off-Windows
  this always returns False — fine, there's no foreground concept in offscreen.
- Screenshot pipeline: mss → PIL RGB → LANCZOS-fit into ≤1024×768 → JPEG q=80 (PNG option) →
  base64 `data:` URI inside the OpenAI content-list format `[{"type":"text"...},{"type":"image_url","image_url":{"url": ...}}]`.
  If capture returns None (headless), payload degrades to plain-text user message. mss API drift is
  handled (`mss.MSS` preferred, `mss.mss()` fallback).
- **Model id**: config ships `hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q8_K_XL` (verified with `ollama ls`;
  the old outline-era default was `qwen2.5:27b`, which is *not* what's installed on this machine).
  The OpenAI-compat payload is model-agnostic, so no other code changes were needed; tests pin the
  new id via FakeConfig/real-config layers (see `test_remote_agent.py`).
- **Autonomous loop** lives in App (not RemoteAgent): QTimer at `remote.ask_interval_ms` (default
  300000 ms = 5 min), starts only when >0 AND `agent.enabled`. If the H100 is unreachable you get a
  repeating log line "vision note: …" — there's no backoff/retry yet.

### M7 obsidian_vault
- Paths under vault root (config `obsidian.vault_root`, env `VAULT_ROOT`): facts →
  `Memory/Facts/{slug(category)}/{slug(key)}.md`; events → `Memory/Events/YYYY-MM-DD/event-{day}-{slug}-{HHMMSS}.md`
  (append-only, never modified); journal → `Journal/YYYY-MM-DD.md`. **Slugified** means
  spaces/camelCase become lowercase-dash file names while the *frontmatter* keeps the original
  category/key — API callers use natural language keys, files stay Obsidian-friendly.
- **`append_journal` is a read-modify-atomic-write, not mode "a"** (full atomicity everywhere; the
  doc allows plain append but RMW costs nothing at journal volume). First write creates the file
  with `{type: journal, date, tags: [desktop-pet, journal]}` frontmatter + `# <day>` heading.
  Entries are `- [HH:MM] text` lines in local time. An existing day file missing `tags` gets them
  backfilled on its next append (`setdefault`, so custom edits to other keys survive).
- Day bucketing timezone comes from `obsidian.date_timezone` (default `utc`; `local` uses system tz).
- **P1 hardening (2026-08-17):** the class is now a `QObject` — see below.
  - *Write sandboxing:* every public write (`write_fact`/`record_event`/`append_journal`) runs its
    target through `_resolve_safe()` — `os.path.realpath()` on both sides, then equality or
    `startswith(allowed_base + os.sep)` (the bare-`startswith` from the spec sketch would let a
    sibling dir like `<root>-evil/` pass). Violation → `PermissionError("WRITE DENIED: …")`, raised
    before any `mkdir`. Read-only access elsewhere is unaffected by design. The guard is on the
    *public* API, not `_atomic_write` (tests legitimately use `_atomic_write` to seed fixtures).
  - *Storage watcher:* `storage_size_bytes()` walks the vault root; `check_storage()` compares it
    against `obsidian.max_size_mb` (default 50; `<= 0` disables) and emits edge-triggered
    `vault_size_warning(bytes)` + a log line once per exceed episode — latched like StatEngine's
    hysteresis, re-armed only after usage drops under the limit. Runs at the end of every public
    write **and** on App's periodic tick (`obsidian.size_check_ms`, default 300000; App connects the
    signal to a loud log). Monitoring never raises — a full disk must not crash the pet.
  - *Fact updates are in-place and non-destructive:* `write_fact` re-reads the existing note, keeps
    `recorded_at`, preserves earlier caller meta (e.g. `confidence`) unless overridden, stamps
    `last_updated`, and only regenerates the body when it still matches the old template line — a
    human-edited body survives machine updates verbatim. New templated body: `{cat} — {key}: {value}`.
  - *Concurrency:* one module-wide `threading.RLock` wraps each public method's read-modify-write,
    so parallel callers can't lose journal lines (atomic rename already prevented torn reads; the
    lock fixes lost updates). Process-level only — VaultSprite is a single instance and takes no
    cross-process file locks.
- **M6 note:** `RemoteAgent`'s text fallback was exercised live against this box's Ollama (see §9):
  with the config model loaded, `/api/chat` vision replies describe probe images correctly; on any
  error path `build_messages()` degrades to a plain-string user message carrying window/OCR metadata.

### M8 health_audio
- **`health.tick_work_seconds` is a test knob**: work-time credited per tick; default null → real
  time (tick length, 5 s). Tests set it to e.g. 100 so the 45–60 min threshold fires in ~2 ticks.
  It's read from the section dict directly; FakeConfig overrides feed it cleanly. Don't remove it —
  nothing else makes the threshold testable without waiting an hour.
- Nudge lifecycle: counter accumulates only while active (WORK context); at threshold it zeroes,
  sets `nudge_pending` (which blocks re-fire), and emits once. **Nothing auto-clears
  `nudge_pending`** — you must call `resolve_nudge(mode)` or the pet stays nudged forever. main.py
  does NOT currently wire a dismissal path: right-click "Stretch break" fires a *new* nudge only if
  not pending, and the real trigger just re-arms after resolution. **This is a known gap** (see §5).
- `SoundBank` fully no-ops when pygame or the mixer fails to init (headless box: it logs "audio
  disabled" once at startup — that line in smoke output is expected, not an error). `play/stop/
  play_loop` never raise regardless.

---

## 4. Known gaps / TODOs for whoever's next (in priority order)

1. **step.wav and yawn.wav are generated but unwired.** M8 doc §5.4 wants walking→step loop,
   sleeping→yawn once. Wiring point: in `main.py`, watch state transitions (`player.state_finished`
   or hook inside `_play`) → `sounds.play_loop("step")` when entering `walking`, stop on exit;
   `sounds.play("yawn")` once per entry to `sleeping`. Only `chirp` is used today (click + nudge).
2. **No stat persistence.** Stats reset to config `stats.initial` on restart (DyberPet's
   `conf.py` PetData port was explicitly optional in M3 §5.6 and skipped). A JSON/YAML sidecar at
   vault or repo level is the obvious shape if wanted.
3. **Health-nudge dismissal path** (§3-M8 above): add an overlay hook (click during stretch_nudge,
   or menu item) → `health.resolve_nudge("stretch"|"skip")`. Postpone semantics exist and are tested.
4. **Windows-only paths untested**: window-standing + standee liveness, self-screenshot skip,
   pywinctl polling with a real title source, Shell_TrayWnd (not coded). All guarded; all inert on
   Linux by design. They need a Windows box to exercise — budget real time there.
5. **No LLM backoff**: repeated unreachable-H100 logs each ask_interval (default 5 min). Cheap fix:
   exponential delay or skip-after-N until context changes.

---

## 5. PySide6 6.11 build traps (verified empirically on this box)

- `QImageReader` multi-frame iteration is broken for Pillow-written animated GIFs
  (`jumpToNextImage()` → False after frame 0; first `read()` works). **Use QMovie.**
- No `reader.hasNext()` — only `jumpToNextImage()` exists (and per above, don't rely on it anyway).
- `QEvent.Type` spelling: `MouseButtonPress`, `MouseMove`, `MouseButtonRelease` (no "Event" suffix).
- `QMouseEvent` 7-arg constructor required for explicit global positions in tests:
  `(type, localPos, scenePos, globalPos, button, buttons, modifiers)`.
- QImage alpha reads can lie (`img.pixel(x,y)` on a frame converted from RGB32 reports fully
  opaque). **Validate transparency by compositing over a known color** — that's what
  `tools/render_check.py` and the "magenta-corner" assertion do.
- `QTest.mousePress(widget, button, pos=...)` expects *screen-global* coords but maps them relative
  to the widget's current geometry; once you move the window mid-drag your math drifts. Hand-built
  events with explicit globals (`tests/test_ui_overlay.py::_evt`) are deterministic — prefer those.

## 6. Testing notes (how the 91 tests work, and how to extend them)

- `tests/test_vault_and_ai.py` is **direct-run only** (`uv run python tests/test_vault_and_ai.py`;
  env: `OLLAMA_BASE_URL`, `VISION_PROBE_TIMEOUT_S`). pytest collects it by name — so it must create
  NO Qt object at import time (a module-level `QCoreApplication` clobbers conftest's session app and
  aborts Qt; the QObject host is built lazily inside `main()`). Its Ollama section talks to a real
  server with a 300 s default read timeout (first inference loads the model); probe PNG lands at
  `/tmp/vaultsprite_vision_probe.png` for visual cross-checking of the reply.

- `tests/conftest.py`: session-scoped offscreen `QApplication`; `FakeConfig` flattens
  **the real** `config/config.yaml` into dotted keys and accepts dotted-string overrides — so a new
  config key shows up in every test automatically the moment you add it to the YAML. Add sections
  via `_nest()` (already generic).
- Timers are driven deterministically: StatEngine/WorkTimer tests call `_tick()` directly (never
  wait on wall time); the one real-QTimer path per module is exercised with `qapp.exec()` + a
  `QTimer.singleShot` exit. TerrainPhysics drives its own manual ticks against a `FakeViewport`
  (position/move_to/pet_size callbacks) — that's also how you inject "windows to land on" for M4
  tests if the win32 mock surface is ever worth faking (`_get_visible_windows()` is the seam).
- RemoteAgent dispatch tests stub `_client.chat.completions` with a fake returning
  `choices[0].message.content`; no network, and they still traverse the real QThread hop (that's
  the part under test — fire-and-forget delivery back onto the GUI thread via `spin(qapp, …)`).
- UI mouse tests: global coords are expressed against the window's **original** origin (see §5);
  the click test releases *inside* the confirmation window on purpose.

## 7. Environment facts specific to this machine

- `.venv` is at repo root (`uv venv --python 3.13`). **Trap**: a stale `VIRTUAL_ENV=/root/.venv` can
  leak into shells; uv warns and ignores it, but run via `uv run …` or the `.venv/bin/…` binaries to be safe.
- The project is installed **editable** (`[tool.uv] package = true` in pyproject). Without that key,
  uv built a wheel with no editable hook (dist-info present but package not importable outside CWD) —
  if imports mysteriously "only work from the repo root", this key got lost. `uv sync --extra dev`.
- System X libs for offscreen Qt are apt-installed (§1 note). Audio: SDL mixer is unavailable here →
  SoundBank no-op (expected, tested as such).
- Image files CAN be read by this session's build agent (native image input — confirmed 2026-08-17:
  it eyeballed `sprite_test.png` and the offscreen renders directly). Visual QA is therefore both
  numeric (alpha probes over magenta, pixel diffs) **and** direct inspection of
  `/tmp/vaultsprite_render.png`; no helper scripts or remote-VLM round trips needed for image checks.

## 9. Changelog

### 2026-08-17 — P1 hardening pass: vault sandboxing, storage watcher, vision verification
1. **Write isolation lock** (§3-M7): `ObsidianVault._resolve_safe()` gates all three public write
   APIs on `os.path.realpath` containment within the vault root (with the sibling-prefix fix the
   spec sketch was missing); escape → `PermissionError("WRITE DENIED: …")` before any filesystem
   mutation. Reads elsewhere stay allowed by contract. The guard deliberately lives on the public
   API, not `_atomic_write`, so test fixtures can seed files directly.
2. **Storage watcher** (§3-M7): `ObsidianVault` is now a `QObject` with edge-triggered
   `vault_size_warning(bytes)`; `check_storage()` runs after every write + on App's new periodic
   tick (`obsidian.size_check_ms: 300000`, started/baselined in `App.start()`, logged via
   `_on_vault_size_warning`). Config gained `obsidian.max_size_mb: 50` (≤0 disables) and the tick key.
   Latch/re-arm semantics mirror StatEngine hysteresis; monitoring never raises.
3. **Frontmatter contract fixes** (§3-M7): new journal files carry `tags: [desktop-pet, journal]`
   (+ backfill via `setdefault` on legacy day files); `write_fact` updates are non-destructive —
   stable `recorded_at`, preserved prior meta (e.g. `confidence`) unless overridden, fresh
   `last_updated`, hand-edited bodies left verbatim.
4. **Concurrency lock** (§3-M7): module RLock over all read-modify-write sections; 19-thread pool
   tests prove zero lost/duplicated journal lines and no torn frontmatter (atomic rename was already
   tearing-proof; the lock closes lost updates). Process-level only, documented as such.
5. **`tests/test_vault_and_ai.py`** — standalone runner (`uv run python tests/test_vault_and_ai.py`,
   exit 0/1 + status report over Sandboxing / Size monitoring / Vault I/O / Ollama+Vision):
   sandbox escape vectors incl. symlink-out-of-root and tampered-`facts_dir` end-to-end; exact-byte
   threshold trip with latch + re-arm phases; journal/fact/concurrency end-to-end in temp roots;
   model discovery via `/api/tags` (CLI `ollama ls` fallback), stored in the runtime context, then a
   native `/api/chat` base64-PNG probe with graceful text-only/unreachable handling that verifies
   RemoteAgent's plain-text metadata fallback. **Live result this pass:** detected
   `hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q8_K_XL` (caps `completion,vision`) via `/api/tags`; probe PNG
   (red circle / blue triangle / green square on white — visually inspected before and after) was
   described correctly in ~9 s; the reply matched direct inspection exactly.
6. **Test-suite note** (§6): pytest imports that file by name at collection time, so it must stay
   free of module-level Qt objects (lazy host inside `main()`).

### 2026-08-17 — user-feedback maintenance pass (commit after `7d35f4e`)
Addressed the report in `USER_NOTES.md` (then rewritten as a resolution log):
1. **Model**: `remote.ollama_model` → `hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q8_K_XL` (`config.yaml`,
   `remote_agent.py` fallback, 4 test pins, docs/06). Verified with `ollama ls`; the old `qwen2.5:27b`
   is not installed here — which also explains why no vision replies ever surfaced (see item 8 in
   USER_NOTES.md on the "can't see stats" symptom).
2. **Sprite size ×~0.7**: `window.width/height` 128 → 89 (`config.yaml`; test assertion updated).
3. **Throw force ≈2×**: `physics.impulse_scale` 0.05 → 0.1 (caps untouched: `max_speed`, `fall_terminal`).
4. **Physics bug fixes** (§3-M4): wall-bounce sign flipped to a real rebound; flight clamped to the
   work area incl. top edge (no more off-screen-top infinite fall); re-arm log once per episode.
5. **SpeechBubble**: lineSpacing-based height + matching draw pads — wrapped lines no longer clip
   their lower half (§3-M1 trap). Stretch-bubble copy shortened so it fits one line.
6. **Housekeeping**: `Implementation Outline.md` → `IMPLEMENTATION_OUTLINE.md` in all 13 citations;
   deleted `sprite_test.png`/`test_vision.py` (vision is native now); vision test image was a chibi
   guitarist sticker — potential future sprite identity, but assets are untouched this pass.
Stats HUD: deliberately **not** added (user hypothesis that invisible replies masked everything checks
out; revisit if stats still need on-screen display).
