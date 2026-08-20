"""Shared fixtures: a session QApplication (offscreen) + config stubs."""
from __future__ import annotations

import copy
import time

import pytest
import yaml
from PySide6.QtWidgets import QApplication

import vaultsprite.config as cfgmod


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app   # worker threads are daemon; process teardown is the cleanup


@pytest.fixture
def config_load():
    from vaultsprite.config import load_config

    def _load(*args, **kwargs):
        kwargs.setdefault("reload", True)   # always a fresh parse for test isolation
        return load_config(*args, **kwargs)

    yield _load


def _repo_config_flat() -> dict[str, object]:
    """Flatten config/config.yaml into dotted keys (matches Config.get('a.b'))."""
    with open(cfgmod.DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    flat: dict[str, object] = {}

    def walk(prefix: str, node):
        for k, v in (node.items() if isinstance(node, dict) else []):
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                walk(key, v)
            else:
                flat[key] = copy.deepcopy(v)

    walk("", raw)
    return flat


def _nest(flat: dict[str, object]) -> dict:
    tree: dict = {}
    for dotted, value in flat.items():
        parts = dotted.split(".")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = copy.deepcopy(value)
    return tree


class FakeConfig:
    """Test stand-in supporting both ``get('a.b')`` and ``section('a')['b']``.

    Base values come from the real config/config.yaml so fixtures only need to
    override the handful of keys they care about (as dotted strings).
    """

    def __init__(self, overrides: dict[str, object] | None = None):
        flat = _repo_config_flat()
        for key, value in (overrides or {}).items():
            flat[key] = value
        self._tree = _nest(flat)
        from pathlib import Path
        self.root = Path(__file__).resolve().parent.parent

    @property
    def asset_config_path(self):
        return self.root / "assets" / "config.yaml"

    def resolve_path(self, raw):
        from pathlib import Path
        p = Path(raw)
        return p if p.is_absolute() else (self.root / p).resolve()

    @property
    def vault_root(self):
        return self.resolve_path(self.get("obsidian.vault_root", "Vault"))

    def get(self, dotted_key: str, default=None):
        node = self._tree
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict:
        value = self.get(name)
        if not isinstance(value, dict):
            raise KeyError(f"missing config section {name!r}")
        return value

    @property
    def mascot_pack_dir(self):
        from pathlib import Path
        packs = self.get("mascot.packs", {}) or {}
        name = str(self.get("mascot.pack", "") or "").strip()
        raw = (packs.get(name) if isinstance(packs, dict) else None) or \
            f"assets/{name}_shimeji"
        return Path(raw).resolve() if Path(raw).is_absolute() else self.root / raw


def spin(qapp_, predicate, timeout_s: float = 3.0) -> bool:
    """Pump the Qt event loop until predicate() is true (or timeout)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        qapp_.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False
