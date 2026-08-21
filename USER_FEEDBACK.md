# User feedback — side-margin clipping at screen edges (latest pass, 2026-08-21)

| Note | Resolution |
|---|---|
| With kazeem/dieter/sesame active the old wall-grip clamp clipped ~half the body/legs when walking along a screen edge (their art fills the full 128×128 canvas; Steve's is narrow so it "looked right" there) | Reverted `_clamp_pos` to window-space clamping: whole sprite stays inside the work area, flush at side walls / head top / feet floor — `mascot_engine_widget.py::_clamp_pos` + same in `mascot_sequence_widget.py::_window_pos_for_anchor`; test updated. Engine-side border logic unchanged (anchor still sits ON a wall so climb/grip behaviors fire); no config knob (user decision). **Revert recipe if you want the Steve half-overhang look back:** BUILD_NOTES.md §9 2026-08-21 entry + `git show a414896:vaultsprite/mascot_engine_widget.py`. **190 tests.** |

# Previous passes
- 2026-08-20 — pack switching: kazeem/dieter/sesame backends (`mascot.pack` + `packs:` map; JP XML aliases in `mascot_xml.py`; Android-bundle FSM backend `mascot_sequence_pack/widget`). Sesame compromises: no IE/window art, synthesized idle budget. **190 tests.**
- 2026-08-20 — drag-sway facing (`setLookRight(false)` port); legacy direct-XML configs still honored alongside the pack map.
- 2026-08-19 — ceiling rebalance (unhide `FallFromCeiling`/wall/floor idles so "On Ceiling" re-rolls climb vs fall); M4/M7 review fixes. **144 tests.**
- 2026-08-18 — on-screen clamp, throw smoothing, scale respawn; wall/ceiling grip (the half-overhang look now replaced), drag-release snap-back, sway settle, breed gag (`e1878ff`→`a414896`). **139 tests.**
- 2026-08-17 — landing loop, pendulum swing-back, hide-walk walk-off-screen + reveal, alignment. **122 tests.**
