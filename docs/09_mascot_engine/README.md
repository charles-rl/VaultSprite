# Module 9 — Mascot Behavior Engine (Shimeji XML runtime)

## 1. Module Overview & Objective

Replaces the hand-tuned 6-state GIF FSM's *ambient* decision layer with the standard
Shimeji XML behavior model (community-pack compatible) while keeping `main.py:App` as the
orchestrator for external forces (drag/flick/stats/vision). The pet parses standard
`actions.xml` / `behaviors.xml` pack files and renders them over the transparent overlay.

Maps to **Module 9** of `IMPLEMENTATION_OUTLINE.md`; produces `mascot_engine.py` (pure-Python
`MascotCore`) + `mascot_environment.py` (border/area geometry + safe expression evaluator).

Extraction sources:
- **`DalekCraft2/Shimeji-Desktop`** (the modern canonical Shimeji-ee, JDK 25, cross-platform,
  New-BSD/zlib licensed) — this is the **primary reference** and supersedes `pixelomer/Shijima-Qt`
  / `libshijima` (C++/GPL) previously studied. We port the *patterns* only (AGENTS.md rule:
  don't copy their GUIs wholesale). Its `conf/{actions,behaviors}.xml` default pack is the
  extraction basis for `assets/steve_shimeji/conf/*.xml` (the Steve character ships its own copy).
- The bundled pack at `assets/steve_shimeji/` (user-supplied `Shimeji-ee.jar` + `img/Steve/*.png`)
  is the *data* side we actually consume — no sprite baking, no Java.

## 2. Minimum Required Libraries

| Package | Why |
|---|---|
| `PySide6` | `QTimer` clock + `QSystemTrayIcon` manager + telemetry overlay (UI layer only) |
| stdlib `xml.etree.ElementTree` | parse the pack XML (core is pure Python, no Qt) |

`MascotCore`/`mascot_environment` import **no Qt** — they unit-test by calling `core.tick()`
directly (same style as terrain/stat tests).

**Structural split (2026-08-18):** the engine is spread across
`mascot_engine.py` (**`MascotCore` + facade** re-exporting the public API),
`mascot_actions.py` (action runners), `mascot_data.py` (pose/animation + behavior-pool
types), `mascot_vars.py` (`ActionVars` `${once}`/`#{per-tick}` store) and `mascot_xml.py`
(namespace-agnostic XML helpers), plus `mascot_environment.py` (geometry + whitelist
evaluator). See §5 for the module map.

## 3. Source Code Extraction (Verbatim)

Full verbatim Java sources from `DalekCraft2/Shimeji-Desktop` are kept under `source/`:

| Pattern | Reference file |
|---|---|
| Action base + per-tick/subtick flow | `source/.../action/Action.java`, `ActionBase.java`, `InstantAction.java` |
| Animation runner (poses, border_type, drag) | `action/Animate.java`, `action/BorderedAction.java`, `action/Dragged.java` |
| Physics: Fall integration + IE stick + clamp | `action/Fall.java`, `action/FallWithIE.java`, `action/Jump.java` |
| Sequence / Select child flow | `action/Sequence.java`, `action/Select.java` |
| Offset / Look / Turn / Move(+Turn) | `action/Offset.java`, `action/Look.java`, `action/Turn.java`, `action/Move.java`, `action/MoveWithTurn.java` |
| Resist (Regist), ThrowIE, WalkWithIE, Breed (no-op) | `action/Regist.java`, `action/ThrowIE.java`, `action/WalkWithIE.java`, `action/Breed.java` |
| Behavior roulette (freq, NextBehavior, flatten) | `behavior/UserBehavior.java`, `behavior/Behavior.java`, `config/BehaviorBuilder.java` |
| XML → runner dispatch + animation build | `config/Configuration.java`, `config/ActionBuilder.java`, `config/AnimationBuilder.java`, `config/ActionRef.java` |
| Environment geometry (Area/Border/Wall/FloorCeiling) | `environment/Environment.java`, `environment/Area.java`, `environment/Border.java`, `environment/Wall.java`, `environment/FloorCeiling.java`, `environment/MascotEnvironment.java` |
| Safe scripting `${...}` / `#{...}` semantics | `script/Script.java`, `script/Variable.java`, `script/VariableMap.java`, `script/Constant.java` |
| Manager tick loop + recovery | `Manager.java`, `Mascot.java` |
| Inspect/telemetry dialog (our overlay mirror) | `DebugWindow.java` |
| System-tray / manager menu (our tray mirror) | `TrayMenu.java`, `Settings.java` |
| Pack definitions (corrected defaults) | `source/conf/actions.xml`, `source/conf/behaviors.xml`, `source/conf/Mascot.xsd` |

### Environment geometry
- **Borders** (`hborder`/`vborder`): `is_on(p) = fabs(coord - line) < 1.0 and faces(p)`; `faces()`
  checks the perpendicular range. **Tolerance is exactly 1.0 px** (`BORDER_TOL = 1.0`). Works
  because Fall sub-steps in small increments.
- **`Area{top,right,bottom,left}`**: `visible()` True when non-degenerate; a sentinel all-negatives
  area is "invisible" → its borders never match (used for a missing tracked window).
- Env fields: `ceiling`, `floor`, `screen`, `work_area`, `active_ie` (tracked foreground window,
  an area that also carries dx/dy = window-move delta), `cursor` (x,y,dx,dy), `allows_breeding`,
  `sticky_ie`, `subtick_count`.
- **Scaling**: `set_scale(s)` multiplies geometry; our tray "scale" control folds into frame sizing.

### The tick loop + the critical robustness ladder (`Manager.java`)
`tick()` tries, in order, and each attempt falls through on failure:
1. normal tick; 2. force `Fall`; 3. `Fall` + `detach_from_borders` (nudge ~1.1 px off a border);
4. `Fall` + `reset_position` (random point inside screen) + detach. If all four fail → broken; throw.

This ladder is the single most important pattern: a malformed or infinite action can never wedge
the pet. Ported verbatim (~15 lines) in `MascotCore.tick()`.

### Behavior roulette (`UserBehavior.java`)
Candidate pool = prior behavior's `<NextBehavior>` block. `Add="true"` merges the *initial* list
with that block; `Add="false"` uses only it. A requested `queued_behavior` makes the pool just
`[that]`. Conditions evaluated at selection time; weighted pick by `Frequency`; `Hidden="true"`
excluded from ambient roulette; reserved built-ins (`Fall`, `Dragged`, `Thrown`) are freq-0/hidden,
entered only when forced.

### Action model — parser dispatch + core actions
`Type` → runner: `Sequence`, `Select`, `Animate`, `Stay`, `Move`, `MoveWithTurn`, `Jump`, `Fall`,
`Dragged`, `Regist`, `Turn`, `Look`, `Offset`, plus `Embedded` classes
(`FallWithIE`, `WalkWithIE`, `ThrowIE`, `Breed`, …). An action may have several `<Animation>`
blocks each gated by a `Condition`; the first matching one plays. The pose list loops
(`time %= total_duration`) until the action's `Duration` attr (or its Condition) ends it.

**Solo-pet constraint**: `env.allows_breeding=False`; `Breed`, `SelfDestruct`, `Scan*`,
`Broadcast*`, `Interact`, `Transform`, `Mute` parse but run as **no-op advances**. Frames 38–46
(used only by the two Breed actions) are instead **repurposed as visual-only gags** (see §5) so
every frame in the pack can render.

> **Fidelity fixes (2026-08-17 feedback pass — see BUILD_NOTES §9):** three places the port
> originally diverged from the reference and now matches it: (a) **`Animate`** runs its effective
> animation **once** then ends (`Animate.hasNext()` = `getTime() < getAnimation().getDuration()`),
> not forever — this is how the `Bouncing` splash releases; (b) **`Select`** picks the first
> effective branch, runs it to completion, then **ends** (`ComplexAction.hasNext()` is false once
> the current child's `hasNext()` is), it must not re-init a finished branch (that re-loop kept the
> pet stuck on the landing bounce); (c) **`Dragged` sway** uses the reference damped oscillator
> `footDx=(footDx+(cursorX-footX)*0.1)*0.8; footX+=footDx` exposed as `FootX`/`FootDX` so the lean
> poses swing like a pendulum around a stopped cursor instead of holding one static lean. The pack
> art is also the LEFT-facing image, so the UI mirrors when `looking_right`.

### Fall physics (`Fall.java`, per substep)
```
vx -= vx*RegistanceX / sub        # default RegX=0.05, RegY=0.1, Gravity=2
vy += (Gravity - vy*RegistanceY) / sub
anchor += velocity / sub
clamp anchor to work_area sides/top/bottom (corner snap: near floor & hit a side → pull y to floor-1.1)
IE_STICK: if the step crosses an activeIE border from outside→inside, snap anchor onto it  # lands "on" a window
```
`on_land()` = any work_area/ceiling/floor border or activeIE `is_on`; IE ignored on the first tick.

### Jump arc (`Jump.java`)
target (TargetX, TargetY); effective velocity = `VelocityParam * unit(delta)` (target.y pre-offset
by `-abs(dx)` so it arcs up); snap + finish once within `VelocityAbs`; set looking_right from sign.

### Expressions — `${...}` (once) vs `#{...}` (every tick)
Evaluated by a **whitelist tokenizer + Pratt parser** in `mascot_environment.py` — **no `eval()`
anywhere**; unknown names/methods evaluate to False so exotic behaviors degrade safely instead of
crashing the pet. Observed tokens: numbers, `+ - * /`, comparisons, `&& ||`, parens,
`Math.random()`, property access (`mascot.environment.floor.isOn(...)`, `.x/.y/.left/...`,
`lookRight`), and ternary.

> **Pack bug fixed (2026-08-17):** `assets/steve_shimeji/conf/actions.xml` lines 449/539 had
> `Math.random*100` (missing parens → NaN TargetX). Same bug exists upstream in DalekCraft2's
> default XML (`source/conf/actions.xml` 455/551) — patched locally in the Steve pack.

## 4. Logic & Data Flow Breakdown

1. **Parse** `conf/actions.xml` + `conf/behaviors.xml` into action runners + behavior pool.
2. **Tick** (`MascotCore.tick()`): roll cursor → `time++` → activate queued behavior → run the
   active action's subtick → on finish, pick next behavior via roulette → the 4-level recovery
   ladder catches anything that slips.
3. **Ambient vs external**: the engine's roulette owns idle-time behavior on its own tick; `App`
   forces *external* behaviors (`Dragged`/`Thrown`/stats/stretch/vision "talking"). Every ambient
   transition is emitted (`behavior_changed`, `pose_changed`, `fell_down`) so `App` can log to the
   Vault, play sounds, or force "talking" — exactly how `TerrainPhysics` owns physics ticks and
   reports via signals.
4. **UI render**: `MascotEngine(QObject)` emits `{image_path, image_anchor, world_anchor,
   looking_right}`; a pixmap cache resolves `QImageReader().read()` single-reads (only animated-GIF
   multi-frame iteration is broken on this build — single reads are reliable).
5. **Telemetry**: every tick, engine state (anchor, active behavior, frame index, surface anchor,
   workArea/activeIE rects) is written to the Vault when `debug.vault_logging` is on, and shown by
   the on-screen inspector when `debug.telemetry_overlay` is on.
6. **Tray manager**: `QSystemTrayIcon` for global scale, behavior toggles (exclude-by-name via
   `mascot.excluded_behaviors`), and dismiss/quit.

## 5. Refactoring & Integration Notes

Target: a pure-Python engine + a thin `MascotEngine(QObject)` owning the `QTimer` and signals in the
UI layer. `App` stays the sole owner of *external* animation-force decisions.

0. **Module map (structural split, 2026-08-18)** — the engine was split out of one 1489-line
   `mascot_engine.py` into focused files so each has one concern (dependency graph is acyclic;
   the action runners talk to the core only through the duck-typed `self.core` + string type-hints,
   so `mascot_actions` never imports `MascotCore`):

   | Module | Responsibility | Depends on |
   |---|---|---|
   | `mascot_engine.py` | **`MascotCore`** (parser, behavior roulette, tick loop, 4-level recovery ladder) + **facade** re-exporting the whole public API; `_js_truthy` | all below |
   | `mascot_actions.py` | action runners `Action`/`AnimationAction`/`Stay`/`Move`/`Fall`/`Jump`/`Sequence`/`Select`/`Reference`/`Instant`/`_Draggable`/`_NoOp*` + `core_view`/`is_true_js`/`strip_js_expr` | environment, vars, data |
   | `mascot_data.py` | `Pose`, `AnimList`, `MascotState`, `BehaviorNode`, `BehaviorDef`, pool-source types, `_PoolSourcesOf` | environment |
   | `mascot_vars.py` | `ActionVars`, `_UNDEFINED`, `_DynOnce`, `_coerce_literal` (`${once}` / `#{per-tick}`) | environment |
   | `mascot_xml.py` | namespace-agnostic XML helpers (`local_name`, `_attr_of`, `_attrs`, `_iter_elements`, `_load`) | stdlib |
   | `mascot_environment.py` | geometry (`BORDER_TOL`, areas/borders) + whitelist expression evaluator (`ExpressionCompiler`, `JSMascot`) | stdlib |

   `tests/test_mascot_engine.py` and `mascot_engine_widget.py` import from
   `vaultsprite.mascot_engine` only (the facade) — keep it the single public import surface.

1. **Core is pure Python / no Qt** → unit-tested by `core.tick()`. The QObject wrapper owns
   `QTimer` (`mascot.tick_ms: 40`), emits `pose_changed` / `behavior_changed` / `fell_down` /
   telemetry; `App` wires those.
2. **Coordinate space**: everything in logical Qt px (contract shared with terrain_physics); win32
   window rects get the `devicePixelRatio()` divide at the bridge.
3. **Frame decoding** in the UI layer: `QImageReader().read()` single-read per frame, cached; per-pose
   image anchor; mirror horizontally when `not looking_right`.
4. **Sprite maximization**: frames 38–41 (`PullUpShimeji1` Breed anim) and 42–46 (`Divide1` Breed
   anim) are repurposed as **visual-only gags** — a solo-pet ambient flourish that plays the PullUp /
   Divide animation *without spawning a second pet*. Breed itself stays a no-op advance. This keeps
   the solo-pet constraint while rendering every frame in the pack.
5. **Config** (`config.yaml` `mascot:`): `pack` (name key into the `packs` map → pack root dir,
   e.g. `steve: assets/steve_shimeji`, `kazeem: assets/kazeem_shimeji`) — switch pets by changing one line;
   the engine resolves `<pack>/conf/{actions,behaviors}.xml` and auto-discovers frames in
   `<pack>/img/<first subdir>` (mascot_engine_widget.py `_pack_xml_path`). Legacy direct `actions_xml`/
   `behaviors_xml` paths are still honored if present. Plus `tick_ms`, `time_scale`, `excluded_behaviors`
   (unknown names inert). Tray icon follows the active pack's `img/icon.png`. Packs must use the
   standard layout (`conf/*.xml` at pack root) — classic Shimeji-ee packs that nest XML in
   `img/<Name>/conf/` need the two files copied to pack-level `conf/` first (done for kazeem).
6. **Testing**: `tests/test_mascot_engine.py` drives `core.tick()` directly — parse the Steve pack,
   assert a Fall from empty air, assert the recovery ladder resets a wedged action, assert Breed
   stays a no-op, and assert the visual-gag behavior plays frames 38–46 without spawning.

## 6. Source Files (Reference Copies)

Full verbatim copies from `DalekCraft2/Shimeji-Desktop` (Java + corrected default pack XML), kept
locally under `source/` (86 Java files across `action/ animation/ behavior/ config/ environment/
image/ script/ sound/` plus `Manager/Mascot/DebugWindow/TrayMenu/Settings/Main/Localizable` and
`conf/{actions,behaviors}.xml` + `Mascot.xsd`).

> The reference is Java — port only the *patterns* to Python. The *data* (Steve pack) ships as
> `assets/steve_shimeji/` and is what the running engine parses.
