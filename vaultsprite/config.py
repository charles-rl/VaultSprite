"""Central YAML configuration loader for VaultSprite.

Loads ``config/config.yaml`` (relative paths resolve against the repo root)
and applies environment-variable overrides for machine-specific keys so a
single config file works on the H100 box and the dev laptop alike.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Union

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "config.yaml"

# env var -> (dotted config key, cast)
_ENV_OVERRIDES: dict[str, tuple[str, type]] = {
    "OLLAMA_BASE_URL": ("remote.ollama_base_url", str),
    "OLLAMA_MODEL": ("remote.ollama_model", str),
    "LLM_TIMEOUT_S": ("remote.llm_timeout_s", float),
    "VISION_ENABLED": ("remote.vision_enabled", bool),
    "VAULT_ROOT": ("obsidian.vault_root", str),
    "HEALTH_WORK_MIN": ("health.work_threshold_min", int),
}


def _cast_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class _DotDict(dict):
    """dict with attribute access for nested config sections."""

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as exc:  # pragma: no cover - defensive
            raise AttributeError(f"no config key {item!r}") from exc


def _dotify(data: dict[str, Any]) -> _DotDict:
    out = _DotDict()
    for key, value in data.items():
        out[key] = _dotify(value) if isinstance(value, dict) else value
    return out


class Config:
    """Typed accessor over the YAML config tree."""

    def __init__(self, raw: dict[str, Any], root: Path):
        self.root = root
        self.raw = _dotify(raw or {})

    # -- access -----------------------------------------------------------
    def get(self, dotted_key: str, default: Any = None) -> Any:
        """Fetch a value by dotted key, e.g. ``get('stats.tick_ms')``."""
        node: Any = self.raw
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> _DotDict:
        value = self.get(name)
        if not isinstance(value, _DotDict):
            raise KeyError(f"missing config section {name!r}")
        return value

    # -- path helpers -----------------------------------------------------
    def resolve_path(self, raw: Union[str, Path]) -> Path:
        """Resolve a possibly-relative path against the repo root."""
        p = Path(raw)
        return p if p.is_absolute() else (self.root / p).resolve()

    @property
    def vault_root(self) -> Path:
        return self.resolve_path(self.get("obsidian.vault_root", "Vault"))

    @property
    def asset_config_path(self) -> Path:
        """Path to the sprite FSM matrix (JSON or YAML)."""
        return self.resolve_path(
            self.get("animation.config_path", "assets/config.yaml")
        )


def _apply_env_overrides(raw: dict[str, Any]) -> None:
    for env_name, (dotted_key, cast) in _ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value is None or value == "":
            continue
        try:
            # bools must parse from the raw string — bool("false") is True!
            parsed = _cast_bool(value) if cast is bool else cast(value)
        except ValueError:
            continue
        node = raw
        parts = dotted_key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = parsed


def load_config(
    path: Union[str, Path, None] = None, *, reload: bool = False
) -> Config:
    """Load and cache the config. Pass ``reload=True`` to bypass the cache."""
    global _CACHED  # noqa: PLW0603
    if _CACHED is not None and not reload:
        return _CACHED
    target = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(target, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    _apply_env_overrides(raw)
    config = Config(raw, REPO_ROOT)
    _CACHED = config
    return config


_CACHED: Config | None = None
