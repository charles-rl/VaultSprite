"""Mascot environment geometry + a *safe* Shimeji expression evaluator (Module 9).

Pure-Python port of Shimeji-ee ``environment``/``script`` semantics (pattern source:
``DalekCraft2/Shimeji-Desktop``, see ``docs/09_mascot_engine/README.md``) and the
``${...}``/`#{...}` scripting used by standard Shimeji ``actions.xml``/``behaviors.xml``.
No Qt import — unit-testable without a QApplication (same style as terrain/stat tests).

Key fidelity points (see ``docs/09_mascot_engine/README.md`` for sources):
- Border ``is_on(p) = |coord - line| < BORDER_TOL and faces(p)`` with **BORDER_TOL == 1.0 px**
  (works because falls sub-step in small increments).
- ``area.visible()`` False when degenerate → an all-negative sentinel area is "no window".
- Expressions: a whitelist tokenizer + Pratt parser (numbers, comparisons, boolean ops,
  ternary, member access, calls into the fixed mascot/environment view, ``Math.random``).
  Anything unknown evaluates to **False** for conditions so exotic community behaviors degrade
  safely instead of crashing or executing arbitrary code. No ``eval()`` anywhere.
"""
from __future__ import annotations

import math
import random as _random
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


BORDER_TOL: float = 1.0


class parse_error(ValueError):
    """Raised when a Shimeji expression cannot be tokenized/parsed (engines should
    treat the containing action/behavior as *invalid*, not crash)."""


# --------------------------------------------------------------------------- geometry --
@dataclass
class Vec2:
    x: float = 0.0
    y: float = 0.0

    def __add__(self, o): return Vec2(self.x + o.x, self.y + o.y)
    def __sub__(self, o): return Vec2(self.x - o.x, self.y - o.y)
    def __mul__(self, k: float): return Vec2(self.x * k, self.y * k)
    def copy(self) -> "Vec2": return Vec2(self.x, self.y)


@dataclass
class DVec2(Vec2):
    dx: float = 0.0
    dy: float = 0.0

    def move_to(self, nx: float, ny: float):
        self.dx += nx - self.x
        self.dy += ny - self.y
        self.x, self.y = nx, ny


class HBorder:  # horizontal (floor / ceiling) — y + x range
    __slots__ = ("y", "xstart", "xend")

    def __init__(self, y=0.0, xstart=0.0, xend=0.0):
        self.y, self.xstart, self.xend = y, xstart, xend

    def faces(self, p: Vec2) -> bool:
        return self.xstart <= p.x <= self.xend

    def is_on(self, p: Vec2, tol: float = BORDER_TOL) -> bool:
        return abs(p.y - self.y) < tol and self.faces(p)


class VBorder:  # vertical (walls) — x + y range
    __slots__ = ("x", "ystart", "yend")

    def __init__(self, x=0.0, ystart=0.0, yend=0.0):
        self.x, self.ystart, self.yend = x, ystart, yend

    def faces(self, p: Vec2) -> bool:
        return self.ystart <= p.y <= self.yend

    def is_on(self, p: Vec2, tol: float = BORDER_TOL) -> bool:
        return abs(p.x - self.x) < tol and self.faces(p)


