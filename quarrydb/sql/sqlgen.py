"""Render AST expressions back to SQL text (for column names, DEFAULT/CHECK storage, EXPLAIN)."""

from __future__ import annotations

from . import ast as A


def quote_ident(name: str) -> str:
    if name.isidentifier() and name.upper() not in {"SELECT", "FROM", "WHERE", "TABLE", "ORDER", "GROUP", "KEY"}:
        return name
    return '"' + name.replace('"', '""') + '"'


def literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (bytes, bytearray)):
        return "X'" + bytes(v).hex().upper() + "'"
    return "'" + str(v).replace("'", "''") + "'"


def to_sql(e: A.Expr) -> str:
    if isinstance(e, A.Literal):
        return literal(e.value)
    if isinstance(e, A.Param):
        return "?" if isinstance(e.key, int) else f":{e.key}"
    if isinstance(e, A.Column):
        return f"{quote_ident(e.table)}.{quote_ident(e.name)}" if e.table else quote_ident(e.name)
    if isinstance(e, A.Star):
        return f"{quote_ident(e.table)}.*" if e.table else "*"
    if isinstance(e, A.Unary):
        if e.op == "NOT":
            return f"NOT {to_sql(e.operand)}"
        return f"{e.op}{to_sql(e.operand)}"
    if isinstance(e, A.Binary):
        return f"{to_sql(e.left)} {e.op} {to_sql(e.right)}"
    if isinstance(e, A.IsNull):
        return f"{to_sql(e.operand)} IS {'NOT ' if e.negated else ''}NULL"
    if isinstance(e, A.In):
        if isinstance(e.items, A.Select):
            inner = "<subquery>"
        else:
            inner = ", ".join(to_sql(x) for x in e.items)
        return f"{to_sql(e.operand)} {'NOT ' if e.negated else ''}IN ({inner})"
    if isinstance(e, A.Between):
        return f"{to_sql(e.operand)} {'NOT ' if e.negated else ''}BETWEEN {to_sql(e.low)} AND {to_sql(e.high)}"
    if isinstance(e, A.Like):
        op = "GLOB" if e.glob else "LIKE"
        s = f"{to_sql(e.operand)} {'NOT ' if e.negated else ''}{op} {to_sql(e.pattern)}"
        if e.escape is not None:
            s += f" ESCAPE {to_sql(e.escape)}"
        return s
    if isinstance(e, A.Call):
        args = ", ".join(to_sql(a) for a in e.args)
        return f"{e.name}({'DISTINCT ' if e.distinct else ''}{args})"
    if isinstance(e, A.Case):
        parts = ["CASE"]
        if e.operand is not None:
            parts.append(to_sql(e.operand))
        for cond, res in e.whens:
            parts.append(f"WHEN {to_sql(cond)} THEN {to_sql(res)}")
        if e.else_ is not None:
            parts.append(f"ELSE {to_sql(e.else_)}")
        parts.append("END")
        return " ".join(parts)
    if isinstance(e, A.Cast):
        return f"CAST({to_sql(e.operand)} AS {e.type_name})"
    if isinstance(e, A.Exists):
        return f"{'NOT ' if e.negated else ''}EXISTS (<subquery>)"
    if isinstance(e, A.Subquery):
        return "(<subquery>)"
    return "?"


def column_sql(c: A.ColumnDef) -> str:
    parts = [quote_ident(c.name)]
    if c.type_name:
        parts.append(c.type_name)
    if c.primary_key:
        parts.append("PRIMARY KEY")
        if c.autoincrement:
            parts.append("AUTOINCREMENT")
    if c.not_null:
        parts.append("NOT NULL")
    if c.unique:
        parts.append("UNIQUE")
    if c.default is not None:
        parts.append(f"DEFAULT ({to_sql(c.default)})")
    if c.check is not None:
        parts.append(f"CHECK ({to_sql(c.check)})")
    return " ".join(parts)


def create_table_sql(stmt: A.CreateTable) -> str:
    defs = [column_sql(c) for c in stmt.columns]
    for tc in stmt.constraints:
        if tc.kind == "CHECK":
            defs.append(f"CHECK ({to_sql(tc.expr)})")
        else:
            defs.append(f"{tc.kind} ({', '.join(quote_ident(c) for c in tc.columns)})")
    return f"CREATE TABLE {quote_ident(stmt.name)} ({', '.join(defs)})"


def create_index_sql(stmt: A.CreateIndex) -> str:
    u = "UNIQUE " if stmt.unique else ""
    cols = ", ".join(quote_ident(c) for c in stmt.columns)
    return f"CREATE {u}INDEX {quote_ident(stmt.name)} ON {quote_ident(stmt.table)} ({cols})"
