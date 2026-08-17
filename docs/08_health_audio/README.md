# Module 8 — Chiptune Sound & Health Nudge Engine

## 1. Module Overview & Objective

Provides non-blocking 8-bit sound triggers (preloaded `.wav` files: `step.wav`, `chirp.wav`, `yawn.wav`) and an active-work timer that — after 45–60 min of continuous work — forces the FSM to `stretch_nudge`, plays `chirp.wav`, and surfaces a health prompt.

Maps to **Module 8** of `IMPLEMENTATION_OUTLINE.md`; produces `health_audio.py`.

Extraction sources:
- **`pieterdd/StretchBreak`** — the work-timer state machine + non-blocking sound trigger pattern. **This repo is Rust (GTK4/relm4 + rodio), not Python, and has no `pygame.mixer`.** We extract the *patterns* (non-blocking playback path, idle-based break arming, skip-vs-postpone) and pair them with the canonical `pygame.mixer` recipe (researched from the official pygame/`pygame-ce` docs).
- Key Rust files: `src/main.rs` (`play_break_end_sound`, `monitor_idle_forever`), `src/backend/idle_monitoring.rs` (constants, `IdleChecker`, `ModeState`).

## 2. Minimum Required Libraries

| Package | Why |
|---|---|
| `pygame` (or `pygame-ce`) | `pygame.mixer` — preloads small WAVs into RAM, non-blocking `Sound.play()` |
| `PySide6` | `QTimer`/`QThread` for the work timer + FSM coupling |

