# M9 Code Review — Verified Findings & Fix Plan (2026-08-18)

Independent audit of the M9 mascot engine against the `DalekCraft2/Shimeji-Desktop`
Java reference. Findings were produced by three `@explore` subagents (independent
perspectives) and then **re-verified by live probes** (`uv run python -c` driving
`MascotCore.tick()` against the bundled Steve pack). This document is the durable
record — read it first; it drives the fix order A → B → C → D.

Reference root: `docs/09_mascot_engine/source/src/main/java/com/group_finity/mascot/`

---

## Verified bug table (severity order)

| # | Area | Finding | File:line | Verified by | Sev |
|---|------|---------|-----------|-------------|-----|
| 1 | Launch | **Throw velocity clobbered** every tick: widget `_tick` overwrites `env.cursor.dx/dy` with the live QCursor delta (line 254) *before* `core.tick()` (255) consumes the queued `Thrown`, so `InitialVX="${cursor.dx}"` resolves to ~0 → every flick degrades to a near-zero drop. The tick timer keeps running during a drag (only `position_changed` is suppressed). | `mascot_engine_widget.py:244-262`; `main.py:243-256`; `mascot_engine.py:329-334` | read (order) | **HIGH** |
| 2 | Landing | **Select `${...}` latch**: `SelectAction._cond_vars[i]` (built once per shared action instance) holds `ActionVars` whose `${...}` Condition resolves through `_DynOnce` → cached **forever**. The Fall/Thrown Select branches use `${floor.isOn \|\| activeIE.topBorder.isOn}` on a parse-once-shared instance → the landing branch is locked after the *first* fall. Probe: wall-fall then center-fall → center wrongly takes GrabWall (no Bouncing splash). Java re-evaluates `${}` once per **action init** (`Script.init`). | `mascot_actions.py:533-541` + `mascot_vars.py:26-40`; pack `actions.xml:305-311,324-329` | probe | **HIGH** |
| 3 | Climb | **Bare action-attribute identifiers are dead in conditions**: `#{TargetY < mascot.anchor.y}` (ClimbWall, `actions.xml:147,158`) is evaluated with an empty scope → `TargetY`→`_UNDEFINED`→comparison False. Both branches False → `_current_anim()`→None → `MoveAction.step` bails → wall climbs (`ClimbAlongWall`, `ClimbIEWall`, …) never move/animate. Java evaluates conditions against the action's `VariableMap` (which holds `TargetY`). | `mascot_environment.py:709-713` (scope `{}`); `mascot_actions.py:146-158,283-284` | probe | **HIGH** |
| 4 | Engine | **No-match Select blocks its parent Sequence forever**: `SelectAction.step` returns `True` when `_select()==-1` (no branch matches) → parent Sequence never advances. ChaseMouse's bare Select needs `activeIE.topBorder` (invisible → no match) → behavior frozen, **zero frames rendered**. Java `ComplexAction.hasNext()` is false once the current child has no next → Select completes. | `mascot_actions.py:583-584`; pack `actions.xml:631-638` | probe | **HIGH** |
| 5 | Robustness | **Unknown forced behavior → unguarded `KeyError` + `queued_behavior` stuck forever**: `_pick_behavior.find()` raises `KeyError`; the recovery ladder only catches `MalformedAction`; the queue is cleared only after a successful pick, so it stays set → KeyError every tick. Widget's broad `except` keeps the process alive but the pet freezes. | `mascot_engine.py:359-368,499-501,530-542`; `mascot_engine_widget.py:263-264` | probe | MED |
| 6 | Safety | **`_js_truthy` diverges from `js_truthy` → fail-OPEN**: env `_UNDEFINED` (a *different* sentinel than the vars `_UNDEFINED_TYPE2` that `_cond_true` checks) has no `__len__` → `_js_truthy` hits the TypeError branch → **True**; NaN also → True. So a behavior Condition that is a bare unknown identifier (`#{mascot.nonexistent}`) runs instead of being skipped — opposite of the documented fail-closed contract (`mascot_environment.py:14-15`). `js_truthy` handles both correctly. | `mascot_engine.py:553-565` vs `mascot_environment.py:322-337`, used at `419-432` | probe | MED |
| 7 | Feature | **Tracked window never fed**: `set_tracked_window` (`mascot_engine_widget.py:216`) has **zero callers**. `env.active_ie` stays the invisible sentinel forever → IE_STICK landing, window-top/IE pools, `ThrowIE*`, and `activeIE.*` condition groups are all inert; the documented `÷devicePixelRatio()` bridge is never applied. | `mascot_engine_widget.py:216-218`; `mascot_engine.py:335-347` | grep | MED |
| 8 | Hide/show | **Reveal restores anchor from the window top-left, not the feet**: `_hide_walk_done` does `sync_anchor(rx, ry)` where `(rx,ry)=_hide_restore` (window pos) but `sync_anchor` expects **feet**. Walk-step correctly converts (`+w//2, +h`, `main.py:475`); reveal doesn't → visible vertical/horizontal snap on every show (self-corrects over a few ticks). | `main.py:491-492` vs `475` | read | MED |
| 9 | Engine | **`_init_count` "20 consecutive failures" guard is dead code**: the recovery ladder resets `_init_count = 0` on every attempt (`mascot_engine.py:537`), so consecutive failures never accumulate → a permanently-broken behavior is re-picked every 40 ms forever (log spam + `reset_position` teleports every ~160 ms), with no stop (Java `reached_init_limit` stops trying). | `mascot_engine.py:483-489` vs `537` | read | MED |
| 10 | Motion | **AnimateAction ignores pose velocity**: `Tripping` poses carry `-8/-4/-2/-4` px (pack `actions.xml:226-234`) but `AnimateAction.step` never moves the anchor → the pet stumbles in place. Java `animation.apply` integrates pose velocity. | `mascot_actions.py:230-246` | read+pack | LOW-MED |