class Area:
    """Screen / work-area rectangle; borders are fresh value objects built from current sides."""

    __slots__ = ("top", "right", "bottom", "left")

    def __init__(self, top=0.0, right=0.0, bottom=0.0, left=0.0):
        self.top, self.right, self.bottom, self.left = top, right, bottom, left

    # JS-facing aliases (camelCase names used by shimeji expressions)
    @property
    def width(self) -> float: return self.right - self.left
    @property
    def height(self) -> float: return self.bottom - self.top
    @property
    def visible(self) -> bool: return (self.left != self.right) and (self.top != self.bottom)

    def is_on(self, p: Vec2, tol: float = BORDER_TOL) -> bool:
        return (self.left_border(tol).is_on(p) or self.right_border(tol).is_on(p)
                or self.bottom_border(tol).is_on(p) or self.top_border(tol).is_on(p))

    # border factories (built from *current* sides at call time, like the C++ area)
    def bottom_border(self, tol: float = BORDER_TOL): return HBorder(self.bottom, self.left, self.right)
    def top_border(self, tol: float = BORDER_TOL): return HBorder(self.top, self.left, self.right)
    def left_border(self, tol: float = BORDER_TOL): return VBorder(self.left, self.top, self.bottom)
    def right_border(self, tol: float = BORDER_TOL): return VBorder(self.right, self.top, self.bottom)

    # JS-facing names
    bottomBorder = property(lambda s: s.bottom_border())
    topBorder = property(lambda s: s.top_border())
    leftBorder = property(lambda s: s.left_border())
    rightBorder = property(lambda s: s.right_border())


class DArea(Area):
    """A tracked foreground window ("activeIE") that also carries its per-tick move delta."""

    __slots__ = ("dx", "dy")

    def __init__(self, top=0.0, right=0.0, bottom=0.0, left=0.0, dx: float = 0.0, dy: float = 0.0):
        super().__init__(top, right, bottom, left)
        self.dx, self.dy = dx, dy

    @staticmethod
    def invisible() -> "DArea":
        # all-negative sentinel → visible()==False → borders never match (no tracked window)
        return DArea(-50, -50, -50, -50, 0.0, 0.0)


@dataclass
class MascotEnvironment:
    """Live surface geometry. Updated in place each tick from the platform layer."""

    ceiling: HBorder = field(default_factory=lambda: HBorder(0, 0, 1))
    floor: HBorder = field(default_factory=lambda: HBorder(1, 0, 1))
    screen: Area = field(default_factory=Area)
    work_area: Area = field(default_factory=Area)
    active_ie: DArea = field(default_factory=DArea.invisible)
    cursor: DVec2 = field(default_factory=DVec2)
    allows_breeding: bool = False        # solo-pet: breeding never happens
    mascot_count: int = 1
    subtick_count: int = 1

    _rng: _random.Random = field(default_factory=_random.Random, repr=False)

    def random(self, upper=None):
        if upper is None:
            return self._rng.random()
        return self._rng.randint(0, max(0, int(upper)) - 1)


# --------------------------------------------------------------------------- views -----
# JS-facing wrapper objects the expression evaluator sees. Attribute/method names match the
# camelCase used in shimeji XML (isOn, lookRight, activeIE, workArea …). They delegate to a
# live MascotEnvironment + mascot state dict so reads always reflect the current tick.

class _View:  # base for member access; unknown attr → False-y sentinel on call
    def __getattr__(self, name):        # only called when normal lookup fails
        if name == "isOn":
            raise AttributeError(name)
        return _UNDEFINED


class JSVec2(_View):
    def __init__(self, get: Callable[[], Vec2]):
        self._get = get

    @property
    def x(self): return self._get().x
    @property
    def y(self): return self._get().y


class JSDVec2(JSVec2):
    def __init__(self, get: Callable[[], DVec2]):
        super().__init__(lambda: get())  # type: ignore[arg-type]

    @property
    def dx(self): return self._get().dx
    @property
    def dy(self): return self._get().dy


class JSHBorder(_View):
    def __init__(self, get: Callable[[], HBorder]):
        self._get = get

    @property
    def y(self): return self._get().y
    @property
    def left(self): return self._get().xstart
    @property
    def right(self): return self._get().xend

    def isOn(self, p) -> bool:          # noqa: N802 (JS name)
        g = self._get()
        x = getattr(p, "x", None); y = getattr(p, "y", None)
        if x is None or y is None:
            return False
        return g.is_on(Vec2(x, y))


