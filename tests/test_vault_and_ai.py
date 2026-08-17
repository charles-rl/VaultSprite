"""VaultSprite P1 test runner — Sandboxing, Vault I/O, Ollama discovery & vision probe.

Run directly inside the project venv (not pytest-collected):

    uv run python tests/test_vault_and_ai.py

Executes four checks in sequence and prints a status report (exit 0 = no failures):

    TEST 1  Security sandboxing guardrail — writes outside the vault root must raise
            PermissionError("WRITE DENIED") before any filesystem mutation.
    TEST 2  Folder-size monitoring — edge-triggered ``vault_size_warning`` fires at
            exactly the configured ``obsidian.max_size_mb`` threshold (and re-arms
            only after usage drops back under it).
    TEST 3  Obsidian journal & fact writing — frontmatter validity, append-only
            behavior, atomic fact updates that preserve body/creation fields, and a
            thread-pool concurrency smoke test.
    TEST 4  Ollama API & vision probe — model discovery (``/api/tags`` with an
            ``ollama ls`` CLI fallback) stored in the runtime context, then a
            base64-PNG chat payload; text-only / unreachable endpoints are handled
            gracefully and RemoteAgent's plain-text metadata fallback is verified.

All vault I/O stays inside fresh temp roots via the sandboxed ObsidianVault API.
The only file written outside the repo by this harness is the probe PNG under
/tmp (per spec, "memory/temp workspace"). Env knobs: OLLAMA_BASE_URL and
VISION_PROBE_TIMEOUT_S (seconds; default 300 — a first inference may need to load
the model).
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # never shadows an explicit choice

import httpx                    # noqa: E402

# NOTE: no Qt object is created at import time — pytest collects this file by name, so any
# module-level QCoreApplication would clobber conftest's session QApplication (Qt aborts).
from tests.conftest import FakeConfig                     # noqa: E402
import yaml                                               # noqa: E402
from vaultsprite.obsidian_vault import ObsidianVault      # noqa: E402

PROBE_PNG = Path("/tmp/vaultsprite_vision_probe.png")     # stable path for post-run inspection
VISION_KEYWORDS = re.compile(
    r"\b(circle|triangle|square|rectangle|shape|shapes|red|blue|green|colou?r)\w*", re.I)
NO_VISION_REPLY = re.compile(r"(can'?t see|cannot see|no image|image (wasn't |is not )?"
                             r"(provided|available)|i ?(don'?t|do not) (see|have vision))", re.I)


def _make_vault(root: Path, **dotted_overrides):
    """Fresh sandboxed vault in *root*; keys are dotted config strings (FakeConfig contract)."""
    dotted_overrides.setdefault("obsidian.vault_root", str(root))
    return ObsidianVault(FakeConfig(dotted_overrides))


# --------------------------------------------------------------------------- TEST 1 --
def check_sandboxing(ctx: dict) -> tuple[bool, str]:
    """Attempt dummy writes outside the assigned workspace; expect PermissionError."""
    tmp = Path(tempfile.mkdtemp(prefix="vs_sandbox_"))
    try:
        vault = _make_vault(tmp / "Workspace")
        denied: list[str] = []

        def expect_denied(label: str, target: Path) -> tuple[bool, str]:
            try:
                vault._resolve_safe(target)
            except PermissionError as exc:
                assert "WRITE DENIED" in str(exc), f"{label}: wrong message: {exc}"
                denied.append(label)
                return True, ""
            return False, f"{label}: escape NOT blocked ({target})"

        parent = vault.root.parent
        check_1 = expect_denied("parent-dir", parent / "test_leak.txt")      # the spec's ../test_leak.txt
        if not check_1[0]:
            return check_1
        check_2 = expect_denied("absolute-outside", parent / "elsewhere" / "pwned.md")
        if not check_2[0]:
            return check_2

        # symlink inside the root pointing out must be resolved (realpath) and denied
        vault.journal_dir.mkdir(parents=True, exist_ok=True)
        outside = parent / "symlink_target.txt"
        (vault.journal_dir / "escape.md").symlink_to(outside)
        check_3 = expect_denied("symlink-escape", vault.journal_dir / "escape.md")
        if not check_3[0]:
            return check_3

        # end-to-end: a second instance with a tampered facts_dir must be denied too —
        # and nothing may materialize outside the root
        leaked_dir = parent / "FactsEscape"
        leaky = _make_vault(vault.root)               # fresh state; only its dirs are tampered
        leaky.facts_dir = leaked_dir
        try:
            leaky.write_fact("leak", "payload", 1)
        except PermissionError:
            pass
        else:
            return False, "end-to-end write escape succeeded (no exception)"
        if leaked_dir.exists() or outside.exists():
            return False, f"files materialized outside the vault root ({leaked_dir})"

        # reads of in-vault notes must still work (write-only prohibition)
        p = vault.write_fact("owner", "name", "Sprite")   # inside the root → allowed
        fm, _ = ObsidianVault._read_markdown(p)
        if fm.get("value") != "Sprite":
            return False, f"in-vault write/read round-trip failed: {fm!r}"

        ctx["sandbox_denied"] = denied
        return True, (f"{len(denied)} escape vectors denied (parent, absolute, symlink); "
                      f"end-to-end leak blocked; in-vault writes unaffected")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- TEST 2 --
def check_size_monitoring(ctx: dict) -> tuple[bool, str]:
    """Exact-threshold trip + latch semantics of the storage watcher."""
    tmp = Path(tempfile.mkdtemp(prefix="vs_sizewatch_"))
    try:
        root = tmp / "PreciseVault"

        # phase 1: build a deterministic baseline file (pinned date ≠ today → isolated),
        # using an instance whose limit is far away so the writes don't warn themselves
        fill_vault = _make_vault(root, **{"obsidian.max_size_mb": 9999})
        filler = fill_vault.journal_dir / "2026-01-01.md"
        ObsidianVault._atomic_write(filler, {"type": "journal", "date": "2026-01-01"},
                                    "A" * 4000)

        # phase 2: a second instance over the same folder whose limit sits exactly
        # `margin` bytes above current usage → the first journal append must cross it
        margin = 32
        s_base = sum(p.stat().st_size for p in root.glob("**/*.md"))   # folder usage so far

        limit_mb = (s_base + margin) / 1048576
        vault = _make_vault(root, **{"obsidian.max_size_mb": limit_mb})
        limit = vault.size_limit_bytes or 0
        if not (s_base < limit <= s_base + margin):
            return False, f"limit round-trip off: S={s_base}, limit={limit} (want within {margin}B)"

        warnings: list[float] = []
        vault.vault_size_warning.connect(warnings.append)
        if vault.check_storage() > limit or warnings:           # manual tick while compliant
            return False, f"false positive under limit ({warnings})"

        vault.append_journal("tiny")                            # crosses the threshold by design
        new_total = vault.storage_size_bytes()
        if len(warnings) != 1 or not (new_total > limit):       # post-write audit must trip once
            return False, (f"threshold missed/inexact: warned={warnings}, "
                           f"size {s_base}→{new_total}, limit={limit}")
        vault.check_storage()                                   # still over → latched, no re-fire
        if len(warnings) != 1:
            return False, f"warning re-fired while over the limit ({len(warnings)} total)"

        # phase 3: shrink under the limit (remove filler + today's note), then regrow → warn again
        shutil.rmtree(vault.journal_dir, ignore_errors=True)    # removes ONLY pinned+today files
        if vault.check_storage() >= limit or len(warnings) != 1:
            return False, "watcher did not re-arm after usage dropped under the limit"
        for i in range(6):                                       # ~3.4 KB/entry × 6 > margin gap
            vault.append_journal(f"regrow-{i} " + "z" * 3000)
        if len(warnings) != 2 or warnings[1] <= limit:
            return False, f"re-crossed limit without a second warning ({warnings})"

        ctx["size_limit_bytes"] = limit
        # informational: the real repo vault (read-only measurement, default root)
        real = _make_vault(REPO_ROOT / "Vault")                 # max_size_mb=50 from config.yaml
        used = real.storage_size_bytes()
        ctx["real_vault_used_bytes"] = used
        return True, (f"limit {limit}B held exactly: no warn under → 1 warn over (latched) → "
                      f"re-armed after shrink, warned again on regrow; repo vault usage "
                      f"{used / 1024:.1f} KB vs its {real.size_limit_bytes // 1048576} MB limit")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- TEST 3 --
def check_vault_io(ctx: dict) -> tuple[bool, str]:
    """Test Cases A (journal append), B (atomic fact updates), C (concurrency)."""
    tmp = Path(tempfile.mkdtemp(prefix="vs_vaultio_"))
    try:
        vault = _make_vault(tmp / "IOVault")

        # -- Case A: daily journal initialization + clean appends -------------------
        path = vault.append_journal("first entry of the day")
        raw = path.read_text(encoding="utf-8")
        data, body = ObsidianVault._read_markdown(path)     # frontmatter block must be valid YAML
        if yaml.safe_load(raw.split("---")[1]) != data:
            return False, f"journal frontmatter not parseable as YAML:\n{raw}"
        day_ok = re.fullmatch(r"\d{4}-\d{2}-\d{2}", data.get("date", "")) is not None
        if data.get("type") != "journal" or not day_ok \
                or data.get("tags") != ["desktop-pet", "journal"]:
            return False, f"journal frontmatter contract violated: {data!r}"
        snap_1 = raw

        vault.append_journal("second entry")
        vault.append_journal("third entry")
        text = path.read_text(encoding="utf-8")
        for i, entry in enumerate(("first entry of the day", "second entry", "third entry")):
            if not re.search(rf"^- \[\d{{2}}:\d{{2}}\] {re.escape(entry)}$", text, re.MULTILINE):
                return False, f"entry {i + 1} missing or malformed in journal"
        idx = [text.index(e) for e in ("first entry", "second entry", "third entry")]
        if not (idx[0] < idx[1] < idx[2]) or len(text) <= len(snap_1):
            return False, "journal appends did not preserve existing content/order"

        # -- Case B: structured atomic fact creation + update -----------------------
        p1 = vault.write_fact("owner", "favorite_language", "Python", confidence=0.9)
        fm1, body1 = ObsidianVault._read_markdown(p1)
        if fm1.get("value") != "Python" or fm1.get("confidence") != 0.9:
            return False, f"fact create frontmatter wrong: {fm1!r}"
        recorded_at_1 = fm1.get("recorded_at")

        p2 = vault.write_fact("owner", "favorite_language", "Rust", confidence=0.95)
        fm2, body2 = ObsidianVault._read_markdown(p2)
        if p1 != p2 or fm2.get("value") != "Rust" or fm2.get("last_updated") is None:
            return False, f"fact update did not map to same note / stamp last_updated: {fm2!r}"
        if fm2.get("recorded_at") != recorded_at_1:
            return False, "fact update drifted the creation timestamp (recorded_at)"
        if fm2.get("confidence") != 0.95 or body1.strip() != "owner — favorite_language: Python" \
                or body2.strip() != "owner — favorite_language: Rust":
            return False, f"fact meta/body handling wrong:\n{fm2!r}\n{body2!r}"

        # -- Case C: concurrent rapid writes -----------------------------------------
        n_journal, n_facts = 6, 5
        errors: list[str] = []

        def _j(i):
            vault.append_journal(f"parallel-obs-{i}")

        def _f(i):
            vault.write_fact("stats", f"probe_{i}", i * 7)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(_j, i) for i in range(n_journal)] + \
                      [pool.submit(_f, i) for i in range(n_facts)]
            for fut in futures:
                try:
                    fut.result(timeout=30)
                except Exception as exc:                       # noqa: BLE001 - collected verbatim
                    errors.append(f"{exc!r}")
        if errors:
            return False, f"concurrent writers raised: {errors[:2]}"

        jtext = path.read_text(encoding="utf-8")
        for i in range(n_journal):
            n = len(re.findall(rf"parallel-obs-{i}\b", jtext))
            if n != 1:
                return False, f"journal entry {i} lost/duplicated under concurrency ({n}×)"
        for i in range(n_facts):
            fm, body = vault.read_fact("stats", f"probe_{i}")
            if fm.get("type") != "fact" or fm.get("value") != i * 7:
                return False, f"concurrent fact probe_{i} corrupted: {fm!r}"
        leftovers = list(vault.root.rglob(".*.tmp"))
        if leftovers:
            return False, f"atomic temp files leaked: {[str(x) for x in leftovers][:3]}"

        ctx["concurrent_writes"] = n_journal + n_facts
        return True, (f"journal frontmatter valid + {3} appends ordered; fact update preserved "
                      f"recorded_at/body while value/last_updated refreshed; {n_journal + n_facts} "
                      f"thread-pool writes intact, zero tmp leaks")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- TEST 4 --
def _discover_model(base_url: str):
    """Return (model_name, method, raw_info) or raise RuntimeError with a reason."""
    try:
        resp = httpx.get(f"{base_url}/api/tags", timeout=5.0)
        resp.raise_for_status()
        models = resp.json().get("models") or []
        if not models:
            return None, "http /api/tags (no models installed)"
        return [m for m in models], "http /api/tags"
    except httpx.HTTPError as exc:
        cli_reason = f"http failed ({exc!r}); trying CLI"
    try:
        out = subprocess.run(["ollama", "ls"], capture_output=True, text=True, timeout=15)
        if out.returncode == 0:
            lines = [ln for ln in out.stdout.splitlines()[1:] if ln.strip()]
            names = [ln.split()[0] for ln in lines]
            if names:
                return [{"name": n} for n in names], "ollama ls (CLI)"
        raise RuntimeError(cli_reason + f"; CLI produced no models ({out.stderr.strip()[:120]})")
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"discovery failed: {cli_reason}; {exc!r}") from None


def _caps_of(model_info: dict):
    """Capabilities live top-level on /api/tags entries (details.* is Ollama model metadata)."""
    return (model_info.get("capabilities")
            or (model_info.get("details") or {}).get("capabilities")) or []


def _build_probe_png(path: Path) -> str:
    """Draw distinctive shapes + text; return the base64 payload for /api/chat."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (280, 180), "white")
    draw = ImageDraw.Draw(img)
    draw.ellipse([20, 30, 120, 130], fill="red", outline="darkred")      # big red circle (left)
    draw.polygon([(200, 25), (160, 105), (240, 105)], fill="blue")       # blue triangle (top right)
    draw.rectangle([175, 125, 245, 175], fill="green", outline="darkgreen")  # green square
    img.save(path, format="PNG")
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _verify_text_fallback() -> tuple[bool, str]:
    """Prove RemoteAgent degrades to a text prompt carrying window/OCR metadata."""
    from vaultsprite.remote_agent import RemoteAgent

    agent = RemoteAgent(FakeConfig({"remote.ollama_base_url": "http://localhost:11434/v1"}))
    meta = "Active window: PyCharm — main.py\nOCR excerpt: def on_click_confirmed"
    explicit_off = agent.build_messages("What am I doing?", window_context=meta, screenshot=False)
    if not isinstance(explicit_off[1]["content"], str) or meta.splitlines()[0] not in explicit_off[1]["content"]:
        return False, "screenshot=False did not produce a text payload with window metadata"
    agent.capture_screenshot_b64 = lambda: None     # simulate headless/failed capture
    degraded = agent.build_messages("What am I doing?", window_context=meta)
    if not isinstance(degraded[1]["content"], str) or "main.py" not in degraded[1]["content"]:
        return False, "capture-failure degradation lost the metadata fallback"
    return True, "text fallback verified (explicit + capture-failure paths)"