---

## Verified but lower priority / latent

- **`_tick_once` clears `queued_behavior` after the next-pool pick** (`mascot_engine.py:497-511`): a queued Dragged/Fall set by `_dragging_ok`/`_border_type_ok` during a **direct** (non-Sequence) action's `subtick()` would be discarded. **Not triggerable on this pack** — every Stay/Move behavior is Sequence/Reference-wrapped, and Sequence preserves the queue. Latent fragility for community packs with a direct Move/Stay. (Agents A3 Q1 + reviewer agree.)
- **Fall lacks the reference substep sweep** + `velocityY<0`-on-floor nuance (`Fall.java:87-97,123-149`): acknowledged simplification; the IE crossing test + work-area clamp mitigate most tunneling; `y+j∈[-80,0]` window probe has no analog. Fidelity gap, not a crash. (A3 R6/R7, LOW.)
- **Pet doesn't ride moving tracked windows** (`BorderedAction.java` translates anchor by the window's delta): dormant until finding 7 is fixed. (A3 R8.)
- **Stale `_anim_t0` across re-runs of shared action instances**: pose loop starts mid-cycle on re-init; purely visual (durations use `real_elapsed`). (A3 R10 / A2 F7.)
- **`get_bool` treats NaN as true** (`mascot_vars.py:92-107`). (A2 F5.)
- **`_ENV_RNG_HOLDER` is dead code** — never assigned anywhere; all real call sites pass `core.rng` explicitly. Harmless. (A2 F6.)
- **`Area.is_on` not gated on `visible`** (unlike Java `FloorCeiling.isOn`); `MascotEnvironment.random(0)` → ValueError. Latent edges. (A2 F8/F9.)
- **Move x-mirror for vertical-only moves**: **self-retracted by A3 (R2)** — v.x=0 makes the flip a no-op; `abs()`-normalization of dy is correct. NOT a bug.
- **Additive pool duplication / dropped ref frequency** (`_PoolSourcesOf`, `mascot_data.py:113-123`): ambient-weight skew when a `<NextBehavior Add="true">` behavior (SitDown) is picked. (A4 #3.)
- **Throw-anchor uses the `_px` scalar vs `window.size_px()`** (`main.py:253` vs `474`): diverges at scale ≠ 1 / non-square. (A4 #2.)
- **Pixmap cache doesn't cache misses**; **hide during an in-flight drag strands `dragging=True`**; **MoveAction dead safety-cap** (`_move_t0_e`, `pass`). (A4 #7/#8/#9.)

---

## Fix plan

### A — Launch/landing correctness (HIGH)
1. **#1 throw-velocity clobber**: in `MascotEngine`, don't let the per-tick `cursor_pos` refresh overwrite an injected throw until it is consumed. Add a consume-once flag (`_pending_throw`) or pass the throw velocity via the engine so `_tick` skips the cursor-delta overwrite for the tick that launches `Thrown`.
2. **#2 Select `${}` latch**: rebuild `_cond_vars` (reset per-run) in `SelectAction.init` / when re-picking a branch, so `${...}` conditions re-resolve once per action run (matching Java). Keep `#{}` per-tick.
3. **#4 no-match Select**: `SelectAction.step` must **end** the Select (return False) when no branch matches, so the parent Sequence advances (Java `ComplexAction.hasNext` semantics) instead of returning `True` forever. Preserve the "still animating a chosen branch" return-True path.
4. **#8 reveal-anchor**: store the pre-hide **feet anchor** (`_hide_anchor`) and restore it in `_hide_walk_done` (or convert `_hide_restore` with `+w//2, +h` like the walk step).

### B — Expression scope + safety
5. **#3 inject action vars into condition scope**: evaluate animation/behavior conditions with the action's `ActionVars` available as bare identifiers (so `#{TargetY < mascot.anchor.y}` works). Keep the whitelist — only add the action's own vars to the resolution scope, never `eval()`.
6. **#6 align `_js_truthy` with `js_truthy`**: handle the env `_UNDEFINED` sentinel (not just the vars `_UNDEFINED_TYPE2`) and NaN → falsy, so `_cond_true` fails closed.

### C — Robustness
7. **#5 unknown forced name**: `_pick_behavior` on a queued-name miss should log + fall through to ambient/`_fallback_fall()` (and clear the queue), never raise KeyError.
8. **#9 `_init_count`**: move the reset out of the per-attempt ladder position and into the `attempt == 3` `reset_position` branch (+ keep reset-on-success), so consecutive failures genuinely accumulate and the guard can fire — while a position reset still clears it.
9. **#10 Animate pose velocity**: apply the effective pose velocity to the anchor in `AnimateAction.step` (mirroring like `AnimationAction.step`), so `Tripping` tumbles.

### D — Feature (do last)
10. **#7 tracked-window wiring**: give `MascotEngine` a way to receive the foreground window rect (logical px) each tick and call `core.update_environment(tracked_window=...)`; bridge from App/M5 context, dividing Win32 physical px by `devicePixelRatio()`. Keep the `None` → invisible-sentinel path for "no window".

---

## Test plan (add; nothing pruned — audit-only)

- Expression-evaluator safety suite (no `eval`, unknown→False, NaN, ternary, `Math.random`, malformed fail-closed).
- Regression: Select `${}` latch (first fall must not lock landing branch).
- Regression: ClimbWall with a `TargetY` overlay actually climbs (animation selected + vertical motion).
- Regression: no-match Select advances (ChaseMouse with IE invisible does not freeze / renders a frame).
- Regression: unknown forced behavior falls back (no KeyError, queue cleared, pet recovers).
- Regression: `_init_count` accumulates on consecutive init failures.
- Regression: `AnimateAction` (Tripping) moves the anchor by pose velocity.
- Throw-velocity path through `MascotEngine` (Qt) — injected throw survives the tick refresh.
