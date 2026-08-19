# User feedback — ceiling-stuck fix 2026-08-19 (latest pass)

| Note | Resolution |
|---|---|
| Pet always climbs around the ceiling; never comes down | Solo-pet static geometry makes Shimeji's "On Ceiling" pool a one-way ratchet (`ClimbAlongCeiling` was the only non-hidden candidate → deterministic single-candidate loop). Unhid `FallFromCeiling`, `HoldOntoWall`, `FallFromWall`, `StandUp` in `steve_shimeji/conf/behaviors.xml`: ceiling now re-rolls climb/fall (verified ~58% climb / 42% fall), floor idles instead of always running to a wall. |

Window landing: **works only in legacy GIF mode** (`mascot.enabled: false`) — the M9 engine's `set_tracked_window` is never fed by any caller, so it models work-area edges only (documented in BUILD_NOTES). Not changed.

Housekeeping: **144 tests pass**, `--smoke` exit 0; +3 M9 regression tests (descent paths visible, ceiling pick distribution, forced-fall reaches floor).

# Previous passes
- 2026-08-19 — non-M9 review: M7 event tz/uniqueness, M4 standee x-overlap / stale `_vx` fixes. **141 tests.**
- 2026-08-18 — on-screen clamp, throw smoothing, scale respawn, richer logs (137). M9 wall/ceiling grip, drag-release snap-back, sway settle, breed gag (from `e1878ff`→`a414896`, 122→139).
- 2026-08-17 — landing loop, pendulum swing-back, hide-walk, alignment, wall-grip climbs. **122 tests.**

Not changed: M9 real-window landing and cursor-proximity reaction remain off (optional features); scale control still multiplies.
