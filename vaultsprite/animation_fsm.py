"""Sprite animation finite-state machine (Module 2).

Pure-Python port of Shirros/desktop-pet's JSON-driven probabilistic FSM.
No Qt imports here so the state logic unit-tests headlessly; the overlay
module owns GIF playback and calls :meth:`AnimationFSM.get_next_state`.

Schema extensions over the reference:
- ``duration_ms``  per-state total hold before a transition (default 100 ms,
  matching the reference's single global frame interval).
- ``one_shot``     forced states that always return to ``idle`` afterwards.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union

import yaml


# ---------------------------------------------------------------------------
# weighted selection (verbatim logic from Shirros util.py)
# ---------------------------------------------------------------------------
def _normalize(weights: list[float]) -> list[float]:
    mag = sum(weights)
    if mag <= 0:
        return [1.0 / len(weights)] * len(weights)
    return [w / mag for w in weights]


def _make_cumulative(weights: list[float]) -> list[float]:
    """Running sums written in-place, entry i = sum of all *previous* entries."""
    acc = 0.0
    out = []
    for w in weights:
        out.append(acc)
        acc += w
    return out


class WeightedRandomMap:
    """Weighted random name picker (normalizes; weights need not sum to 1)."""

    def __init__(self, entries: list[dict[str, Any]]):
        self.names = [e["name"] for e in entries]
        weights = [float(e.get("probability", 1.0)) for e in entries]
        self._cdf = _make_cumulative(_normalize(weights))

    def get_rand(self) -> str:
        val = random.random()
        for i, p in enumerate(self._cdf):
            if p > val:
                return self.names[i - 1]
        return self.names[-1]

    __call__ = get_rand


@dataclass(frozen=True)
class StateTransition:
    """Result of one FSM step: which state, its asset, timing and offsets."""

    name: str
    sprite_path: Path
    duration_ms: int          # total hold before the next transition
    frame_ms: int             # per-frame interval for the GIF slicer
    offset_x: int = 0         # anchor offset from the logical cursor
    offset_y: int = 0
    dx: int = 0               # per-frame movement vector (walking states)
    dy: int = 0

    @property
    def one_shot(self) -> bool:
        return self.name in ("stretch_nudge", "falling")


class SpriteState:
    """One declared state from the config matrix."""

    def __init__(self, name: str, obj: dict[str, Any], base_dir: Path):
        self.name = name
        sprite = Path(obj.get("sprite") or obj.get("file_name", ""))
        if not sprite.is_absolute():
            sprite = (base_dir / sprite).resolve()
        self.sprite_path = sprite
        dims = obj.get("dims", [0, 0])
        self.offset_x = int(dims[0])
        self.offset_y = int(dims[1])
        move = obj.get("move") or [0, 0]
        self.dx, self.dy = int(move[0]), int(move[1])
        default_ms = int(obj.get("duration_ms", 100))
        self.duration_ms = max(1, default_ms)
        self.one_shot = bool(obj.get("one_shot", False))
        # forced states transition deterministically (probability-1 to idle),
        # normal states use the weighted table.
        transitions = obj.get("transitions_to") or [{"name": "idle", "probability": 1}]
        self.next_states = WeightedRandomMap(transitions)


class AnimationFSM:
    """Load a state matrix and decide the next animation on demand."""

    def __init__(self, config_path: Union[str, Path]):
        self.config_path = Path(config_path).resolve()
        self.base_dir = self.config_path.parent
        with open(self.config_path, "r", encoding="utf-8") as handle:
            raw = (yaml.safe_load(handle) if self.config_path.suffix in (".yaml", ".yml")
                   else json.load(handle)) or {}

        self.default_frame_ms = int(raw.get("default_frame_ms", 100))
        size = raw.get("size", [96, 96])
        self.size_w, self.size_h = int(size[0]), int(size[1])
        self._states: dict[str, SpriteState] = {}

        states_raw = raw.get("states", [])
        # accept both the Shirros list-of-objects schema and a name-keyed mapping
        if isinstance(states_raw, dict):
            items = [(name, {**obj, "state_name": name}) for name, obj in states_raw.items()]
        else:
            items = [(obj.get("state_name") or obj.get("name"), obj) for obj in states_raw]
        for name, obj in items:
            if not name:
                continue
            self._states[name] = SpriteState(name, {**obj}, self.base_dir)

        if not self._states:
            raise ValueError(f"no states declared in {self.config_path}")
        # fail-fast on typo'd transition targets (reference main.py validation)
        for state in self._states.values():
            for target in state.next_states.names:
                assert target in self._states, (
                    f"state {state.name!r} transitions to unknown {target!r}"
                )

        initial = raw.get("initial_state", next(iter(self._states)))
        if initial not in self._states:
            raise ValueError(f"unknown initial_state {initial!r}")
        self.current_name = initial
        self._cursor_x = 0.0     # logical position cursor, advanced by dx/dy

    # -- introspection ------------------------------------------------------
    @property
    def states(self) -> dict[str, SpriteState]:
        return dict(self._states)

    @property
    def current_state(self) -> str:
        return self.current_name

    def has_state(self, name: str) -> bool:
        return name in self._states

    # -- the outline contract -------------------------------------------------
    def get_next_state(self, current: Union[str, None] = None) -> StateTransition:
        """Pick and return the next state after ``current`` finishes.

        Advances the internal (x, y) cursor by the per-frame move vector of
        the *new* state (matching the reference frame-advance semantics).
        """
        if current is not None:
            self.current_name = self._check(current)
        state = self._states[self._pick_next()]
        return StateTransition(
            name=state.name,
            sprite_path=state.sprite_path,
            duration_ms=state.duration_ms,
            frame_ms=self.default_frame_ms,
            offset_x=state.offset_x,
            offset_y=state.offset_y,
            dx=state.dx,
            dy=state.dy,
        )

    def force_state(self, name: str) -> StateTransition:
        """Jump to a named state (used by M8 stretch nudges / M4 fall)."""
        self.current_name = self._check(name)
        state = self._states[name]
        return StateTransition(
            name=name,
            sprite_path=state.sprite_path,
            duration_ms=state.duration_ms,
            frame_ms=self.default_frame_ms,
            offset_x=state.offset_x,
            offset_y=state.offset_y,
            dx=state.dx,   # forced states still carry their declared move vector
            dy=state.dy,
        )

    # -- internals -----------------------------------------------------------
    def _check(self, name: str) -> str:
        if name not in self._states:
            raise KeyError(f"unknown state {name!r}")
        return name

    def _pick_next(self) -> str:
        state = self._states[self.current_name]
        if state.one_shot:
            target = "idle" if self.current_name != "idle" else None
            if target is not None and target in self._states:
                return target
        chosen = state.next_states.get_rand()
        # never let a one-shot forced state chain into another oddity
        return chosen if chosen in self._states else list(self._states)[0]
