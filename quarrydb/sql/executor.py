"""Statement execution: planning, access paths, joins, aggregation and DML."""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..catalog import Catalog, ColumnInfo, IndexInfo, TableInfo
from ..encoding import decode_record, decode_rowid, encode_key, encode_record, encode_rowid, key_successor
from . import ast as A
from . import values as V
from .compiler import Compiler, Scope, TableSlot, column_refs, contains_aggregate, split_conjuncts
from .errors import IntegrityError, ProgrammingError, SQLError
from .parser import parse_expr
from .sqlgen import create_index_sql, create_table_sql, to_sql

Row = List[Any]


class Result:
    __slots__ = ("columns", "rows", "rowcount", "lastrowid")

    def __init__(self, columns: Optional[List[str]] = None, rows: Optional[List[tuple]] = None,
                 rowcount: int = -1, lastrowid: Optional[int] = None) -> None:
        self.columns = columns or []
        self.rows = rows or []
        self.rowcount = rowcount
        self.lastrowid = lastrowid


# --------------------------------------------------------------------------- #
# Access paths                                                                #
# --------------------------------------------------------------------------- #


class AccessPath:
    """How to fetch the rows of one table given the rows produced so far."""

    __slots__ = ("kind", "index", "eq_fns", "lo_fn", "lo_incl", "hi_fn", "hi_incl", "eq_cols", "range_col", "ordered_cols")

    def __init__(self, kind: str = "scan", index: Optional[IndexInfo] = None) -> None:
        self.kind = kind  # scan | rowid_eq | rowid_range | index_eq | index_range
        self.index = index
        self.eq_fns: List[Callable] = []
        self.eq_cols: List[str] = []
        self.lo_fn: Optional[Callable] = None
        self.lo_incl = True
        self.hi_fn: Optional[Callable] = None
        self.hi_incl = True
        self.range_col: Optional[str] = None
        self.ordered_cols: List[str] = []  # output order guaranteed by this path (ascending)

    def describe(self, table: TableInfo) -> str:
        if self.kind == "scan":
            return f"SCAN TABLE {table.name}"
        if self.kind == "rowid_eq":
            return f"SEARCH TABLE {table.name} USING INTEGER PRIMARY KEY (rowid=?)"
        if self.kind == "rowid_range":
            return f"SEARCH TABLE {table.name} USING INTEGER PRIMARY KEY (rowid{self._range_desc()})"
        assert self.index is not None
        parts = [f"{c}=?" for c in self.eq_cols]
        if self.range_col:
            parts.append(f"{self.range_col}{self._range_desc()}")
        return f"SEARCH TABLE {table.name} USING INDEX {self.index.name} ({' AND '.join(parts)})"

    def _range_desc(self) -> str:
        s = ""
        if self.lo_fn is not None:
            s += ">=?" if self.lo_incl else ">?"
        if self.hi_fn is not None:
            s += (" AND " if s else "") + ("<=?" if self.hi_incl else "<?")
        return s


class SourceStep:
    """One table (or derived table) in a left-deep join pipeline."""

    __slots__ = ("slot", "kind", "path", "filters", "filter_sql", "subplan", "table", "on")

    def __init__(self, slot: TableSlot, kind: str) -> None:
        self.slot = slot
        self.kind = kind  # INNER | LEFT | CROSS
        self.on: Optional[A.Expr] = None
        self.path = AccessPath()
        self.filters: List[Callable] = []
        self.filter_sql: List[str] = []
        self.subplan: Optional["SelectPlan"] = None
        self.table: Optional[TableInfo] = slot.table


# --------------------------------------------------------------------------- #
# SELECT plan                                                                 #
# --------------------------------------------------------------------------- #


