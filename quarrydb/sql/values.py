"""SQL value semantics: three-valued logic, comparisons, arithmetic, affinity.

Values are ``None`` (NULL), ``int``, ``float``, ``str`` or ``bytes``.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Optional, Tuple

from .errors import ProgrammingError

# Class rank for mixed-type comparison / sorting: NULL < numbers < text < blob
_RANK_NULL, _RANK_NUM, _RANK_TEXT, _RANK_BLOB = 0, 1, 2, 3


def rank(v: Any) -> int:
    if v is None:
        return _RANK_NULL
    if isinstance(v, (int, float)):
        return _RANK_NUM
    if isinstance(v, str):
        return _RANK_TEXT
    return _RANK_BLOB


def sort_key(v: Any) -> Tuple[int, Any]:
    """Key usable with ``sorted`` for a single SQL value (NULLs first)."""
    r = rank(v)
    return (r, v if r else 0)


def compare(a: Any, b: Any) -> Optional[int]:
    """Three-valued compare: -1/0/1, or None if either side is NULL."""
    if a is None or b is None:
        return None
    ra, rb = rank(a), rank(b)
    if ra != rb:
        return -1 if ra < rb else 1
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def truth(v: Any) -> Optional[bool]:
    """SQL truthiness: NULL -> None, numbers by value, text parsed as a number."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        n = to_number(v)
        return n != 0 if n is not None else False
    return bool(len(v))


def is_true(v: Any) -> bool:
    return truth(v) is True


def not_(v: Optional[bool]) -> Optional[int]:
    if v is None:
        return None
    return 0 if v else 1


def and_(a: Any, b: Any) -> Optional[int]:
    ta, tb = truth(a), truth(b)
    if ta is False or tb is False:
        return 0
    if ta is None or tb is None:
        return None
    return 1


def or_(a: Any, b: Any) -> Optional[int]:
    ta, tb = truth(a), truth(b)
    if ta is True or tb is True:
        return 1
    if ta is None or tb is None:
        return None
    return 0


_NUM_RE = re.compile(r"^\s*[+-]?(\d+\.?\d*([eE][+-]?\d+)?|\.\d+([eE][+-]?\d+)?)\s*$")


def to_number(v: Any) -> Optional[Any]:
    """Coerce a value to a number for arithmetic; None if impossible."""
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        if _NUM_RE.match(v):
            s = v.strip()
            try:
                return int(s)
            except ValueError:
                return float(s)
        # SQLite takes the longest numeric prefix; keep it simple: 0 otherwise.
        m = re.match(r"^\s*[+-]?\d+", v)
        return int(m.group()) if m else 0
    if isinstance(v, (bytes, bytearray)):
        return to_number(bytes(v).decode("utf-8", "ignore"))
    return None


def arith(op: str, a: Any, b: Any) -> Any:
    if a is None or b is None:
        return None
    x, y = to_number(a), to_number(b)
    if x is None or y is None:
        return None
    if op == "+":
        return x + y
    if op == "-":
        return x - y
    if op == "*":
        return x * y
    if op == "/":
        if y == 0:
            return None
        if isinstance(x, int) and isinstance(y, int):
            q = abs(x) // abs(y)
            return q if (x >= 0) == (y >= 0) else -q
        return x / y
    if op == "%":
        if y == 0:
            return None
        if isinstance(x, float) or isinstance(y, float):
            x, y = int(x), int(y)
            if y == 0:
                return None
        r = abs(x) % abs(y)
        return r if x >= 0 else -r
    raise ProgrammingError(f"unknown operator {op}")


def concat(a: Any, b: Any) -> Any:
    if a is None or b is None:
        return None
    if isinstance(a, bytes) and isinstance(b, bytes):
        return a + b
    return to_text(a) + to_text(b)


def to_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            return f"{v:.1f}"
        return repr(v)
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    return str(v)


def typeof(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool) or isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "real"
    if isinstance(v, str):
        return "text"
    return "blob"


def apply_affinity(affinity: str, v: Any) -> Any:
    """Coerce a value being stored into a column with the given affinity."""
    if v is None:
        return None
    if isinstance(v, bool):
        v = int(v)
    if affinity == "INTEGER":
        if isinstance(v, float):
            return int(v) if v == int(v) and abs(v) < 2**63 else v
        if isinstance(v, str) and _NUM_RE.match(v):
            n = to_number(v)
            if isinstance(n, float) and n == int(n):
                return int(n)
            return n
        return v
    if affinity == "REAL":
        if isinstance(v, int):
            return float(v)
        if isinstance(v, str) and _NUM_RE.match(v):
            return float(to_number(v))  # type: ignore[arg-type]
        return v
    if affinity == "TEXT":
        if isinstance(v, (int, float)):
            return to_text(v)
        return v
    return v


def cast(v: Any, type_name: str) -> Any:
    if v is None:
        return None
    t = type_name.upper()
    if "INT" in t or "BOOL" in t:
        if isinstance(v, (bytes, bytearray)):
            v = bytes(v).decode("utf-8", "ignore")
        if isinstance(v, str):
            m = re.match(r"^\s*[+-]?\d+", v)
            if m:
                return int(m.group())
            n = to_number(v)
            return int(n) if n is not None else 0
        return int(v)
    if "REAL" in t or "FLOA" in t or "DOUB" in t or "NUM" in t or "DEC" in t:
        if isinstance(v, (bytes, bytearray)):
            v = bytes(v).decode("utf-8", "ignore")
        if isinstance(v, str):
            n = to_number(v)
            return float(n) if n is not None else 0.0
        return float(v)
    if "CHAR" in t or "TEXT" in t or "CLOB" in t or "STRING" in t:
        return to_text(v)
    if "BLOB" in t:
        if isinstance(v, str):
            return v.encode("utf-8")
        if isinstance(v, (int, float)):
            return to_text(v).encode("utf-8")
        return bytes(v)
    return v


@lru_cache(maxsize=256)
def _like_regex(pattern: str, escape: Optional[str], glob: bool) -> "re.Pattern[str]":
    out = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if escape and c == escape and i + 1 < n:
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if glob:
            if c == "*":
                out.append(".*")
            elif c == "?":
                out.append(".")
            elif c == "[":
                j = pattern.find("]", i + 1)
                if j < 0:
                    out.append(re.escape(c))
                else:
                    body = pattern[i + 1 : j]
                    if body.startswith("^"):
                        body = "^" + re.escape(body[1:]).replace("\\-", "-")
                    else:
                        body = re.escape(body).replace("\\-", "-")
                    out.append("[" + body + "]")
                    i = j
            else:
                out.append(re.escape(c))
        else:
            if c == "%":
                out.append(".*")
            elif c == "_":
                out.append(".")
            else:
                out.append(re.escape(c))
        i += 1
    flags = re.DOTALL if glob else (re.DOTALL | re.IGNORECASE)
    return re.compile("^" + "".join(out) + "$", flags)


def like(value: Any, pattern: Any, escape: Any = None, glob: bool = False) -> Optional[int]:
    if value is None or pattern is None:
        return None
    return 1 if _like_regex(to_text(pattern), to_text(escape) if escape else None, glob).match(to_text(value)) else 0
