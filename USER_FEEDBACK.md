# User feedback — resolved 2026-08-18 (latest pass)

Commit `48e7549` (history: `git log` + `BUILD_NOTES.md` §9).

| Note | Resolution |
|---|---|
| Fling/run goes infinitely off-screen (no cap) | On-screen clamp keeps the whole sprite inside the work area (`mascot_engine_widget._clamp_pos`). |
| Climbed up and vanished at the ceiling | Same clamp stops the sprite rendering above the top edge. |
| Changing scale makes it fly out / float | Scale change now replays the breed-gag "spawn a new version" flourish + drop (`MascotEngine.respawn`). |
| Throw looks frame-by-frame / jittery (144Hz) | New `mascot.smooth_motion` interpolation timer eases the window between 25 Hz engine ticks. |
| Logs missing stuff | Throttled anchor/behavior/frame/work-area telemetry → `Memory/Debug/mascot-*.md`. |
| Sway plays too slow | **Not changed** — tune manually: `mascot_actions.py:722` (gain `0.1`, damping `0.8`). |
| README should explain config.yaml | Added "Configuring VaultSprite" section after Quick start. |

Housekeeping: **137 tests pass**, `--smoke` exit 0, `tools/render_check.py` PASS.

# Previous pass (2026-08-17, commit `e1878ff`)
Bubble/telemetry alignment, pendulum swing-back, throw returns + no landing loop, hide uses walk animation, walk mirror fix, one-cycle Animate/Select, wall-grip for climbs. **122 tests pass.**

# Recent User Feedback
- Now the wall and ceiling has a bit of a margin and I think it can move much higher and much more to the sides. It looks like it isn't gripping the sides very well nor climbing the ceiling well. 
- The animations are great now and smooth but when I click and drag him away and then let go. At the exact moment I let go, I see a few frames of him traveling through frames and snapping back to where I picked him up before snapping back to where I let him go and he falls normally and follows the correct animation.
- I have tried different combinations of mascot_actions.py:722 like 0.2/0.8 for catch up speed and damping. I like the speed but it just doesn't settle and instead oscillates forever.
- When I change the scale to a smaller size, the breeding/split animation runs but it runs multiple times and I think it runs in a loop forever. It also runs the animation very slowly.
