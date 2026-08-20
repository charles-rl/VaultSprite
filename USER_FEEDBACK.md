# User feedback — shimeji pack switching + localized packs (latest pass)

| Note | Resolution |
|---|---|
| Swap steve for other Shimeji packs without breaking code; switch back from config | Config-driven `mascot.pack` + `packs:` map (steve\|kazeem\|dieter); engine resolves `<pack>/conf/*.xml`, auto-discovers frames (`img/<Name>` or flat `img/`). Kazeem: XMLs copied to pack-level `conf/`. **Dieter** (ja-JP): parser normalizes JP tags/attrs/enum values at the XML boundary (`mascot_xml.py`) + canonical English name aliases so forced behaviors resolve. Legacy direct-XML configs still honored. |

Not changed: per-pack excluded list stays a single global `mascot.excluded_behaviors` (unknown names inert); kazeem/dieter breed-style behaviors are no-op advances via the solo-pet rule; steve stays default when unsure — active pack is whatever config says (currently user-set). Respawn test pinned to steve (its PullUpShimeji gag is Steve-specific; other packs correctly degrade to Fall). **181 tests.**

# Previous passes
- 2026-08-20 — drag-sway facing: `_DraggableAction.step` forces left-facing while held (`setLookRight(false)` port); release re-syncs from throw velocity.
- 2026-08-19 — ceiling rebalance (unhide `FallFromCeiling`/wall/floor idles so the "On Ceiling" pool re-rolls climb vs fall); M4/M7 review fixes. **144 tests.**
- 2026-08-18 — on-screen clamp, throw smoothing, scale respawn; wall/ceiling grip, drag-release snap-back, pendulum sway settle, breed gag (`e1878ff`→`a414896`). **139 tests.**
- 2026-08-17 — landing loop, pendulum swing-back, hide-walk walk-off-screen + reveal, alignment. **122 tests.**
