"""ObsidianVault: atomic writes, frontmatter round-trip, journal appends.

P1 additions (2026-08-17): write-sandboxing guardrail tests (PermissionError on
any escape from the vault root), storage-size watcher tests (edge-triggered
``vault_size_warning`` at ``obsidian.max_size_mb``), and Test Cases A/B/C —
daily-journal append, structured atomic fact updates, and concurrent-write
safety under a thread pool.
"""
from __future__ import annotations

import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml

from tests.conftest import FakeConfig
from vaultsprite.obsidian_vault import ObsidianVault


@pytest.fixture()
def vault(qapp, tmp_path):
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


def test_record_event_local_tz_matches_bucket_day(qapp, tmp_path):
    """Regression: under date_timezone=local, occurred_at must share the local
    day of the bucket dir — never a hardcoded-UTC stamp that contradicts it."""
    from unittest.mock import patch
    from datetime import datetime, timezone, timedelta

    cfg = FakeConfig({"obsidian.vault_root": str(tmp_path / "TzVault"),
                      "obsidian.date_timezone": "local"})
    v = ObsidianVault(cfg)
    assert v._tz is None, "local tz must yield a naive local clock"

    # Near-midnight local time when UTC is already the next day: local date 2026-01-01,
    # UTC date 2026-01-02. The event must land in the local-day bucket AND stamp it.
    local_now = datetime(2026, 1, 1, 23, 59, 59)
    utc_now = local_now + timedelta(hours=8)   # UTC +8h → 2026-01-02
    with patch("vaultsprite.obsidian_vault.datetime") as dtm:
        dtm.now.side_effect = lambda tz: utc_now if tz is timezone.utc else local_now
        dtm.now.return_value = local_now
        dtm.now.isoformat = datetime.isoformat
        path = v.record_event("tz boundary event")

    day = path.parent.name
    assert day == "2026-01-01", f"bucket must use local day, got {day}"
    fm, _ = ObsidianVault._read_markdown(path)
    assert fm["occurred_at"].startswith("2026-01-01"), \
        f"occurred_at must share the local bucket day, got {fm['occurred_at']}"


def test_same_second_events_do_not_clobber(qapp, tmp_path):
    """Regression: two events with the same summary within one second must produce
    distinct files — the second must not silently overwrite the first (append-only)."""
    from unittest.mock import patch
    from datetime import datetime, timezone

    cfg = FakeConfig({"obsidian.vault_root": str(tmp_path / "UniqueVault")})
    v = ObsidianVault(cfg)
    fixed = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    with patch("vaultsprite.obsidian_vault.datetime") as dtm:
        dtm.now.return_value = fixed
        dtm.now.isoformat = datetime.isoformat
        dtm.now.strftime = staticmethod(lambda fmt: fixed.strftime(fmt))
        dtm.utcnow.return_value = fixed
        p1 = v.record_event("identical summary")
        p2 = v.record_event("identical summary")

    assert p1 != p2, "same-second same-slug events must not share a path"
    assert p1.exists() and p2.exists(), "both events must be written (no overwrite)"
    fm1, _ = ObsidianVault._read_markdown(p1)
    fm2, _ = ObsidianVault._read_markdown(p2)
    assert fm1["summary"] == fm2["summary"] == "identical summary"


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


def test_vault_root_env_override(qapp, tmp_path, monkeypatch):
    from vaultsprite.config import load_config
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path / "EnvVault"))
    v = ObsidianVault(load_config(reload=True))
    assert v.root == tmp_path / "EnvVault"


# -- P1: write-sandboxing guardrail --------------------------------------------------

def test_resolve_safe_rejects_escape_to_parent(vault):
    with pytest.raises(PermissionError, match="WRITE DENIED"):
        vault._resolve_safe(vault.root.parent / "test_leak.txt")


def test_resolve_safe_rejects_absolute_outside_path(vault):
    outside = vault.root.parent / "elsewhere" / "pwned.md"
    with pytest.raises(PermissionError, match=r"allowed vault directory"):
        vault._resolve_safe(outside)


def test_resolve_safe_rejects_sibling_prefix_dir(vault):
    """A sibling dir sharing the root's name prefix must not pass a naive startswith."""
    sibling = vault.root.parent / (vault.root.name + "-evil") / "pwn.md"
    with pytest.raises(PermissionError, match="WRITE DENIED"):
        vault._resolve_safe(sibling)


def test_resolve_safe_follows_symlinks_out_of_root(vault):
    outside = vault.root.parent / "outside_target.txt"
    link = vault.journal_dir / "escape.md"
    vault.journal_dir.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)
    with pytest.raises(PermissionError, match="WRITE DENIED"):
        vault._resolve_safe(link)


