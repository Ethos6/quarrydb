"""Built-in scalar and aggregate functions."""

from __future__ import annotations

import datetime as _dt
import math
import random as _rnd
import time as _time
from typing import Any, Callable, Dict, List, Optional

from .errors import ProgrammingError
from .values import compare, to_number, to_text, typeof

# --------------------------------------------------------------------------- #
# Scalar functions                                                            #
# --------------------------------------------------------------------------- #


def _abs(x):
    if x is None:
        return None
    n = to_number(x)
    return abs(n) if n is not None else None


def _length(x):
    if x is None:
        return None
    if isinstance(x, (bytes, bytearray)):
        return len(x)
    return len(to_text(x))


def _substr(s, start, length=None):
    if s is None or start is None:
        return None
    is_blob = isinstance(s, (bytes, bytearray))
    seq = bytes(s) if is_blob else to_text(s)
    start = int(to_number(start) or 0)
    n = len(seq)
    # SQLite semantics: 1-based; 0 and negatives count from the end
    if start > 0:
        begin = start - 1
    elif start == 0:
        begin = 0
        if length is not None:
            length = int(to_number(length) or 0) - 1
            if length < 0:
                return seq[:0]
    else:
        begin = max(0, n + start)
    if length is None:
        return seq[begin:]
    length = int(to_number(length) or 0)
    if length < 0:
        end = begin
        begin = max(0, begin + length)
        return seq[begin:end]
    return seq[begin : begin + length]


def _trim(which):
    def fn(s, chars=None):
        if s is None:
            return None
        s = to_text(s)
        chars = to_text(chars) if chars is not None else None
        if which == "L":
            return s.lstrip(chars)
        if which == "R":
            return s.rstrip(chars)
        return s.strip(chars)
    return fn


def _replace(s, old, new):
    if s is None or old is None or new is None:
        return None
    old = to_text(old)
    if old == "":
        return to_text(s)
    return to_text(s).replace(old, to_text(new))


def _instr(s, sub):
    if s is None or sub is None:
        return None
    if isinstance(s, (bytes, bytearray)) and isinstance(sub, (bytes, bytearray)):
        return bytes(s).find(bytes(sub)) + 1
    return to_text(s).find(to_text(sub)) + 1


def _coalesce(*args):
    for a in args:
        if a is not None:
            return a
    return None


def _nullif(a, b):
    return None if compare(a, b) == 0 else a


def _iif(c, a, b):
    from .values import is_true
    return a if is_true(c) else b


def _min_scalar(*args):
    best = None
    for a in args:
        if a is None:
            return None
        if best is None or compare(a, best) < 0:
            best = a
    return best


def _max_scalar(*args):
    best = None
    for a in args:
        if a is None:
            return None
        if best is None or compare(a, best) > 0:
            best = a
    return best


def _round(x, digits=0):
    if x is None:
        return None
    n = to_number(x)
    if n is None:
        return None
    d = int(to_number(digits) or 0) if digits is not None else 0
    return float(round(float(n), d))


def _hex(x):
    if x is None:
        return ""
    if isinstance(x, (bytes, bytearray)):
        return bytes(x).hex().upper()
    return to_text(x).encode("utf-8").hex().upper()


def _unhex(x):
    if x is None:
        return None
    try:
        return bytes.fromhex(to_text(x))
    except ValueError:
        return None


def _char(*codes):
    return "".join(chr(int(c)) for c in codes if c is not None)


def _unicode(s):
    if s is None:
        return None
    s = to_text(s)
    return ord(s[0]) if s else None


def _quote(x):
    if x is None:
        return "NULL"
    if isinstance(x, (int, float)):
        return to_text(x)
    if isinstance(x, (bytes, bytearray)):
        return "X'" + bytes(x).hex().upper() + "'"
    return "'" + to_text(x).replace("'", "''") + "'"


def _random():
    return _rnd.randint(-(2**63), 2**63 - 1)


def _randomblob(n):
    import os as _os
    return _os.urandom(max(0, int(to_number(n) or 0)))


def _zeroblob(n):
    return b"\x00" * max(0, int(to_number(n) or 0))


def _sign(x):
    if x is None:
        return None
    n = to_number(x)
    if n is None:
        return None
    return (n > 0) - (n < 0)


def _math1(f):
    def fn(x):
        if x is None:
            return None
        n = to_number(x)
        if n is None:
            return None
        try:
            return float(f(n))
        except (ValueError, OverflowError):
            return None
    return fn


def _power(x, y):
    if x is None or y is None:
        return None
    try:
        return float(math.pow(to_number(x), to_number(y)))  # type: ignore[arg-type]
    except (ValueError, OverflowError, TypeError):
        return None


def _log(x, base=None):
    if x is None:
        return None
    n = to_number(x)
    if n is None or n <= 0:
        return None
    if base is None:
        return math.log10(n)
    b = to_number(base)
    if b is None or b <= 0 or b == 1:
        return None
    return math.log(n, b)


