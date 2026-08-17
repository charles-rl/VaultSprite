"""Obsidian atomic memory file engine (Module 7).

Atomic Markdown read/write into an Obsidian vault, ported from the
``obsidian-memory-for-ai`` SPEC-v4 patterns: dot-temp + ``Path.replace`` for
atomicity, YAML frontmatter via PyYAML. No Obsidian plugin dependency.

Fixed paths under the (configurable) vault root::

    Memory/Facts/{category}/{key}.md     mutable typed facts   (write_fact)
    Memory/Events/YYYY-MM-DD/{id}.md     append-only events    (record_event)
    Journal/YYYY-MM-DD.md                daily journal         (append_journal)

Safety contracts (P1 hardening):
- **Write sandboxing**: every public write resolves its target against the vault
  root and raises ``PermissionError("WRITE DENIED: ...")`` for anything outside
  it (symlinks included, via ``os.path.realpath``). Read access elsewhere on the
  system is unaffected — only writes are hard-locked to this folder.
- **Storage watching**: ``check_storage()`` sizes the whole vault folder after
  every write (App also ticks it periodically) and emits edge-triggered
  ``vault_size_warning(bytes)`` when ``obsidian.max_size_mb`` is exceeded;
  monitoring never raises, so a full disk can't crash the pet.
- **Concurrency**: all read-modify-write sections run under one RLock, so
  parallel callers cannot lose journal lines (the atomic rename already prevents
  torn reads; the lock fixes lost updates). Process-level only — VaultSprite is
  a single app instance and takes no cross-process file locks.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Union

import yaml
from PySide6.QtCore import QObject, Signal

from .config import Config, load_config

logger = logging.getLogger(__name__)

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or "item"


def _fact_body(category: str, key: str, value: Any) -> str:
    """Canonical one-line body for a machine-managed fact note."""
    return f"{category} — {key}: {value}"


JOURNAL_TAGS = ["desktop-pet", "journal"]


class ObsidianVault(QObject):
    """Filesystem-only vault writer/reader; all writes sandboxed to the root."""

    vault_size_warning = Signal(float)   # current folder size in bytes, edge-triggered

    def __init__(self, config: Union[Config, None] = None):
        super().__init__()
        self.config = config or load_config()
        root = Path(self.config.get("obsidian.vault_root", "Vault"))
        if not root.is_absolute():
            root = (Path(__file__).resolve().parent.parent / root).resolve()
        self.root = root
        self.events_dir = self.root / str(
            self.config.get("obsidian.events_dir", "Memory/Events")
        )
        self.facts_dir = self.root / str(
            self.config.get("obsidian.facts_dir", "Memory/Facts")
        )
        self.journal_dir = self.root / str(
            self.config.get("obsidian.journal_dir", "Journal")
        )

        # storage watcher state: None limit (max_size_mb <= 0) disables monitoring
        max_mb = float(self.config.get("obsidian.max_size_mb", 50) or 0)
        self.size_limit_bytes: Union[int, None] = \
            int(max_mb * 1024 * 1024) if max_mb > 0 else None
        self._size_warned_over = False
        # single writer lock across read-modify-write sections (P1 concurrency fix)
        self._lock = threading.RLock()

    # -- write sandboxing guard (P1: absolute write prohibition outside root) ----
    def _resolve_safe(self, file_path: Union[str, Path]) -> Path:
        """Resolve *file_path* to its real path and refuse anything outside the vault.

        Raises ``PermissionError`` when the resolved target escapes the vault
        root — including through symlinks or sibling directories that merely
        share the root's name prefix (hence ``startswith(base + os.sep)``)."""
        target_path = os.path.realpath(file_path)
        allowed_base = os.path.realpath(str(self.root))
        if not (target_path == allowed_base or target_path.startswith(allowed_base + os.sep)):
            raise PermissionError(
                f"WRITE DENIED: Path {target_path} is outside "
                f"allowed vault directory {allowed_base}")
        return Path(target_path)

    # -- storage size watcher -----------------------------------------------------
    def storage_size_bytes(self) -> int:
        """Total on-disk bytes in the vault folder (best-effort; never raises)."""
        total = 0
        for dirpath, _dirnames, filenames in os.walk(str(self.root)):
            for name in filenames:          # vanished mid-walk → skipped via OSError guard
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
        return total

    def check_storage(self) -> int:
        """Size the vault folder and warn when over ``obsidian.max_size_mb``.

        Edge-triggered (mirrors StatEngine hysteresis): one log line + signal per
        exceed episode, re-armed only after usage drops back under the limit.
        Returns the current size in bytes; monitoring never raises."""
        current = self.storage_size_bytes()
        if not (self.size_limit_bytes and current > self.size_limit_bytes):
            self._size_warned_over = False     # healthy again → re-arm the trip wire
            return current
        if not self._size_warned_over:
            self._size_warned_over = True
            logger.warning(
                "vault storage warning: %.1f MB used, limit %.2f MB (%s)",
                current / 1048576, (self.size_limit_bytes or 0) / 1048576, self.root)
            try:
                self.vault_size_warning.emit(current)
            except RuntimeError as exc:        # teardown race: QObject already gone
                logger.debug("vault_size_warning emit failed: %s", exc)
        return current

    # -- atomic I/O primitives (SPEC-v4 canonical helpers) --------------------
    @staticmethod
    def _atomic_write(path: Path, frontmatter: dict[str, Any], body: str = "") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = f"---\n{yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()}\n---\n"
        if body:
            text += f"\n{body.rstrip()}\n"
        else:
            text += "\n"
        tmp = path.with_name(f".{path.name}.tmp")   # hidden dot-temp sibling
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)                            # atomic rename

    @staticmethod
    def _read_markdown(path: Path) -> tuple[dict[str, Any], str]:
        if not path.exists():
            return {}, ""
        text = path.read_text(encoding="utf-8")
        match = FRONTMATTER_RE.match(text)
        if not match:
            return {}, text
        data = yaml.safe_load(match.group(1)) or {}
        if not isinstance(data, dict):
            return {}, text[match.end():]
        return data, text[match.end():]

    # -- date handling ---------------------------------------------------------
    @property
    def _tz(self):
        return None if self.config.get("obsidian.date_timezone", "utc") == "local" \
            else timezone.utc

    def _today(self) -> str:
        now = datetime.now(timezone.utc if self._tz is not None else None)
        return now.date().isoformat()

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # -- public API (all writes: locked → guarded → atomic → size-checked) ----
    def write_fact(self, category: str, key: str, value: Any, **meta: Any) -> Path:
        """Create/update ``Memory/Facts/{category}/{key}.md`` (atomic, sandboxed).

        Frontmatter updates in place on subsequent calls: ``value`` and
        ``last_updated`` change each time; the original ``recorded_at`` and any
        earlier caller metadata (e.g. ``confidence``) are preserved unless a new
        kwarg overrides them. A hand-authored body (anything other than the
        generated template line) is left untouched by machine updates."""
        with self._lock:
            path = self.facts_dir / _slugify(category) / f"{_slugify(key)}.md"
            target = self._resolve_safe(path)        # may raise PermissionError
            prior_fm, prior_body = \
                (self._read_markdown(target) if target.exists() else ({}, ""))
            now = self._now_iso()

            frontmatter: dict[str, Any] = {
                "type": "fact",
                "entity": category,
                "predicate": key,
                "value": value,
                "recorded_at": prior_fm.get("recorded_at") or now,   # stable across updates
            }
            preserved = {k: v for k, v in prior_fm.items() if k not in (
                "type", "entity", "predicate", "value", "recorded_at", "last_updated")}
            frontmatter.update({k: v for k, v in preserved.items() if k not in meta})
            frontmatter["last_updated"] = now
            frontmatter.update(meta)                 # explicit new kwargs win

            prior_body_stripped = prior_body.strip()
            old_template = _fact_body(category, key, prior_fm.get("value")) if prior_fm else None
            if old_template is not None and prior_body_stripped \
                    and prior_body_stripped != old_template:
                body = prior_body_stripped           # custom content → keep verbatim
            else:
                body = _fact_body(category, key, value)   # templated → track the new value

            self._atomic_write(target, frontmatter, body)
        self.check_storage()                          # post-write size audit (never raises)
        return target

    def read_fact(self, category: str, key: str) -> tuple[dict[str, Any], str]:
        path = self.facts_dir / _slugify(category) / f"{_slugify(key)}.md"
        return self._read_markdown(path)

    def record_event(self, summary: str, **details: Any) -> Path:
        """Append-only episodic event under ``Memory/Events/YYYY-MM-DD/``."""
        with self._lock:
            day = self._today()
            slug = _slugify(summary)[:24]
            stamp = datetime.now(timezone.utc).strftime("%H%M%S")
            path = self.events_dir / day / f"event-{day}-{slug}-{stamp}.md"
            target = self._resolve_safe(path)         # may raise PermissionError
            body = details.pop("body", None) or summary
            frontmatter: dict[str, Any] = {
                "type": "event",
                "id": f"event-{day}-{slug}",
                "summary": summary,
                "occurred_at": self._now_iso(),
            }
            frontmatter.update(details)
            self._atomic_write(target, frontmatter, str(body))
        self.check_storage()                          # post-write size audit (never raises)
        return target

    def append_debug_log(self, category: str, entry: str) -> Path:
        """Append a timestamped line to ``Memory/Debug/{category}.md``.

        Rolling debug trail for diagnosing user-reported issues (context switches,
        FSM transitions, physics events). Files roll daily by appending — the same
        atomic RMW as the journal but without its frontmatter contract."""
        with self._lock:
            day = self._today()
            stamp = datetime.now().strftime("%H:%M:%S")
            path = self.root / "Memory" / "Debug" / f"{_slugify(category)}-{day}.md"
            target = self._resolve_safe(path)         # may raise PermissionError
            existing = "" if not target.exists() else target.read_text(encoding="utf-8")
            text = (existing.rstrip("\n") + ("\n" if existing.strip() else "")
                    + f"- [{stamp}] {entry.strip()}\n")
            tmp = target.with_name(f".{target.name}.tmp")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(target)                        # atomic rename
        self.check_storage()                          # post-write size audit (never raises)
        return target

    def append_journal(self, entry: str) -> Path:
        """Append a timestamped line to ``Journal/YYYY-MM-DD.md`` (atomic RMW)."""
        with self._lock:
            day = self._today()
            stamp = datetime.now().strftime("%H:%M")
            line = f"- [{stamp}] {entry.strip()}"
            path = self.journal_dir / f"{day}.md"
            target = self._resolve_safe(path)         # may raise PermissionError
            if target.exists():
                frontmatter, body = self._read_markdown(target)
                new_body = body.rstrip("\n") + "\n" + line
                # keep existing frontmatter; backfill the contract tags on older files
                frontmatter.setdefault("type", "journal")
                frontmatter.setdefault("date", day)
                frontmatter.setdefault("tags", list(JOURNAL_TAGS))
            else:
                frontmatter = {"type": "journal", "date": day,
                               "tags": list(JOURNAL_TAGS)}
                new_body = f"# {day}\n\n{line}"
            self._atomic_write(target, frontmatter, new_body)
        self.check_storage()                          # post-write size audit (never raises)
        return target
