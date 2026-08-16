"""Obsidian atomic memory file engine (Module 7).

Atomic Markdown read/write into an Obsidian vault, ported from the
``obsidian-memory-for-ai`` SPEC-v4 patterns: dot-temp + ``Path.replace`` for
atomicity, YAML frontmatter via PyYAML. No Obsidian plugin dependency.

Fixed paths under the (configurable) vault root::

    Memory/Facts/{category}/{key}.md     mutable typed facts   (write_fact)
    Memory/Events/YYYY-MM-DD/{id}.md     append-only events    (record_event)
    Journal/YYYY-MM-DD.md                daily journal         (append_journal)
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Union

import yaml

from .config import Config, load_config

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or "item"


class ObsidianVault:
    """Filesystem-only vault writer/reader."""

    def __init__(self, config: Union[Config, None] = None):
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

    # -- public API ------------------------------------------------------------
    def write_fact(self, category: str, key: str, value: Any, **meta: Any) -> Path:
        """Create/overwrite ``Memory/Facts/{category}/{key}.md`` (atomic)."""
        path = self.facts_dir / _slugify(category) / f"{_slugify(key)}.md"
        frontmatter: dict[str, Any] = {
            "type": "fact",
            "entity": category,
            "predicate": key,
            "value": value,
            "recorded_at": self._now_iso(),
        }
        frontmatter.update(meta)
        body = f"{category} — {key}: {value}" if not isinstance(value, str) else \
               f"{category} — {key}: {value}"
        self._atomic_write(path, frontmatter, body)
        return path

    def read_fact(self, category: str, key: str) -> tuple[dict[str, Any], str]:
        path = self.facts_dir / _slugify(category) / f"{_slugify(key)}.md"
        return self._read_markdown(path)

    def record_event(self, summary: str, **details: Any) -> Path:
        """Append-only episodic event under ``Memory/Events/YYYY-MM-DD/``."""
        day = self._today()
        slug = _slugify(summary)[:24]
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        path = self.events_dir / day / f"event-{day}-{slug}-{stamp}.md"
        body = details.pop("body", None) or summary
        frontmatter: dict[str, Any] = {
            "type": "event",
            "id": f"event-{day}-{slug}",
            "summary": summary,
            "occurred_at": self._now_iso(),
        }
        frontmatter.update(details)
        self._atomic_write(path, frontmatter, str(body))
        return path

    def append_journal(self, entry: str) -> Path:
        """Append a timestamped line to ``Journal/YYYY-MM-DD.md`` (atomic RMW)."""
        day = self._today()
        stamp = datetime.now().strftime("%H:%M")
        line = f"- [{stamp}] {entry.strip()}"
        path = self.journal_dir / f"{day}.md"
        if path.exists():
            frontmatter, body = self._read_markdown(path)
            new_body = (body.rstrip("\n") + "\n" + line + "\n").strip() + "\n"
            self._atomic_write(path, frontmatter or {"type": "journal", "date": day},
                               new_body.strip())
        else:
            self._atomic_write(
                path,
                {"type": "journal", "date": day},
                f"# {day}\n\n{line}\n",
            )
        return path