class SelectPlan:
    def __init__(self, executor: "Executor", scope: Scope) -> None:
        self.executor = executor
        self.scope = scope
        self.steps: List[SourceStep] = []
        self.post_filters: List[Callable] = []
        self.post_filter_sql: List[str] = []
        self.columns: List[str] = []
        self.item_fns: List[Callable] = []
        self.aggregate = False
        self.group_fns: List[Callable] = []
        self.having_fn: Optional[Callable] = None
        self.distinct = False
        # ORDER BY terms as (output column position, descending).  When a term is
        # not an output column, ``order_fns`` holds one key function per term
        # (evaluated on the source row / group) and the positions are unused.
        self.order: List[Tuple[int, bool]] = []
        self.order_fns: List[Callable] = []
        self.order_nulls: List[Optional[str]] = []
        self.limit: Optional[int] = None
        self.offset: int = 0
        self.compound: List[Tuple[str, "SelectPlan"]] = []
        self.order_satisfied = False
        self.width = 0

    # ----------------------------- execution ---------------------------- #
    #
    # Every core produces a list of (output_row, sort_keys) pairs.  Sort keys
    # are computed from the source row (or group) when ORDER BY references
    # something that is not an output column; they are None otherwise.

    def run(self) -> List[tuple]:
        if self.compound:
            rows = [r for r, _ in self._run_core()]
            for op, plan in self.compound:
                if len(plan.columns) != len(self.columns):
                    raise ProgrammingError(
                        "SELECTs to the left and right of a compound operator have different column counts")
                rows = _combine(op, rows, [r for r, _ in plan._run_core()])
            pairs = [(r, None) for r in rows]
        else:
            pairs = self._run_core()
            if self.distinct:
                pairs = _dedupe_pairs(pairs)
        if self.order and not self.order_satisfied:
            pairs = self._sort(pairs)
        return self._limit([r for r, _ in pairs])

    def _run_core(self) -> List[Tuple[tuple, Optional[list]]]:
        return self._run_aggregate() if self.aggregate else self._run_plain()

    def _run_plain(self) -> List[Tuple[tuple, Optional[list]]]:
        fns = self.item_fns
        out: List[Tuple[tuple, Optional[list]]] = []
        if self.order_fns:
            ofns = self.order_fns
            for row in self._produce():
                out.append((tuple(f(row) for f in fns), [k(row) for k in ofns]))
            return out
        # No sort needed: stop as soon as LIMIT is satisfied.
        stop = None
        if self.limit is not None and (not self.order or self.order_satisfied) and not self.distinct and not self.compound:
            stop = self.limit + self.offset
        for row in self._produce():
            out.append((tuple(f(row) for f in fns), None))
            if stop is not None and len(out) >= stop:
                break
        return out

    def _run_aggregate(self) -> List[Tuple[tuple, Optional[list]]]:
        if self.group_fns:
            groups: Dict[tuple, Tuple[Row, List[Row]]] = {}
            order: List[tuple] = []
            gfns = self.group_fns
            for row in self._produce():
                key = tuple((V.rank(v), v) for v in (g(row) for g in gfns))
                grp = groups.get(key)
                if grp is None:
                    groups[key] = (row, [row])
                    order.append(key)
                else:
                    grp[1].append(row)
            glist = [groups[k] for k in order]
        else:
            rows = list(self._produce())
            glist = [(rows[0] if rows else [None] * self.scope.width, rows)]
        fns = self.item_fns
        having = self.having_fn
        ofns = self.order_fns
        out: List[Tuple[tuple, Optional[list]]] = []
        for g in glist:
            if having is not None and not V.is_true(having(g)):
                continue
            out.append((tuple(f(g) for f in fns), [k(g) for k in ofns] if ofns else None))
        return out

    def _sort(self, pairs: List[Tuple[tuple, Optional[list]]]) -> List[Tuple[tuple, Optional[list]]]:
        descs = [d for _, d in self.order]
        if self.order_fns:
            keyed = pairs
        else:
            positions = [p for p, _ in self.order]
            keyed = [(r, [r[i] for i in positions]) for r, _ in pairs]
        for i in reversed(range(len(descs))):
            nulls = self.order_nulls[i] if i < len(self.order_nulls) else None
            # Default: NULLs sort first ascending (hence last descending).
            null_rank = 2 if (nulls == "LAST") != descs[i] and nulls is not None else 0
            keyed.sort(key=lambda kr, i=i, nr=null_rank: (nr if kr[1][i] is None else 1, V.sort_key(kr[1][i])),  # type: ignore[index]
                       reverse=descs[i])
        return keyed

    def _limit(self, rows: List[tuple]) -> List[tuple]:
        if self.offset:
            rows = rows[self.offset :]
        if self.limit is not None:
            rows = rows[: self.limit]
        return rows

    # ----------------------------- row production ----------------------- #

    def _produce(self) -> Iterator[Row]:
        rows: Iterable[Row] = [[None] * self.scope.width]
        for step in self.steps:
            rows = self._join(rows, step)
        if self.post_filters:
            pf = self.post_filters
            for row in rows:
                if all(V.is_true(f(row)) for f in pf):
                    yield row
        else:
            yield from rows

    def _join(self, lefts: Iterable[Row], step: SourceStep) -> Iterator[Row]:
        slot = step.slot
        off, width = slot.offset, slot.width
        filters = step.filters
        left_join = step.kind == "LEFT"
        for left in lefts:
            matched = False
            for seg in self._fetch(step, left):
                row = left[:]
                row[off : off + width] = seg
                if filters and not all(V.is_true(f(row)) for f in filters):
                    continue
                matched = True
                yield row
            if left_join and not matched:
                row = left[:]
                row[off : off + width] = [None] * width
                yield row

    def _fetch(self, step: SourceStep, left: Row) -> Iterator[Row]:
        if step.subplan is not None:
            step.subplan.scope.outer_cell[0] = left
            for r in step.subplan.run():
                yield [None, *r]
            return
        table = step.table
        assert table is not None
        ex = self.executor
        tree = ex.catalog.tree_for(table)
        path = step.path
        kind = path.kind
        if kind == "scan":
            for key, val in tree.seek(b""):
                yield ex.decode_row(table, decode_rowid(key), val)
        elif kind == "rowid_eq":
            v = V.apply_affinity("INTEGER", path.eq_fns[0](left))
            if isinstance(v, float) and v == int(v):
                v = int(v)
            if isinstance(v, int) and not isinstance(v, bool):
                val = tree.get(encode_rowid(v))
                if val is not None:
                    yield ex.decode_row(table, v, val)
        elif kind == "rowid_range":
            lo, hi = self._rowid_bounds(path, left)
            if lo is None:
                return
            for key, val in tree.scan(lo, hi):
                yield ex.decode_row(table, decode_rowid(key), val)
        else:
            assert path.index is not None
            itree = ex.catalog.tree_for(path.index)
            bounds = self._index_bounds(path, left)
            if bounds is None:
                return
            lo, hi = bounds
            for key, _ in itree.scan(lo, hi):
                rowid = decode_rowid(key[-8:])
                val = tree.get(encode_rowid(rowid))
                if val is None:
                    continue
                yield ex.decode_row(table, rowid, val)

    def _rowid_bounds(self, path: AccessPath, left: Row) -> Tuple[Optional[bytes], bytes]:
        lo = b""
        hi = b""
        if path.lo_fn is not None:
            v = V.to_number(path.lo_fn(left))
            if v is None:
                return None, b""
            n = int(v) if (isinstance(v, int) or v == int(v)) else int(v) + (1 if v > 0 else 0)
            if not path.lo_incl and n == v:
                n += 1
            lo = encode_rowid(max(min(n, 2**63 - 1), -(2**63)))
        if path.hi_fn is not None:
            v = V.to_number(path.hi_fn(left))
            if v is None:
                return None, b""
            n = int(v) if (isinstance(v, int) or v == int(v)) else int(v) - (1 if v < 0 else 0)
            if path.hi_incl or n != v:
                n += 1  # scan() upper bound is exclusive
            if n > 2**63 - 1:
                hi = b""
            else:
                hi = encode_rowid(max(n, -(2**63)))
        return lo, hi

    def _index_bounds(self, path: AccessPath, left: Row) -> Optional[Tuple[bytes, bytes]]:
        assert path.index is not None
        table = path.index.table
        tinfo = self.executor.catalog.get_table(table)
        eq_vals = []
        for fn, col in zip(path.eq_fns, path.eq_cols):
            v = fn(left)
            if v is None:
                return None  # NULL never matches an equality
            aff = tinfo.columns[tinfo.column_index(col)].affinity  # type: ignore[index]
            eq_vals.append(V.apply_affinity(aff, v))
        prefix = encode_key(eq_vals[:-1]) if eq_vals else b""
        if eq_vals and path.range_col is None:
            last = eq_vals[-1]
            lo = prefix + encode_key([_int_form(last)])
            hi = key_successor(prefix + encode_key([_float_form(last)]))
            return lo, hi
        if eq_vals:
            prefix = encode_key(eq_vals)
        lo = prefix
        hi = key_successor(prefix) if prefix else b""
        if path.lo_fn is not None:
            v = path.lo_fn(left)
            if v is None:
                return None
            lo = prefix + encode_key([_int_form(v)])
        if path.hi_fn is not None:
            v = path.hi_fn(left)
            if v is None:
                return None
            hi = key_successor(prefix + encode_key([_float_form(v)]))
        return lo, hi

    # ----------------------------- explain ------------------------------ #

    def explain(self, indent: int = 0) -> List[str]:
        pad = "  " * indent
        lines = []
        for i, step in enumerate(self.steps):
            if step.subplan is not None:
                lines.append(f"{pad}{'JOIN ' if i else ''}SUBQUERY AS {step.slot.alias}")
                lines.extend(step.subplan.explain(indent + 1))
            else:
                assert step.table is not None
                prefix = "" if i == 0 else f"{step.kind} JOIN "
                desc = step.path.describe(step.table)
                lines.append(f"{pad}{prefix}{desc}")
            if step.filter_sql:
                lines.append(f"{pad}  FILTER {' AND '.join(step.filter_sql)}")
        if not self.steps:
            lines.append(f"{pad}CONSTANT ROW")
        if self.post_filter_sql:
            lines.append(f"{pad}FILTER {' AND '.join(self.post_filter_sql)}")
        if self.aggregate:
            lines.append(f"{pad}AGGREGATE" + (f" GROUP BY {len(self.group_fns)} expr(s)" if self.group_fns else ""))
        if self.distinct:
            lines.append(f"{pad}DISTINCT")
        if self.order:
            lines.append(f"{pad}ORDER BY " + ("(satisfied by index)" if self.order_satisfied else f"{len(self.order)} term(s) via sort"))
        if self.limit is not None or self.offset:
            lines.append(f"{pad}LIMIT {self.limit} OFFSET {self.offset}")
        for op, plan in self.compound:
            lines.append(f"{pad}{op}")
            lines.extend(plan.explain(indent + 1))
        return lines