class JSVBorder(_View):
    def __init__(self, get: Callable[[], VBorder]):
        self._get = get

    @property
    def x(self): return self._get().x
    @property
    def top(self): return self._get().ystart
    @property
    def bottom(self): return self._get().yend

    def isOn(self, p) -> bool:          # noqa: N802
        g = self._get()
        x = getattr(p, "x", None); y = getattr(p, "y", None)
        if x is None or y is None:
            return False
        return g.is_on(Vec2(x, y))


class JSArea(_View):
    def __init__(self, get: Callable[[], Area]):
        self._get = get

    @property
    def left(self): return self._get().left
    @property
    def right(self): return self._get().right
    @property
    def top(self): return self._get().top
    @property
    def bottom(self): return self._get().bottom
    @property
    def width(self): return self._get().width
    @property
    def height(self): return self._get().height
    @property
    def visible(self): return bool(getattr(self._get(), "visible", False))

    @property
    def leftBorder(self):               # noqa: N802
        base = self._get()
        return JSVBorder(lambda: VBorder(base.left, base.top, base.bottom))

    @property
    def rightBorder(self):              # noqa: N802
        base = self._get()
        return JSVBorder(lambda: VBorder(base.right, base.top, base.bottom))

    @property
    def topBorder(self):                # noqa: N802
        base = self._get()
        return JSHBorder(lambda: HBorder(base.top, base.left, base.right))

    @property
    def bottomBorder(self):             # noqa: N802
        base = self._get()
        return JSHBorder(lambda: HBorder(base.bottom, base.left, base.right))

    def isOn(self, p) -> bool:          # noqa: N802
        g = self._get()
        x = getattr(p, "x", None); y = getattr(p, "y", None)
        if x is None or y is None:
            return False
        return g.is_on(Vec2(x, y))


class JSEnvironment(_View):
    def __init__(self, env_get: Callable[[], MascotEnvironment]):
        self._env = env_get

    @property
    def floor(self):                   # noqa: N802
        return JSHBorder(lambda: self._env().floor)

    @property
    def ceiling(self):                 # noqa: N802
        return JSHBorder(lambda: self._env().ceiling)

    @property
    def workArea(self):                # noqa: N802
        return JSArea(lambda: self._env().work_area)

    @property
    def screen(self):                  # noqa: N802
        return JSArea(lambda: self._env().screen)

    @property
    def activeIE(self):                # noqa: N802
        return JSArea(lambda: self._env().active_ie)

    @property
    def cursor(self):                  # noqa: N802
        e = self._env()
        return JSDVec2(lambda: e.cursor)


class _UndefinedType:
    """Sentinel for unknown identifiers / failed expressions — falsy so conditions fail closed."""

    def __bool__(self) -> bool:
        return False

    def __call__(self, *a, **k):
        return self

    def __getattr__(self, name):      # any further member access stays undefined
        if name.startswith("__"):
            raise AttributeError(name)
        return _UNDEFINED


_UNDEFINED = _UndefinedType()


def is_undefined(v: Any) -> bool:
    """True for the shared undefined sentinel (both modules use this one)."""
    return v is _UNDEFINED or type(v) is _UndefinedType


def js_truthy(v: Any) -> bool:
    """JS truthiness — falsy = undefined, null, false, 0/NaN, ''. Shared by both modules."""
    if v is None or is_undefined(v):
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, float) and math.isnan(v):
        return False
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return len(v) > 0
    try:
        return len(v) > 0                                # noqa: B023 - truthiness probe only
    except TypeError:
        return True


