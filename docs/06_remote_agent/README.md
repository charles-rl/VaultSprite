# Module 6 — Screen Vision & Remote Ollama Client

## 1. Module Overview & Objective

Captures a downscaled screenshot of the desktop, base64-encodes it, and builds an OpenAI-compatible `/v1/chat/completions` payload (text + `image_url` with a `data:` URI) to dispatch to a **remote H100 Ollama endpoint** (`http://<H100_IP>:11434/v1/chat/completions`, Qwen 27B). Runs asynchronously so the GUI/FSM never blocks.

Maps to **Module 6** of `Implementation Outline.md`; produces `remote_agent.py`.

Extraction source: **`Koishi007/koishi-ai-pet`** — `pet/agent/screen_reader.py` (capture/downscale/base64) + `pet/brain/llm_client.py` (client construction) + `pet/brain/context_builder.py` (message/payload assembly).

> **Reality check vs. the outline**: koishi uses the **`openai` SDK (sync client)** run inside a `QThread`, **not** an aiohttp/async HTTP client. There is no async HTTP path in the repo. For `remote_agent.py` you can (a) keep the sync `openai` client + worker thread (lowest friction, matches reference), or (b) write an `httpx.AsyncClient` wrapper fresh. Both options are covered in §5.

## 2. Minimum Required Libraries

| Package | Why |
|---|---|
| `Pillow` (`pillow`) | `Image.resize(LANCZOS)`, JPEG encode, RGBA→RGB |
| `mss` | Fast multi-monitor screen capture (`sct.grab`) |
| `openai` (Python SDK) | OpenAI-compatible chat completions against Ollama's `/v1` endpoint |
| `httpx` | `httpx.Timeout` config passed to the openai client (koishi pattern) |
| `PySide6` | `QThread` + `QObject`/`Signal` for background dispatch |

No system drivers.

## 3. Source Code Extraction (Verbatim)

### 3.1 Screen capture + downscale + base64 — `pet/agent/screen_reader.py`

```python
import base64
import io
from typing import Optional

from PIL import Image
import mss

from pet.config import config


class ScreenReader:
    def __init__(self):
        self._enabled = False

    def enable(self):
        self._enabled = True

    def disable(self):
        self._enabled = False

    def capture_fullscreen(self, all_screens: bool = False) -> Optional[Image.Image]:
        if not self._enabled:
            return None
        sct = mss.mss()
        try:
            monitor_index = 0 if all_screens else 1
            sct_img = sct.grab(sct.monitors[monitor_index])
            return Image.frombytes(
                "RGB", sct_img.size, sct_img.bgra, "raw", "BGRX"
            )
        except Exception as e:
            return None
        finally:
            sct.close()

    def capture_area(self, x: int, y: int, width: int, height: int) -> Optional[Image.Image]:
        if not self._enabled:
            return None
        sct = mss.mss()
        try:
            sct_img = sct.grab({"top": y, "left": x, "width": width, "height": height})
            return Image.frombytes(
                "RGB", sct_img.size, sct_img.bgra, "raw", "BGRX"
            )
        except Exception as e:
            return None
        finally:
            sct.close()

    def prepare_image(
        self,
        image: Optional[Image.Image] = None,
        vision_scale: float = 1.0,
        min_px: int = 1536,
    ) -> Optional[str]:
        if image is None:
            image = self.capture_fullscreen()
        if image is None:
            return None
        if vision_scale < 1.0:
            w, h = image.size
            new_w, new_h = int(w * vision_scale), int(h * vision_scale)
            if max(new_w, new_h) < min_px:
                ratio = min_px / max(new_w, new_h)
                new_w, new_h = int(new_w * ratio), int(new_h * ratio)
            image = image.resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        fmt = config.SCREENSHOT_FORMAT or "jpeg"
        if fmt == "png":
            image.save(buf, format="PNG")
        else:
            # JPEG 压缩，保存为 RGB 避免 RGBA → JPEG 异常
            if image.mode == "RGBA":
                image = image.convert("RGB")
            image.save(buf, format="JPEG", quality=95)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
```

### 3.2 LLM client construction — `pet/brain/llm_client.py`

