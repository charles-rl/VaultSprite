# User feedback — drag sway facing 2026-08-20 (latest pass)

| Note | Resolution |
|---|---|
| Facing right + click-drag → sway mirrored sideways (lean opposite to cursor) | Ported C++ `Dragged.tick()`'s `setLookRight(false)`: `_DraggableAction.step` now forces left-facing while held — the Pinched lean frames are authored for the left sprite in world space, so a mirror during a right-facing drag played them backwards. Left-facing was already correct (no-op there); release re-syncs facing from throw velocity as before. +1 regression test. |

Window landing: **legacy GIF mode only** — M9 `set_tracked_window` is never fed by any caller; models work-area edges only. Not changed.

Not changed: real-window landing, cursor-proximity reaction (both optional); scale control still multiplies.

# Previous passes
- 2026-08-19 — ceiling rebalance (unhide `FallFromCeiling`/wall/floor idles so the "On Ceiling" pool re-rolls climb vs fall); M4/M7 review fixes. **144 tests.**
- 2026-08-18 — on-screen clamp, throw smoothing, scale respawn; wall/ceiling grip, drag-release snap-back, pendulum sway settle, breed gag (from `e1878ff`→`a414896`). **139 tests.**
- 2026-08-17 — landing loop, pendulum swing-back, hide-walk walk-off-screen + reveal, alignment. **122 tests.**
