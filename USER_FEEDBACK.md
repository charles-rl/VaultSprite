# User notes — resolved 2026-08-17

Recent items (the swing/throw/vision pass) all fixed. Full history lives in `git log` + `BUILD_NOTES.md`.

| # | Note | Resolution |
|---|---|---|
| 10 | Mascot doesn't sway when swinging Steve | Fixed: inverted conditional-branch skip + missing `FootX` in M9 engine. |
| 11 | Sprite snaps back to spawn / keeps falling after a swing | Fixed: sync anchor to release feet on drag; forward `ActionReference` overlay attrs (throw now has real velocity). |
| 12 | "Ask what I see" freezes the GUI | Fixed: blocking LLM call actually runs off-thread (`QThread.run()`); "Thinking…" bubble + thread-id logging added. |
| 13 | Log debug info to the Vault | Done: drag/throw telemetry → `Memory/Debug/mascot-<day>.md`. |
| 14 | Swap base_url on Linux | No code change: `OLLAMA_BASE_URL` env overrides config. |

Housekeeping: **115 tests pass**, `--smoke` exit 0, `tools/render_check.py` PASS. Commit `1593dcd`; push pending (no credentials in this shell).
