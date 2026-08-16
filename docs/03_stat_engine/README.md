# Module 3 — Needs & Stat Decay Engine

## 1. Module Overview & Objective

Provides the background time-driven decay of pet stats (Hunger/Satiety, Energy, Boredom) and emits PySide6 signals when a stat crosses a critical level (e.g. boredom > 80). The FSM (M2) and health module (M8) subscribe to those signals to change pet behavior.

Maps to **Module 3** of `Implementation Outline.md`; produces `stat_engine.py`. The directive is to strip DyberPet's shop, inventory, GUI menus, pomodoro/task/dashboard systems and keep *only* the decay math + threshold signalling.

Extraction source: **`ChaozhongLiu/DyberPet`** (PySide2-era project). Key files: `DyberPet/settings.py` (constants), `DyberPet/modules.py` (`Scheduler_worker`, `change_hp`/`change_fv`), `DyberPet/DyberPet.py` (`DP_HpBar.updateValue`, `_change_status`).

> **Reality check vs. the outline**: DyberPet has **no `core/pet.py`** and does **not** use `QTimer` for decay — it uses **APScheduler's `QtScheduler`** (`interval.IntervalTrigger(minutes=1)`). Only two stats exist (**HP/"Satiety"** and **FV/"Favorability"**), with a 4-tier threshold ladder standing in for separate "energy/boredom" bars. The extraction below documents the actual code; the refactoring section adapts it to the outline's `QTimer` + multi-stat `(Hunger, Energy, Boredom)` contract.

## 2. Minimum Required Libraries

| Package | Why |
|---|---|
| `PySide6` | `QObject`, `Signal`, `QThread`, `QTimer` — the refactored engine is signal-driven |
| `APScheduler` (3.x, incl. Qt extras) | Only if you keep DyberPet's scheduler; `pip install apscheduler` pulls the Qt scheduler extra. **Recommended: drop it** and use `QTimer` per the outline |
| `PyYAML`/JSON (stdlib) | Only if porting persistence (`conf.py`) — optional |

No system drivers.

## 3. Source Code Extraction (Verbatim)

### 3.1 Constants — `DyberPet/settings.py` (lines 44–55, 87–90)

```python
HP_TIERS = [0,50,80,100]
TIER_NAMES = ['Starving', 'Hungry', 'Normal', 'Energetic']
HP_INTERVAL = 2
LVL_BAR = [20] + [120]*200
PP_HEART = 0.8
PP_COIN = 0.9
PP_BUBBLE = 0.15
...
# when falling met the screen boundary,
# it will be bounced back with this speed decay factor
SPEED_DECAY = 0.5
AUTOFEED_THRESHOLD = 60
```

- `HP_INTERVAL = 2`: internal stat resolution (units per display point). `HP_TIERS = [0, 50, 80, 100]` are the display-point thresholds. `TIER_NAMES` maps a tier index (0–3) to a severity label.

### 3.2 Scheduler worker — `DyberPet/modules.py` (lines 821–887, 1122–1126)

```python
class Scheduler_worker(QObject):
    sig_settext_sche = Signal(str, str, name='sig_settext_sche')
    sig_setact_sche = Signal(str, name='sig_setact_sche')
    sig_setstat_sche = Signal(str, int, name='sig_setstat_sche')
    ...
    def __init__(self, parent=None):
        super(Scheduler_worker, self).__init__(parent)
        self.is_killed = False
        self.is_paused = False
        ...
        self.scheduler = QtScheduler()
        self.scheduler.add_job(self.change_hp, interval.IntervalTrigger(minutes=1))
        self.scheduler.add_job(self.change_fv, interval.IntervalTrigger(minutes=1))
        self.scheduler.start()
    ...
    def kill(self):
        self.is_paused = False
        self.is_killed = True
        self.scheduler.shutdown()

    def pause(self):
        self.is_paused = True
        self.scheduler.pause()

    def resume(self):
        self.is_paused = False
        self.scheduler.resume()
    ...
    def change_hp(self):
        self.sig_setstat_sche.emit('hp', -1)

    def change_fv(self):
        self.sig_setstat_sche.emit('fv', 1)
```

### 3.3 Decay application + tier signalling — `DyberPet/DyberPet.py` `DP_HpBar.updateValue` (lines 137–197)