```python
import threading

import httpx
from openai import OpenAI
from pet.config import config


class LLMClient:

    def __init__(self):
        self._client: OpenAI | None = None
        self._model: str | None = None
        self._lock = threading.RLock()
        self._build()

    @staticmethod
    def _make_timeout() -> httpx.Timeout:
        return httpx.Timeout(
            connect=10.0,
            read=config.LLM_TIMEOUT,
            write=10.0,
            pool=5.0,
        )

    def _build(self):
        brain = config.BRAIN or "local"
        key = config.LLM_KEY
        url = config.LLM_URL
        model = config.LLM_MODEL

        if brain == "ollama":
            self._client = OpenAI(
                api_key="ollama",
                base_url=config.OLLAMA_BASE_URL,
                timeout=self._make_timeout(),
            )
            self._model = model or "llama3.2"
        elif brain == "api" and key:
            self._client = OpenAI(
                api_key=key,
                base_url=url or "",
                timeout=self._make_timeout(),
            )
            self._model = model
        else:
            self._client = None

    def rebuild(self):
        with self._lock:
            self._build()

    @property
    def client(self) -> OpenAI | None:
        return self._client

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def has_vision(self) -> bool:
        return self._client is not None and config.VISION_ENABLED

    def __bool__(self):
        return self._client is not None
```

### 3.3 Payload/message assembly — `pet/brain/context_builder.py`

```python
def build_autonomous_decide(self, window_context: str, screenshot: bool = True) -> list[dict]:
    base64_img = self._prepare_image() if screenshot else None
    vision = base64_img is not None
    mode = "autonomous_vision" if vision else "autonomous_non_vision"
    memory_search_text = self._extract_window_titles(window_context)
    system = self._build_system(mode, "autonomous", user_message=memory_search_text)
    return self._build_multi_turn_autonomous(system, window_context, vision, base64_img)

# image-bearing user message (the multimodal payload core):
if vision:
    mime = self._image_mime()
    messages.append({"role": "user", "content": [
        {"type": "text", "text": current_prompt},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64_img}"}},
    ]})
else:
    messages.append({"role": "user", "content": current_prompt})

def _prepare_image(self) -> Optional[str]:
    if not config.VISION_ENABLED or not self._screen_reader:
        return None
    return self._screen_reader.prepare_image(vision_scale=config.VISION_SCALE)

def _image_mime(self) -> str:
    fmt = getattr(config, "SCREENSHOT_FORMAT", "jpeg") or "jpeg"
    if fmt == "png":
        return "image/png"
    return "image/jpeg"
```

### 3.4 Background execution (QThread pattern) — `pet/agent/pet_agent.py` (abbreviated)

```python
class BrainWorker(QObject):
    finished = Signal(object)
    error = Signal(str)

    def run(self, fn, *args):
        try:
            result = fn(*args)
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))

# per-call thread (koishi `_async_brain`):
thread = QThread()
worker = BrainWorker()
worker.moveToThread(thread)
thread.started.connect(lambda: worker.run(fn, *args))
worker.finished.connect(lambda r: (result_cb(r), thread.quit()))
worker.error.connect(lambda e: (error_cb(e), thread.quit()))
thread.finished.connect(worker.deleteLater)
thread.start()
```

## 4. Logic & Data Flow Breakdown