def _int_form(v: Any) -> Any:
    if isinstance(v, float) and v == int(v) and abs(v) < 2**63:
        return int(v)
    return v


def _float_form(v: Any) -> Any:
    if isinstance(v, bool):
        return float(int(v))
    if isinstance(v, int) and abs(v) < 2**63:
        return float(v)
    return v


def _dedupe(rows: List[tuple]) -> List[tuple]:
    seen = set()
    out = []
    for r in rows:
        k = tuple((V.rank(v), v) for v in r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _dedupe_pairs(pairs: List[Tuple[tuple, Optional[list]]]) -> List[Tuple[tuple, Optional[list]]]:
    seen = set()
    out = []
    for p in pairs:
        k = tuple((V.rank(v), v) for v in p[0])
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def _combine(op: str, a: List[tuple], b: List[tuple]) -> List[tuple]:
    if op == "UNION ALL":
        return a + b
    if op == "UNION":
        return _dedupe(a + b)
    keys_b = {tuple((V.rank(v), v) for v in r) for r in b}
    if op == "INTERSECT":
        return _dedupe([r for r in a if tuple((V.rank(v), v) for v in r) in keys_b])
    if op == "EXCEPT":
        return _dedupe([r for r in a if tuple((V.rank(v), v) for v in r) not in keys_b])
    raise SQLError(f"unknown compound operator {op}")


# --------------------------------------------------------------------------- #
# Executor                                                                    #
# --------------------------------------------------------------------------- #


class Executor:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self._default_cache: Dict[str, A.Expr] = {}

    # ----------------------------- rows --------------------------------- #

    def decode_row(self, table: TableInfo, rowid: int, raw: bytes) -> Row:
        values = decode_record(raw)
        if table.rowid_column is not None:
            values[table.rowid_column] = rowid
        values.insert(0, rowid)
        return values

    # ----------------------------- dispatch ----------------------------- #

    def execute(self, stmt: A.Statement, params: Dict[Any, Any]) -> Result:
        if isinstance(stmt, A.Select):
            plan = self.plan_select(stmt, compiler=Compiler(self, params))
            return Result(plan.columns, plan.run())
        if isinstance(stmt, A.Insert):
            return self.exec_insert(stmt, params)
        if isinstance(stmt, A.Update):
            return self.exec_update(stmt, params)
        if isinstance(stmt, A.Delete):
            return self.exec_delete(stmt, params)
        if isinstance(stmt, A.CreateTable):
            return self.exec_create_table(stmt, params)
        if isinstance(stmt, A.DropTable):
            if not self.catalog.has_table(stmt.name):
                if stmt.if_exists:
                    return Result()
                raise ProgrammingError(f"no such table: {stmt.name}")
            self.catalog.drop_table(stmt.name)
            return Result()
        if isinstance(stmt, A.CreateIndex):
            return self.exec_create_index(stmt)
        if isinstance(stmt, A.DropIndex):
            if not self.catalog.has_index(stmt.name):
                if stmt.if_exists:
                    return Result()
                raise ProgrammingError(f"no such index: {stmt.name}")
            if self.catalog.get_index(stmt.name).auto:
                raise ProgrammingError(f"index {stmt.name} is associated with a constraint and cannot be dropped")
            self.catalog.drop_index(stmt.name)
            return Result()
        if isinstance(stmt, A.Explain):
            return self.exec_explain(stmt, params)
        raise SQLError(f"unsupported statement {type(stmt).__name__}")

    def exec_explain(self, stmt: A.Explain, params: Dict[Any, Any]) -> Result:
        inner = stmt.statement
        comp = Compiler(self, params)
        if isinstance(inner, A.Select):
            lines = self.plan_select(inner, compiler=comp).explain()
        elif isinstance(inner, (A.Update, A.Delete)):
            table = self.catalog.get_table(inner.table)
            scope = Scope()
            scope.add(table.name, table.column_names, table)
            step = SourceStep(scope.slots[0], "INNER")
            self._plan_access(step, split_conjuncts(inner.where), scope, comp, 0)
            lines = [step.path.describe(table)]
            if step.filter_sql:
                lines.append(f"  FILTER {' AND '.join(step.filter_sql)}")
            lines.append("UPDATE" if isinstance(inner, A.Update) else "DELETE")
        else:
            lines = [type(inner).__name__.upper()]
        return Result(["plan"], [(ln,) for ln in lines])

    # ----------------------------- SELECT planning ---------------------- #

    def plan_select(self, sel: A.Select, parent_scope: Optional[Scope] = None,
                    scope_out: Optional[List[Scope]] = None, compiler: Optional[Compiler] = None) -> SelectPlan:
        comp = compiler or Compiler(self, {})
        scope = Scope(parent_scope)
        if scope_out is not None:
            scope_out.append(scope)
        plan = SelectPlan(self, scope)

        # FROM clause -> linear join steps
        refs = _flatten_from(sel.source)
        for ref, kind, on in refs:
            if isinstance(ref, A.TableRef):
                table = self.catalog.get_table(ref.name)
                alias = ref.alias or ref.name
                if scope.find_slot(alias) is not None:
                    raise ProgrammingError(f"ambiguous table alias: {alias}")
                slot = scope.add(alias, table.column_names, table)
                step = SourceStep(slot, kind)
            else:
                assert isinstance(ref, A.SubqueryRef)
                sub_holder: List[Scope] = []
                subplan = self.plan_select(ref.query, parent_scope=scope, scope_out=sub_holder, compiler=comp)
                if scope.find_slot(ref.alias) is not None:
                    raise ProgrammingError(f"ambiguous table alias: {ref.alias}")
                slot = scope.add(ref.alias, subplan.columns, None)
                step = SourceStep(slot, kind)
                step.subplan = subplan
            plan.steps.append(step)
            step.on = on
        plan.width = scope.width

        # Output aliases may be referenced in WHERE / GROUP BY / HAVING (SQLite extension).
        alias_map: Dict[str, A.Expr] = {}
        for it in sel.items:
            if it.alias and not isinstance(it.expr, A.Star):
                alias_map[it.alias.lower()] = it.expr

        # WHERE + ON conjuncts -> pushed to the earliest step that can evaluate them
        where_conjs = split_conjuncts(self._resolve_alias(sel.where, alias_map, scope))
        deferred: List[A.Expr] = []
        per_step: List[List[A.Expr]] = [[] for _ in plan.steps]
        for i, step in enumerate(plan.steps):
            per_step[i].extend(split_conjuncts(step.on))
        for conj in where_conjs:
            pos = self._last_step_referenced(conj, scope)
            if pos is None:
                deferred.append(conj)
                continue
            # A WHERE condition on the null-extended side of a LEFT JOIN must run after the join.
            if plan.steps[pos].kind == "LEFT":
                deferred.append(conj)
            else:
                per_step[pos].append(conj)
        for i, step in enumerate(plan.steps):
            self._plan_access(step, per_step[i], scope, comp, i)
        for conj in deferred:
            plan.post_filters.append(comp.compile(conj, scope, "row"))
            plan.post_filter_sql.append(to_sql(conj))

        # Select list
        items = self._expand_items(sel.items, scope)
        plan.aggregate = bool(sel.group_by) or any(contains_aggregate(e) for e, _ in items) or (
            sel.having is not None
        )
        mode = "group" if plan.aggregate else "row"
        if plan.aggregate:
            self._check_bare_columns(items, sel, scope)
        plan.columns = [name for _, name in items]
        plan.item_fns = [comp.compile(e, scope, mode) for e, _ in items]
        for e, name in items:
            alias_map.setdefault(name.lower(), e)

        if sel.group_by:
            plan.group_fns = [comp.compile(self._resolve_alias(g, alias_map, scope), scope, "row") for g in sel.group_by]
        if sel.having is not None:
            plan.having_fn = comp.compile(self._resolve_alias(sel.having, alias_map, scope), scope, "group")
        plan.distinct = sel.distinct

        # Compound selects
        for op, other in sel.compound:
            plan.compound.append((op, self.plan_select(other, parent_scope=parent_scope, compiler=comp)))

        # ORDER BY: each term is either an output column (by position or name)
        # or an arbitrary expression over the source rows.
        terms: List[Tuple[Optional[int], Optional[A.Expr], bool]] = []
        plan.order_nulls = [n for _, _, n in sel.order_by]
        for expr, desc, _nulls in sel.order_by:
            if isinstance(expr, A.Literal) and isinstance(expr.value, int):
                idx = expr.value - 1
                if not 0 <= idx < len(plan.columns):
                    raise ProgrammingError(f"ORDER BY term out of range: {expr.value}")
                terms.append((idx, None, desc))
                continue
            out_idx = None
            if isinstance(expr, A.Column) and expr.table is None:
                lname = expr.name.lower()
                for i, c in enumerate(plan.columns):
                    if c.lower() == lname:
                        out_idx = i
                        break
                if out_idx is not None and scope.resolve_local(None, expr.name) is not None and not plan.compound:
                    # Name is both an output column and a source column: only an
                    # explicit alias wins (otherwise they are the same thing anyway).
                    if not any(it.alias and it.alias.lower() == lname for it in sel.items):
                        out_idx = None
            if out_idx is None and plan.compound:
                raise ProgrammingError("ORDER BY term does not match any result column")
            terms.append((out_idx, None if out_idx is not None else expr, desc))
        if all(t[1] is None for t in terms):
            plan.order = [(t[0], t[2]) for t in terms]  # type: ignore[misc]
        else:
            plan.order = [(0, t[2]) for t in terms]
            plan.order_fns = [
                plan.item_fns[t[0]] if t[1] is None else comp.compile(t[1], scope, mode)  # type: ignore[index]
                for t in terms
            ]
        if plan.order and not plan.aggregate and not plan.compound and not plan.distinct:
            plan.order_satisfied = self._order_satisfied(plan, sel, scope)

        # LIMIT / OFFSET
        if sel.limit is not None:
            lim = comp.constant(sel.limit)
            if lim is None:
                raise ProgrammingError("LIMIT must be an integer")
            lim = int(V.to_number(lim) or 0)
            plan.limit = None if lim < 0 else lim
        if sel.offset is not None:
            off = comp.constant(sel.offset)
            plan.offset = max(0, int(V.to_number(off) or 0))
        return plan

    def _resolve_alias(self, e: Optional[A.Expr], alias_map: Dict[str, A.Expr], scope: Scope) -> Optional[A.Expr]:
        """Replace references to output aliases (SQLite extension) inside ``e``."""
        if e is None:
            return None
        if isinstance(e, A.Column):
            if e.table is None and scope.resolve_local(None, e.name) is None:
                target = alias_map.get(e.name.lower())
                if target is not None and not (isinstance(target, A.Column) and target.table is None
                                               and target.name.lower() == e.name.lower()):
                    return target
            return e
        if isinstance(e, A.Unary):
            return A.Unary(e.op, self._resolve_alias(e.operand, alias_map, scope))
        if isinstance(e, A.Binary):
            return A.Binary(e.op, self._resolve_alias(e.left, alias_map, scope), self._resolve_alias(e.right, alias_map, scope))
        if isinstance(e, A.IsNull):
            return A.IsNull(self._resolve_alias(e.operand, alias_map, scope), e.negated)
        if isinstance(e, A.Between):
            return A.Between(self._resolve_alias(e.operand, alias_map, scope), self._resolve_alias(e.low, alias_map, scope),
                             self._resolve_alias(e.high, alias_map, scope), e.negated)
        if isinstance(e, A.Like):
            return A.Like(self._resolve_alias(e.operand, alias_map, scope), self._resolve_alias(e.pattern, alias_map, scope),
                          self._resolve_alias(e.escape, alias_map, scope), e.negated, e.glob)
        if isinstance(e, A.In):
            items = e.items if isinstance(e.items, A.Select) else tuple(self._resolve_alias(x, alias_map, scope) for x in e.items)
            return A.In(self._resolve_alias(e.operand, alias_map, scope), items, e.negated)
        if isinstance(e, A.Call):
            return A.Call(e.name, tuple(self._resolve_alias(a, alias_map, scope) for a in e.args), e.distinct)
        if isinstance(e, A.Case):
            return A.Case(self._resolve_alias(e.operand, alias_map, scope),
                          tuple((self._resolve_alias(c, alias_map, scope), self._resolve_alias(r, alias_map, scope)) for c, r in e.whens),
                          self._resolve_alias(e.else_, alias_map, scope))
        if isinstance(e, A.Cast):
            return A.Cast(self._resolve_alias(e.operand, alias_map, scope), e.type_name)
        return e

    def _expand_items(self, items: List[A.SelectItem], scope: Scope) -> List[Tuple[A.Expr, str]]:
        out: List[Tuple[A.Expr, str]] = []
        for it in items:
            if isinstance(it.expr, A.Star):
                slots = scope.slots
                if it.expr.table is not None:
                    s = scope.find_slot(it.expr.table)
                    if s is None:
                        raise ProgrammingError(f"no such table: {it.expr.table}")
                    slots = [s]
                if not slots:
                    raise ProgrammingError("no tables specified for '*'")
                for s in slots:
                    for name in s.names:
                        out.append((A.Column(s.alias, name), name))
            else:
                name = it.alias or (it.expr.name if isinstance(it.expr, A.Column) else to_sql(it.expr))
                out.append((it.expr, name))
        return out

    def _check_bare_columns(self, items, sel: A.Select, scope: Scope) -> None:
        # Bare (non-aggregated) columns are allowed, SQLite-style: they take the
        # value from an arbitrary row of the group.  Nothing to enforce.
        return

    def _last_step_referenced(self, e: A.Expr, scope: Scope) -> Optional[int]:
        refs = column_refs(e)
        if refs is None:
            return None
        last = 0
        for c in refs:
            found = scope.resolve_local(c.table, c.name)
            if found is None:
                if scope.parent is not None:
                    continue  # correlated reference: constant for this query
                raise ProgrammingError(f"no such column: {c.table + '.' if c.table else ''}{c.name}")
            last = max(last, scope.slots.index(found[0]))
        return last

    def _refs_before(self, e: A.Expr, scope: Scope, pos: int) -> bool:
        """True if ``e`` only references steps before ``pos`` (or nothing)."""
        refs = column_refs(e)
        if refs is None:
            return False
        for c in refs:
            found = scope.resolve_local(c.table, c.name)
            if found is None:
                if scope.parent is not None:
                    continue
                return False
            if scope.slots.index(found[0]) >= pos:
                return False
        return True

    def _plan_access(self, step: SourceStep, conjs: List[A.Expr], scope: Scope, comp: Compiler, pos: int) -> None:
        table = step.table
        slot = step.slot
        remaining = list(conjs)
        path = AccessPath()
        if table is not None and conjs:
            # Collect candidate predicates of the form <col> <op> <expr-over-earlier-steps>.
            eqs: Dict[str, Tuple[A.Expr, A.Expr]] = {}  # column -> (value expr, conj)
            ranges: Dict[str, List[Tuple[str, A.Expr, A.Expr]]] = {}
            for conj in conjs:
                for col, op, val in _predicates(conj, slot, scope, table):
                    if not self._refs_before(val, scope, pos):
                        continue
                    if op == "=":
                        eqs.setdefault(col, (val, conj))
                    else:
                        ranges.setdefault(col, []).append((op, val, conj))
            rowid_names = {"rowid"}
            if table.rowid_column is not None:
                rowid_names.add(table.columns[table.rowid_column].name.lower())
            used: List[A.Expr] = []
            chosen = None
            # 1. rowid equality
            for name in rowid_names:
                if name in eqs:
                    path = AccessPath("rowid_eq")
                    path.eq_fns = [comp.compile(eqs[name][0], scope, "row")]
                    used.append(eqs[name][1])
                    chosen = path
                    break
            # 2. unique index equality > index equality (longest prefix)
            if chosen is None:
                best: Optional[Tuple[int, int, IndexInfo, List[A.Expr]]] = None
                for ix in table.indexes:
                    n = 0
                    conj_used: List[A.Expr] = []
                    for c in ix.columns:
                        if c.lower() in eqs:
                            n += 1
                            conj_used.append(eqs[c.lower()][1])
                        else:
                            break
                    if n == 0:
                        continue
                    full_unique = ix.unique and n == len(ix.columns)
                    score = (n, 1 if full_unique else 0)
                    if best is None or score > (best[0], best[1]):
                        best = (n, 1 if full_unique else 0, ix, conj_used)
                if best is not None:
                    n, _, ix, conj_used = best
                    path = AccessPath("index_eq", ix)
                    path.eq_cols = ix.columns[:n]
                    path.eq_fns = [comp.compile(eqs[c.lower()][0], scope, "row") for c in path.eq_cols]
                    used.extend(conj_used)
                    # Optional range on the next column
                    if n < len(ix.columns):
                        nxt = ix.columns[n].lower()
                        if nxt in ranges:
                            self._apply_range(path, ranges[nxt], comp, scope, used)
                            if path.lo_fn or path.hi_fn:
                                path.range_col = ix.columns[n]
                                path.kind = "index_range"
                    chosen = path
            # 3. rowid range
            if chosen is None:
                for name in rowid_names:
                    if name in ranges:
                        path = AccessPath("rowid_range")
                        self._apply_range(path, ranges[name], comp, scope, used)
                        if path.lo_fn or path.hi_fn:
                            chosen = path
                            break
                        path = AccessPath()
            # 4. index range on the first column
            if chosen is None:
                for ix in table.indexes:
                    first = ix.columns[0].lower()
                    if first in ranges:
                        path = AccessPath("index_range", ix)
                        self._apply_range(path, ranges[first], comp, scope, used)
                        if path.lo_fn or path.hi_fn:
                            path.range_col = ix.columns[0]
                            chosen = path
                            break
                        path = AccessPath()
            if chosen is None:
                path = AccessPath()
            # Predicates consumed by an equality lookup are exact; range lookups keep the
            # predicate as a filter because bounds are widened for mixed int/real keys.
            if path.kind in ("rowid_eq",):
                remaining = [c for c in conjs if not any(c is u for u in used)]
            elif path.kind == "index_eq":
                # Keep them: cheap, and protects against affinity edge cases.
                remaining = list(conjs)
            if path.kind == "rowid_eq":
                path.ordered_cols = list(rowid_names)
            elif path.kind in ("scan", "rowid_range"):
                path.ordered_cols = list(rowid_names)
            elif path.index is not None:
                path.ordered_cols = [c.lower() for c in path.index.columns] + ["rowid"]
        elif table is not None:
            path.ordered_cols = ["rowid"] + ([table.columns[table.rowid_column].name.lower()] if table.rowid_column is not None else [])
        step.path = path
        step.filters = [comp.compile(c, scope, "row") for c in remaining]
        step.filter_sql = [to_sql(c) for c in remaining]

    def _apply_range(self, path: AccessPath, items, comp: Compiler, scope: Scope, used: List[A.Expr]) -> None:
        for op, val, conj in items:
            fn = comp.compile(val, scope, "row")
            if op in (">", ">=") and path.lo_fn is None:
                path.lo_fn, path.lo_incl = fn, (op == ">=")
                used.append(conj)
            elif op in ("<", "<=") and path.hi_fn is None:
                path.hi_fn, path.hi_incl = fn, (op == "<=")
                used.append(conj)

    def _order_satisfied(self, plan: SelectPlan, sel: A.Select, scope: Scope) -> bool:
        if len(plan.steps) != 1 or plan.steps[0].table is None:
            return False
        step = plan.steps[0]
        cols = step.path.ordered_cols
        if not cols:
            return False
        wanted = []
        for expr, desc, nulls in sel.order_by:
            if desc or nulls is not None or not isinstance(expr, A.Column):
                return False
            wanted.append(expr.name.lower())
        if step.path.kind == "rowid_eq":
            return True
        if step.path.kind in ("scan", "rowid_range"):
            return len(wanted) == 1 and wanted[0] in cols
        return cols[: len(wanted)] == wanted

    # ----------------------------- DML helpers -------------------------- #

    def _default_expr(self, col: ColumnInfo) -> Optional[A.Expr]:
        if col.default_sql is None:
            return None
        e = self._default_cache.get(col.default_sql)
        if e is None:
            e = parse_expr(col.default_sql)
            self._default_cache[col.default_sql] = e
        return e

    def _check_expr(self, sql: str) -> A.Expr:
        e = self._default_cache.get(sql)
        if e is None:
            e = parse_expr(sql)
            self._default_cache[sql] = e
        return e

    def _table_scope(self, table: TableInfo) -> Scope:
        scope = Scope()
        scope.add(table.name, table.column_names, table)
        return scope

    def _validate_row(self, table: TableInfo, values: Row, comp: Compiler, scope: Scope, rowid: int) -> None:
        for i, col in enumerate(table.columns):
            if col.not_null and values[i] is None and i != table.rowid_column:
                raise IntegrityError(f"NOT NULL constraint failed: {table.name}.{col.name}")
        checks = [c.check_sql for c in table.columns if c.check_sql] + list(table.checks)
        if checks:
            row = [rowid, *values]
            for sql in checks:
                fn = comp.compile(self._check_expr(sql), scope, "row")
                r = V.truth(fn(row))
                if r is False:
                    raise IntegrityError(f"CHECK constraint failed: {sql}")

    def _unique_conflict(self, ix: IndexInfo, table: TableInfo, values: Row, exclude_rowid: Optional[int]) -> Optional[int]:
        vals = [values[table.column_index(c)] for c in ix.columns]  # type: ignore[index]
        if any(v is None for v in vals):
            return None
        itree = self.catalog.tree_for(ix)
        for key, _ in itree.prefix(encode_key(vals)):
            rid = decode_rowid(key[-8:])
            if rid != exclude_rowid:
                return rid
        return None

    def _find_conflicts(self, table: TableInfo, values: Row, rowid: Optional[int], exclude: Optional[int]) -> List[Tuple[str, Optional[int]]]:
        """Return [(constraint description, conflicting rowid)] for an insert/update."""
        out = []
        tree = self.catalog.tree_for(table)
        if rowid is not None and rowid != exclude and tree.contains(encode_rowid(rowid)):
            name = table.columns[table.rowid_column].name if table.rowid_column is not None else "rowid"
            out.append((f"UNIQUE constraint failed: {table.name}.{name}", rowid))
        for ix in table.indexes:
            if not ix.unique:
                continue
            rid = self._unique_conflict(ix, table, values, exclude)
            if rid is not None:
                cols = ", ".join(f"{table.name}.{c}" for c in ix.columns)
                out.append((f"UNIQUE constraint failed: {cols}", rid))
        return out

    def _next_rowid(self, table: TableInfo) -> int:
        tree = self.catalog.tree_for(table)
        last = tree.last()
        n = decode_rowid(last[0]) + 1 if last else 1
        if any(c.autoincrement for c in table.columns):
            n = max(n, table.seq + 1)
        if n > 2**63 - 1:
            raise IntegrityError("database or disk is full (rowid overflow)")
        return n

    def _store_row(self, table: TableInfo, rowid: int, values: Row) -> None:
        tree = self.catalog.tree_for(table)
        tree.insert(encode_rowid(rowid), encode_record(values))
        for ix in table.indexes:
            vals = [values[table.column_index(c)] for c in ix.columns]  # type: ignore[index]
            self.catalog.tree_for(ix).insert(encode_key(vals) + encode_rowid(rowid), b"")
        if table.seq < rowid and any(c.autoincrement for c in table.columns):
            table.seq = rowid
            self.catalog.save_table(table)

    def _remove_row(self, table: TableInfo, rowid: int, values: Row) -> None:
        tree = self.catalog.tree_for(table)
        tree.delete(encode_rowid(rowid))
        for ix in table.indexes:
            vals = [values[table.column_index(c)] for c in ix.columns]  # type: ignore[index]
            self.catalog.tree_for(ix).delete(encode_key(vals) + encode_rowid(rowid))

    def _load_row_values(self, table: TableInfo, rowid: int) -> Optional[Row]:
        raw = self.catalog.tree_for(table).get(encode_rowid(rowid))
        if raw is None:
            return None
        return self.decode_row(table, rowid, raw)[1:]

    def insert_values(self, table: TableInfo, values: Row, rowid: Optional[int], comp: Compiler,
                      scope: Scope, or_replace: bool = False, or_ignore: bool = False) -> Optional[int]:
        """Insert a fully materialised row.  Returns the rowid, or None if ignored."""
        values = [V.apply_affinity(c.affinity, v) for c, v in zip(table.columns, values)]
        if table.rowid_column is not None:
            v = values[table.rowid_column]
            if v is not None:
                if not isinstance(v, int):
                    raise IntegrityError(f"datatype mismatch: {table.columns[table.rowid_column].name} must be an integer")
                rowid = v
        if rowid is None:
            rowid = self._next_rowid(table)
        if table.rowid_column is not None:
            values[table.rowid_column] = rowid
        self._validate_row(table, values, comp, scope, rowid)
        conflicts = self._find_conflicts(table, values, rowid, None)
        if conflicts:
            if or_ignore:
                return None
            if not or_replace:
                raise IntegrityError(conflicts[0][0])
            for _, rid in conflicts:
                if rid is not None:
                    old = self._load_row_values(table, rid)
                    if old is not None:
                        self._remove_row(table, rid, old)
        self._store_row(table, rowid, values)
        return rowid

    # ----------------------------- INSERT ------------------------------- #

    def exec_insert(self, stmt: A.Insert, params: Dict[Any, Any]) -> Result:
        table = self.catalog.get_table(stmt.table)
        comp = Compiler(self, params)
        scope = self._table_scope(table)
        ncols = len(table.columns)
        if stmt.columns is None:
            targets = list(range(ncols))
        else:
            targets = []
            for c in stmt.columns:
                idx = table.column_index(c)
                if idx is None:
                    if c.lower() in ("rowid", "_rowid_", "oid") and table.rowid_column is None:
                        targets.append(-1)
                        continue
                    raise ProgrammingError(f"table {table.name} has no column named {c}")
                targets.append(idx)
        defaults = [self._default_expr(c) for c in table.columns]
        default_fns = [comp.compile(d, Scope(), "row") if d is not None else None for d in defaults]

        def build(row_vals: List[Any]) -> Tuple[Row, Optional[int]]:
            if len(row_vals) != len(targets):
                raise ProgrammingError(f"table {table.name} has {len(targets)} columns but {len(row_vals)} values were supplied")
            values: Row = [None] * ncols
            given = [False] * ncols
            rowid = None
            for idx, v in zip(targets, row_vals):
                if idx == -1:
                    rowid = int(v) if v is not None else None
                else:
                    values[idx] = v
                    given[idx] = True
            for i in range(ncols):
                if not given[i] and default_fns[i] is not None:
                    values[i] = default_fns[i]([])  # type: ignore[misc]
            return values, rowid

        count = 0
        last = None
        if stmt.query is not None:
            plan = self.plan_select(stmt.query, compiler=comp)
            source_rows: Iterable[Sequence[Any]] = plan.run()  # materialised: safe for self-inserts
        else:
            assert stmt.rows is not None
            source_rows = ([comp.compile(e, Scope(), "row")([]) for e in r] for r in stmt.rows)
        for row_vals in source_rows:
            values, rowid = build(list(row_vals))
            rid = self.insert_values(table, values, rowid, comp, scope, stmt.or_replace, stmt.or_ignore)
            if rid is not None:
                count += 1
                last = rid
        return Result(rowcount=count, lastrowid=last)

    # ----------------------------- UPDATE ------------------------------- #

    def _target_rows(self, table: TableInfo, where: Optional[A.Expr], comp: Compiler) -> List[Row]:
        scope = self._table_scope(table)
        plan = SelectPlan(self, scope)
        step = SourceStep(scope.slots[0], "INNER")
        self._plan_access(step, split_conjuncts(where), scope, comp, 0)
        plan.steps.append(step)
        return list(plan._produce())

    def exec_update(self, stmt: A.Update, params: Dict[Any, Any]) -> Result:
        table = self.catalog.get_table(stmt.table)
        comp = Compiler(self, params)
        scope = self._table_scope(table)
        assigns = []
        for col, expr in stmt.assignments:
            idx = table.column_index(col)
            if idx is None:
                if col.lower() in ("rowid", "_rowid_", "oid") and table.rowid_column is None:
                    idx = -1
                else:
                    raise ProgrammingError(f"no such column: {col}")
            assigns.append((idx, comp.compile(expr, scope, "row")))
        rows = self._target_rows(table, stmt.where, comp)
        count = 0
        for row in rows:
            old_rowid = row[0]
            old_values = row[1:]
            new_values = list(old_values)
            new_rowid = old_rowid
            for idx, fn in assigns:
                v = fn(row)
                if idx == -1:
                    if not isinstance(v, int):
                        raise IntegrityError("datatype mismatch: rowid must be an integer")
                    new_rowid = v
                else:
                    new_values[idx] = V.apply_affinity(table.columns[idx].affinity, v)
            if table.rowid_column is not None:
                v = new_values[table.rowid_column]
                if v is None:
                    raise IntegrityError(f"datatype mismatch: {table.columns[table.rowid_column].name} must be an integer")
                if not isinstance(v, int):
                    raise IntegrityError(f"datatype mismatch: {table.columns[table.rowid_column].name} must be an integer")
                new_rowid = v
            self._validate_row(table, new_values, comp, scope, new_rowid)
            conflicts = self._find_conflicts(table, new_values, new_rowid if new_rowid != old_rowid else None, old_rowid)
            if conflicts:
                raise IntegrityError(conflicts[0][0])
            self._remove_row(table, old_rowid, old_values)
            self._store_row(table, new_rowid, new_values)
            count += 1
        return Result(rowcount=count)

    # ----------------------------- DELETE ------------------------------- #

    def exec_delete(self, stmt: A.Delete, params: Dict[Any, Any]) -> Result:
        table = self.catalog.get_table(stmt.table)
        comp = Compiler(self, params)
        if stmt.where is None:
            # Truncate: rebuild empty trees.
            n = len(self.catalog.tree_for(table))
            self.catalog.tree_for(table).clear()
            for ix in table.indexes:
                self.catalog.tree_for(ix).clear()
            return Result(rowcount=n)
        rows = self._target_rows(table, stmt.where, comp)
        for row in rows:
            self._remove_row(table, row[0], row[1:])
        return Result(rowcount=len(rows))

    # ----------------------------- DDL ---------------------------------- #

    def exec_create_table(self, stmt: A.CreateTable, params: Dict[Any, Any]) -> Result:
        if self.catalog.has_table(stmt.name):
            if stmt.if_not_exists:
                return Result()
            raise ProgrammingError(f"table {stmt.name} already exists")
        if stmt.name.lower().startswith("quarry_"):
            raise ProgrammingError("table names starting with 'quarry_' are reserved")
        comp = Compiler(self, params)
        if stmt.as_select is not None:
            plan = self.plan_select(stmt.as_select, compiler=comp)
            rows = plan.run()
            cols = [ColumnInfo(name) for name in plan.columns]
            seen = set()
            for c in cols:
                if c.name.lower() in seen:
                    raise ProgrammingError(f"duplicate column name: {c.name}")
                seen.add(c.name.lower())
            table = TableInfo(stmt.name, cols, 0, None, [], f"CREATE TABLE {stmt.name} AS SELECT ...")
            self.catalog.create_table(table)
            scope = self._table_scope(table)
            for r in rows:
                self.insert_values(table, list(r), None, comp, scope)
            return Result(rowcount=len(rows))

        cols: List[ColumnInfo] = []
        names = set()
        rowid_column = None
        pk_cols: List[str] = []
        unique_sets: List[List[str]] = []
        checks: List[str] = []
        for cd in stmt.columns:
            if cd.name.lower() in names:
                raise ProgrammingError(f"duplicate column name: {cd.name}")
            names.add(cd.name.lower())
            ci = ColumnInfo(cd.name, cd.type_name, cd.not_null, cd.primary_key, cd.unique,
                            to_sql(cd.default) if cd.default is not None else None,
                            to_sql(cd.check) if cd.check is not None else None, cd.autoincrement)
            cols.append(ci)
            if cd.primary_key:
                if pk_cols:
                    raise ProgrammingError("table has more than one primary key")
                pk_cols = [cd.name]
            if cd.unique and not cd.primary_key:
                unique_sets.append([cd.name])
        for tc in stmt.constraints:
            if tc.kind == "PRIMARY KEY":
                if pk_cols:
                    raise ProgrammingError("table has more than one primary key")
                pk_cols = tc.columns
            elif tc.kind == "UNIQUE":
                unique_sets.append(tc.columns)
            elif tc.kind == "CHECK" and tc.expr is not None:
                checks.append(to_sql(tc.expr))
        for group in [pk_cols] + unique_sets:
            for c in group:
                if c.lower() not in names:
                    raise ProgrammingError(f"no such column: {c}")
        if len(pk_cols) == 1:
            idx = next(i for i, c in enumerate(cols) if c.name.lower() == pk_cols[0].lower())
            if (cols[idx].type_name or "").upper() in ("INTEGER", "INT", "BIGINT"):
                rowid_column = idx  # INTEGER PRIMARY KEY is an alias for the rowid
                cols[idx].primary_key = True
        for c in cols:
            if c.autoincrement and rowid_column is None:
                raise ProgrammingError("AUTOINCREMENT is only allowed on an INTEGER PRIMARY KEY")
        if pk_cols:
            for c in pk_cols:
                ci = next(x for x in cols if x.name.lower() == c.lower())
                ci.primary_key = True
                if rowid_column is None:
                    ci.not_null = True  # PRIMARY KEY implies NOT NULL (unlike legacy SQLite)
        table = TableInfo(stmt.name, cols, 0, rowid_column, checks, create_table_sql(stmt))
        self.catalog.create_table(table)
        if pk_cols and rowid_column is None:
            self.catalog.create_index(IndexInfo(f"{stmt.name}_pk", table.name, list(pk_cols), True, 0, auto=True))
        for i, us in enumerate(unique_sets):
            name = f"{stmt.name}_{'_'.join(us)}_unique" if len(us) == 1 else f"{stmt.name}_unique_{i + 1}"
            base = name
            k = 1
            while self.catalog.has_index(name):
                k += 1
                name = f"{base}_{k}"
            self.catalog.create_index(IndexInfo(name, table.name, list(us), True, 0, auto=True))
        return Result()

    def exec_create_index(self, stmt: A.CreateIndex) -> Result:
        if self.catalog.has_index(stmt.name):
            if stmt.if_not_exists:
                return Result()
            raise ProgrammingError(f"index {stmt.name} already exists")
        table = self.catalog.get_table(stmt.table)
        for c in stmt.columns:
            if table.column_index(c) is None:
                raise ProgrammingError(f"no such column: {c}")
        ix = IndexInfo(stmt.name, table.name, [table.columns[table.column_index(c)].name for c in stmt.columns],  # type: ignore[index]
                       stmt.unique, 0, sql=create_index_sql(stmt))
        self.catalog.create_index(ix)
        itree = self.catalog.tree_for(ix)
        tree = self.catalog.tree_for(table)
        seen = set()
        for key, raw in tree.seek(b""):
            rowid = decode_rowid(key)
            values = self.decode_row(table, rowid, raw)[1:]
            vals = [values[table.column_index(c)] for c in ix.columns]  # type: ignore[index]
            if stmt.unique and not any(v is None for v in vals):
                k = tuple((V.rank(v), v) for v in vals)
                if k in seen:
                    raise IntegrityError(f"UNIQUE constraint failed: {', '.join(table.name + '.' + c for c in ix.columns)}")
                seen.add(k)
            itree.insert(encode_key(vals) + encode_rowid(rowid), b"")
        return Result()


# --------------------------------------------------------------------------- #
# Planning helpers                                                            #
# --------------------------------------------------------------------------- #


def _flatten_from(src) -> List[Tuple[Any, str, Optional[A.Expr]]]:
    """Turn a (left-deep) join tree into [(ref, join_kind, on_expr)]."""
    if src is None:
        return []
    if isinstance(src, A.Join):
        left = _flatten_from(src.left)
        right = _flatten_from(src.right)
        if len(right) != 1:
            # Right-nested join: treat its ON as a plain filter after everything.
            steps = left + [(r, k if i else src.kind, o) for i, (r, k, o) in enumerate(right)]
            if src.on is not None:
                r, k, o = steps[-1]
                steps[-1] = (r, k, A.Binary("AND", o, src.on) if o is not None else src.on)
            return steps
        r, _, o = right[0]
        return left + [(r, src.kind, src.on if o is None else (A.Binary("AND", o, src.on) if src.on else o))]
    return [(src, "INNER", None)]


_FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "="}


