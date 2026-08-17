# Module 7 — Obsidian Atomic Memory File Engine

## 1. Module Overview & Objective

Provides atomic Markdown read/write into an Obsidian vault: facts to `Memory/Facts/`, events to `Memory/Events/`, and a daily journal `Journal/YYYY-MM-DD.md`. Writes are **atomic** (same-dir dot-temp + `Path.replace`), with YAML frontmatter fronting each file. No Obsidian plugin dependency.

Maps to **Module 7** of `IMPLEMENTATION_OUTLINE.md`; produces `obsidian_vault.py` with API `write_fact(category, key, value)` and `append_journal(entry)`.

Extraction source: **`jrcruciani/obsidian-memory-for-ai`** (SPEC-v4 + reference Python tools):
- `SPEC-v4.md` — canonical directory tree + frontmatter schemas + invariants.
- `examples/v4-minimal-vault/tools/compact.py` — the canonical `write_markdown()` atomic-write helper.
- `examples/v4-minimal-vault/tools/lint.py` — `split_frontmatter()` parser + frontmatter regex.
- `examples/v3-minimal-vault/tools/compact.py` — `target_for()` path resolver (event → `events/YYYY-MM-DD/`, fact → `facts/{entity}/{pred}.md`).

> **Reality check vs. the outline**: the spec has **no daily-journal file type** — daily content is per-event files under `memory/events/YYYY-MM-DD/`, and legacy v2 has an append-only `log.md` with `## [YYYY-MM-DD] operation | topic` headers. `append_journal()` must therefore be hand-built (§5) from the atomic-write pattern plus a plain append convention. Also: **no true append helper exists** — "append-only" is enforced by *convention* (one event per file + lint), not by code that text-appends.

## 2. Minimum Required Libraries

| Package | Why |
|---|---|
| `PyYAML` (`pyyaml`) | `yaml.safe_dump`/`yaml.safe_load` for frontmatter (only real dep in the reference tools) |
| stdlib | `pathlib`, `re`, `datetime` (`isoformat`), `hashlib` (optional IDs) |

No system drivers. No Obsidian plugin, no `python-frontmatter` (the reference hand-rolls it).

## 3. Source Code Extraction (Verbatim)

### 3.1 Canonical atomic write — `examples/v4-minimal-vault/tools/compact.py` (lines 51–60)

```python
def write_markdown(path: Path, frontmatter: dict[str, Any], body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"---\n{yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()}\n---\n"
    if body:
        text += f"\n{body.rstrip()}\n"
    else:
        text += "\n"
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
```

The atomicity contract: write to a hidden dot-prefixed sibling (so Markdown globbers ignore it), then `tmp.replace(path)` — a single atomic rename on POSIX (`os.replace` on Windows). Readers never observe a partially written file.

### 3.2 Frontmatter parser — `examples/v4-minimal-vault/tools/lint.py` (lines 22, 46–59)

```python
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)

def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}

def split_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    data = yaml.safe_load(match.group(1)) or {}
    if not isinstance(data, dict):
        return {}, text[match.end():]
    return data, text[match.end():]
```

### 3.3 Type → path resolver — `examples/v3-minimal-vault/tools/compact.py` (lines 58–72)

```python
def target_for(root: Path, path: Path, data: dict[str, Any]) -> Path | None:
    typ = data.get("type")
    if typ == "event":
        occurred = parse_datetime(data["occurred_at"])
        day = occurred.astimezone(dt.timezone.utc).date().isoformat()
        return root / "memory/events" / day / path.name
    if typ == "fact":
        entity = data["entity"]
        predicate = data["predicate"]
        return root / "memory/facts" / entity / f"{predicate}.md"
    if typ == "decision":
        return root / "memory/decisions" / path.name
    if typ == "insight":
        return root / "memory/insights" / path.name
    return None
```

This is the exact `events/YYYY-MM-DD/` (ISO date) and `facts/{entity}/{predicate}.md` naming convention the outline asks for.

### 3.4 Canonical directory tree — `SPEC-v4.md` (lines 100–124, excerpt)

