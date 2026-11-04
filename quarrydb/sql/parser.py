"""Recursive-descent parser producing :mod:`quarrydb.sql.ast` nodes."""

from __future__ import annotations

from typing import List, Optional, Tuple

from . import ast as A
from .errors import SQLError
from .lexer import Token, tokenize

# Binary operator precedence (higher binds tighter), SQLite-compatible.
_PRECEDENCE = {
    "OR": 1,
    "AND": 2,
    # NOT is a prefix op at level 3
    "=": 4, "==": 4, "<>": 4, "!=": 4, "IS": 4, "IN": 4, "LIKE": 4, "GLOB": 4, "BETWEEN": 4,
    "<": 5, "<=": 5, ">": 5, ">=": 5,
    "+": 6, "-": 6,
    "*": 7, "/": 7, "%": 7,
    "||": 8,
}

_TYPE_WORDS = {"INTEGER", "INT", "BIGINT", "SMALLINT", "TINYINT", "REAL", "FLOAT", "DOUBLE",
               "NUMERIC", "DECIMAL", "TEXT", "VARCHAR", "CHAR", "STRING", "BLOB", "BOOLEAN",
               "BOOL", "ANY", "DATE", "DATETIME", "TIMESTAMP", "JSON"}


class Parser:
    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.toks = tokenize(sql)
        self.i = 0
        self._param_counter = 0

    # ------------------------------------------------------------------ #
    # Token helpers                                                       #
    # ------------------------------------------------------------------ #

    @property
    def cur(self) -> Token:
        return self.toks[self.i]

    def peek(self, offset: int = 1) -> Token:
        j = min(self.i + offset, len(self.toks) - 1)
        return self.toks[j]

    def advance(self) -> Token:
        t = self.toks[self.i]
        if t.kind != "EOF":
            self.i += 1
        return t

    def at_keyword(self, *words: str) -> bool:
        t = self.cur
        return t.kind == "KEYWORD" and t.value in words

    def at_op(self, *ops: str) -> bool:
        t = self.cur
        return t.kind == "OP" and t.value in ops

    def accept_keyword(self, *words: str) -> bool:
        if self.at_keyword(*words):
            self.advance()
            return True
        return False

    def accept_op(self, *ops: str) -> bool:
        if self.at_op(*ops):
            self.advance()
            return True
        return False

    def error(self, msg: str) -> SQLError:
        t = self.cur
        near = "end of input" if t.kind == "EOF" else repr(self.sql[t.pos : t.pos + 20])
        return SQLError(f"{msg} near {near}")

    def expect_keyword(self, *words: str) -> Token:
        if not self.at_keyword(*words):
            raise self.error(f"expected {' or '.join(words)}")
        return self.advance()

    def expect_op(self, op: str) -> Token:
        if not self.at_op(op):
            raise self.error(f"expected '{op}'")
        return self.advance()

    def ident(self) -> str:
        t = self.cur
        if t.kind == "IDENT":
            self.advance()
            return t.value  # type: ignore[return-value]
        # Allow non-reserved keywords as identifiers (e.g. a column called "key").
        if t.kind == "KEYWORD" and t.value in {"KEY", "REPLACE", "IF", "INDEX", "TABLE",
                                               "TRANSACTION", "BEGIN", "COMMIT", "ROLLBACK",
                                               "VACUUM", "EXPLAIN", "GLOB", "CAST", "ESCAPE",
                                               "RETURNING", "AUTOINCREMENT", "COLLATE",
                                               "CONSTRAINT", "OFFSET", "DEFAULT", "CHECK", "ASC", "DESC"}:
            self.advance()
            return t.value  # type: ignore[return-value]
        raise self.error("expected identifier")

    # ------------------------------------------------------------------ #
    # Entry points                                                        #
    # ------------------------------------------------------------------ #

    def parse_statement(self) -> A.Statement:
        stmt = self._statement()
        self.accept_op(";")
        if self.cur.kind != "EOF":
            raise self.error("unexpected trailing input")
        return stmt

    def _statement(self) -> A.Statement:
        t = self.cur
        if t.kind != "KEYWORD":
            raise self.error("expected a statement")
        kw = t.value
        if kw == "SELECT" or kw == "VALUES":
            return self.select()
        if kw == "INSERT" or kw == "REPLACE":
            return self.insert()
        if kw == "UPDATE":
            return self.update()
        if kw == "DELETE":
            return self.delete()
        if kw == "CREATE":
            return self.create()
        if kw == "DROP":
            return self.drop()
        if kw == "BEGIN":
            self.advance()
            self.accept_keyword("TRANSACTION")
            return A.Begin()
        if kw == "COMMIT" or kw == "END":
            self.advance()
            self.accept_keyword("TRANSACTION")
            return A.Commit()
        if kw == "ROLLBACK":
            self.advance()
            self.accept_keyword("TRANSACTION")
            return A.Rollback()
        if kw == "VACUUM":
            self.advance()
            return A.Vacuum()
        if kw == "EXPLAIN":
            self.advance()
            return A.Explain(self._statement())
        raise self.error(f"unsupported statement {kw}")

    # ------------------------------------------------------------------ #
    # SELECT                                                              #
    # ------------------------------------------------------------------ #

    def select(self) -> A.Select:
        first = self._select_core()
        compound: List[Tuple[str, A.Select]] = []
        while self.at_keyword("UNION", "INTERSECT", "EXCEPT"):
            op = self.advance().value
            if op == "UNION" and self.accept_keyword("ALL"):
                op = "UNION ALL"
            compound.append((op, self._select_core()))  # type: ignore[arg-type]
        first.compound = first.compound + compound
        if self.accept_keyword("ORDER"):
            self.expect_keyword("BY")
            first.order_by = self._order_terms()
        if self.accept_keyword("LIMIT"):
            first.limit = self.expr()
            if self.accept_keyword("OFFSET"):
                first.offset = self.expr()
            elif self.accept_op(","):
                # LIMIT offset, count  (MySQL / SQLite style)
                first.offset = first.limit
                first.limit = self.expr()
        return first

    def _select_core(self) -> A.Select:
        if self.accept_keyword("VALUES"):
            rows = []
            while True:
                self.expect_op("(")
                rows.append(self._expr_list())
                self.expect_op(")")
                if not self.accept_op(","):
                    break
            # VALUES (...) is sugar for SELECT ... UNION ALL SELECT ...
            width = len(rows[0])
            selects = []
            for r in rows:
                if len(r) != width:
                    raise SQLError("all VALUES rows must have the same number of columns")
                selects.append(A.Select(items=[A.SelectItem(e, f"column{i + 1}") for i, e in enumerate(r)]))
            head = selects[0]
            head.compound = [("UNION ALL", s) for s in selects[1:]]
            return head
        self.expect_keyword("SELECT")
        distinct = False
        if self.accept_keyword("DISTINCT"):
            distinct = True
        else:
            self.accept_keyword("ALL")
        items = [self._select_item()]
        while self.accept_op(","):
            items.append(self._select_item())
        source = None
        if self.accept_keyword("FROM"):
            source = self._join_source()
        where = self.expr() if self.accept_keyword("WHERE") else None
        group_by: List[A.Expr] = []
        having = None
        if self.accept_keyword("GROUP"):
            self.expect_keyword("BY")
            group_by = self._expr_list()
            if self.accept_keyword("HAVING"):
                having = self.expr()
        return A.Select(items=items, source=source, where=where, group_by=group_by,
                        having=having, distinct=distinct)

    def _select_item(self) -> A.SelectItem:
        if self.at_op("*"):
            self.advance()
            return A.SelectItem(A.Star())
        if self.cur.kind == "IDENT" and self.peek().kind == "OP" and self.peek().value == "." \
                and self.peek(2).kind == "OP" and self.peek(2).value == "*":
            table = self.ident()
            self.expect_op(".")
            self.expect_op("*")
            return A.SelectItem(A.Star(table))
        e = self.expr()
        alias = None
        if self.accept_keyword("AS"):
            alias = self.ident()
        elif self.cur.kind == "IDENT":
            alias = self.ident()
        return A.SelectItem(e, alias)

    def _join_source(self):
        left = self._table_or_subquery()
        while True:
            if self.accept_op(","):
                right = self._table_or_subquery()
                left = A.Join(left, right, "CROSS", None)
                continue
            kind = None
            if self.accept_keyword("CROSS"):
                kind = "CROSS"
            elif self.accept_keyword("INNER"):
                kind = "INNER"
            elif self.accept_keyword("LEFT"):
                self.accept_keyword("OUTER")
                kind = "LEFT"
            elif self.at_keyword("JOIN"):
                kind = "INNER"
            if kind is None:
                return left
            self.expect_keyword("JOIN")
            right = self._table_or_subquery()
            on = None
            if self.accept_keyword("ON"):
                on = self.expr()
            elif self.accept_keyword("USING"):
                self.expect_op("(")
                cols = [self.ident()]
                while self.accept_op(","):
                    cols.append(self.ident())
                self.expect_op(")")
                lname = _ref_alias(left)
                rname = _ref_alias(right)
                conds = [A.Binary("=", A.Column(lname, c), A.Column(rname, c)) for c in cols]
                on = conds[0]
                for c in conds[1:]:
                    on = A.Binary("AND", on, c)
            if kind == "CROSS" and on is not None:
                kind = "INNER"
            left = A.Join(left, right, kind, on)

    def _table_or_subquery(self):
        if self.accept_op("("):
            if self.at_keyword("SELECT", "VALUES"):
                q = self.select()
                self.expect_op(")")
                self.accept_keyword("AS")
                alias = self.ident() if self.cur.kind == "IDENT" else f"subquery{self.i}"
                return A.SubqueryRef(q, alias)
            src = self._join_source()
            self.expect_op(")")
            return src
        name = self.ident()
        alias = None
        if self.accept_keyword("AS"):
            alias = self.ident()
        elif self.cur.kind == "IDENT":
            alias = self.ident()
        return A.TableRef(name, alias)

    def _order_terms(self) -> List[Tuple[A.Expr, bool, Optional[str]]]:
        terms = []
        while True:
            e = self.expr()
            desc = False
            if self.accept_keyword("DESC"):
                desc = True
            else:
                self.accept_keyword("ASC")
            nulls = None
            if self.cur.kind == "IDENT" and self.cur.value.upper() == "NULLS":
                self.advance()
                if self.cur.kind == "IDENT" and self.cur.value.upper() in ("FIRST", "LAST"):
                    nulls = self.advance().value.upper()
                else:
                    raise self.error("expected FIRST or LAST")
            terms.append((e, desc, nulls))
            if not self.accept_op(","):
                return terms

    def _expr_list(self) -> List[A.Expr]:
        items = [self.expr()]
        while self.accept_op(","):
            items.append(self.expr())
        return items

    # ------------------------------------------------------------------ #
    # INSERT / UPDATE / DELETE                                            #
    # ------------------------------------------------------------------ #

    def insert(self) -> A.Insert:
        or_replace = or_ignore = False
        if self.accept_keyword("REPLACE"):
            or_replace = True
        else:
            self.expect_keyword("INSERT")
            if self.accept_keyword("OR"):
                if self.accept_keyword("REPLACE"):
                    or_replace = True
                elif self.cur.kind == "IDENT" and self.cur.value.upper() == "IGNORE":
                    self.advance()
                    or_ignore = True
                else:
                    raise self.error("expected REPLACE or IGNORE")
        self.expect_keyword("INTO")
        table = self.ident()
        columns = None
        if self.accept_op("("):
            columns = [self.ident()]
            while self.accept_op(","):
                columns.append(self.ident())
            self.expect_op(")")
        if self.accept_keyword("VALUES"):
            rows = []
            while True:
                self.expect_op("(")
                rows.append(self._expr_list() if not self.at_op(")") else [])
                self.expect_op(")")
                if not self.accept_op(","):
                    break
            return A.Insert(table, columns, rows, or_replace=or_replace, or_ignore=or_ignore)
        if self.at_keyword("SELECT"):
            return A.Insert(table, columns, None, query=self.select(), or_replace=or_replace, or_ignore=or_ignore)
        if self.accept_keyword("DEFAULT"):
            self.expect_keyword("VALUES")
            return A.Insert(table, [], [[]], or_replace=or_replace, or_ignore=or_ignore)
        raise self.error("expected VALUES or SELECT")

    def update(self) -> A.Update:
        self.expect_keyword("UPDATE")
        table = self.ident()
        self.expect_keyword("SET")
        assigns = []
        while True:
            col = self.ident()
            self.expect_op("=")
            assigns.append((col, self.expr()))
            if not self.accept_op(","):
                break
        where = self.expr() if self.accept_keyword("WHERE") else None
        return A.Update(table, assigns, where)

    def delete(self) -> A.Delete:
        self.expect_keyword("DELETE")
        self.expect_keyword("FROM")
        table = self.ident()
        where = self.expr() if self.accept_keyword("WHERE") else None
        return A.Delete(table, where)

    # ------------------------------------------------------------------ #
    # DDL                                                                 #
    # ------------------------------------------------------------------ #

    def create(self):
        self.expect_keyword("CREATE")
        unique = self.accept_keyword("UNIQUE")
        if self.accept_keyword("TABLE"):
            if unique:
                raise self.error("UNIQUE is only valid for CREATE INDEX")
            return self._create_table()
        if self.accept_keyword("INDEX"):
            return self._create_index(unique)
        raise self.error("expected TABLE or INDEX")

    def _if_not_exists(self) -> bool:
        if self.accept_keyword("IF"):
            self.expect_keyword("NOT")
            self.expect_keyword("EXISTS")
            return True
        return False

    def _create_table(self) -> A.CreateTable:
        ine = self._if_not_exists()
        name = self.ident()
        if self.accept_keyword("AS"):
            return A.CreateTable(name, [], [], ine, as_select=self.select())
        self.expect_op("(")
        columns: List[A.ColumnDef] = []
        constraints: List[A.TableConstraint] = []
        while True:
            if self.at_keyword("PRIMARY", "UNIQUE", "CHECK", "CONSTRAINT"):
                constraints.append(self._table_constraint())
            else:
                columns.append(self._column_def())
            if not self.accept_op(","):
                break
        self.expect_op(")")
        if not columns:
            raise SQLError("a table needs at least one column")
        return A.CreateTable(name, columns, constraints, ine)

    def _column_def(self) -> A.ColumnDef:
        name = self.ident()
        type_name = None
        if self.cur.kind == "IDENT" and self.cur.value.upper() in _TYPE_WORDS:
            words = [self.advance().value.upper()]
            # e.g. DOUBLE PRECISION, VARCHAR(255)
            while self.cur.kind == "IDENT" and self.cur.value.upper() in {"PRECISION", "VARYING"}:
                words.append(self.advance().value.upper())
            if self.accept_op("("):
                while not self.at_op(")"):
                    self.advance()
                self.expect_op(")")
            type_name = " ".join(words)
        col = A.ColumnDef(name, type_name)
        while True:
            if self.accept_keyword("CONSTRAINT"):
                self.ident()
            if self.accept_keyword("PRIMARY"):
                self.expect_keyword("KEY")
                col.primary_key = True
                if self.accept_keyword("DESC"):
                    col.pk_desc = True
                else:
                    self.accept_keyword("ASC")
                if self.accept_keyword("AUTOINCREMENT"):
                    col.autoincrement = True
            elif self.accept_keyword("NOT"):
                self.expect_keyword("NULL")
                col.not_null = True
            elif self.accept_keyword("NULL"):
                pass
            elif self.accept_keyword("UNIQUE"):
                col.unique = True
            elif self.accept_keyword("DEFAULT"):
                if self.accept_op("("):
                    col.default = self.expr()
                    self.expect_op(")")
                else:
                    col.default = self._literal_or_signed()
            elif self.accept_keyword("CHECK"):
                self.expect_op("(")
                col.check = self.expr()
                self.expect_op(")")
            elif self.accept_keyword("COLLATE"):
                self.ident()
            else:
                break
        return col

    def _literal_or_signed(self) -> A.Expr:
        if self.at_op("-", "+"):
            op = self.advance().value
            lit = self._primary()
            return A.Unary(op, lit)
        return self._primary()

    def _table_constraint(self) -> A.TableConstraint:
        name = None
        if self.accept_keyword("CONSTRAINT"):
            name = self.ident()
        if self.accept_keyword("PRIMARY"):
            self.expect_keyword("KEY")
            return A.TableConstraint("PRIMARY KEY", self._column_name_list(), name=name)
        if self.accept_keyword("UNIQUE"):
            return A.TableConstraint("UNIQUE", self._column_name_list(), name=name)
        if self.accept_keyword("CHECK"):
            self.expect_op("(")
            e = self.expr()
            self.expect_op(")")
            return A.TableConstraint("CHECK", expr=e, name=name)
        raise self.error("expected table constraint")

    def _column_name_list(self) -> List[str]:
        self.expect_op("(")
        cols = [self.ident()]
        self.accept_keyword("ASC", "DESC")
        while self.accept_op(","):
            cols.append(self.ident())
            self.accept_keyword("ASC", "DESC")
        self.expect_op(")")
        return cols

    def _create_index(self, unique: bool) -> A.CreateIndex:
        ine = self._if_not_exists()
        name = self.ident()
        self.expect_keyword("ON")
        table = self.ident()
        cols = self._column_name_list()
        return A.CreateIndex(name, table, cols, unique, ine)

    def drop(self):
        self.expect_keyword("DROP")
        what = self.expect_keyword("TABLE", "INDEX").value
        if_exists = False
        if self.accept_keyword("IF"):
            self.expect_keyword("EXISTS")
            if_exists = True
        name = self.ident()
        return A.DropTable(name, if_exists) if what == "TABLE" else A.DropIndex(name, if_exists)

    # ------------------------------------------------------------------ #
    # Expressions                                                         #
    # ------------------------------------------------------------------ #

    def expr(self, min_prec: int = 1) -> A.Expr:
        left = self._unary()
        while True:
            t = self.cur
            if t.kind == "OP" and t.value in _PRECEDENCE:
                op = t.value
            elif t.kind == "KEYWORD" and t.value in ("AND", "OR", "IS", "IN", "LIKE", "GLOB", "BETWEEN", "NOT"):
                op = t.value
                if op == "NOT":
                    # NOT IN / NOT LIKE / NOT BETWEEN / NOT GLOB
                    nxt = self.peek()
                    if nxt.kind == "KEYWORD" and nxt.value in ("IN", "LIKE", "GLOB", "BETWEEN"):
                        op = nxt.value
                    else:
                        return left
            else:
                return left
            prec = _PRECEDENCE[op]
            if prec < min_prec:
                return left
            negated = False
            if self.cur.value == "NOT":
                self.advance()
                negated = True
            self.advance()  # consume the operator
            if op == "IS":
                neg = self.accept_keyword("NOT")
                if self.accept_keyword("NULL"):
                    left = A.IsNull(left, neg)
                else:
                    right = self.expr(prec + 1)
                    left = A.Binary("IS NOT" if neg else "IS", left, right)
            elif op == "IN":
                self.expect_op("(")
                if self.at_keyword("SELECT", "VALUES"):
                    items = self.select()
                    self.expect_op(")")
                    left = A.In(left, items, negated)
                else:
                    lst = tuple(self._expr_list()) if not self.at_op(")") else ()
                    self.expect_op(")")
                    left = A.In(left, lst, negated)
            elif op in ("LIKE", "GLOB"):
                pattern = self.expr(prec + 1)
                esc = self.expr(prec + 1) if self.accept_keyword("ESCAPE") else None
                left = A.Like(left, pattern, esc, negated, glob=(op == "GLOB"))
            elif op == "BETWEEN":
                low = self.expr(prec + 1)
                self.expect_keyword("AND")
                high = self.expr(prec + 1)
                left = A.Between(left, low, high, negated)
            else:
                right = self.expr(prec + 1)
                if op == "==":
                    op = "="
                elif op == "!=":
                    op = "<>"
                left = A.Binary(op, left, right)

    def _unary(self) -> A.Expr:
        if self.accept_keyword("NOT"):
            return A.Unary("NOT", self.expr(3))
        if self.at_op("-", "+"):
            op = self.advance().value
            operand = self.expr(9)  # binds tighter than any binary operator
            if op == "-" and isinstance(operand, A.Literal) and isinstance(operand.value, (int, float)):
                return A.Literal(-operand.value)
            return A.Unary(op, operand)
        return self._primary()

    def _primary(self) -> A.Expr:
        t = self.cur
        if t.kind in ("INT", "REAL", "STRING", "BLOB"):
            self.advance()
            return A.Literal(t.value)
        if t.kind == "PARAM":
            self.advance()
            key = t.value
            if key is None:
                self._param_counter += 1
                key = self._param_counter
            elif isinstance(key, int):
                self._param_counter = max(self._param_counter, key)
            return A.Param(key)
        if t.kind == "KEYWORD":
            if t.value == "NULL":
                self.advance()
                return A.Literal(None)
            if t.value == "TRUE":
                self.advance()
                return A.Literal(1)
            if t.value == "FALSE":
                self.advance()
                return A.Literal(0)
            if t.value == "CASE":
                return self._case()
            if t.value == "CAST":
                self.advance()
                self.expect_op("(")
                e = self.expr()
                self.expect_keyword("AS")
                words = [self.ident().upper()]
                while self.cur.kind == "IDENT":
                    words.append(self.ident().upper())
                if self.accept_op("("):
                    while not self.at_op(")"):
                        self.advance()
                    self.expect_op(")")
                self.expect_op(")")
                return A.Cast(e, " ".join(words))
            if t.value == "EXISTS":
                self.advance()
                self.expect_op("(")
                q = self.select()
                self.expect_op(")")
                return A.Exists(q)
            if t.value == "NOT" and self.peek().kind == "KEYWORD" and self.peek().value == "EXISTS":
                self.advance()
                self.advance()
                self.expect_op("(")
                q = self.select()
                self.expect_op(")")
                return A.Exists(q, negated=True)
        if t.kind == "OP" and t.value == "(":
            self.advance()
            if self.at_keyword("SELECT", "VALUES"):
                q = self.select()
                self.expect_op(")")
                return A.Subquery(q)
            e = self.expr()
            self.expect_op(")")
            return e
        if t.kind == "IDENT" or (t.kind == "KEYWORD" and t.value in {"KEY", "REPLACE", "GLOB", "LEFT", "IF"}):
            name = self.ident() if t.kind == "IDENT" else self.advance().value
            if self.at_op("("):
                return self._call(name)  # type: ignore[arg-type]
            if self.accept_op("."):
                col = self.ident()
                return A.Column(name, col)  # type: ignore[arg-type]
            return A.Column(None, name)  # type: ignore[arg-type]
        raise self.error("expected expression")

    def _call(self, name: str) -> A.Expr:
        self.expect_op("(")
        distinct = False
        if self.accept_op(")"):
            return A.Call(name.upper(), ())
        if self.at_op("*"):
            self.advance()
            self.expect_op(")")
            return A.Call(name.upper(), (A.Star(),))
        if self.accept_keyword("DISTINCT"):
            distinct = True
        args = tuple(self._expr_list())
        self.expect_op(")")
        return A.Call(name.upper(), args, distinct)

    def _case(self) -> A.Expr:
        self.expect_keyword("CASE")
        operand = None
        if not self.at_keyword("WHEN"):
            operand = self.expr()
        whens = []
        while self.accept_keyword("WHEN"):
            cond = self.expr()
            self.expect_keyword("THEN")
            whens.append((cond, self.expr()))
        if not whens:
            raise self.error("CASE needs at least one WHEN")
        else_ = self.expr() if self.accept_keyword("ELSE") else None
        self.expect_keyword("END")
        return A.Case(operand, tuple(whens), else_)


def _ref_alias(ref) -> Optional[str]:
    if isinstance(ref, A.TableRef):
        return ref.alias or ref.name
    if isinstance(ref, A.SubqueryRef):
        return ref.alias
    return None


def parse(sql: str) -> A.Statement:
    return Parser(sql).parse_statement()


def parse_expr(sql: str) -> A.Expr:
    p = Parser(sql)
    e = p.expr()
    if p.cur.kind != "EOF":
        raise p.error("unexpected trailing input")
    return e
