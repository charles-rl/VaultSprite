"""M9 action attribute store — ``${...}`` (once) vs ``#{...}`` (per-tick) variables.

Pure Python (no Qt). ``ActionVars`` is the per-run attribute store for one action:
``${expr}`` is evaluated **once** at init and cached, ``#{expr}`` is re-evaluated
on every access, and plain literals are coerced to number/bool where possible.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from .mascot_environment import ExpressionCompiler, JSMascot, parse_error

logger = logging.getLogger(__name__)


class _UNDEFINED_TYPE2:
    def __bool__(self): return False

    def __repr__(self): return "undefined"


_UNDEFINED = _UNDEFINED_TYPE2()


class _DynOnce:                      # `${...}` placeholder resolved at first access / init
    def __init__(self, comp: ExpressionCompiler, view_factory, rng):
        self.comp, self._vf, self.rng = comp, view_factory, rng
        self._value: Any = None
        self._done = False

    def value(self) -> Any:
        if not self._done:
            try:
                self._value = self.comp.eval_value(self._vf(), self.rng)
            except Exception as exc:               # noqa: BLE001 - malformed expr → undefined
                logger.warning("action expression failed once-at-init: %s", exc)
                self._value = _UNDEFINED
            self._done = True
        return self._value


def _coerce_literal(v: str) -> Any:
    if v == "true":
        return True
    if v == "false":
        return False
    try:
        if "." in v:
            return float(v)
        return int(v)
    except ValueError:
        return v


class ActionVars:
    """Attribute store for one action run.

    ``${...}`` → evaluated **once** at init (value cached); `#{...}` → re-evaluated every
    access; plain literals parsed to number/bool where possible."""

    def __init__(self, attrs: dict[str, str], view_factory: Callable[[], JSMascot], rng):
        self._view = view_factory
        self._rng = rng
        self._static: dict[str, Any] = {}
        self._dynamic: dict[str, ExpressionCompiler] = {}
        for key, raw in attrs.items():
            v = (raw or "").strip()
            if v.startswith("${") and v.endswith("}"):
                comp = ExpressionCompiler(v[2:-1])     # raises parse_error → caught by parser
                self._static[key] = _DynOnce(comp, self._view, rng)
            elif v.startswith("#{") and v.endswith("}"):
                self._dynamic[key] = ExpressionCompiler(v[2:-1])
            else:
                self._static[key] = _coerce_literal(v)

    def has(self, key: str) -> bool:
        return key in self._static or key in self._dynamic

    def as_scope(self) -> dict:
        """Expose this action's attributes as a bare-identifier scope for expression
        evaluation (e.g. ``#{TargetY < mascot.anchor.y}`` in an <Animation> Condition).
        Dynamic ``#{}`` values re-resolve now (called per tick); ``${}`` resolve once."""
        return {k: self.resolve(k) for k in set(self._static) | set(self._dynamic)}

    def get_num(self, key: str, fallback: float = 0.0) -> float:
        v = self.resolve(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        try:
            s = str(v).strip()
            if s == "":
                return fallback
            return float(s)
        except ValueError:
            return fallback

    def get_bool(self, key: str, fallback: bool = True) -> bool:
        v = self.resolve(key)
        if v is _UNDEFINED:
            return fallback
        if isinstance(v, bool):
            return v
        try:
            s = str(v).strip().lower()
            if s in ("", "false", "0"):
                return False
            if s in ("true", "1"):
                return True
            float(s)      # a plain number → JS-truthy (nonzero)
            return v not in (None, 0, 0.0)
        except ValueError:
            return fallback

    def get_str(self, key: str, fallback: str = "") -> str:
        v = self.resolve(key)
        return fallback if v is _UNDEFINED else str(v)

    def resolve(self, key: str):
        if key in self._dynamic:
            try:
                val = self._dynamic[key].eval_value(self._view(), self._rng)
            except Exception:
                return _UNDEFINED
            return val
        v = self._static.get(key, _UNDEFINED)
        return v.value() if isinstance(v, _DynOnce) else v