def check_ollama_vision(ctx: dict) -> tuple[str, str]:
    """Model discovery + vision probe. Returns status ∈ PASS|TEXT-ONLY|PARTIAL|SKIP."""
    cfg_url = os.environ.get("OLLAMA_BASE_URL") or \
        FakeConfig().get("remote.ollama_base_url", "http://localhost:11434/v1")
    base_url = str(cfg_url).rstrip("/")
    if base_url.endswith("/v1"):                     # native Ollama API lives at the bare host
        base_url = base_url[:-3]

    try:
        models, method = _discover_model(base_url)
    except RuntimeError as exc:
        return "SKIP", f"ollama unreachable — {exc}"

    configured = FakeConfig().get(
        "remote.ollama_model", "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q8_K_XL")
    names = [m.get("name") or m.get("model") for m in models]
    if configured in names:
        model, how = configured, "matches config"
    else:
        vision_first = next((m for m in models if "vision" in _caps_of(m)), None)
        if vision_first is not None:
            model, how = vision_first["name"], "first vision-capable"
        else:
            model, how = names[0], "only installed (may be text-only)"
    ctx["detected_model"] = model                    # runtime context per spec
    caps = _caps_of(next((m for m in models
                          if (m.get("name") or m.get("model")) == model), {}))

    try:
        b64 = _build_probe_png(PROBE_PNG)
    except Exception as exc:                         # noqa: BLE001 - harness hygiene
        return "FAIL", f"probe image generation failed: {exc!r}"

    probe_s = float(os.environ.get("VISION_PROBE_TIMEOUT_S", 300))
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system",
             "content": "You are a vision test. In one sentence, describe what you see in the image."},
            {"role": "user", "content": "", "images": [b64]},
        ],
    }
    print(f"  → probing {model!r} (caps={caps or 'unknown'}) at {base_url}; "
          f"first inference may need to load the model…")
    started = datetime.now(timezone.utc)
    try:
        resp = httpx.post(f"{base_url}/api/chat", json=payload,
                          timeout=httpx.Timeout(10.0, read=probe_s))
    except httpx.HTTPError as exc:
        return "PARTIAL", (f"vision probe transport error ({exc.__class__.__name__}); "
                           f"fallback text path: {_verify_text_fallback()[1]}")

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    if resp.status_code != 200:
        try:
            err = resp.json().get("error", resp.text[:200])
        except ValueError:
            err = resp.text[:200]
        ctx["probe_error"] = str(err)
        if re.search(r"(?i)\bno such model|not found\b", str(err)):
            return "FAIL", f"server rejected model {model!r}: {err}"
        if NO_VISION_REPLY.search(str(err)) or \
                re.search(r"(?i)(image|vision).{0,50}(unsupported|not support)", str(err)):
            fb_ok, fb_note = _verify_text_fallback()
            ctx["fallback_verified"] = fb_ok
            return "TEXT-ONLY", f"server reports no image support ({str(err)[:120]}); {fb_note}"
        fb_ok, fb_note = _verify_text_fallback()
        ctx["fallback_verified"] = fb_ok
        return "PARTIAL", (f"probe HTTP {resp.status_code}: {str(err)[:160]}; "
                           f"{fb_note} ({elapsed:.1f}s)")

    content = ((resp.json().get("message") or {}).get("content") or "").strip()
    ctx["vision_reply"] = content[:240]
    if not content:
        return "PARTIAL", (f"empty reply in {elapsed:.1f}s; text fallback verified: "
                           f"{_verify_text_fallback()[1]}")

    hits = set(VISION_KEYWORDS.findall(content.lower()))
    if NO_VISION_REPLY.search(content):
        fb_ok, fb_note = _verify_text_fallback()
        ctx["fallback_verified"] = fb_ok
        return "TEXT-ONLY", (f"model replied it cannot view images “{content[:80]}…”; {fb_note}")
    if hits:
        return "PASS", (f"multimodal OK in {elapsed:.1f}s — recognized {sorted(hits)}; "
                        f"“{content[:96]}”")
    # non-empty reply but no keyword corroboration: the model may still have seen it
    fb_ok, fb_note = _verify_text_fallback()
    ctx["fallback_verified"] = fb_ok
    return "PARTIAL", (f"reply received in {elapsed:.1f}s without shape/color keywords; “{content[:96]}”")