```python
def updateValue(self, change_value, from_mod):
    before_value = self.value()

    if from_mod == 'Scheduler':
        if settings.HP_stop:
            return
        new_hp_inner = max(self.hp_inner + change_value, 0)
    else:
        if change_value > 0:
            new_hp_inner = min(self.hp_inner + change_value*self.interval, self.hp_max)
        elif change_value < 0:
            new_hp_inner = max(self.hp_inner + change_value*self.interval, 0)
        else:
            return 0

    if new_hp_inner == self.hp_inner:
        return 0
    else:
        self.hp_inner = new_hp_inner

    new_hp_perct = math.ceil(round(self.hp_inner/self.interval, 1))

    if new_hp_perct == self.hp_perct:
        settings.pet_data.change_hp(self.hp_inner)
        return 0
    else:
        self.hp_perct = new_hp_perct
        self.setFormat('%i/100'%self.hp_perct)
        self.setValue(self.hp_perct)

    after_value = self.value()

    hp_tier = sum([int(after_value>i) for i in self.hp_tiers])

    #告知动画模块、通知模块
    if hp_tier > settings.pet_data.hp_tier:
        self.hptier_changed.emit(hp_tier,'up')
        settings.pet_data.change_hp(self.hp_inner, hp_tier)
        self._onTierChanged()
    elif hp_tier < settings.pet_data.hp_tier:
        self.hptier_changed.emit(hp_tier,'down')
        settings.pet_data.change_hp(self.hp_inner, hp_tier)
        self._onTierChanged()
    else:
        settings.pet_data.change_hp(self.hp_inner)

    self.hp_updated.emit(self.hp_perct)
    return int(after_value - before_value)
```

### 3.4 Central stat-mutation slot — `DyberPet/DyberPet.py` `_change_status` (lines 1390–1427)

```python
def _change_status(self, status, change_value, from_mod='Scheduler', send_note=False):
    # Check system status
    if from_mod == 'Scheduler' and is_system_locked() and settings.auto_lock:
        print("System locked, skip HP and FV changes")
        return
    if status not in ['hp','fv']:
        return
    elif status == 'hp':
        diff = self.pet_hp.updateValue(change_value, from_mod)
    elif status == 'fv':
        diff = self.pet_fv.updateValue(change_value, from_mod)

    if send_note:
        if diff > 0:
            diff = '+%s'%diff
        elif diff < 0:
            diff = str(diff)
        else:
            return
        if status == 'hp':
            message = self.tr('Satiety') + " " f'{diff}'
        else:
            message = self.tr('Favorability') + " " f'{diff}'
        self.register_notification('status_%s'%status, message)

    # Periodically triggered events
    if status == 'hp' and from_mod == 'Scheduler': # avoid being called in both hp and fv
        # Random Bubble
        if random.uniform(0, 1) < settings.PP_BUBBLE:
            self.bubble_manager.trigger_scheduled()
        # Auto-Feed
        if settings.pet_data.hp <= settings.AUTOFEED_THRESHOLD*settings.HP_INTERVAL:
            self.autofeed.emit()
```

### 3.5 Thread wiring — `DyberPet/DyberPet.py` (lines 1888–1907, abbreviated)

```python
self.scheduler_thread = QThread()
self.scheduler_worker = Scheduler_worker()
self.scheduler_worker.moveToThread(self.scheduler_thread)
# key wire — scheduler → central slot, cross-thread via Qt queued signal
self.scheduler_worker.sig_setstat_sche.connect(self._change_status)
self.scheduler_thread.start()
```

## 4. Logic & Data Flow Breakdown