def test_write_fact_escape_blocked_end_to_end(vault):
    """Pointing facts_dir outside the root must raise before anything is written."""
    vault.facts_dir = vault.root.parent / "FactsEscape"
    with pytest.raises(PermissionError, match="WRITE DENIED"):
        vault.write_fact("evil", "key", 1)
    assert not (vault.root.parent / "FactsEscape").exists(), "no dir may be created outside the root"


def test_append_journal_escape_blocked_end_to_end(vault):
    vault.journal_dir = vault.root.parent / "JournalEscape"
    with pytest.raises(PermissionError, match="WRITE DENIED"):
        vault.append_journal("break out of the sandbox!")
    assert not (vault.root.parent / "JournalEscape").exists()


def test_reads_still_work_after_guard(vault):
    """The guard is write-only: reads of existing in-vault notes are unaffected."""
    p = vault.write_fact("owner", "name", "Sprite")
    fm, _ = vault.read_fact("owner", "name")
    assert fm["value"] == "Sprite"


# -- P1: storage size watcher ---------------------------------------------------------

@pytest.fixture()
def small_vault(qapp, tmp_path):
    cfg = FakeConfig({
        "obsidian.vault_root": str(tmp_path / "SmallVault"),
        "obsidian.max_size_mb": 0.05,       # ~52 KB cap so filler trips it fast
    })
    return ObsidianVault(cfg)


def test_check_storage_warns_once_per_exceed_episode(small_vault):
    warnings: list = []
    small_vault.vault_size_warning.connect(warnings.append)

    size = small_vault.check_storage()            # empty folder, under limit
    assert size < small_vault.size_limit_bytes and warnings == []

    for i in range(15):                           # ~60 KB of journal > 52.4 KB cap
        small_vault.append_journal(f"filler-{i} " + "x" * 4000)
    assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}"

    first_count = len(warnings)
    small_vault.check_storage()                   # still over → latched, no re-fire
    small_vault.append_journal("one more line")   # post-write audit also must not re-fire
    assert len(warnings) == first_count, "warning re-fired while usage stayed over the limit"


def test_check_storage_rearms_after_shrinking(small_vault):
    warnings: list = []
    small_vault.vault_size_warning.connect(warnings.append)
    for i in range(15):
        small_vault.append_journal(f"bulk-{i} " + "x" * 4000)
    assert len(warnings) == 1

    import shutil
    shutil.rmtree(str(small_vault.journal_dir))   # usage drops under the limit again
    assert small_vault.check_storage() < small_vault.size_limit_bytes
    for i in range(15):                           # cross back over → warn a second time
        small_vault.append_journal(f"refill-{i} " + "x" * 4000)
    assert len(warnings) == 2, "warning should re-arm after usage returned under the limit"


def test_check_storage_disabled_when_no_limit(qapp, tmp_path):
    cfg = FakeConfig({"obsidian.vault_root": str(tmp_path / "NoLimit"),
                      "obsidian.max_size_mb": 0})
    v = ObsidianVault(cfg)
    assert v.size_limit_bytes is None
    warnings: list = []
    v.vault_size_warning.connect(warnings.append)
    for i in range(5):
        v.append_journal("y" * 4000)
    assert v.check_storage() > 0 and warnings == [], "monitoring must be fully off at limit 0"


def test_check_storage_never_raises_on_missing_root(qapp, tmp_path):
    cfg = FakeConfig({"obsidian.vault_root": str(tmp_path / "Gone")})
    v = ObsidianVault(cfg)
    assert v.storage_size_bytes() == 0            # walk tolerates a missing directory
    assert v.check_storage() == 0


# -- P1 Test Case A: daily journal append ---------------------------------------------

def test_journal_initializes_with_contract_frontmatter(vault):
    path = vault.append_journal("first entry of the day")
    text = path.read_text(encoding="utf-8")
    data, body = ObsidianVault._read_markdown(path)   # frontmatter must parse as YAML
    assert yaml.safe_load(text.split("---")[1]) == data  # raw block round-trips too
    assert data["type"] == "journal"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", data["date"])
    assert data["tags"] == ["desktop-pet", "journal"]   # spec-required tags on creation
    assert body.lstrip().startswith(f"# {data['date']}")
    assert "first entry of the day" in body