1. **Capture** (§3.1): `capture_fullscreen` opens an `mss` session, grabs monitor index `1` (primary) or `0` (all screens), and converts the BGRA buffer to a PIL `Image` (`Image.frombytes("RGB", size, bgra, "raw", "BGRX")`). `capture_area` grabs a sub-region by dict.
2. **Downscale + encode** (§3.1 `prepare_image`): scale = `vision_scale` (koishi default 0.7 — a 1920×1080 screen → ~1344×756; **note**: koishi's `min_px=1536` floor makes output *larger* than our 1024×768 target — parameterize or drop the floor). Resize is `Image.LANCZOS`. Then RGBA→RGB (JPEG can't hold alpha) and `image.save(buf, format="JPEG", quality=95)` into a `BytesIO`; result `base64.b64encode(...).decode()` is the string embedded in the payload.
3. **Client** (§3.2): `LLMClient` builds an `openai.OpenAI` instance pointed at the Ollama base URL with the dummy key `"ollama"` (Ollama's OpenAI-compat layer ignores auth). Timeout is `httpx.Timeout(connect=10, read=LLM_TIMEOUT, write=10, pool=5)` — critical for a remote H100 box where reads can be slow. `rebuild()` re-configures at runtime under an RLock.
4. **Payload** (§3.3): the multimodal user message is a content list: `[{"type":"text","text":...}, {"type":"image_url","image_url":{"url": "data:image/jpeg;base64,<b64>"}}]`. This is the exact shape Ollama's `/v1/chat/completions` accepts for Qwen-VL-class models. A `system` message precedes it; for the outline's Qwen 27B, ensure the model name is configurable (`LLM_MODEL`) and prompt includes the window/task context.
5. **Async execution** (§3.4): koishi runs the sync call inside a fresh `QThread` per request (`BrainWorker` + `moveToThread`), marshaling results back via Qt queued signals. Screenshot capture also happens in that thread. This keeps the UI responsive despite a blocking network call.

## 5. Refactoring & Integration Notes

Target: `remote_agent.py` exposing a single `RemoteAgent` that dispatches visual context + prompt to the remote Ollama endpoint asynchronously. The **IP must come from env/config** (`OLLAMA_BASE_URL`), never hardcoded.

Step-by-step:

1. **Configuration** (config.py):
   ```python
   OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
   OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:27b")     # Qwen 27B per outline
   LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "120"))          # remote H100 → generous read timeout
   VISION_ENABLED = bool(os.getenv("VISION_ENABLED", "1"))
   SCREENSHOT_MAX_PX = 1024 * 768                               # our downscale target
   ```
   Base URL uses `/v1` (the client appends `/chat/completions`); the outline's `http://<H100_IP>:11434/v1/chat/completions` is the same URL with the path written out.
2. **Port `ScreenReader`** nearly verbatim, but change `prepare_image` to target **~1024×768**: drop the `min_px=1536` floor (or set `min_px=0`) and fix `quality=80` for a smaller JPEG payload; keep the LANCZOS + RGBA→RGB + base64 pipeline.
3. **Port `LLMClient`** verbatim; default `OLLAMA_MODEL` to a Qwen 27B id and make the read timeout large. Keep the `rebuild()`/RLock pattern so connection settings can change at runtime.
4. **Choose the async transport**:
   - *(a) Matches reference, least risk:* keep the sync `openai` client but call it inside a `QThread` per request (koishi §3.4). Wrap in a `Promise`-style API: `ask(prompt, screenshot=True, callback=...)`.
   - *(b) True async:* if the outline's "asynchronously" must be literal, build an `httpx.AsyncClient` (`httpx.AsyncClient(base_url=..., timeout=...)`) and `POST /chat/completions` with `{"model": ..., "messages": [...]}`; run it via `asyncio` + `QEventLoop` integration, or keep an `asyncio` loop in a dedicated thread. This is new code (no repo reference), so keep it thin.
5. **Message builder**: port `_build_multi_turn_*`'s vision branch (the `data:` URI content-list) and the `_image_mime()` helper. Include `window_context` text from M5 in the prompt so the agent knows what the user is doing.
6. **Integration**: connect `RemoteAgent.response_ready(str)` and `RemoteAgent.error(str)` signals to the overlay's speech bubble / logging. Gate capture on `VISION_ENABLED`; never capture while the pet itself is foreground (skip via the overlay's `winId`).
7. **Testing**: mock `httpx`/`openai` (no network) — assert payload JSON shape (model, system+user messages, `data:image/jpeg;base64,...` content list), correct base64 round-trip, timeout config, and that `RemoteAgent.ask` returns without blocking the caller (fire-and-forget + callback). Headless-safe (no real capture needed).

## 6. Source Files (Reference Copies)

Full verbatim copies from `Koishi007/koishi-ai-pet`, kept locally:

| File | Purpose |
|---|---|
| `source/screen_reader.py` | `ScreenReader` — mss capture, LANCZOS downscale, RGBA→RGB, JPEG→base64 (the vision pipeline) |
| `source/llm_client.py` | `LLMClient` — openai client construction (Ollama base URL, dummy key), `httpx.Timeout`, `rebuild()`/RLock |
| `source/context_builder.py` | Message assembly: system prompts, `data:`-URI image content-list, `_image_mime`/`_prepare_image` |
| `source/pet_agent.py` | `BrainWorker` + `_async_brain` — the per-call QThread execution pattern |
| `source/behavior.py` | `_create_completion`/`_llm_call`/`_llm_call_stream` — the actual model call layer (400-fallback, `extra_body`) |
| `source/prompts.py` | System/chat/vision prompt templates |
| `source/config.py` | `OLLAMA_BASE_URL`/`OLLAMA_MODEL`/`VISION_*` config keys — port into our config |

> The reference is sync `openai`-SDK-in-a-QThread (not async HTTP). For a literal async client, build `httpx.AsyncClient` fresh per README §5.4b.