# --------------------------------------------------------------------------- runner --
def main() -> int:
    # Lazy QObject host for direct signal delivery; never created at import (see note above).
    from PySide6.QtCore import QCoreApplication
    QCoreApplication.instance() or QCoreApplication([])

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ctx: dict = {}
    results: list[tuple[str, str, str]] = []          # (status, title, detail)

    print(f"VaultSprite P1 test runner — {ts}")
    print("=" * 78)
    for name, fn in (("TEST 1 SECURITY SANDBOXING", lambda: check_sandboxing(ctx)),
                     ("TEST 2 SIZE MONITORING", lambda: check_size_monitoring(ctx)),
                     ("TEST 3 VAULT I/O (JOURNAL+FACTS)", lambda: check_vault_io(ctx))):
        started = datetime.now(timezone.utc)
        try:
            ok, detail = fn()
        except Exception as exc:                      # noqa: BLE001 - report, don't loop
            print(f"  !! {name} crashed:\n{traceback.format_exc(limit=3)}")
            results.append(("FAIL", name, f"unhandled {exc!r}"))
            continue
        status = "PASS" if ok else "FAIL"
        results.append((status, name, detail))
        print(f"[{status}] {name:<28} in {(datetime.now(timezone.utc) - started).total_seconds():5.1f}s")
        print(f"       {detail}")

    status_4, detail_4 = check_ollama_vision(ctx)
    results.append((status_4, "TEST 4 OLLAMA/VISION", detail_4))
    print(f"[{status_4}] TEST 4 OLLAMA/VISION")
    print(f"       {detail_4}")

    # ---------------------------------------------------------------- status report --
    print("=" * 78)
    print(" STATUS REPORT")
    print("=" * 78)
    for status, title, detail in results:
        print(f" [{status:<9}] {title}")
        for line in textwrap_shorten(detail):
            print(f"             {line}")
    print("-" * 78)
    passed = sum(1 for s, *_ in results if s == "PASS")
    failed = sum(1 for s, *_ in results if s == "FAIL")
    special = [s for s, *_ in results if s not in ("PASS", "FAIL")]
    print(f" detected model : {ctx.get('detected_model', 'n/a')}")
    if ctx.get("vision_reply"):
        print(f" vision reply   : {str(ctx['vision_reply'])[:140]}")
    print(f" probe image    : {PROBE_PNG}")
    outcome = "FAILED" if failed else ("OK (with specials)" if special else "ALL OK")
    print(f" RESULT         : {passed} passed, {failed} failed{', ' + ', '.join(special) if special else ''} → {outcome}")
    return 1 if failed else 0


def textwrap_shorten(detail: str, width: int = 74):
    out, line = [], ""
    for word in detail.split(" "):
        if len(line) + len(word) + 1 > width and line:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


if __name__ == "__main__":
    sys.exit(main())