class JSMascot(_View):
    """Root object exposed as global ``mascot`` to expressions."""

    def __init__(self, state_get: Callable[[], Any], env_get: Callable[[], MascotEnvironment]):
        self._state = state_get
        self._env = env_get

    @property
    def anchor(self):                  # noqa: N802
        return JSVec2(lambda: self._state().anchor)

    @property
    def lookRight(self):               # noqa: N802
        return bool(getattr(self._state(), "looking_right", False))

    @property
    def FootX(self):                   # noqa: N802 (Shimeji Dragged/Pinched condition var)
        st = self._state()
        foot = getattr(st, "foot_x", None)
        return float(foot if foot is not None else getattr(st, "anchor", Vec2()).x)

    @property
    def FootDX(self):                  # noqa: N802 (pendulum oscillator, C++ Dragged)
        return float(getattr(self._state(), "foot_dx", 0.0))

    @property
    def totalCount(self):              # noqa: N802
        return int(getattr(self._env(), "mascot_count", 1))

    @property
    def activeBehavior(self):          # noqa: N802
        s = self._state()
        q = getattr(s, "queued_behavior", "")
        if q:
            return str(q)
        b = getattr(s, "behavior_name", "")
        return str(b)

    @activeBehavior.setter
    def activeBehavior(self, value):   # noqa: N802 (setActiveBehavior -> queued behavior)
        self._state().queued_behavior = str(value)

    @property
    def environment(self):             # noqa: N802
        return JSEnvironment(self._env)


class ScopedMascotView(JSMascot):
    """JSMascot plus action-local identifiers (Shimeji's Dragged exposes ``FootX`` to its
    per-pose conditions; other packs expose their own). Unknown extras → _UNDEFINED."""

    def __init__(self, state_get: Callable[[], Any], env_get: Callable[[], MascotEnvironment],
                 extras: Callable[[], dict]):
        super().__init__(state_get, env_get)
        self._extras = extras

    @property
    def FootX(self):                              # noqa: N802 (Shimeji Dragged condition var)
        st = self._state()                        # type: ignore[attr-defined]
        foot = getattr(st, "foot_x", None)
        return float(foot if foot is not None else getattr(st, "anchor", Vec2()).x)


# --------------------------------------------------------------------------- evaluator -
_TOKEN_SPEC = [
    ("NUM", r"\d+\.\d+|\d+"),
    ("STR", r'"[^"]*"|\'[^\']*\''),
    ("IDENT", r"[A-Za-z_][A-Za-z0-9_]*"),
    ("OP", r"<=[=]?|>=[=]?|==|!=|&&|\|\||[+\-*/%<>=!?:().,]"),
    ("SKIP", r"\s+"),
]


def _tokenize(js: str) -> list[tuple[str, Any]]:
    import re
    master = "|".join(f"(?P<{n}>{p})" for n, p in _TOKEN_SPEC)
    pos, toks = 0, []
    while pos < len(js):
        m = re.match(master, js[pos:])
        if not m:
            raise parse_error(f"cannot tokenize at {js[pos:]!r}")
        pos += m.end()
        kind = m.lastgroup or "OP"
        text = m.group()
        if kind == "SKIP":
            continue
        if kind == "NUM":
            toks.append(("num", float(text) if "." in text else int(text)))
        elif kind == "STR":
            toks.append(("str", text[1:-1]))          # strip the quotes
        elif kind == "IDENT":
            toks.append(("ident", text))
        else:
            toks.append((text, text))
    return toks


# Pratt precedence (low → high): ternary < || < && < compare < add < mul < unary < postfix
_PREC = {"||": 10, "&&": 20, "<": 30, "<=": 30, ">": 30, ">=": 30, "==": 30,
         "!=": 30, "+": 40, "-": 40, "*": 50, "/": 50, "%": 50}



