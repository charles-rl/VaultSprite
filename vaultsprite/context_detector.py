"""Contextual focus detector (Module 5) — Work vs. Play classification.

Polls the foreground window title and classifies it via keyword lists,
emitting ``context_changed(str)`` only on a real change. The PyWinCtl import
is guarded: the library raises ``NotImplementedError``/``ImportError`` off of
Windows, so headless Linux dev runs degrade to ``UNKNOWN`` (tests inject a
fake probe instead).
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Callable, Optional, Union

from PySide6.QtCore import QObject, Signal

from .config import Config, load_config

logger = logging.getLogger(__name__)

CONTEXT_WORK = "WORK"
CONTEXT_PLAY = "PLAY"
CONTEXT_UNKNOWN = "UNKNOWN"

try:  # Windows-only in practice; fails fast on the Linux dev box by design
    if sys.platform == "win32":
        import pywinctl as _pwc   # noqa: N813
    else:
        _pwc = None
except (ImportError, NotImplementedError) as exc:  # pragma: no cover - platform gate
    logger.debug("pywinctl unavailable (%s); context detection disabled", exc)
    _pwc = None


def default_title_probe():
    """Foreground-window probe for the detector's poll loop.

    Platform contract (the detector normalizes any of these shapes):
      * Windows — ``(hwnd, title, app_name)``; ``app_name`` is the real exe name
        resolved from the window PID via pywinctl/WMI, so a browser tab titled with a
        'work' word can't outvote the fact that it's chrome.exe.
      * elsewhere / no probe — plain ``""`` (legacy title-only shape; test probes may
        also inject either a bare title string or ``(title, app)`` pairs)."""
    if _pwc is None or sys.platform != "win32":
        return ""
    try:
        win = _pwc.getActiveWindow()
    except Exception as exc:  # probe must never kill the poller thread
        logger.debug("active window probe failed: %s", exc)
        return "", "", 0
    if not win:
        return "", "", 0
    try:
        title = (win.title or "").strip()
    except Exception as exc:
        logger.debug("title read failed: %s", exc)
        title = ""
    try:
        app = str(win.getAppName() or "").strip()   # WMI PID→exe name; fine at 5 s cadence
    except Exception as exc:
        logger.debug("app-name probe failed: %s", exc)
        app = ""
    try:
        hwnd = int(win.getHandle() or 0)
    except Exception:
        hwnd = 0
    return (hwnd, title, app)


class ContextDetector(QObject):
    """5-second foreground-window poller: app-name buckets + title-keyword classification."""

    context_changed = Signal(str)   # "WORK" | "PLAY" | "UNKNOWN"

    def __init__(self, config: Union[Config, None] = None,
                 probe: Optional[Callable] = None):
        super().__init__()
        self.config = config or load_config()
        section = self.config.section("context")
        self._poll_ms = int(section.get("poll_ms", 5000))
        # how many consecutive no-match polls before the pet stops assuming a bucket —
        # without this, once a title matches WORK (or PLAY) and then goes to an
        # unclassified app the context would stick forever ("always switched to work")
        self._unknown_decay_polls = int(section.get("unknown_decay_polls", 6))
        self.work_keywords: list[str] = [str(k).lower() for k in
                                           section.get("work_keywords", [])]
        self.play_keywords: list[str] = [str(k).lower() for k in
                                           section.get("play_keywords", [])]
        # app-name channel (Windows only, filled by the default probe): exact exe-name
        # buckets win over title keywords — chrome.exe is PLAY whatever the tab says.
        self.work_apps: list[str] = [str(a).lower() for a in
                                     section.get("work_apps", []) or []]
        self.play_apps: list[str] = [str(a).lower() for a in
                                     section.get("play_apps", []) or []]
        # test seam + fallback probe
        self._probe: Callable = probe or default_title_probe
        # the pet's own overlay HWND (App wires it); when our window briefly holds the
        # foreground we must not classify ourselves as some unknown app.
        self._own_winid: int = 0

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._current = CONTEXT_UNKNOWN
        self._unknown_streak = 0
        self.last_title: str = ""      # last raw foreground title (debug/vision prompts)

    def set_overlay_winid(self, winid):
        """App wiring: our own window id so the probe can filter it out."""
        try:
            self._own_winid = int(winid or 0) & 0xFFFFFFFF
        except (TypeError, ValueError):
            self._own_winid = 0

    # -- probe normalization -----------------------------------------------------
    @staticmethod
    def _read_probe_context(raw) -> tuple[str, str]:
        """Normalize a probe result to ``(title, app_name)``.

        Accepts every shape in use: bare title string (legacy/test probes),
        ``("", "")`` off-Windows, 2-tuples from injected tests, and the real Windows
        3-tuple ``(hwnd, title, app)``."""
        if isinstance(raw, str):
            return (raw or ""), ""
        try:
            seq = list(raw)
        except TypeError:               # anything uniterable → treat as no context
            return "", ""
        if len(seq) == 3 and not isinstance(seq[0], str):   # Windows shape: (hwnd, title, app) — hwnd first
            return (((seq[1] or "").strip() if isinstance(seq[1], str) else ""),
                    ((seq[2] or "").strip() if isinstance(seq[2], str) else ""))
        if len(seq) >= 2:               # legacy 2-tuple (title, app?)
            t = seq[0] if isinstance(seq[0], str) else ""
            a = seq[1] if isinstance(seq[1], str) else ""
            return (t or "").strip(), (a or "").strip()
        return "", ""

    def _poll_probe(self):
        """One probe call, normalized; returns ``None`` when the pet itself is foreground."""
        try:
            raw = self._probe()
        except Exception as exc:
            logger.debug("probe raised %s", exc)
            return ("", "")
        title, app = self._read_probe_context(raw)
        if (self._own_winid and isinstance(raw, tuple) and len(raw) == 3
                and (int(raw[0] or 0) & 0xFFFFFFFF) == self._own_winid):
            return None                 # we are the foreground window → ignore this poll
        return (title, app)

    # -- classification (pure, unit-testable) ----------------------------------
    @staticmethod
    def _keyword_in(low_title: str, keyword: str) -> bool:
        """Whole-word keyword match.

        A keyword like "word" must not fire inside "world"/"broadway"; but the title's
        own separators (spaces, dashes, colons, parens, dots — e.g. 'Visual Studio Code
        — file.py') delimit words for us too. So: scan for the literal keyword and
        require that no letter/digit touches either side of it."""
        start = 0
        while True:
            i = low_title.find(keyword, start)
            if i < 0:
                return False
            before_ok = i == 0 or not (low_title[i - 1].isalnum())
            j = i + len(keyword)
            after_ok = j >= len(low_title) or not (low_title[j].isalnum())
            if before_ok and after_ok:
                return True
            start = i + 1

    def classify(self, title: str = "", app_name: str = "") -> Optional[str]:
        """Classify a foreground window into WORK/PLAY or ``None``.

        Channel 1 (Windows): the real exe/app NAME wins — exact bucket match on
        ``context.work_apps`` / ``context.play_apps``, so chrome.exe is PLAY regardless of
        what its tab title says (title keywords can't outvote the process). Channel 2:
        whole-word keywords over the window TITLE — still authoritative wherever no app
        name is available (non-Windows, unresolvable process). Returns None when nothing
        matches (incl. empty input); unknown apps don't clear the last-known bucket
        immediately — after ``context.unknown_decay_polls`` consecutive unknown polls the
        poller emits UNKNOWN instead of sticking to the old bucket forever."""
        app = (app_name or "").strip().lower()
        if app:
            base = app[:-4] if app.endswith(".exe") else app
            for name in self.work_apps:
                if name == app or name == base:
                    return CONTEXT_WORK
            for name in self.play_apps:
                if name == app or name == base:
                    return CONTEXT_PLAY
        low = (title or "").lower()
        if not low.strip():
            return None
        is_work = any(self._keyword_in(low, k) for k in self.work_keywords)
        is_play = any(self._keyword_in(low, k) for k in self.play_keywords)
        if is_work:
            return CONTEXT_WORK
        if is_play:
            return CONTEXT_PLAY
        return None

    # -- lifecycle ---------------------------------------------------------------
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="context-detector",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        # Only drop the reference once the thread is actually gone. If a probe call is
        # hung past the join timeout, keeping `_thread` set means start()'s liveness guard
        # refuses to spawn a SECOND poller on top of the stuck one (the old code nulled it
        # unconditionally → stop/start cycles could accumulate runaway threads).
        if t is None or not t.is_alive():
            self._thread = None

    @property
    def current_context(self) -> str:
        return self._current

    def is_available(self) -> bool:
        """True when a real title probe exists (not the Linux no-op)."""
        return _pwc is not None

    # -- poll loop ---------------------------------------------------------------
    def _run(self):
        while not self._stop_event.is_set():
            ctx = self._poll_probe()       # (title, app) or None (we are foreground)
            if ctx is not None:
                self._maybe_change(ctx[0], ctx[1])
            self._stop_event.wait(self._poll_ms / 1000.0)

    def _emit_context(self, context: str):
        """Emit on a real change — safe from the worker thread mid-teardown.

        The poller runs in a daemon thread; emitting after App has destroyed us raises
        RuntimeError (or worse). Check the stop flag and swallow teardown errors so a
        shutdown race can never surface as an exception in a non-GUI thread."""
        if self._stop_event.is_set():
            return
        try:
            self.context_changed.emit(context)   # queued across threads
        except RuntimeError as exc:              # pragma: no cover - QObject deleted
            logger.debug("context emit failed during teardown: %s", exc)

    def _maybe_change(self, title: str, app_name: str = ""):
        self.last_title = (title or "").strip()     # kept for vision prompts + debug trail
        context = self.classify(title, app_name)
        if context is None:      # unknown app → hold, then decay after N consecutive polls
            self._unknown_streak += 1
            if self._current != CONTEXT_UNKNOWN and \
                    self._unknown_streak >= self._unknown_decay_polls:
                logger.info("context change: %s -> UNKNOWN (%d unknown polls; title=%r)",
                            self._current, self._unknown_streak, (title or "")[:60])
                self._current = CONTEXT_UNKNOWN     # stop assuming the old bucket forever
                self._emit_context(CONTEXT_UNKNOWN)
            return
        self._unknown_streak = 0
        if context != self._current:
            logger.info("context change: %s -> %s (title=%r app=%r)",
                        self._current, context, (title or "")[:60], (app_name or "")[:40])
            self._current = context
            self._emit_context(context)

    def poll_once(self):
        """Run a single poll cycle synchronously (tests, one-shot diagnostics)."""
        ctx = self._poll_probe()
        if ctx is not None:
            self._maybe_change(ctx[0], ctx[1])
        return self._current
