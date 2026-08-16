"""ObsidianVault: atomic writes, frontmatter round-trip, journal appends."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from tests.conftest import FakeConfig
from vaultsprite.obsidian_vault import ObsidianVault


@pytest.fixture()
def vault(tmp_path):
    cfg = FakeConfig({"obsidian.vault_root": str(tmp_path / "TestVault")})
    return ObsidianVault(cfg)


def test_write_fact_creates_frontmatter_and_body(vault, tmp_path):
    path = vault.write_fact("owner", "favorite_language", "Python")
    assert (tmp_path / "TestVault" / "Memory/Facts/owner/favorite-language.md").exists()
    fm, body = ObsidianVault._read_markdown(path)
    assert fm["type"] == "fact"
    assert fm["entity"] == "owner"
    assert fm["predicate"] == "favorite_language"
    assert fm["value"] == "Python"
    assert "recorded_at" in fm
    assert "Python" in body


def test_write_fact_is_overwrite_in_place(vault):
    p1 = vault.write_fact("owner", "mood", "happy")
    p2 = vault.write_fact("owner", "mood", "grumpy")   # same file, new value
    assert p1 == p2
    fm, _ = ObsidianVault._read_markdown(p1)
    assert fm["value"] == "grumpy"


def test_atomic_write_leaves_no_tmp_files(vault):
    vault.write_fact("stats", "last_bored_critical", 82)
    leftovers = list((vault.facts_dir / "stats").glob(".*.tmp"))
    assert leftovers == []


def test_frontmatter_round_trip(vault, tmp_path):
    target = tmp_path / "x.md"
    fm = {"type": "journal", "date": "2026-01-01"}
    ObsidianVault._atomic_write(target, fm, "# 2026-01-01\n\nline one")
    data, body = ObsidianVault._read_markdown(target)
    assert data == fm
    assert "line one" in body


def test_read_missing_returns_empty(vault):
    fm, body = vault.read_fact("ghost", "nothing")
    assert fm == {} and body == ""


def test_record_event_under_date_bucket(vault):
    path = vault.record_event("context switched to WORK", source="test")
    assert re.match(r"^event-\d{4}-\d{2}-\d{2}", path.stem)
    day = path.parent.name
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
    fm, _ = ObsidianVault._read_markdown(path)
    assert fm["type"] == "event" and fm["source"] == "test"


def test_append_journal_creates_and_appends(vault):
    first = vault.append_journal("started working on the pet")
    second = vault.append_journal("took a break")
    assert first == second, "same-day entries share one file"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", first.stem)
    text = first.read_text(encoding="utf-8")
    fm, body = ObsidianVault._read_markdown(first)
    assert fm["type"] == "journal"
    # both entries present, chronological order preserved
    i1, i2 = body.find("started working"), body.find("took a break")
    assert 0 <= i1 < i2


def test_journal_entries_are_templated_lines(vault):
    vault.append_journal("hello")
    day_file = next(iter(vault.journal_dir.glob("*.md")))
    content = day_file.read_text(encoding="utf-8")
    assert re.search(r"^- \[\d{2}:\d{2}\] hello$", content, re.MULTILINE)


def test_vault_root_env_override(tmp_path, monkeypatch):
    from vaultsprite.config import load_config
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path / "EnvVault"))
    v = ObsidianVault(load_config(reload=True))
    assert v.root == tmp_path / "EnvVault"
