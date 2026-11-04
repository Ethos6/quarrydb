"""Compile AST expressions into Python closures.

A compiled expression is a function ``fn(row) -> value``.  In *row mode* the
argument is a flat list holding every column of every table in scope (see
:class:`Scope`).  In *group mode* (aggregate queries) the argument is a pair
``(first_row, rows)`` for one group: plain column references read from
``first_row`` and aggregate calls fold over ``rows``.

Compiling once per statement keeps per-row evaluation cheap: column lookups
become list indexing and operators become direct function calls.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import ast as A
from . import functions as F
from . import values as V
from .errors import ProgrammingError, SQLError

Fn = Callable[[Any], Any]

ROWID_NAMES = {"rowid", "_rowid_", "oid"}


class TableSlot:
    __slots__ = ("alias", "names", "offset", "table", "lower")

    def __init__(self, alias: str, names: Sequence[str], offset: int, table=None) -> None:
        self.alias = alias
        self.names = list(names)
        self.offset = offset  # position of the hidden rowid slot in the row
        self.table = table  # catalog TableInfo or None for derived tables
        self.lower = {n.lower(): i for i, n in enumerate(self.names)}

    @property
    def width(self) -> int:
        return len(self.names) + 1

    def column_position(self, name: str) -> Optional[int]:
        """Absolute row index of ``name`` (or the rowid) in this slot, or None."""
        i = self.lower.get(name.lower())
        if i is not None:
            return self.offset + 1 + i
        if name.lower() in ROWID_NAMES and self.table is not None:
            return self.offset
        return None


class Scope:
    def __init__(self, parent: Optional["Scope"] = None) -> None:
        self.slots: List[TableSlot] = []
        self.width = 0
        self.parent = parent
        self.outer_cell: List[Any] = [None]  # holds the parent's current row during a correlated run
        self.used_outer = False

    def add(self, alias: str, names: Sequence[str], table=None) -> TableSlot:
        slot = TableSlot(alias, names, self.width, table)
        self.slots.append(slot)
        self.width += slot.width
        return slot

    def find_slot(self, alias: str) -> Optional[TableSlot]:
        a = alias.lower()
        for s in self.slots:
            if s.alias.lower() == a or (s.table is not None and s.table.name.lower() == a):
                return s
        return None

    def resolve_local(self, table: Optional[str], name: str) -> Optional[Tuple[TableSlot, int]]:
        if table is not None:
            slot = self.find_slot(table)
            if slot is None:
                return None
            pos = slot.column_position(name)
            if pos is None:
                raise ProgrammingError(f"no such column: {table}.{name}")
            return slot, pos
        matches = [(s, p) for s in self.slots for p in [s.column_position(name)] if p is not None]
        if not matches:
            return None
        if len(matches) > 1:
            # Prefer a real column over an implicit rowid alias.
            real = [(s, p) for s, p in matches if p != s.offset]
            if len(real) == 1:
                return real[0]
            raise ProgrammingError(f"ambiguous column name: {name}")
        return matches[0]

    def slot_index_of_position(self, pos: int) -> int:
        for i, s in enumerate(self.slots):
            if s.offset <= pos < s.offset + s.width:
                return i
        raise IndexError(pos)


class Compiler:
    def __init__(self, executor, params: Optional[Dict[Any, Any]] = None) -> None:
        self.executor = executor
        self.params = params or {}
        self.param_count_used = 0

    # ------------------------------------------------------------------ #
    # Entry                                                               #
    # ------------------------------------------------------------------ #

    def compile(self, e: A.Expr, scope: Scope, mode: str = "row") -> Fn:
        m = getattr(self, "_c_" + type(e).__name__, None)
        if m is None:
            raise SQLError(f"unsupported expression: {type(e).__name__}")
        return m(e, scope, mode)

    def constant(self, e: A.Expr) -> Any:
        """Evaluate an expression that must not reference any column."""
        fn = self.compile(e, Scope(), "row")
        return fn([])

    # ------------------------------------------------------------------ #
    # Leaves                                                              #
    # ------------------------------------------------------------------ #

    def _c_Literal(self, e: A.Literal, scope, mode) -> Fn:
        v = e.value
        return lambda row: v

    def _c_Param(self, e: A.Param, scope, mode) -> Fn:
        key = e.key
        if key not in self.params:
            if isinstance(key, int):
                raise ProgrammingError(f"missing value for parameter {key} (got {len(self.params)})")
            raise ProgrammingError(f"missing value for named parameter :{key}")
        v = self.params[key]
        if isinstance(v, bool):
            v = int(v)
        elif not (v is None or isinstance(v, (int, float, str, bytes))):
            if isinstance(v, (bytearray, memoryview)):
                v = bytes(v)
            else:
                raise ProgrammingError(f"unsupported parameter type: {type(v).__name__}")
        return lambda row: v

    def _c_Column(self, e: A.Column, scope: Scope, mode) -> Fn:
        found = scope.resolve_local(e.table, e.name)
        if found is not None:
            pos = found[1]
            if mode == "group":
                return lambda g: g[0][pos]
            return lambda row: row[pos]
        if scope.parent is not None:
            # Correlated reference: read from the parent's current row.
            parent_fn = self.compile(e, scope.parent, "row")
            scope.used_outer = True
            cell = scope.outer_cell
            return lambda row: parent_fn(cell[0])
        name = f"{e.table}.{e.name}" if e.table else e.name
        raise ProgrammingError(f"no such column: {name}")

    def _c_Star(self, e: A.Star, scope, mode) -> Fn:
        raise SQLError("'*' is only valid in a SELECT list or COUNT(*)")

    # ------------------------------------------------------------------ #
    # Operators                                                           #
    # ------------------------------------------------------------------ #

    def _c_Unary(self, e: A.Unary, scope, mode) -> Fn:
        f = self.compile(e.operand, scope, mode)
        if e.op == "NOT":
            return lambda row: V.not_(V.truth(f(row)))
        if e.op == "-":
            def neg(row):
                v = f(row)
                n = V.to_number(v)
                return None if n is None else -n
            return neg
        if e.op == "+":
            return lambda row: V.to_number(f(row))
        raise SQLError(f"unknown unary operator {e.op}")

    def _c_Binary(self, e: A.Binary, scope, mode) -> Fn:
        op = e.op
        lf = self.compile(e.left, scope, mode)
        rf = self.compile(e.right, scope, mode)
        if op == "AND":
            def and_fn(row):
                a = V.truth(lf(row))
                if a is False:
                    return 0
                return V.and_(1 if a else None, rf(row))
            return and_fn
        if op == "OR":
            def or_fn(row):
                a = V.truth(lf(row))
                if a is True:
                    return 1
                return V.or_(0 if a is False else None, rf(row))
            return or_fn
        if op in ("=", "<>", "<", "<=", ">", ">="):
            cmp = V.compare
            lf, rf = self._apply_comparison_affinity(e.left, lf, e.right, rf, scope)
            if op == "=":
                return lambda row: _cmp_result(cmp(lf(row), rf(row)), lambda c: c == 0)
            if op == "<>":
                return lambda row: _cmp_result(cmp(lf(row), rf(row)), lambda c: c != 0)
            if op == "<":
                return lambda row: _cmp_result(cmp(lf(row), rf(row)), lambda c: c < 0)
            if op == "<=":
                return lambda row: _cmp_result(cmp(lf(row), rf(row)), lambda c: c <= 0)
            if op == ">":
                return lambda row: _cmp_result(cmp(lf(row), rf(row)), lambda c: c > 0)
            return lambda row: _cmp_result(cmp(lf(row), rf(row)), lambda c: c >= 0)
        if op == "IS":
            return lambda row: 1 if _is_equal(lf(row), rf(row)) else 0
        if op == "IS NOT":
            return lambda row: 0 if _is_equal(lf(row), rf(row)) else 1
        if op == "||":
            return lambda row: V.concat(lf(row), rf(row))
        if op in ("+", "-", "*", "/", "%"):
            arith = V.arith
            return lambda row: arith(op, lf(row), rf(row))
        raise SQLError(f"unknown operator {op}")

    def _affinity_of(self, e: A.Expr, scope: Scope) -> Optional[str]:
        """Declared affinity of a column reference, else None (no affinity)."""
        if not isinstance(e, A.Column):
            return None
        sc: Optional[Scope] = scope
        while sc is not None:
            found = sc.resolve_local(e.table, e.name)
            if found is not None:
                slot, pos = found
                if slot.table is None:
                    return None
                if pos == slot.offset:
                    return "INTEGER"
                return slot.table.columns[pos - slot.offset - 1].affinity
            sc = sc.parent
        return None

    def _apply_comparison_affinity(self, le: A.Expr, lf: Fn, re_: A.Expr, rf: Fn, scope: Scope) -> Tuple[Fn, Fn]:
        """SQLite comparison rule: a numeric column coerces a text/untyped operand, a text column coerces an untyped one."""
        la, ra = self._affinity_of(le, scope), self._affinity_of(re_, scope)
        if la == ra:
            return lf, rf
        numeric = ("INTEGER", "REAL")
        if la in numeric and (ra is None or ra in ("TEXT", "BLOB")):
            return lf, _coerce(rf, la)
        if ra in numeric and (la is None or la in ("TEXT", "BLOB")):
            return _coerce(lf, ra), rf
        if la == "TEXT" and ra is None:
            return lf, _coerce(rf, "TEXT")
        if ra == "TEXT" and la is None:
            return _coerce(lf, "TEXT"), rf
        return lf, rf

    def _c_IsNull(self, e: A.IsNull, scope, mode) -> Fn:
        f = self.compile(e.operand, scope, mode)
        if e.negated:
            return lambda row: 0 if f(row) is None else 1
        return lambda row: 1 if f(row) is None else 0

    def _c_Between(self, e: A.Between, scope, mode) -> Fn:
        f = self.compile(e.operand, scope, mode)
        lo = self.compile(e.low, scope, mode)
        hi = self.compile(e.high, scope, mode)
        _, lo = self._apply_comparison_affinity(e.operand, f, e.low, lo, scope)
        _, hi = self._apply_comparison_affinity(e.operand, f, e.high, hi, scope)
        neg = e.negated

        def between(row):
            v = f(row)
            a = V.compare(v, lo(row))
            b = V.compare(v, hi(row))
            if a is None or b is None:
                # NULL unless one side already decides it
                if (a is not None and a < 0) or (b is not None and b > 0):
                    return 1 if neg else 0
                return None
            r = a >= 0 and b <= 0
            return int(r != neg)
        return between

    def _c_Like(self, e: A.Like, scope, mode) -> Fn:
        f = self.compile(e.operand, scope, mode)
        p = self.compile(e.pattern, scope, mode)
        esc = self.compile(e.escape, scope, mode) if e.escape is not None else None
        glob = e.glob
        neg = e.negated

        def like(row):
            r = V.like(f(row), p(row), esc(row) if esc else None, glob)
            if r is None:
                return None
            return int(bool(r) != neg)
        return like

    def _c_In(self, e: A.In, scope, mode) -> Fn:
        f = self.compile(e.operand, scope, mode)
        neg = e.negated
        if isinstance(e.items, A.Select):
            runner = self._subquery_runner(e.items, scope, mode, want="column")

            def in_sub(row):
                v = f(row)
                if v is None:
                    return None
                vals = runner(row)
                return _in_result(v, vals, neg)
            return in_sub
        item_fns = [self._apply_comparison_affinity(e.operand, f, x, self.compile(x, scope, mode), scope)[1] for x in e.items]
        if all(isinstance(x, A.Literal) for x in e.items):
            consts = [g(None) for g in item_fns]
            return lambda row: _in_result(f(row), consts, neg)
        return lambda row: _in_result(f(row), [g(row) for g in item_fns], neg)

    def _c_Case(self, e: A.Case, scope, mode) -> Fn:
        base = self.compile(e.operand, scope, mode) if e.operand is not None else None
        whens = [(self.compile(c, scope, mode), self.compile(r, scope, mode)) for c, r in e.whens]
        else_ = self.compile(e.else_, scope, mode) if e.else_ is not None else (lambda row: None)

        if base is None:
            def case_fn(row):
                for c, r in whens:
                    if V.is_true(c(row)):
                        return r(row)
                return else_(row)
        else:
            def case_fn(row):
                v = base(row)
                for c, r in whens:
                    if V.compare(v, c(row)) == 0:
                        return r(row)
                return else_(row)
        return case_fn

    def _c_Cast(self, e: A.Cast, scope, mode) -> Fn:
        f = self.compile(e.operand, scope, mode)
        t = e.type_name
        return lambda row: V.cast(f(row), t)

    def _c_Exists(self, e: A.Exists, scope, mode) -> Fn:
        runner = self._subquery_runner(e.query, scope, mode, want="rows")
        neg = e.negated
        return lambda row: int(bool(runner(row)) != neg)

    def _c_Subquery(self, e: A.Subquery, scope, mode) -> Fn:
        runner = self._subquery_runner(e.query, scope, mode, want="scalar")
        return runner

    def _subquery_runner(self, query: A.Select, scope: Scope, mode: str, want: str) -> Fn:
        inner_scope_holder: List[Scope] = []
        plan = self.executor.plan_select(query, parent_scope=scope, scope_out=inner_scope_holder, compiler=self)
        inner_scope = inner_scope_holder[0]
        if want == "column" and len(plan.columns) != 1:
            raise ProgrammingError("subquery in IN must return exactly one column")
        if want == "scalar" and len(plan.columns) != 1:
            raise ProgrammingError("scalar subquery must return exactly one column")
        correlated = inner_scope.used_outer
        cache: List[Any] = []

        def run(outer_row):
            if not correlated and cache:
                return cache[0]
            inner_scope.outer_cell[0] = outer_row
            rows = plan.run()
            if want == "rows":
                result: Any = rows
            elif want == "column":
                result = [r[0] for r in rows]
            else:
                result = rows[0][0] if rows else None
            if not correlated:
                cache.append(result)
            return result

        if mode == "group":
            return lambda g: run(g[0])
        return run

    # ------------------------------------------------------------------ #
    # Function calls                                                      #
    # ------------------------------------------------------------------ #

    def _c_Call(self, e: A.Call, scope: Scope, mode: str) -> Fn:
        name = e.name.upper()
        if F.is_aggregate(name, len(e.args)):
            if mode != "group":
                raise ProgrammingError(f"misuse of aggregate function {name}()")
            return self._aggregate(e, scope)
        if any(isinstance(a, A.Star) for a in e.args):
            raise ProgrammingError(f"{name}(*) is not valid")
        fn = F.lookup_scalar(name, len(e.args))
        arg_fns = [self.compile(a, scope, mode) for a in e.args]
        if not arg_fns:
            return lambda row: fn()
        if len(arg_fns) == 1:
            a0 = arg_fns[0]
            return lambda row: fn(a0(row))
        if len(arg_fns) == 2:
            a0, a1 = arg_fns
            return lambda row: fn(a0(row), a1(row))
        return lambda row: fn(*[a(row) for a in arg_fns])

    def _aggregate(self, e: A.Call, scope: Scope) -> Fn:
        name = e.name.upper()
        factory = F.AGGREGATES[name]
        star = len(e.args) == 1 and isinstance(e.args[0], A.Star)
        if name == "COUNT" and (star or not e.args):
            if e.distinct:
                raise ProgrammingError("COUNT(DISTINCT *) is not valid")
            return lambda g: len(g[1])
        if not e.args:
            raise ProgrammingError(f"{name}() needs an argument")
        arg_fns = [self.compile(a, scope, "row") for a in e.args]
        distinct = e.distinct
        first = arg_fns[0]
        rest = arg_fns[1:]

        def agg(g):
            acc = factory()
            seen: Optional[Set[Any]] = set() if distinct else None
            for row in g[1]:
                v = first(row)
                if seen is not None:
                    key = (V.rank(v), v)
                    if key in seen:
                        continue
                    seen.add(key)
                if rest:
                    acc.step(v, *[r(row) for r in rest])
                else:
                    acc.step(v)
            return acc.finish()
        return agg


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _coerce(fn: Fn, affinity: str) -> Fn:
    apply = V.apply_affinity
    return lambda row: apply(affinity, fn(row))


def _cmp_result(c: Optional[int], pred) -> Optional[int]:
    if c is None:
        return None
    return 1 if pred(c) else 0


def _is_equal(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return V.compare(a, b) == 0


def _in_result(v: Any, items: Sequence[Any], negated: bool) -> Optional[int]:
    if v is None:
        return None
    saw_null = False
    for item in items:
        if item is None:
            saw_null = True
            continue
        if V.compare(v, item) == 0:
            return 0 if negated else 1
    if saw_null:
        return None
    return 1 if negated else 0


def contains_aggregate(e: A.Expr) -> bool:
    """True if the expression contains an aggregate call outside any subquery."""
    if isinstance(e, A.Call):
        if F.is_aggregate(e.name, len(e.args)):
            return True
        return any(contains_aggregate(a) for a in e.args)
    if isinstance(e, A.Unary):
        return contains_aggregate(e.operand)
    if isinstance(e, A.Binary):
        return contains_aggregate(e.left) or contains_aggregate(e.right)
    if isinstance(e, A.IsNull):
        return contains_aggregate(e.operand)
    if isinstance(e, A.Between):
        return any(contains_aggregate(x) for x in (e.operand, e.low, e.high))
    if isinstance(e, A.Like):
        return contains_aggregate(e.operand) or contains_aggregate(e.pattern)
    if isinstance(e, A.In):
        if contains_aggregate(e.operand):
            return True
        return not isinstance(e.items, A.Select) and any(contains_aggregate(x) for x in e.items)
    if isinstance(e, A.Case):
        if e.operand is not None and contains_aggregate(e.operand):
            return True
        return any(contains_aggregate(c) or contains_aggregate(r) for c, r in e.whens) or (
            e.else_ is not None and contains_aggregate(e.else_)
        )
    if isinstance(e, A.Cast):
        return contains_aggregate(e.operand)
    return False


def column_refs(e: A.Expr, out: Optional[List[A.Column]] = None) -> Optional[List[A.Column]]:
    """Collect column references; return None if the expression contains a subquery."""
    if out is None:
        out = []
    if isinstance(e, A.Column):
        out.append(e)
    elif isinstance(e, (A.Literal, A.Param, A.Star)):
        pass
    elif isinstance(e, A.Unary):
        if column_refs(e.operand, out) is None:
            return None
    elif isinstance(e, A.Binary):
        if column_refs(e.left, out) is None or column_refs(e.right, out) is None:
            return None
    elif isinstance(e, A.IsNull):
        if column_refs(e.operand, out) is None:
            return None
    elif isinstance(e, A.Between):
        for x in (e.operand, e.low, e.high):
            if column_refs(x, out) is None:
                return None
    elif isinstance(e, A.Like):
        for x in (e.operand, e.pattern, e.escape):
            if x is not None and column_refs(x, out) is None:
                return None
    elif isinstance(e, A.In):
        if isinstance(e.items, A.Select):
            return None
        if column_refs(e.operand, out) is None:
            return None
        for x in e.items:
            if column_refs(x, out) is None:
                return None
    elif isinstance(e, A.Call):
        for x in e.args:
            if column_refs(x, out) is None:
                return None
    elif isinstance(e, A.Case):
        if e.operand is not None and column_refs(e.operand, out) is None:
            return None
        for c, r in e.whens:
            if column_refs(c, out) is None or column_refs(r, out) is None:
                return None
        if e.else_ is not None and column_refs(e.else_, out) is None:
            return None
    elif isinstance(e, A.Cast):
        if column_refs(e.operand, out) is None:
            return None
    else:
        return None
    return out


def split_conjuncts(e: Optional[A.Expr]) -> List[A.Expr]:
    if e is None:
        return []
    if isinstance(e, A.Binary) and e.op == "AND":
        return split_conjuncts(e.left) + split_conjuncts(e.right)
    return [e]
