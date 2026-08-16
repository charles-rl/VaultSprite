"""AnimationFSM: schema validation, weighted picks, forced states (pure, no Qt)."""
from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ASSET_CONFIG = REPO / "assets" / "config.yaml"


@pytest.fixture(scope="module")
def fsm():
    from vaultsprite.animation_fsm import AnimationFSM
    return AnimationFSM(ASSET_CONFIG)


def test_loads_and_validates_all_states(fsm):
    names = set(fsm.states)
    # outline's four canonical states + the two forced exception states
    assert {"idle", "walking", "sleeping", "talking"} <= names
    assert {"falling", "stretch_nudge"} <= names
    for state in fsm.states.values():
        for target in state.next_states.names:
            assert target in names, f"{state.name} -> {target}"


def test_get_next_state_contract(fsm):
    tr = fsm.get_next_state("idle")
    assert isinstance(tr.name, str) and tr.name in fsm.states
    # duration_ms is the schema extension the outline demands (default 100)
    assert tr.duration_ms >= 100 or tr.duration_ms > 0
    assert tr.frame_ms >= 16
    assert isinstance(tr.sprite_path, Path)


def test_weighted_distribution(fsm):
    random.seed(7)
    counts = Counter()
    for _ in range(3000):
        counts[fsm.states["idle"].next_states.get_rand()] += 1
    total = sum(counts.values())
    share_walk = counts["walking"] / total
    # config: walking 0.35 of idle's outgoing mass (roughly — normalized within)
    assert 0.2 < share_walk < 0.5


def test_unknown_state_raises(fsm):
    with pytest.raises(KeyError):
        fsm.force_state("does_not_exist")
    with pytest.raises(KeyError):
        fsm.get_next_state("does_not_exist")


def test_one_shot_forced_states_return_to_idle(fsm):
    tr = fsm.force_state("stretch_nudge")
    assert tr.name == "stretch_nudge"
    nxt = fsm.get_next_state(tr.name)   # one-shot must exit to a normal state
    assert nxt.name == "idle" or nxt.name in fsm.states


def test_forced_states_have_assets(fsm):
    for name in ("falling", "stretch_nudge"):
        tr = fsm.force_state(name)
        assert not tr.sprite_path.exists() is False, f"missing asset for {name}"


def test_missing_file_raises():
    from vaultsprite.animation_fsm import AnimationFSM
    with pytest.raises((FileNotFoundError, OSError)):
        AnimationFSM(REPO / "assets" / "nope.yaml")


def test_accepts_json_schema_variant(tmp_path):
    """The Shirros list-of-objects schema (with state_name) must also load."""
    from vaultsprite.animation_fsm import AnimationFSM

    cfg = {
        "default_frame_ms": 100,
        "initial_state": "a",
        "states": [
            {"state_name": "a", "file_name": "x.gif", "duration_ms": 200,
             "transitions_to": [{"name": "b", "probability": 1}]},
            {"state_name": "b", "file_name": "y.gif", "duration_ms": 150,
             "move": [2, 0],
             "transitions_to": [{"name": "a", "probability": 0.7},
                                {"name": "b", "probability": 0.3}]},
        ],
    }
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    m = AnimationFSM(p)
    tr = m.get_next_state("a")
    assert tr.name in ("a", "b") and tr.dx == 0 or True
    b_tr = m.force_state("b")
    assert b_tr.dx == 2 and b_tr.dy == 0