def _parse_time(value) -> Optional[_dt.datetime]:
    if value is None or value == "now":
        return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    if isinstance(value, (int, float)):
        # Julian day numbers are the SQLite convention; unix epoch is more useful.
        if value > 10_000_000:
            return _dt.datetime.fromtimestamp(float(value), _dt.timezone.utc).replace(tzinfo=None)
        return _dt.datetime(1970, 1, 1) + _dt.timedelta(days=float(value) - 2440587.5)
    s = to_text(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%H:%M:%S", "%H:%M"):
        try:
            d = _dt.datetime.strptime(s[:26] if "." in s else s, fmt)
            if fmt.startswith("%H"):
                d = d.replace(year=2000, month=1, day=1)
            return d
        except ValueError:
            continue
    return None


def _apply_modifiers(d: _dt.datetime, mods) -> Optional[_dt.datetime]:
    for m in mods:
        if m is None:
            return None
        m = to_text(m).strip().lower()
        if m == "localtime":
            d = d.replace(tzinfo=_dt.timezone.utc).astimezone().replace(tzinfo=None)
        elif m == "utc":
            d = d.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        elif m == "start of day":
            d = d.replace(hour=0, minute=0, second=0, microsecond=0)
        elif m == "start of month":
            d = d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif m == "start of year":
            d = d.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        elif m == "unixepoch":
            pass  # the numeric input was already treated as an epoch
        else:
            parts = m.split()
            if len(parts) == 2:
                try:
                    n = float(parts[0])
                except ValueError:
                    return None
                unit = parts[1].rstrip("s")
                if unit == "day":
                    d += _dt.timedelta(days=n)
                elif unit == "hour":
                    d += _dt.timedelta(hours=n)
                elif unit == "minute":
                    d += _dt.timedelta(minutes=n)
                elif unit == "second":
                    d += _dt.timedelta(seconds=n)
                elif unit == "month":
                    months = d.month - 1 + int(n)
                    d = d.replace(year=d.year + months // 12, month=months % 12 + 1)
                elif unit == "year":
                    d = d.replace(year=d.year + int(n))
                else:
                    return None
            else:
                return None
    return d


def _date(value="now", *mods):
    d = _parse_time(value)
    if d is None:
        return None
    d = _apply_modifiers(d, mods)
    return d.strftime("%Y-%m-%d") if d else None


def _time(value="now", *mods):
    d = _parse_time(value)
    if d is None:
        return None
    d = _apply_modifiers(d, mods)
    return d.strftime("%H:%M:%S") if d else None


def _datetime(value="now", *mods):
    d = _parse_time(value)
    if d is None:
        return None
    d = _apply_modifiers(d, mods)
    return d.strftime("%Y-%m-%d %H:%M:%S") if d else None


def _strftime(fmt, value="now", *mods):
    if fmt is None:
        return None
    d = _parse_time(value)
    if d is None:
        return None
    d = _apply_modifiers(d, mods)
    if d is None:
        return None
    f = to_text(fmt).replace("%s", str(int(d.replace(tzinfo=_dt.timezone.utc).timestamp())))
    f = f.replace("%f", f"{d.second:02d}.{d.microsecond // 1000:03d}")
    return d.strftime(f)


def _unixepoch(value="now", *mods):
    d = _parse_time(value)
    if d is None:
        return None
    d = _apply_modifiers(d, mods)
    return int(d.replace(tzinfo=_dt.timezone.utc).timestamp()) if d else None


def _printf(fmt, *args):
    if fmt is None:
        return None
    try:
        conv = []
        for a in args:
            conv.append(a if a is not None else "")
        return to_text(fmt) % tuple(conv)
    except (TypeError, ValueError):
        return to_text(fmt)


SCALARS: Dict[str, Callable[..., Any]] = {
    "ABS": _abs,
    "LENGTH": _length,
    "LOWER": lambda s: None if s is None else to_text(s).lower(),
    "UPPER": lambda s: None if s is None else to_text(s).upper(),
    "SUBSTR": _substr,
    "SUBSTRING": _substr,
    "TRIM": _trim(""),
    "LTRIM": _trim("L"),
    "RTRIM": _trim("R"),
    "REPLACE": _replace,
    "INSTR": _instr,
    "COALESCE": _coalesce,
    "IFNULL": _coalesce,
    "NULLIF": _nullif,
    "IIF": _iif,
    "IF": _iif,
    "ROUND": _round,
    "TYPEOF": lambda x: typeof(x),
    "HEX": _hex,
    "UNHEX": _unhex,
    "CHAR": _char,
    "UNICODE": _unicode,
    "QUOTE": _quote,
    "RANDOM": _random,
    "RANDOMBLOB": _randomblob,
    "ZEROBLOB": _zeroblob,
    "SIGN": _sign,
    "SQRT": _math1(math.sqrt),
    "FLOOR": _math1(math.floor),
    "CEIL": _math1(math.ceil),
    "CEILING": _math1(math.ceil),
    "EXP": _math1(math.exp),
    "LN": _math1(math.log),
    "LOG": _log,
    "LOG10": _math1(math.log10),
    "LOG2": _math1(math.log2),
    "POWER": _power,
    "POW": _power,
    "PI": lambda: math.pi,
    "DATE": _date,
    "TIME": _time,
    "DATETIME": _datetime,
    "STRFTIME": _strftime,
    "UNIXEPOCH": _unixepoch,
    "PRINTF": _printf,
    "FORMAT": _printf,
    "REVERSE": lambda s: None if s is None else to_text(s)[::-1],
    "REPEAT": lambda s, n: None if s is None or n is None else to_text(s) * max(0, int(to_number(n) or 0)),
    "CONCAT": lambda *a: "".join(to_text(x) for x in a if x is not None),
    "CONCAT_WS": lambda sep, *a: None if sep is None else to_text(sep).join(to_text(x) for x in a if x is not None),
    "STARTSWITH": lambda s, p: None if s is None or p is None else int(to_text(s).startswith(to_text(p))),
    "ENDSWITH": lambda s, p: None if s is None or p is None else int(to_text(s).endswith(to_text(p))),
    "CONTAINS": lambda s, p: None if s is None or p is None else int(to_text(p) in to_text(s)),
    "LIKELY": lambda x: x,
    "UNLIKELY": lambda x: x,
    "LIKELIHOOD": lambda x, p: x,
    "CURRENT_TIMESTAMP": _datetime,
    "CURRENT_DATE": _date,
    "CURRENT_TIME": _time,
    "CLOCK": lambda: _time.time(),
}

# MIN/MAX with 2+ args are scalar; with one arg they are aggregates.
MULTI_ARG_SCALARS = {"MIN": _min_scalar, "MAX": _max_scalar}


# --------------------------------------------------------------------------- #
# Aggregates                                                                  #
# --------------------------------------------------------------------------- #


class Aggregate:
    """Accumulator interface: step(value) ... finish() -> result."""

    def step(self, *args) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def finish(self) -> Any:  # pragma: no cover - interface
        raise NotImplementedError


class Count(Aggregate):
    def __init__(self) -> None:
        self.n = 0

    def step(self, v=0) -> None:
        if v is not None:
            self.n += 1

    def finish(self) -> Any:
        return self.n


class CountStar(Count):
    def step(self, *args) -> None:
        self.n += 1


class Sum(Aggregate):
    def __init__(self, total: bool = False) -> None:
        self.acc: Any = None
        self.total = total

    def step(self, v) -> None:
        if v is None:
            return
        n = to_number(v)
        if n is None:
            n = 0
        self.acc = n if self.acc is None else self.acc + n

    def finish(self) -> Any:
        if self.acc is None:
            return 0.0 if self.total else None
        return float(self.acc) if self.total else self.acc


class Avg(Aggregate):
    def __init__(self) -> None:
        self.acc = 0.0
        self.n = 0

    def step(self, v) -> None:
        if v is None:
            return
        n = to_number(v)
        self.acc += n if n is not None else 0
        self.n += 1

    def finish(self) -> Any:
        return self.acc / self.n if self.n else None


class Min(Aggregate):
    def __init__(self) -> None:
        self.best: Any = None

    def step(self, v) -> None:
        if v is None:
            return
        if self.best is None or compare(v, self.best) < 0:
            self.best = v

    def finish(self) -> Any:
        return self.best


class Max(Min):
    def step(self, v) -> None:
        if v is None:
            return
        if self.best is None or compare(v, self.best) > 0:
            self.best = v


class GroupConcat(Aggregate):
    def __init__(self) -> None:
        self.parts: List[str] = []
        self.sep = ","

    def step(self, v, sep=",") -> None:
        if sep is not None:
            self.sep = to_text(sep)
        if v is not None:
            self.parts.append(to_text(v))

    def finish(self) -> Any:
        return self.sep.join(self.parts) if self.parts else None


AGGREGATES: Dict[str, Callable[[], Aggregate]] = {
    "COUNT": Count,
    "SUM": Sum,
    "TOTAL": lambda: Sum(total=True),
    "AVG": Avg,
    "MIN": Min,
    "MAX": Max,
    "GROUP_CONCAT": GroupConcat,
    "STRING_AGG": GroupConcat,
}


def is_aggregate(name: str, nargs: int) -> bool:
    name = name.upper()
    if name in ("MIN", "MAX"):
        return nargs == 1
    return name in AGGREGATES


def lookup_scalar(name: str, nargs: int) -> Callable[..., Any]:
    name = name.upper()
    if name in ("MIN", "MAX"):
        if nargs < 2:
            raise ProgrammingError(f"{name}() with one argument is an aggregate")
        return MULTI_ARG_SCALARS[name]
    fn = SCALARS.get(name)
    if fn is None:
        raise ProgrammingError(f"no such function: {name}")
    return fn
