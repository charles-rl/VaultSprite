# User feedback — shimeji pack switching 2026-08-20 (latest pass)

| Note | Resolution |
|---|---|
| Swap steve_shimeji for another Shimeji without breaking code; be able to switch back from config | Config-driven `mascot.pack` + `mascot.packs` map (steve\|kazeem); engine resolves `<pack>/conf/*.xml`, `_resolve_img_dir` auto-finds frames; tray icon now follows active pack (main.py:490, fallback to steve). Kazeem added with its XMLs copied to pack-level `conf/` (classic layout nests them in `img/Kazeem/conf/`). Legacy `mascot.actions_xml` paths still honored. |

Not changed: kazeem's breed-style behaviors (`Divided`, `PullUp`) are no-op advances via the solo-pet rule; per-pack excluded list is global `mascot.excluded_behaviors` (unknown names inert) — left as a single knob for the user.

# Previous passes
- 2026-08-20 — drag-sway facing fixed: `_DraggableAction.step` forces left-facing while held (`setLookRight(false)` port); release re-syncs from throw velocity.
- 2026-08-19 — ceiling rebalance (unhide `FallFromCeiling`/wall/floor idles so the "On Ceiling" pool re-rolls climb vs fall); M4/M7 review fixes. **144 tests.**
- 2026-08-18 — on-screen clamp, throw smoothing, scale respawn; wall/ceiling grip, drag-release snap-back, pendulum sway settle, breed gag (from `e1878ff`→`a414896`). **139 tests.**
- 2026-08-17 — landing loop, pendulum swing-back, hide-walk walk-off-screen + reveal, alignment. **122 tests.**
