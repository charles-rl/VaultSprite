# User feedback — resolved 2026-08-17 (latest pass)

Fixed in commit `e1878ff` (full history: `git log` + `BUILD_NOTES.md` §9).

| Note | Resolution |
|---|---|
| Bubble (thinking/response) not aligned to the sprite | Size the bubble before positioning (was dead-center) + anchor to the sprite's opaque bounds. |
| Top-left debug text not aligned to its box | Padding moved onto the telemetry label. |
| Sway correct but no pendulum swing-back | Ported C++ Dragged `FootX`/`FootDX` damped oscillator — now leans while moving and swings/settles. |
| Throw at high speed never comes back | Landing no longer loops (Animate one-cycle + Select ends on finish); pet returns to ambient. |
| Hide should use the walk animation | Synthetic `HideWalk` behavior plays walk frames while App owns the walk; engine freezes only off-screen. |
| Walking animation flipped | Pack art is left-facing; mirror when `looking_right`. |
| Lands → spams shime18 in an infinite loop | `Animate` runs one cycle; `Select` no longer re-inits its finished branch. |
| Wall-climb / ceiling-grab never used | FallAction grips walls so throws reach the wall/ceiling climb pool. |

Housekeeping: **122 tests pass**, `--smoke` exit 0, `tools/render_check.py` PASS.