```text
memory/
  schema/
    *.schema.yaml           — YAML schemas for all types
  facts/{entity}/{pred}.md  — atomic typed facts
  events/YYYY-MM-DD/{id}.md — append-only episodic records
  people/, projects/, context/, decisions/, insights/
  _transactions/            — transaction journals/receipts (Markdown)
  _proposals/, _reviews/, _staging/, _views/, _indexes/, _claims/
```

VaultSprite maps this onto fixed vault paths: `Memory/Events/`, `Memory/Facts/`, `Journal/YYYY-MM-DD.md`.

### 3.5 Frontmatter field conventions (SPEC-v4 + example schemas)

- **Fact** (`memory/facts/{entity}/{pred}.md`): required `type, entity, predicate, value, recorded_at`; optional `id, valid_from/valid_to, confidence (high|medium|low), sources[], tags[], decay{review_after_days, ...}`.
- **Event** (`memory/events/YYYY-MM-DD/{id}.md`): required `type, id, summary, occurred_at`; optional `entities[], sources[], tags[], body`. ID convention `event-{YYYY-MM-DD}-{slug}`.
- **Transaction** (`_transactions/`): `type: transaction, transaction_id, idempotency_key, agent_id, created_at, status, ops[], committed_revision, ...` — the idempotency machinery (port only if multi-op atomicity is needed).
- **Invariants** (SPEC §5): "Append-only events: Events are never modified after creation"; idempotent replay via committed-journal check.

## 4. Logic & Data Flow Breakdown

1. **Write path** (§3.1): `write_markdown` mkdirs parents, builds `---`-fenced YAML frontmatter via `yaml.safe_dump(sort_keys=False, allow_unicode=True)` (stable key order, UTF-8-safe), appends the body, writes the whole file to a **dot-temp sibling**, then `tmp.replace(path)`. Because the temp lives in the same directory as the target, the rename is atomic on the same filesystem. Empty body still emits `\n` so files end with a newline.
2. **Read path** (§3.2): `split_frontmatter` matches the leading `^---\n(.*?)\n---\n?` block (DOTALL) — no frontmatter ⇒ `({}, text)`; non-dict YAML ⇒ also treated as body-only. This is the round-trip inverse of `write_markdown`.
3. **Path resolution** (§3.3): `target_for` derives the destination from the record's `type`. Events are bucketed by the **UTC ISO date** of `occurred_at` (`YYYY-MM-DD`), facts by `entity/predicate`. The outline's `Journal/YYYY-MM-DD.md` is the same date-bucketing idea collapsed into one daily file.
4. **Naming**: event IDs `event-{YYYY-MM-DD}-{slug}`; fact IDs `fact-{entity}-{predicate}`; transaction IDs `txn-{slug}-{timestamp}-{hex8}`. Slugs are lowercase `[a-z0-9-]`.
5. **Data flow**: caller (remote agent M6 / stat events) produces a dict → `write_markdown` serializes atomically → later reads go through `split_frontmatter` → UI/brain consumes the dict. The engine is purely filesystem-level; no Obsidian app is invoked.

## 5. Refactoring & Integration Notes

Target: `obsidian_vault.py` exposing **`write_fact(category, key, value)`** and **`append_journal(entry)`**, with the vault root configurable (env var or `config.py` — the reference tools assume CWD and have no config; we must add one).

Step-by-step:

1. **Vault root + fixed paths**:
   ```python
   VAULT_ROOT = Path(os.getenv("VAULT_ROOT", "Vault"))
   EVENTS_DIR = VAULT_ROOT / "Memory" / "Events"
   FACTS_DIR  = VAULT_ROOT / "Memory" / "Facts"
   JOURNAL_DIR = VAULT_ROOT / "Journal"
   ```
   The outline hardcodes `Memory/Events/`, `Memory/Facts/`, `Journal/YYYY-MM-DD.md` — implement as module constants.
