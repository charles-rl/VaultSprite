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

# Recent User Feedback
- Flinging the pet can go beyond the monitor and infinitely goes out of the screen, so there is no cap or something. I don't know how they did the limits in the previous repos but you should look into it. I included the `Vault` in the repo maybe the logs will help you there. I think the logs are still missing stuff that you don't know what is happening.
- The pet just climbed up and vanished once it touched the ceiling
- The pet was just standing but changing its scale to be smaller makes it fly out of the screen kind of like a physics glitch. I think this can be fixed by teleporting him at a higher spot and letting him drop down, it can even be in the center. Kind of like I spawned a new version of him. Actually, maybe you could reuse the breeding and splitting animation here if that isn't too hard.
- This is an extremely minor issue but the sway animation when I click and drag and move him around, he sways. It should play faster like it looks like it is running in slow motion
- Another minor visual issue, when I throw him around, it doesn't look smooth like I can visibly see him jitter and move frame by frame across the screen when I throw him around. Maybe you don't know but my monitor is 144Hz in windows.
- I changed the default animation fps to be 30 right now.
- After the quickstart of the `README.md` I think you should give some instructions on what to edit in the `config.yaml` like give more detail to the critical configs that would affect the pet greatly.
