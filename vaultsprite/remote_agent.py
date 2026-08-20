"""Screen vision & remote Ollama client (Module 6).

Captures a downscaled screenshot (~1024x768 JPEG), base64-encodes it into an
OpenAI-compatible ``/chat/completions`` multimodal payload, and dispatches it to
a **remote** Ollama endpoint. The transport is the reference koishi pattern: the
sync ``openai`` SDK runs inside a fresh per-call ``QThread`` so the GUI never
blocks; results return via queued signals (fire-and-forget ``ask()``).

The H100 IP/base URL comes from config/env (``OLLAMA_BASE_URL``) — never
hardcoded. Capture is skipped while the pet overlay itself is foreground.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import sys
import threading
import time
from typing import Callable, Optional, Union

from PySide6.QtCore import QObject, QThread, Signal, Slot

from .config import Config, load_config

logger = logging.getLogger(__name__)

# --- optional heavy deps: guarded so module imports stay cheap on the dev box ----
try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover - Pillow is a hard dep via pyproject
    logger.debug("Pillow unavailable (%s); vision disabled", exc)
    Image = None

try:
    import mss
except ImportError as exc:  # pragma: no cover - platform gate (needs X11/DXGI)
    logger.debug("mss unavailable (%s); capture disabled", exc)
    mss = None

try:
    import httpx
    from openai import (OpenAI, APIConnectionError, APITimeoutError, InternalServerError,
                        RateLimitError)
except ImportError as exc:  # pragma: no cover
    logger.debug("openai/httpx unavailable (%s); agent disabled", exc)
    OpenAI = None


def _transient_llm_errors() -> tuple[type[Exception], ...]:
    """Exception types worth ONE retry after a backoff — transport-level blips only.

    Resolved lazily (per call) so the module stays importable and test-stubbable whether
    or not the openai SDK is present/complete: any name this SDK build doesn't export is
    skipped, an absent SDK yields () → single-attempt behavior, identical to before."""
    if OpenAI is None:
        return ()
    try:
        from openai import MaxRetriesExceededError     # not exported by every SDK version
    except ImportError:
        MaxRetriesExceededError = None
    candidates = (APIConnectionError, APITimeoutError, InternalServerError,
                  RateLimitError, MaxRetriesExceededError)
    return tuple(t for t in candidates if t is not None)


def _retryable_error(exc: BaseException) -> bool:
    """True when a failed LLM call is worth one retry after a backoff.

    Prefers the SDK's own transport-level exception set; falls back to httpx's
    ``TransportError`` (the base class of APIConnectionError) so a partial/oddly-named
    SDK build still retries connection blips instead of silently going single-attempt."""
    transient = _transient_llm_errors()
    if transient and isinstance(exc, transient):
        return True
    try:
        import httpx as _httpx
        return bool(_httpx) and isinstance(exc, _httpx.TransportError)
    except Exception:  # pragma: no cover - httpx is a hard dep
        return False


class _BrainThread(QThread):
    """Runs the blocking ``fn`` off the GUI thread and reports via signals.

    Subclassing ``QThread`` and overriding ``run()`` is the robust way to do this here:
    ``run()`` always executes in the new OS thread, so the whole blocking capture + LLM
    call genuinely runs off the GUI thread. The ``result``/``error`` signals are emitted
    from that worker thread and delivered back to the GUI thread via queued connections
    (the receiver lives in the main thread, which runs ``app.exec()``).

    The previous approaches were subtly broken on this PySide6 build and caused the "ask
    what I see freezes" bug (pet went to a black background and stopped animating):
    - ``thread.started.connect(lambda: worker.run(fn))`` ran the lambda — and therefore the
      whole blocking ``fn`` — on the **GUI** thread.
    - ``thread.started.connect(worker.start)`` (bound slot + ``moveToThread``) never ran
      reliably without a worker-thread event loop, so replies were silently lost.

    NOTE: the custom signal is named ``result``, NOT ``finished`` — shadowing Qt's built-in
    no-arg ``QThread.finished()`` broke the cleanup connection (``thread.finished.connect(
    thread.deleteLater)``), which only ran on the success path and leaked one QThread per
    failed request. The built-in ``finished`` signal fires post-run in every case, so it is
    the single place worker threads get deleted (see ``RemoteAgent.ask``).
    """

    result = Signal(object)
    error = Signal(str)

    def __init__(self, fn: Callable[[], object], parent: Optional[QObject] = None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self.result.emit(self._fn())
        except Exception as exc:  # noqa: BLE001 - surfaced on `error` signal
            logger.exception("brain worker failed")
            self.error.emit(str(exc))


class RemoteAgent(QObject):
    """Asynchronous (thread-hopping) dispatch of screen+prompt to remote Ollama."""

    response_ready = Signal(str)   # assistant text reply
    error = Signal(str)            # human-readable failure reason

    def __init__(self, config: Union[Config, None] = None):
        super().__init__()
        self.config = config or load_config()
        c = self.config.section("remote")

        base_url = str(c.get("ollama_base_url", "http://localhost:11434/v1"))
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.model = str(c.get(
            "ollama_model", "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q8_K_XL"))
        timeout = float(c.get("llm_timeout_s", 120))   # generous for a remote H100
        self.vision_enabled = bool(c.get("vision_enabled", True))
        self._max_tokens = int(c.get("max_tokens", 4096))   # cap generation length (koishi parity)

        shot = c.get("screenshot", {}) or {}
        self._max_w = int(shot.get("max_w", 1024))
        self._max_h = int(shot.get("max_h", 768))
        self._quality = int(shot.get("quality", 80))
        self._fmt = str(shot.get("format", "jpeg")).lower()

        self.system_prompt = str(c.get(
            "system_prompt",
            "You are a small desktop mascot. Reply in one short sentence."
        )).strip()
        # the pet's own winId, so we never screenshot ourselves (set by main)
        self._overlay_winid: Optional[object] = None

        # -- request tracking (every access on the GUI thread; workers only emit) ---
        self._in_flight = False       # one concurrent ask max; extra asks are dropped
        self.last_error_at = 0.0      # monotonic() of last failed call (autonomous-loop cooldown gate)

        client = None
        if OpenAI is not None:
            try:
                timeout_obj = httpx.Timeout(connect=10.0, read=timeout,
                                            write=10.0, pool=5.0)
                # Ollama's OpenAI-compat layer ignores auth; dummy key required.
                client = OpenAI(api_key="ollama", base_url=self.base_url,
                                timeout=timeout_obj)
            except Exception as exc:  # pragma: no cover - constructor hiccups
                logger.warning("failed to build Ollama client: %s", exc)
        self._client = client

    @property
    def enabled(self) -> bool:
        """True when an LLM client exists (openai SDK installed + constructed)."""
        return self._client is not None

    def set_overlay_winid(self, winid):
        self._overlay_winid = winid

    # --------------------------------------------------------------------------
    # screen capture pipeline (koishi ScreenReader port) — 1024x768 JPEG base64
    # --------------------------------------------------------------------------
    def _self_is_foreground(self) -> bool:
        if sys.platform != "win32" or self._overlay_winid is None:
            return False
        try:  # pragma: no cover - Windows only
            import win32gui
            fg = win32gui.GetForegroundWindow()
            own = int(self._overlay_winid) & 0xFFFFFFFF
            return bool(fg and (fg == own))
        except Exception:
            return False

    def capture_screenshot_b64(self) -> Optional[str]:
        """Grab the primary screen, downscale to ~max_w x max_h, JPEG base64."""
        if Image is None or mss is None:
            logger.info("vision disabled (Pillow/mss unavailable)")
            return None
        try:
            sct_cls = getattr(mss, "MSS", None) or mss.mss   # new API, old fallback
            with sct_cls() as sct:
                monitor = sct.monitors[1]   # index 0 = virtual combined; 1 = primary
                shot = sct.grab(monitor)
                image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        except Exception as exc:
            logger.warning("screenshot capture failed: %s", exc)
            return None

        w, h = image.size
        scale = min(self._max_w / w, self._max_h / h)   # fit inside the box
        if scale < 1.0:
            image = image.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        if self._fmt == "png":
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            mime = "image/png"
        else:
            if image.mode != "RGB":   # JPEG cannot carry alpha
                image = image.convert("RGB")
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=self._quality)
            mime = "image/jpeg"
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        logger.info("screenshot captured: %dx%d -> %d bytes (%s)",
                    w, h, len(b64), self._fmt)
        return f"data:{mime};base64,{b64}"

    # --------------------------------------------------------------------------
    # payload building (koishi context_builder port) — data-URI content list
    # --------------------------------------------------------------------------
    def build_messages(self, prompt: str, window_context: str = "",
                       screenshot: bool = True) -> list[dict]:
        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
        ]
        user_text = prompt if not window_context else f"{prompt}\n\nActive window context:\n{window_context}"

        data_uri = None
        if screenshot and self.vision_enabled:
            data_uri = self.capture_screenshot_b64()

        if data_uri is not None:
            messages.append({"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]})
        else:
            messages.append({"role": "user", "content": user_text})
        return messages

    # --------------------------------------------------------------------------
    # async dispatch (sync openai SDK in a per-call QThread) — fire & forget
    # --------------------------------------------------------------------------
    @property
    def in_flight(self) -> bool:
        """True while a dispatched ask has not been delivered yet (either outcome)."""
        return self._in_flight

    def cancel_inflight(self):
        """GUI-thread escape hatch for App's watchdog timeout.

        The worker QThread itself cannot be cancelled safely (it may be inside the SDK),
        so this only clears the gate; when the late worker finally delivers, its queued
        signals will simply arrive after App has already moved on and the one-in-flight
        invariant is restored."""
        self._in_flight = False

    def ask(self, prompt: str, window_context: str = "", screenshot: bool = True):
        """Dispatch a request off-thread; reply arrives via ``response_ready``.

        Everything blocking — the self-foreground check, mss capture + resize/encode,
        payload building AND the LLM call — runs inside the per-call QThread worker, so
        ``ask()`` itself returns in microseconds and the GUI never blocks on a slow
        screen grab or first-inference model load.

        At most ONE request may be in flight: while an earlier ask has not delivered
        (success *or* error), further asks are dropped with a log line — otherwise the
        autonomous tick plus user clicks would stack concurrent 120s LLM calls against
        a slow or cold-starting remote model."""
        if not self.enabled:
            self.error.emit("LLM client unavailable (openai SDK missing or build failed)")
            return
        if self._in_flight:
            logger.debug("vision ask dropped — one already in flight")
            return
        self._in_flight = True

        def _call() -> str:
            # Diagnostic: confirm the blocking work runs OFF the GUI thread. The GUI thread
            # is the one that created this RemoteAgent; if these ids ever match (or this log
            # line blocks), the "ask what I see" freeze is a real GUI-thread/GIL stall rather
            # than a slow-but-async model reply — investigate, don't assume.
            logger.info("vision worker starting on thread=%s (gui thread=%s)",
                        threading.get_ident(), threading.main_thread().ident)
            t0 = time.monotonic()
            take_shot = screenshot
            if take_shot and self._self_is_foreground():
                logger.info("pet is foreground; skipping screenshot capture")
                take_shot = False
            messages = self.build_messages(prompt, window_context=window_context,
                                           screenshot=take_shot)
            # One retry after a short backoff on TRANSPORT-level failures only (dead
            # connection / DNS blip / timeout — the dev-tunnel class of hiccups). Semantic
            # errors (4xx auth/model-not-found) are deterministic: no point re-asking.
            last_exc: Optional[Exception] = None
            for attempt in range(2):
                try:
                    resp = self._client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        temperature=0.7,
                        max_tokens=int(self._max_tokens),   # cap 27B rambles (koishi parity)
                    )
                    last_exc = None
                    break
                except Exception as exc:
                    if not _retryable_error(exc) or attempt == 1:
                        raise   # semantic errors (4xx class) and exhausted retry → surface once
                    logger.warning("LLM transport error (%s); retrying once", type(exc).__name__)
                    time.sleep(2.0)
            assert last_exc is None   # loop only exits via break or raise
            text = (resp.choices[0].message.content or "").strip()
            logger.info("vision worker done thread=%s in %.1fs (%d chars)",
                        threading.get_ident(), time.monotonic() - t0, len(text))
            return text

        thread = _BrainThread(_call, parent=self)
        thread.result.connect(self._deliver)               # queued to the GUI thread
        thread.error.connect(self._on_worker_error)        # queued to the GUI thread
        # built-in (no-arg) finished — fires post-run in EVERY case, success or failure.
        # This is the single cleanup point: deleting here means no failed request leaks a
        # QThread (the old custom-shadowed `finished` only connected _deliver on success).
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _on_worker_error(self, msg: str):
        """Worker exception surfaced off-thread: log + re-emit as our public error."""
        logger.warning("LLM request failed: %s", msg)
        self.last_error_at = time.monotonic()
        try:
            self.error.emit(msg)
        except RuntimeError as exc:       # teardown race: QObject already deleted
            logger.debug("error emit failed: %s", exc)
        finally:
            self._in_flight = False

    def _deliver(self, text: str):
        logger.info("LLM reply (%d chars): %r", len(text), text[:120])
        self._in_flight = False
        try:
            self.response_ready.emit(text)
        except RuntimeError as exc:       # teardown race: QObject already deleted
            logger.debug("response emit failed: %s", exc)
