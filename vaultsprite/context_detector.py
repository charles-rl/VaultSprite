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
        self.work_keywords: list[str] = [str(k).lower() for k in
                                         section.get("work_keywords", [])]
        self.play_keywords: list[str] = [str(k).lower() for k in
                                         section.get("play_keywords", [])]
        # test seam + fallback probe
        self._probe: Callable[[], str] = probe or default_title_probe

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._current = CONTEXT_UNKNOWN

    # -- classification (pure, unit-testable) ----------------------------------
    def classify(self, title: str) -> Optional[str]:
        """Keyword-substring classification.

        Returns WORK/PLAY on a hit, or ``None`` when the title matches no
        bucket (incl. empty titles). The poller keeps its last-known context
        on ``None``, per the extraction doc's unknown-app rule; only an explicit
        switch between buckets re-emits.
        """
        low = (title or "").lower()
        if not low.strip():
            return None
        is_work = any(k in low for k in self.work_keywords)
        is_play = any(k in low for k in self.play_keywords)
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
        context = self.classify(title)
        if context is None:      # unknown app → keep last-known context
            return
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