class _Parser:
    def __init__(self, toks):
        self.toks = toks
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self):
        t = self.peek()
        self.i += 1
        return t

    def parse(self):
        node = self.ternary()
        if self.peek()[0] is not None:
            raise ValueError("trailing tokens")
        return node

    def ternary(self):
        cond = self.or_()
        k, _v = self.peek()
        if k == "?":
            self.take()
            then_b = self.ternary()
            exp = self.take()[0]
            assert exp == ":", f"expected ':' in ternary, got {exp!r}"
            else_b = self.ternary()
            return ("tern", cond, then_b, else_b)
        return cond

    def _bin(self, min_prec):
        left = self.unary()
        while True:
            k, _v = self.peek()
            if not isinstance(k, str) or k not in _PREC or _PREC[k] < min_prec:
                break
            op = self.take()[0]
            right = self._bin(_PREC[op] + 1)
            left = ("bin", op, left, right)
        return left

    def or_(self):     return self._bin(10)
    def and_(self):    return self._bin(20)

    def _cmp(self):    return self._bin(30)
    def addsub(self):  return self._bin(40)
    def muldiv(self):  return self._bin(50)

    def unary(self):
        k, _v = self.peek()
        if k == "!":
            self.take()
            return ("not", self.unary())
        if k == "-":
            self.take()
            return ("neg", self.unary())
        return self.postfix()

    def postfix(self):
        node = self.primary()
        while True:
            k, _v = self.peek()
            if k == ".":                       # member access
                self.take()
                name = self.take()[1]
                node = ("member", node, str(name))
            elif k == "(":                     # call
                self.take()                    # consume '('
                args = []
                k2, _v2 = self.peek()
                if k2 != ")":
                    while True:
                        args.append(self.ternary())
                        kk, _vv = self.take()
                        if kk == ",":
                            continue
                        assert kk == ")"       # consumes ')' after the last arg
                        break
                else:
                    self.take()                # empty call f(): consume the lone ')'
                node = ("call", node, args)
            else:
                break
        return node

    def primary(self):
        k, v = self.take()
        if k == "num":
            return ("num", v)
        if k == "str":
            return ("str", str(v))
        if k == "ident":
            return ("ident", str(v))
        if k == "(":
            inner = self.ternary()
            assert self.take()[0] == ")"
            return inner
        raise ValueError(f"unexpected token {k!r}")


