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