2. **Port `write_markdown` verbatim** as the private `_atomic_write(path, frontmatter, body)` — including the dot-temp + `replace` atomicity. On Windows, `Path.replace` maps to `os.replace` (rename-over-existing works); keep the hidden-dot temp so Obsidian's globber ignores in-progress files.
3. **Port `split_frontmatter` + `FRONTMATTER_RE`** verbatim as `_read_markdown(path) -> (dict, body)`. This is the reader for facts/events and the journal.
4. **`write_fact(category, key, value)`**:
   - Map `category` → entity dir under `FACTS_DIR` (e.g. `Memory/Facts/{category}/`), file name `{key}.md`.
   - Frontmatter: `type: fact, entity: <category>, predicate: <key>, value: <value>, recorded_at: <now_utc().isoformat()>` (plus optional `confidence`/`tags`).
   - Body: human-readable sentence form of the value.
   - Call `_atomic_write`. **Overwrite semantics**: a fact file is *updated in place* atomically (facts are mutable; events are not) — mirror SPEC: facts are typed and versionable, events are append-only.
5. **`append_journal(entry)`** — hand-build the daily append since no reference helper exists:
   ```python
   day = datetime.now(timezone.utc).date().isoformat()     # YYYY-MM-DD
   path = JOURNAL_DIR / f"{day}.md"
   if path.exists():
       fm, body = _read_markdown(path)
       with path.open("a", encoding="utf-8") as fh:        # plain append
           fh.write(f"\n{entry}\n")
   else:
       _atomic_write(path, {"type": "journal", "date": day}, f"## {entry}\n")
   ```
   To stay strictly atomic on the *first* write, use `_atomic_write`; for appends a mode-`a` write is acceptable (single-line entry, journal tolerates non-atomic appends; alternatively do read-modify-`_atomic_write` if you demand full atomicity).
6. **Optional idempotency**: if you want SPEC-v4-grade safety, wrap multi-op writes in the transaction pattern (`_transactions/` journal + `idempotency_key`) — but the outline's API only needs the two methods, so keep it simple unless multi-op atomicity becomes a requirement.
7. **Journal vs event date consistency**: journal entries use `YYYY-MM-DD` local-or-UTC consistently (SPEC uses UTC `astimezone(timezone.utc).date().isoformat()`); pick one and document it.
8. **Testing**: against a tempdir vault (no real Obsidian): assert `write_fact` creates `Memory/Facts/{category}/{key}.md` with correct frontmatter, atomicity (no `.tmp` leftovers, valid file after write), `split_frontmatter` round-trips, `append_journal` creates today's file and appends on second call, and journal filename matches `\d{4}-\d{2}-\d{2}\.md`. All headless, stdlib + PyYAML only.

## 6. Source Files (Reference Copies)

Full verbatim copies from `jrcruciani/obsidian-memory-for-ai`, kept locally:

| File | Purpose |
|---|---|
| `source/SPEC-v4.md` | Authoritative spec: directory tree (§3), frontmatter schemas (§4), invariants (§5) — read this first |
| `source/SPEC-v3.md` | Previous spec: v3 `facts/{entity}/{pred}.md` + `events/YYYY-MM-DD/` migration notes |
| `source/compact.py` | v4 canonical `write_markdown()` (atomic dot-temp + `replace`) |
| `source/lint.py` | `FRONTMATTER_RE` + `split_frontmatter`/`load_yaml` (frontmatter parse), validation |
| `source/transact.py` | Full transaction engine (journal, commit/rollback/recover, idempotency) — optional, only if multi-op atomicity is needed |
| `source/propose.py` / `source/review.py` | Review-gated proposal system (strip unless needed) |
| `source/compact_v3.py` | v3 `write_markdown` + **`target_for()`** — the type→path resolver (`events/YYYY-MM-DD/`, `facts/{entity}/{pred}.md`) |
| `source/ops_v3.py` | v3 operation envelope writer + `cmd_add_event` (daily `YYYY-MM-DD` event pattern) |
| `source/fact.schema.yaml` / `source/event.schema.yaml` / `source/transaction.schema.yaml` | Exact YAML frontmatter field schemas |

> No daily-journal type exists in either spec; `append_journal()` is hand-built per README §5.5. Deps: PyYAML + stdlib only.
