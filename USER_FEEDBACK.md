# User feedback — pack switching: kazeem/dieter/sesame backends (latest pass)

| Note | Resolution |
|---|---|
| Swap between shimejis from config without breaking code; support the JP pack and the Android bundle too | `mascot.pack` + `packs:` map (steve\|kazeem\|dieter\|sesame). Kazeem: XMLs copied to `conf/`. Dieter (ja-JP): parser normalizes JP tags/attrs/values at the XML boundary (`mascot_xml.py`) + English name aliases for forced behaviors. **Sesame/lacis** (Android bundle, NOT XML): separate additive backend — `mascot_sequence_pack.py` (pure-Python FSM over `animation.json`, no Qt) + `mascot_sequence_widget.py` (same signal/method surface as MascotEngine); App selects by pack content (`manifest.json` present), PC/XML path byte-unchanged. |

Sesame compromises (data has less than a PC pack): no window(IE)/mouse-face art; throw = fling pose + gravity integration of flick velocity; feet anchored bottom-center (art is full-canvas, tune via `window.width`); idle self-loop given a synthesized 12-36 s budget so it always returns to ambient (Android exits it by tap — nothing taps here). Ceiling-hang frames flipped vertically only for CEILING animations. Not changed: global `mascot.excluded_behaviors` knob stays single; PC packs keep their exact behavior. **190 tests.**

# Previous passes
- 2026-08-20 — drag-sway facing (`setLookRight(false)` port); legacy-direct-XML configs still honored alongside the pack map.
- 2026-08-19 — ceiling rebalance (unhide `FallFromCeiling`/wall/floor idles so the "On Ceiling" pool re-rolls climb vs fall); M4/M7 review fixes. **144 tests.**
- 2026-08-18 — on-screen clamp, throw smoothing, scale respawn; wall/ceiling grip, drag-release snap-back, pendulum sway settle, breed gag (`e1878ff`→`a414896`). **139 tests.**
- 2026-08-17 — landing loop, pendulum swing-back, hide-walk walk-off-screen + reveal, alignment. **122 tests.**
