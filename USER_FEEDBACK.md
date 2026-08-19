# User feedback — code-review pass 2026-08-19 (latest)

Review of non-M9 modules vs reference repos (koishi gravity.py, obsidian-memory-for-ai SPEC-v4). Fixed:

| Area | Fix |
|---|---|
| M7 events tz | `_now_iso()` now honors `obsidian.date_timezone`; `occurred_at` day always matches its bucket dir (was hardcoded UTC). |
| M7 event uniqueness | append-only event filenames get a per-instance seq suffix — same-slug/same-second events no longer clobber. |
| M4 standee grounding | grounded check now requires feet-x-overlap with the standing window; a pet walked off its window re-arms a real fall (was x-blind hover). |
| M4 stale `_vx` | landing zeroes `_vx` (matches koishi `gravity.py`), so a later fall can't side-launch with residual flick velocity. |

4 regression tests added. **141 tests pass**, `--smoke` exit 0, `tools/render_check.py` PASS.

# Previous pass (2026-08-18)
Wall-grip, drag-release throw, sway dead-zone, breed gag → Animate. **139 tests pass.**

# Previous pass (2026-08-18, commit `48e7549`)
On-screen clamp, throw smoothing, scale respawn, throttled mascot telemetry, README section. **137 tests pass.**

# Previous pass (2026-08-17, commit `e1878ff`)
Bubble/telemetry alignment, pendulum swing-back, throw returns + no landing loop, hide walk animation, walk mirror fix, one-cycle Animate/Select, wall-grip climbs. **122 tests pass.**

Not changed: scale control still multiplies (re-select replays the flourish); M4 occlusion/window-filter enumeration and M1 legacy drop-freeze documented but deferred.