No system drivers. **Headless caveat**: `pygame.mixer.init()` may fail without an audio device — guard with `pygame.mixer.get_init()` and fall back to a no-op stub (mirrors the repo's win32 guard convention).

## 3. Source Code Extraction (Verbatim)

### 3.1 Non-blocking sound trigger — `StretchBreak/src/main.rs` (lines 36–55, Rust)

```rust
fn play_break_end_sound() {
    thread::spawn(|| {
        fn helper() -> Result<(), ()> {
            let (_stream, handle) = OutputStream::try_default().map_err(|_| ())?;
            let sink = Sink::try_new(&handle).map_err(|_| ())?;
            let file = BufReader::new(Cursor::new(include_bytes!("sounds/break_end.wav")));
            let decoder = Decoder::new(file).map_err(|_| ())?;
            sink.append(decoder);
            sink.sleep_until_end();
            Ok(())
        }

        match helper() {
            Ok(()) => {}
            Err(()) => {
                error!("Could not play break end sound");
            }
        }
    });
}
```

Pattern: playback happens on its own ephemeral thread; the caller never blocks; failures are swallowed (log only). This is exactly the property `pygame.mixer.Sound.play()` gives us for free (see §3.3).

### 3.2 Work/activity timer constants + state — `StretchBreak/src/backend/idle_monitoring.rs` (lines 12–21, 27–39, 89–104)

```rust
pub const DEFAULT_TIME_TO_BREAK_SECS: i64 = 20 * 60;
pub const DEFAULT_BREAK_LENGTH_SECS: i64 = 90;
pub const REQUIRED_PREBREAK_IDLE_STREAK_SECONDS: u64 = 5;
const FRAME_DROP_CUTOFF_POINT_SECS: i64 = 30;
const TRANSITION_THRESHOLD_SECS: u64 = 3;
const END_OF_ACTIVE_PUSHING_TRANSITION_WINDOW: u64 = TRANSITION_THRESHOLD_SECS - 2;

pub struct IdleChecker;
impl AbstractIdleChecker for IdleChecker {
    fn get_idle_time_in_seconds(&self) -> u64 {
        match UserIdle::get_time() {
            Ok(time) => time.as_seconds(),
            Err(_) => { 1 }   // fake 1s on error
        }
    }
}

pub enum ModeState {
    Normal { progress_towards_break: Duration, progress_towards_reset: Duration, idle_state: DebouncedIdleState },
    PreBreak { started_at: DateTime<Utc> },
    Break   { progress_towards_finish: Duration, idle_state: DebouncedIdleState },
}
```

Key semantics:
- `DEFAULT_TIME_TO_BREAK_SECS = 20 min` (note: StretchBreak defaults to **20 min**, not 45–60 — VaultSprite will use 45–60 min per the outline).
- Break is armed by **OS idle time** (`UserIdle::get_time()`), *not* a wall-clock work counter. Break countdown advances **only while the user is idle**; any activity pauses it. A PreBreak→Break transition requires 5 s of continuous idle (`REQUIRED_PREBREAK_IDLE_STREAK_SECONDS`) — the debounce that prevents false nudges.
- State machine: `Normal → PreBreak → Break` (+ `SnoozedUntil`/`Muted` presence modes; `skip_break()` resets all progress, `postpone_break()` credits partial progress back).

### 3.3 Main loop — `StretchBreak/src/main.rs` `monitor_idle_forever` (lines 57–114, excerpt)

```rust
loop {
    let idle_info = idle_monitor_ref.lock().expect("unlock failed").refresh_idle_info();
    // persist state every >= 15s
    if last_state_write.checked_add_signed(TimeDelta::seconds(15)).unwrap() < Utc::now() {
        let persistable_state = idle_monitor_ref.lock().expect("unlock failed").export_persistable_state();
        persistable_state.save_to_disk(); // best-effort
        last_state_write = Utc::now();
    }
    idle_info_sender.send(idle_info).expect("could not send idle info");

    match idle_info.last_mode_state {
        ModeState::Normal { .. } => {
            if let Some(prev) = previous_idle_info {
                if matches!(prev.last_mode_state,
                    ModeState::Break { idle_state: DebouncedIdleState::Idle { .. }, .. }) {
                    play_break_end_sound();   // only when genuinely idle through the break
                }
            }
        }
        _ => {}
    }
    previous_idle_info = Some(idle_info);
    sleep(StdDuration::from_millis(250));     // 250 ms poll cadence
}
```

### 3.4 Canonical `pygame.mixer` recipe (from pygame/pygame-ce official docs)

```python
import pygame

pygame.mixer.pre_init(44100, -16, 2, 1024)   # low-latency SFX; must run BEFORE pygame.init()
pygame.init()
assert pygame.mixer.get_init(), "mixer failed to init (headless?)"

step  = pygame.mixer.Sound("assets/step.wav")   # whole file copied into RAM at load time
chirp = pygame.mixer.Sound("assets/chirp.wav")
yawn  = pygame.mixer.Sound("assets/yawn.wav")

def blip(s: pygame.mixer.Sound, fade_in_ms: int = 0):
    ch = s.play(loops=0, maxtime=0, fade_ms=fade_in_ms)   # non-blocking; returns Channel | None
    return ch                                             # hold for get_busy()/stop() if needed

step.set_volume(0.7)     # 0.0–1.0; applies to all current+future plays of this Sound
ch = blip(step)          # overlapping SFX fine: same Sound can play on many channels
if ch and ch.get_busy():
    ch.stop()
```

Documented contract points (from `docs/reST/ref/mixer.rst`, pygame & pygame-ce):
- **"All sound playback is mixed in background threads. When you begin to play a Sound object, it will return immediately"** — `Sound.play()` is non-blocking by design.
- `Sound` "represents actual sound sample data" — the constructor copies the whole file into memory (contrast `pygame.mixer.music`, which streams). Constructor forms: `Sound(filename)`, `Sound(file=...)`, `Sound(buffer=...)`; use keywords to avoid bytes-as-path ambiguity.
- `Sound.play(loops=0, maxtime=0, fade_ms=0) -> Channel | None`; `loops=-1` repeats forever; `fade_ms` is a fade-*in*. Default 8 simultaneous playback channels (`set_num_channels`); `set_reserved(n)` protects channels from auto-stealing.
- Volume is quantized to /128 steps (`set_volume(0.1)` reads back `0.09375`); channel volume resets on each new play.
- Buffer default is **512** since pygame 2.0 (was 4096); docs' own low-latency example is `pre_init(44100, -16, 2, 1024)`.
- **Threading**: the "sounds must be called from the same thread as mixer.init" warning is *not* in the official docs — only `pygame.event.*` calls are documented as display-thread-bound. Safe pattern for PySide6: call all mixer code from the Qt main thread via `QTimer`/signals.

## 4. Logic & Data Flow Breakdown

1. **Audio path** (StretchBreak §3.1 / pygame §3.4): a tiny WAV is decoded and played without blocking the caller. In Rust it's an explicit spawned thread; in pygame it's intrinsic — `Sound.play()` returns immediately and mixing continues in SDL background threads. `health_audio.py` therefore needs **no** audio thread for short SFX: just call `play()` from a `QTimer`-driven slot.
2. **Preload into RAM**: `pygame.mixer.Sound(path)` copies the file into memory at construction. Load `step.wav`/`chirp.wav`/`yawn.wav` once at startup; later calls are instant. For overlapping footsteps, the same `Sound` may play on multiple channels simultaneously.
3. **Work timer** (StretchBreak §3.2): the outline wants 45–60 min of *continuous work* before a nudge. Two viable models:
   - *(a) Idle-based (StretchBreak's):* poll OS idle time every ~250 ms; accumulate work-seconds only while idle time is near-zero (user active). A nudge fires when accumulated work ≥ threshold and the user then goes idle for 5 s (debounced) — this is the `stretch_nudge` trigger.
   - *(b) Context-based (matches M5):* accumulate time while `context == "WORK"` (from `context_detector.py`); reset when context flips to PLAY. Simpler and directly matches our architecture.
4. **State machine**: `Normal → PreBreak → Break`. On entering Break: force FSM state `stretch_nudge`, play `chirp.wav`, show a health prompt (overlay bubble). Skip = full reset; Postpone = partial credit (`progress = threshold − postpone`). This skip-vs-postpone distinction is worth porting.
5. **Reset conditions**: any significant idle (sleep/standby, `FRAME_DROP_CUTOFF_POINT_SECS`) or context switch to PLAY resets/zeroes progress. The 45–60 min window should be a config constant.

## 5. Refactoring & Integration Notes

Target: `health_audio.py` exposing a `HealthAudio`/`HealthNudge` QObject combining (a) pygame mixer sound playback methods and (b) a background work timer emitting `stretch_nudge` events.

Step-by-step:

1. **Audio wrapper**:
   ```python
   class SoundBank:
       def __init__(self, assets: Path):
           try:
               pygame.mixer.pre_init(44100, -16, 2, 1024)
               pygame.init()
           except pygame.error:
               self._disabled = True            # headless dev box → no-op
           if pygame.mixer.get_init():
               self._sounds = {
                   "step":  pygame.mixer.Sound(str(assets / "step.wav")),
                   "chirp": pygame.mixer.Sound(str(assets / "chirp.wav")),
                   "yawn":  pygame.mixer.Sound(str(assets / "yawn.wav")),
               }
       def play(self, name, volume=1.0):
           if not self._disabled:
               self._sounds[name].set_volume(volume)
               self._sounds[name].play()
       def play_loop(self, name, loops=-1): ...   # for repeated walking steps
       def stop(self, name): self._sounds[name].stop()
   ```
   Call from the Qt main thread (QTimer/slot); no worker thread needed for these short SFX.
2. **Work timer** — `class WorkTimer(QObject)`:
   ```python
   stretch_nudge = Signal()
   WORK_THRESHOLD_MIN = int(os.getenv("HEALTH_WORK_MIN", "50"))   # 45–60 per outline
   def __init__(self):
       self._timer = QTimer(self)          # 5 s tick
       self._timer.timeout.connect(self._tick)
       self._work_seconds = 0
   def set_active(self, active: bool):     # connect to M5 context_changed
       self._active = active
       if not active: self._work_seconds = 0   # PLAY → reset
   def _tick(self):
       if not self._active: return
       self._work_seconds += 5
       if self._work_seconds >= self.WORK_THRESHOLD_MIN * 60:
           self._work_seconds = 0
           self.stretch_nudge.emit()
   ```
   Add the 5 s idle debounce (port `REQUIRED_PREBREAK_IDLE_STREAK_SECONDS`) if you prefer StretchBreak's OS-idle model — use the `user_idle`-style poll or the M5 context as the activity source.
3. **Nudge handler**: connect `stretch_nudge → (soundbank.play("chirp"), fsm.force_state("stretch_nudge"), overlay.show_health_prompt())`. Implement `snooze()`/`skip()` to reset; `postpone()` to credit back, mirroring StretchBreak §3.2.
4. **Wire walking sounds**: the FSM (M2) emits `state_changed("walking")` → play `step` loop; `"sleeping"` → `yawn` once; stop loops on state exit. This is the "chiptune triggers" half of the module.
5. **Strip from the reference**: everything GTK/relm4/libnotify/DBus/Rust — keep only the timer state machine + sound trigger pattern. The `.wav` assets (`step.wav`, `chirp.wav`, `yawn.wav`) must be created locally (StretchBreak ships only `break_end.wav`; source tiny 8-bit chiptunes via any WAV generator and check them into `assets/`).
6. **Configuration**: `HEALTH_WORK_MIN` (45–60), sound volumes, and per-sound enable flags as module constants/env.
7. **Testing**: with `pygame` mocked/disabled (headless), drive `WorkTimer._tick` manually: assert accumulation pauses outside WORK context, resets on PLAY, fires `stretch_nudge` exactly once at threshold, and re-arms after. SoundBank tests assert `play`/`stop` calls and that a disabled mixer yields no-ops (no exceptions).

## 6. Source Files (Reference Copies)

Full verbatim copies from `pieterdd/StretchBreak` (Rust), kept locally:

| File | Purpose |
|---|---|
| `source/main.rs` | `play_break_end_sound()` (non-blocking sound pattern, L36–55) + `monitor_idle_forever()` (250 ms loop, idle-based break arming, state persist, sound trigger, L57–114) |
| `source/idle_monitoring.rs` | **The work-timer state machine** (~3k lines): constants (`DEFAULT_TIME_TO_BREAK_SECS`, `REQUIRED_PREBREAK_IDLE_STREAK_SECONDS`, L12–21), `IdleChecker` (OS idle via `UserIdle`), `ModeState`/`DebouncedIdleState` (L53–120), `refresh_idle_info` (L509–723), `snooze`/`mute`/`skip_break`/`postpone_break` (L427–834) |

> The repo is Rust — port only the *patterns* to Python. `step.wav`/`chirp.wav`/`yawn.wav` assets must be created locally (StretchBreak ships only `break_end.wav`).
