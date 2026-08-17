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


def default_title_probe() -> str:
    """Return the current foreground window title ('' when none/unavailable)."""
    if _pwc is None:
        return ""
    try:
        return _pwc.getActiveWindowTitle() or ""
    except Exception as exc:  # probe must never kill the poller thread
        logger.debug("title probe failed: %s", exc)
        return ""


class ContextDetector(QObject):
    """5-second foreground-title poller with keyword classification."""

    context_changed = Signal(str)   # "WORK" | "PLAY" | "UNKNOWN"

    def __init__(self, config: Union[Config, None] = None,
                 probe: Optional[Callable[[], str]] = None):
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
        # test seam + fallback probe
        self._probe: Callable[[], str] = probe or default_title_probe

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._current = CONTEXT_UNKNOWN
        self._unknown_streak = 0
        self.last_title: str = ""      # last raw foreground title (debug/vision prompts)

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

    def classify(self, title: str) -> Optional[str]:
        """Whole-word keyword classification.

        Returns WORK/PLAY on a hit, ``None`` when no bucket matches (incl. empty
        titles). Unknown apps don't clear the last-known bucket immediately — after
        ``context.unknown_decay_polls`` consecutive unknown polls the poller emits
        UNKNOWN instead of sticking to the old bucket forever."""
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
        if self._thread is not None:
            self._thread.join(timeout=2.0)
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
            try:
                title = (self._probe() or "")
            except Exception as exc:
                logger.debug("probe raised %s", exc)
                title = ""
            self._maybe_change(title)
            self._stop_event.wait(self._poll_ms / 1000.0)

    def _maybe_change(self, title: str):
        self.last_title = (title or "").strip()     # kept for vision prompts + debug trail
        context = self.classify(title)
        if context is None:      # unknown app → hold, then decay after N consecutive polls
            self._unknown_streak += 1
            if self._current != CONTEXT_UNKNOWN and \
                    self._unknown_streak >= self._unknown_decay_polls:
                logger.info("context change: %s -> UNKNOWN (%d unknown polls; title=%r)",
                            self._current, self._unknown_streak, title[:60])
                self._current = CONTEXT_UNKNOWN     # stop assuming the old bucket forever
                self.context_changed.emit(CONTEXT_UNKNOWN)   # queued across threads
            return
        self._unknown_streak = 0
        if context != self._current:
            logger.info("context change: %s -> %s (title=%r)",
                        self._current, context, title[:60])
            self._current = context
            self.context_changed.emit(context)   # queued across threads

    def poll_once(self):
        """Run a single poll cycle synchronously (tests, one-shot diagnostics)."""
        try:
            title = (self._probe() or "")
        except Exception:
            title = ""
        self._maybe_change(title)
        return self._current
