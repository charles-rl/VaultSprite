# User feedback — resolved 2026-08-18 (latest pass)

| Note | Resolution |
|---|---|
| Wall/ceiling margin too big; can't grip sides/ceiling | `_clamp_pos` pins the anchor (feet) to the work-area borders — sprite reaches the ceiling and both walls and hangs upside-down on the ceiling (`mascot_engine_widget.py`). |
| Drag release: frames snap back to grab point before landing | `set_dragging(False)` resets `_pos_cur`, so the throw launches in place from the drop point (no lerp back to the grab). |
| Sway likes speed but oscillates forever | Lock dead-zone in `_DraggableAction.step`: sways while dragging, snaps to the cursor when held still. |
| Scale change: breed/split animation loops forever + slow | Breed gag is now `Animate` (plays once, no loop) instead of `Stay`; gag pose durations shortened. |

Housekeeping: **139 tests pass**, `--smoke` exit 0, `tools/render_check.py` PASS.

# Previous pass (2026-08-18, commit `48e7549`)
On-screen clamp, throw smoothing, scale respawn flourish, throttled mascot telemetry, "Configuring VaultSprite" README section. **137 tests pass.**

# Previous pass (2026-08-17, commit `e1878ff`)
Bubble/telemetry alignment, pendulum swing-back, throw returns + no landing loop, hide uses walk animation, walk mirror fix, one-cycle Animate/Select, wall-grip for climbs. **122 tests pass.**

Not changed this pass: the scale control still multiplies (re-selecting a size replays the now-quick flourish); pendulum gain/damp left at 0.1/0.8.
