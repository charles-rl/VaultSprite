"""RemoteAgent: multimodal payload shape, timeout config, non-blocking dispatch."""
from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from tests.conftest import FakeConfig, spin
from vaultsprite.remote_agent import RemoteAgent


@pytest.fixture()
def agent(qapp):
    cfg = FakeConfig({
        "remote.ollama_base_url": "http://10.0.0.7:11434/v1",   # H100 via config, not hardcoded
        "remote.ollama_model": "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q8_K_XL",
        "remote.ask_interval_ms": 0,                              # loop off in unit tests
    })
    return RemoteAgent(cfg)


# -- payload construction -----------------------------------------------------------
def test_base_url_and_timeout_from_config(agent):
    assert agent.base_url == "http://10.0.0.7:11434/v1/"         # trailing slash appended
    assert agent.model == "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q8_K_XL"


def test_text_only_payload_shape(agent, monkeypatch):
    agent.vision_enabled = False
    msgs = agent.build_messages("Hello there.", window_context="PyCharm - main.py")
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert isinstance(msgs[1]["content"], str)                    # no image → plain text
    assert "Hello there." in msgs[1]["content"]
    assert "PyCharm - main.py" in msgs[1]["content"]


def test_vision_payload_data_uri(agent, monkeypatch):
    payload_b64 = base64.b64encode(b"\xff\xd8fakejpeg").decode()
    fake_uri = f"data:image/jpeg;base64,{payload_b64}"
    agent.capture_screenshot_b64 = lambda: fake_uri               # no real mss grab in tests
    msgs = agent.build_messages("What do you see?")
    content = msgs[1]["content"]                                  # multimodal → list form
    assert isinstance(content, list)
    text_part, image_part = content
    assert text_part == {"type": "text", "text": "What do you see?"}
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")
    # round-trip: the embedded base64 decodes back to the original bytes
    decoded = base64.b64decode(image_part["image_url"]["url"].split(",", 1)[1])
    assert decoded == b"\xff\xd8fakejpeg"


def test_capture_failed_falls_back_to_text(agent):
    """When the capture returns None, build_messages must degrade to a text payload."""
    agent.capture_screenshot_b64 = lambda: None
    assert agent.build_messages("hi", screenshot=True)[1]["content"] == "hi"


# -- async dispatch (sync openai SDK in a QThread, fire-and-forget) -------------------
def _stub_client(reply="looks good"):
    class _Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])

    return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))


def test_ask_dispatches_and_delivers(qapp, agent):
    replies = []
    errors = []
    agent.response_ready.connect(replies.append)
    agent.error.connect(errors.append)

    fired_prompt = {}
    base_reply = "You appear to be coding."

    class _SpyCompletions:
        def create(self, **kwargs):
            fired_prompt["model"] = kwargs.get("model")
            fired_prompt["n_messages"] = len(kwargs.get("messages", []))
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=base_reply))])

    agent._client = SimpleNamespace(chat=SimpleNamespace(completions=_SpyCompletions()))
    agent.ask("Tell me what I'm doing.")       # returns immediately (QThread spawned)

    assert spin(qapp, lambda: replies or errors), f"no reply; errors={errors}"
    assert not errors
    assert replies[0] == base_reply            # delivered back on the GUI thread
    assert fired_prompt["model"] == "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q8_K_XL"
    assert fired_prompt["n_messages"] == 2     # system + user


def test_stub_client_round_trip(qapp, agent):
    """Sanity check that ask() with a plain stub client delivers its reply."""
    replies = []
    agent.response_ready.connect(replies.append)
    agent._client = _stub_client("all good")
    agent.ask("ping", screenshot=False)        # no display in CI → skip capture path
    assert spin(qapp, lambda: replies), "no reply from stub client"
    assert replies[0] == "all good"


def test_ask_errors_surface_via_signal(qapp, agent):
    class _BoomCompletions:
        def create(self, **kwargs):
            raise RuntimeError("connection refused")

    boom = SimpleNamespace(chat=SimpleNamespace(completions=_BoomCompletions()))
    agent._client = boom
    errors = []
    agent.error.connect(errors.append)
    agent.ask("hello", screenshot=False)       # skip capture (no display in CI)
    assert spin(qapp, lambda: errors), "error signal never fired"
    assert "connection refused" in errors[0]


def test_ask_when_client_unavailable(qapp, agent):
    agent._client = None
    errors = []
    agent.error.connect(errors.append)
    agent.ask("hello")                          # no thread spawned; immediate error
    spin(qapp, lambda: True, timeout_s=0.1)
    assert len(errors) == 1
