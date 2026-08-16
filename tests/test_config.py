"""Config loader: dotted access, env overrides, path resolution."""
from __future__ import annotations

import os

import pytest


def test_dotted_get(config_load):
    c = config_load()
    assert c.get("stats.tick_ms") == 60000
    assert c.get("window.width") in (96, 128) or int(c.get("window.width")) > 0
    assert c.get("does.not.exist", "fallback") == "fallback"


def test_section_missing_raises(config_load):
    c = config_load()
    with pytest.raises(KeyError):
        c.section("no_such_section")


def test_env_override_vault_root(tmp_path, monkeypatch, config_load):
    target = tmp_path / "myvault"
    monkeypatch.setenv("VAULT_ROOT", str(target))
    c = config_load(reload=True)
    assert c.get("obsidian.vault_root") == str(target)


def test_env_override_health_min_and_vision(monkeypatch, config_load):
    monkeypatch.setenv("HEALTH_WORK_MIN", "45")
    monkeypatch.setenv("VISION_ENABLED", "false")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3-vl:32b")
    c = config_load(reload=True)
    assert int(c.get("health.work_threshold_min")) == 45
    assert bool(c.get("remote.vision_enabled")) is False
    assert c.get("remote.ollama_model") == "qwen3-vl:32b"


def test_asset_config_path_resolves(config_load):
    c = config_load()
    path = c.asset_config_path
    # resolves to a real file shipped by tools/generate_assets.py
    assert str(path).endswith("config.yaml")