def test_journal_appends_cleanly_without_overwriting(vault):
    path = vault.append_journal("entry one")
    snapshot_1 = path.read_text(encoding="utf-8")

    vault.append_journal("entry two")
    vault.append_journal("entry three")
    text = path.read_text(encoding="utf-8")

    assert len(text) > len(snapshot_1), "appends must grow the file, never truncate it"
    for entry in ("entry one", "entry two", "entry three"):
        assert re.search(rf"^- \[\d{{2}}:\d{{2}}\] {re.escape(entry)}$", text, re.MULTILINE)
    i1, i2, i3 = (text.index(e) for e in ("entry one", "entry two", "entry three"))
    assert i1 < i2 < i3, "chronological order must be preserved"

    data, _ = ObsidianVault._read_markdown(path)
    assert data["tags"] == ["desktop-pet", "journal"], "frontmatter must survive RMW appends"


def test_journal_backfills_tags_on_legacy_today_file(vault):
    """Today's file written before the tags contract gets them backfilled on append."""
    day = vault._today()
    path = vault.journal_dir / f"{day}.md"
    ObsidianVault._atomic_write(
        path, {"type": "journal", "date": day}, f"# {day}\n\n- [09:00] legacy entry")

    out = vault.append_journal("entry after the upgrade")
    assert out == path
    data, body = ObsidianVault._read_markdown(path)
    assert data["tags"] == ["desktop-pet", "journal"], "RMW must backfill missing tags"
    assert "- [09:00] legacy entry" in body        # older content survives the backfill
    assert "entry after the upgrade" in body


# -- P1 Test Case B: structured atomic fact creation ----------------------------------

def test_fact_update_updates_pairs_preserves_creation(vault, monkeypatch):
    stamps = iter(["2026-01-01T00:00:00+00:00", "2026-08-17T12:34:56+00:00"])
    vault._now_iso = lambda: next(stamps)       # distinct stamps prove true preservation

    p1 = vault.write_fact("owner", "favorite_language", "Python", confidence=0.9)
    fm1, body1 = ObsidianVault._read_markdown(p1)
    assert (fm1["value"], fm1["confidence"]) == ("Python", 0.9)
    assert fm1["recorded_at"] == "2026-01-01T00:00:00+00:00"
    assert "Python" in body1

    p2 = vault.write_fact("owner", "favorite_language", "Rust", confidence=0.95)
    assert p1 == p2, "same category/key must map to the same note"
    fm2, body2 = ObsidianVault._read_markdown(p2)
    # frontmatter key-value pairs update correctly...
    assert fm2["value"] == "Rust" and fm2["confidence"] == 0.95
    assert fm2["last_updated"] == "2026-08-17T12:34:56+00:00"
    # ...while creation timestamp and note structure are preserved
    assert fm2["recorded_at"] == fm1["recorded_at"], "recorded_at must not drift on update"
    assert body2.strip() == "owner — favorite_language: Rust"   # template tracks the value


def test_fact_update_preserves_hand_edited_body(vault):
    """A human-edited body is never clobbered by a machine frontmatter update."""
    p = vault.write_fact("stats", "boredom_threshold", 80)
    fm, _ = ObsidianVault._read_markdown(p)
    ObsidianVault._atomic_write(
        p, fm, "# stats — boredom_threshold\n\nHuman note: tuned after week one.")

    p2 = vault.write_fact("stats", "boredom_threshold", 75)
    assert p == p2
    fm2, body2 = ObsidianVault._read_markdown(p2)
    assert fm2["value"] == 75                                  # frontmatter did update
    assert "Human note: tuned after week one." in body2        # ...and the body survived


# -- P1 Test Case C: concurrent / async write safety ----------------------------------

def test_concurrent_writes_no_lost_lines_or_corruption(vault):
    n_journal, n_facts = 12, 8
    errors: list = []

    def _journal(i):
        vault.append_journal(f"parallel-obs-{i}")

    def _facts(i):
        vault.write_fact("stats", f"probe_{i}", i * 7)          # facts while journaling

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_journal, i) for i in range(n_journal)]
        futures += [pool.submit(_facts, i) for i in range(n_facts)]
        for f in futures:
            try:
                f.result(timeout=30)
            except Exception as exc:                            # noqa: BLE001 - collected
                errors.append(exc)
    assert not errors

    day_file = next(iter(vault.journal_dir.glob("*.md")))
    text = day_file.read_text(encoding="utf-8")
    for i in range(n_journal):
        count = len(re.findall(rf"parallel-obs-{i}\b", text))
        assert count == 1, f"journal entry {i} lost or duplicated ({count} occurrences)"

    # every note parses as valid frontmatter + body (no torn markdown from races)
    for i in range(n_facts):
        fm, body = vault.read_fact("stats", f"probe_{i}")
        assert fm.get("type") == "fact" and fm["value"] == i * 7
        assert body.strip()

    leftovers = list(vault.root.rglob(".*.tmp"))
    assert leftovers == [], "atomic temp files must not leak after concurrent writes"