def _predicates(conj: A.Expr, slot: TableSlot, scope: Scope, table: TableInfo) -> List[Tuple[str, str, A.Expr]]:
    """Extract (column, op, value_expr) for simple comparisons on ``slot`` in ``conj``."""
    out: List[Tuple[str, str, A.Expr]] = []

    def col_of(e: A.Expr) -> Optional[str]:
        if not isinstance(e, A.Column):
            return None
        found = scope.resolve_local(e.table, e.name)
        if found is None or found[0] is not slot:
            return None
        pos = found[1]
        if pos == slot.offset:
            return "rowid"
        return slot.names[pos - slot.offset - 1].lower()

    if isinstance(conj, A.Binary) and conj.op in _FLIP:
        c = col_of(conj.left)
        if c is not None and col_of(conj.right) is None:
            out.append((c, conj.op, conj.right))
        else:
            c = col_of(conj.right)
            if c is not None and col_of(conj.left) is None:
                out.append((c, _FLIP[conj.op], conj.left))
    elif isinstance(conj, A.Between) and not conj.negated:
        c = col_of(conj.operand)
        if c is not None:
            out.append((c, ">=", conj.low))
            out.append((c, "<=", conj.high))
    elif isinstance(conj, A.In) and not conj.negated and not isinstance(conj.items, A.Select) and len(conj.items) == 1:
        c = col_of(conj.operand)
        if c is not None:
            out.append((c, "=", conj.items[0]))
    return out


