# User feedback — resolved 2026-08-17

Recent items (the swing/throw/vision pass) all fixed. Full history lives in `git log` + `BUILD_NOTES.md`.

| # | Note | Resolution |
|---|---|---|
| 10 | Mascot doesn't sway when swinging Steve | Fixed: inverted conditional-branch skip + missing `FootX` in M9 engine. |
| 11 | Sprite snaps back to spawn / keeps falling after a swing | Fixed: sync anchor to release feet on drag; forward `ActionReference` overlay attrs (throw now has real velocity). |
| 12 | "Ask what I see" freezes the GUI | Fixed: blocking LLM call actually runs off-thread (`QThread.run()`); "Thinking…" bubble + thread-id logging added. |
| 13 | Log debug info to the Vault | Done: drag/throw telemetry → `Memory/Debug/mascot-<day>.md`. |
| 14 | Swap base_url on Linux | No code change: `OLLAMA_BASE_URL` env overrides config. |

Housekeeping: **115 tests pass**, `--smoke` exit 0, `tools/render_check.py` PASS. Commit `1593dcd`; push pending (no credentials in this shell).

# Recent User Feedback
- The chat bubble when thinking or even responding is not aligned to the actual sprite
- The debug on the top left is not aligned to the actual text like the box it is in.
- The sway is correct but there is no gravity like the original implementation, you may clone the repo in the project root to check then delete. Basically, it sways but after it sways, it doesn't swing back and forth like a pendulum.
- I can throw it at super high speeds and it doesn't come back again. Check the debug logs.
- Hiding the pet is correct but it should use its walk animation
- The walking animation is opposite or flipped.
- The main bug is that after it lands when I drop it, it spams the shime18 animation. Like the water bucket landing animation and is stuck at an infinite loop.
- I notice that there is climbing walls animations and grabbing ceilings but it never utilizes that but maybe it is because it is usually stuck indefinitely in the falling loop.