1. **Scheduling** (`modules.py:882–887`): a `QtScheduler` registers two **1-minute interval** jobs: `change_hp` and `change_fv`. Unlike `QTimer`, these are true cron-style triggers running on the main event loop (Qt's scheduler needs no thread for timing; the enclosing `QThread` only hosts the one-shot greeting, a legacy quirk).
2. **Decay payloads** (`modules.py:1122–1126`): each minute the scheduler emits `sig_setstat_sche('hp', -1)` (hunger −1 internal unit) and `sig_setstat_sche('fv', +1)` (favorability +1). These are **Qt queued signals**, so the receiver runs on the main thread regardless of which thread emitted — the same pattern we want in `stat_engine.py`.
3. **Apply + clamp** (`updateValue:137–161`): the `'Scheduler'` branch applies a raw increment and floors at 0 (`max(hp_inner + change_value, 0)`), respecting a global `HP_stop` pause flag. Item/buff branches (stripped) multiply by an effect duration. If nothing changed, it returns early — a cheap idempotency guard.
4. **Display resolution** (`updateValue:163–171`): internal units are converted to display percent with `math.ceil(inner/interval)`; when the percent actually changes the progress bar repaints. (Internal resolution exists so small decays accumulate into visible steps — e.g. −1 internal ≈ −0.5 display/min, so the bar visibly drops roughly every 2 minutes.)
5. **Threshold / tier detection** (`updateValue:173–191`): `hp_tier = sum(int(after_value > i) for i in HP_TIERS)` bucketizes the display value into one of the 4 tiers. A crossing emits **`hptier_changed(tier, 'up'|'down')`** and persists; no crossing just persists. This is the "threshold event trigger" the outline asks for (e.g. boredom>80 analogue = entering tier 0 `Starving`).
6. **Fan-out hub** (`_change_status:1390–1427`): the single slot all decays/feeds converge on. It (a) skips decay while the OS is locked (`is_system_locked()`), (b) routes `hp`/`fv` to their bar objects, (c) optionally posts a notification, (d) runs **periodic event triggers**: with probability `PP_BUBBLE` a hunger bubble, and when `hp ≤ AUTOFEED_THRESHOLD × HP_INTERVAL` emits `autofeed` — a canonical "critical level" signal.
7. **Threading model**: `QThread` + `QObject.moveToThread` + queued signal is the exact architecture to copy; the reference just runs the jobs on the main loop instead of a `QTimer`. Note the important skip-if-locked guard: decay should not tick while the user is away (matches M8's "only count active work").

## 5. Refactoring & Integration Notes

Target: `stat_engine.py` exposing `class StatEngine(QObject)` with the outline's three stats (**Hunger, Energy, Boredom**), `QTimer`-driven decay, and threshold signals. **Strip everything** about shop/inventory/coins/tasks/pomodoro/accessories/dashboard.

Step-by-step:

1. **Define stats & constants** — port the tier idiom from `settings.py` but expand to three counters:
   ```python
   class StatKind(Enum): HUNGER="hunger"; ENERGY="energy"; BOREDOM="boredom"
   BOUNDS = {HUNGER: (0, 100), ENERGY: (0, 100), BOREDOM: (0, 100)}
   DECAY_RATE = {HUNGER: -1, ENERGY: -1, BOREDOM: +1}   # per tick
   CRITICAL = {BOREDOM: 80, HUNGER: 20, ENERGY: 15}      # boredom > 80 → signal_bored
   ```
   Keep DyberPet's "internal resolution" trick (`HP_INTERVAL`) if you want smooth visible decay from a 1-minute tick.
2. **Replace APScheduler with a single `QTimer`** (per outline):
   ```python
   self._timer = QTimer(self)
   self._timer.setInterval(60_000)          # 1 min
   self._timer.timeout.connect(self._tick)
   ```
   `_tick` decrements hunger/energy, increments boredom, clamps into `BOUNDS`, and calls `_evaluate_thresholds()`.
3. **Emit signals instead of calling UI** — mirror `hptier_changed`/`autofeed`:
   - `stat_changed = Signal(StatKind, int)`
   - `signal_hungry = Signal()` / `signal_bored = Signal()` / `signal_tired = Signal()` — emitted on a *crossing* (track previous tier/level, only fire on change, exactly like the `hp_tier` up/down comparison).
   The FSM (M2) and health module (M8) connect to these; no module reaches into the engine's internals.
4. **Port the system-lock skip** (`_change_status:1392`): gate the tick on an `active()` flag set by the context detector (M5) so stats only decay during real desktop presence (or, per M8, only while in a WORK context).
5. **Pause/resume** — port `pause()`/`resume()`/`kill()` from `Scheduler_worker` to stop the `QTimer` while dragging (the overlay's `drag_started` signal) or while the pet sleeps.
6. **Persistence (optional)** — port `PetData.change_hp` from `conf.py` to save JSON state on change, keyed by stat; load on startup.
7. **Testing**: headless `QCoreApplication` + `QTimer.singleShot`-advanced ticks; assert clamping at bounds, exact threshold-crossing signal emission (emit exactly once), and pause/resume behaviour. No GUI required since the engine is pure `QObject` + signals.

## 6. Source Files (Reference Copies)

Full verbatim copies from `ChaozhongLiu/DyberPet`, kept locally:

| File | Purpose |
|---|---|
| `source/settings.py` | `HP_TIERS`/`TIER_NAMES`/`HP_INTERVAL`/`AUTOFEED_THRESHOLD` + HP_stop/FV_stop pause flags — the constants |
| `source/modules.py` | `Scheduler_worker` (QtScheduler + `change_hp`/`change_fv`), `Animation_worker` (`_cal_prob` stat→behavior gating), `kill`/`pause`/`resume` |
| `source/DyberPet.py` | `DP_HpBar.updateValue` (decay+clamp+tier detection), `DP_FvBar`, `_change_status` (central stat-mutation hub + threshold triggers), thread wiring |
| `source/conf.py` | `PetData` (stat persistence), `ItemData` (strip), JSON save/load |
| `source/run_DyberPet.py` | App-level wiring (signal fan-out to notifications/dashboard — port only the stat paths) |
| `source/bubbleManager.py` | Tier→bubble/notification mapping (`{0:["feed_required"],1:["hp_low"],...}`) — port if you want stat-driven pet speech |