class _Evaluator:
    def __init__(self, root, rng):
        self.root = root                    # a JSMascot-like global object (for "mascot")
        self.rng = rng

    # name resolution for identifiers at the top level of an expression scope
    def resolve(self, name: str) -> Any:
        if name == "mascot":            # in Shimeji-ee the mascot IS the global root object
            return self.root
        if name == "Math":
            return _MathObj(self.rng)
        if name in ("true", "false"):
            return name == "true"
        if name in ("null", "undefined"):
            return None
        # globals may be the mascot itself or constants; try root's attributes then UNDEFINED
        r = self.root
        if r is not None:
            try:
                v = getattr(r, name, None)
            except Exception:                            # properties can raise during teardown
                return _UNDEFINED
            if v is not None and not is_undefined(v):
                return v
        return _UNDEFINED

    def eval(self, node, scope: dict):
        t = node[0]
        if t == "num":
            return node[1]
        if t == "str":
            return node[1]
        if t == "ident":
            name = node[1]
            if name in scope:
                return scope[name]
            return self.resolve(name)
        if t == "not":
            return not js_truthy(self.eval(node[1], scope))
        if t == "neg":
            return -self._num(self.eval(node[1], scope))
        if t == "bin":
            op = node[1]
            left = self.eval(node[2], scope)
            right = self.eval(node[3], scope)
            return self._apply_bin(op, left, right)
        if t == "tern":
            cond, then_b, else_b = node[1], node[2], node[3]
            return self.eval(then_b, scope) if js_truthy(self.eval(cond, scope)) \
                else self.eval(else_b, scope)
        if t == "member":
            base = self.eval(node[1], scope)
            return self._member(base, node[2])
        if t == "call":
            callee = self.eval(node[1], scope)
            args = [self.eval(a, scope) for a in node[2]]
            try:
                if callable(callee):
                    return callee(*args)
            except Exception:                      # any method error → fail closed (False/undefined)
                return _UNDEFINED
            return _UNDEFINED
        raise ValueError(f"bad node {t!r}")

    @staticmethod
    def _num(v) -> float:
        if isinstance(v, bool):
            return 1.0 if v else 0.0
        if isinstance(v, (int, float)):
            return float(v)
        raise ValueError("not a number")

    @staticmethod
    def _member(base, name: str) -> Any:
        if base is None or is_undefined(base):
            return _UNDEFINED
        try:
            v = getattr(base, name, None)
        except Exception:
            return _UNDEFINED
        if v is None and not hasattr(type(base), name):      # no such member at all
            return _UNDEFINED
        if is_undefined(v):
            return _UNDEFINED
        return v

    @staticmethod
    def _apply_bin(op, a, b):
        # boolean ops short-circuit on JS truthiness
        if op == "&&":
            av = js_truthy(a)
            return (a if not av else b) if av else False
        if op == "||":
            if js_truthy(a):
                return a
            return b
        # comparisons
        try:
            if op in ("==", "!="):
                eq = (a is b) or (isinstance(a, (int, float)) and isinstance(b, (int, float))
                                  and float(a) == float(b)) or (str(a) == str(b))
                return eq if op == "==" else not eq
            la = _Evaluator._num_coerce(a); lb = _Evaluator._num_coerce(b)
        except Exception:
            return False
        if op == "<":  return bool(la < lb)
        if op == "<=": return bool(la <= lb)
        if op == ">":  return bool(la > lb)
        if op == ">=": return bool(la >= lb)
        # arithmetic (both sides numeric; otherwise fail closed to NaN→False-ish 0.0)
        try:
            fa, fb = _Evaluator._num(a), _Evaluator._num(b)
        except Exception:
            return float("nan")
        if op == "+":  return la + lb
        if op == "-":  return la - lb
        if op == "*":  return la * lb
        if op == "/":
            return (la / fb) if fb else float("nan")
        if op == "%":
            return (la % fb) if fb else float("nan")
        raise ValueError(op)

    @staticmethod
    def _num_coerce(v):
        if isinstance(v, bool):
            return 1.0 if v else 0.0
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        try:
            return float(s)
        except ValueError:
            raise


class _MathObj(_View):
    def __init__(self, rng):
        self._rng = rng

    def random(self, *_a, **_k):      # Math.random() and (Math.random * x) both work in JS
        return self._rng.random()

    def abs(self, v):  return abs(_Evaluator._num_coerce(v))
    def sqrt(self, v): return math.sqrt(max(0.0, _Evaluator._num_coerce(v)))
    def min(self, *a): return min(_Evaluator._num_coerce(x) for x in a) if a else float("nan")
    def max(self, *a): return max(_Evaluator._num_coerce(x) for x in a) if a else float("nan")

    def __call__(self, *_a, **_k):   # bare `Math.random` used as a callable (pack quirk)
        return self.random()


def parse_expression(js: str):
    """Parse → AST. Raises ValueError on malformed input."""
    toks = _tokenize(js)
    return _Parser(toks).parse()


class ExpressionCompiler:
    """Compile a `${...}`/`#{...}` expression once, evaluate it cheaply per call."""

    def __init__(self, js_body: str):
        self.js_body = js_body.strip()
        self._ast = parse_expression(self.js_body) if self.js_body else None

    def eval_value(self, mascot_view, rng=None, scope: Optional[dict] = None):
        ev = _Evaluator(mascot_view, rng or getattr(_ENV_RNG_HOLDER[0], "_rng", None))
        try:
            return ev.eval(self._ast, scope or {})
        except Exception:
            return _UNDEFINED

    def eval_bool(self, mascot_view, rng=None, scope: Optional[dict] = None) -> bool:
        v = self.eval_value(mascot_view, rng, scope)
        return js_truthy(v)


# small holder so expression evaluators can share the environment's RNG instance
_ENV_RNG_HOLDER = [None]
