"""Public API: :func:`connect`, :class:`Connection`, :class:`Cursor`.

The interface follows the spirit of PEP 249 (``sqlite3``-like) without
claiming full compliance: ``execute`` returns a cursor, parameters can be
positional (``?``) or named (``:name``), and the connection is a context
manager that commits on success and rolls back on error.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Union

from .catalog import Catalog
from .pager import DatabaseError, Pager
from .sql import ast as A
from .sql.errors import ProgrammingError
from .sql.executor import Executor, Result
from .sql.lexer import split_statements
from .sql.parser import parse

Params = Union[Sequence[Any], Dict[str, Any], None]


class Cursor:
    """Result of one statement.  Iterable; rows are plain tuples."""

    def __init__(self, conn: "Connection", result: Optional[Result] = None) -> None:
        self.connection = conn
        self._rows: List[tuple] = []
        self._pos = 0
        self.description: Optional[List[Tuple[Any, ...]]] = None
        self.rowcount = -1
        self.lastrowid: Optional[int] = None
        self.arraysize = 1
        if result is not None:
            self._load(result)

    def _load(self, result: Result) -> "Cursor":
        self._rows = result.rows
        self._pos = 0
        self.description = [(c, None, None, None, None, None, None) for c in result.columns] or None
        self.rowcount = result.rowcount
        self.lastrowid = result.lastrowid
        return self

    @property
    def columns(self) -> List[str]:
        return [d[0] for d in self.description] if self.description else []

    def execute(self, sql: str, params: Params = None) -> "Cursor":
        return self._load(self.connection._run(sql, params))

    def executemany(self, sql: str, seq_of_params: Iterable[Params]) -> "Cursor":
        total = 0
        for p in seq_of_params:
            self.execute(sql, p)
            if self.rowcount > 0:
                total += self.rowcount
        self.rowcount = total
        return self

    def fetchone(self) -> Optional[tuple]:
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size: Optional[int] = None) -> List[tuple]:
        n = size if size is not None else self.arraysize
        out = self._rows[self._pos : self._pos + n]
        self._pos += len(out)
        return out

    def fetchall(self) -> List[tuple]:
        out = self._rows[self._pos :]
        self._pos = len(self._rows)
        return out

    def __iter__(self) -> Iterator[tuple]:
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def close(self) -> None:
        self._rows = []


class Connection:
    def __init__(self, path: str = ":memory:", page_size: int = 4096, fsync: bool = True) -> None:
        self.path = path
        self.pager = Pager(path, page_size=page_size, fsync=fsync)
        self.catalog = Catalog(self.pager)
        self.executor = Executor(self.catalog)
        self.in_transaction = False
        self.closed = False
        self._stmt_cache: Dict[str, A.Statement] = {}

    # ------------------------------------------------------------------ #
    # Statement execution                                                 #
    # ------------------------------------------------------------------ #

    def execute(self, sql: str, params: Params = None) -> Cursor:
        return Cursor(self, self._run(sql, params))

    def executemany(self, sql: str, seq_of_params: Iterable[Params]) -> Cursor:
        cur = Cursor(self)
        return cur.executemany(sql, seq_of_params)

    def executescript(self, script: str) -> None:
        for stmt in split_statements(script):
            self._run(stmt, None)

    def cursor(self) -> Cursor:
        return Cursor(self)

    def _run(self, sql: str, params: Params) -> Result:
        if self.closed:
            raise ProgrammingError("connection is closed")
        stmt = self._stmt_cache.get(sql)
        if stmt is None:
            stmt = parse(sql)
            if len(self._stmt_cache) >= 256:
                self._stmt_cache.clear()
            self._stmt_cache[sql] = stmt
        bound = _bind(params)
        if isinstance(stmt, A.Begin):
            if self.in_transaction:
                raise ProgrammingError("cannot start a transaction within a transaction")
            self.in_transaction = True
            return Result()
        if isinstance(stmt, A.Commit):
            if not self.in_transaction:
                raise ProgrammingError("no transaction is active")
            self.commit()
            return Result()
        if isinstance(stmt, A.Rollback):
            if not self.in_transaction:
                raise ProgrammingError("no transaction is active")
            self.rollback()
            return Result()
        if isinstance(stmt, A.Vacuum):
            if self.in_transaction:
                raise ProgrammingError("cannot VACUUM from within a transaction")
            self.vacuum()
            return Result()

        self.pager.begin_statement()
        try:
            result = self.executor.execute(stmt, bound)
            self.catalog.sync_roots()
        except Exception:
            self.pager.undo_statement()
            self.catalog.reload()
            if not self.in_transaction:
                self.pager.rollback()
            raise
        self.pager.end_statement()
        if not self.in_transaction:
            self.pager.commit()
        return result

    # ------------------------------------------------------------------ #
    # Transactions                                                        #
    # ------------------------------------------------------------------ #

    def begin(self) -> None:
        self._run("BEGIN", None)

    def commit(self) -> None:
        self.pager.commit()
        self.in_transaction = False

    def rollback(self) -> None:
        self.pager.rollback()
        self.catalog.reload()
        self.in_transaction = False

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()

    # ------------------------------------------------------------------ #
    # Introspection                                                       #
    # ------------------------------------------------------------------ #

    def tables(self) -> List[str]:
        return sorted(t.name for t in self.catalog.tables.values())

    def indexes(self, table: Optional[str] = None) -> List[str]:
        return sorted(ix.name for ix in self.catalog.indexes.values() if table is None or ix.table.lower() == table.lower())

    def schema(self, table: Optional[str] = None) -> List[str]:
        out = []
        for t in sorted(self.catalog.tables.values(), key=lambda t: t.name.lower()):
            if table is not None and t.name.lower() != table.lower():
                continue
            out.append(t.sql or f"CREATE TABLE {t.name} (...)")
            for ix in t.indexes:
                if not ix.auto and ix.sql:
                    out.append(ix.sql)
        return out

    def columns(self, table: str) -> List[Dict[str, Any]]:
        t = self.catalog.get_table(table)
        return [
            {"name": c.name, "type": c.type_name, "not_null": c.not_null, "primary_key": c.primary_key,
             "unique": c.unique, "default": c.default_sql}
            for c in t.columns
        ]

    def iterdump(self) -> Iterator[str]:
        """Yield SQL statements that recreate the database."""
        yield "BEGIN;"
        for t in sorted(self.catalog.tables.values(), key=lambda t: t.name.lower()):
            yield (t.sql or f"CREATE TABLE {t.name} ({', '.join(c.name for c in t.columns)})") + ";"
            from .sql.sqlgen import literal, quote_ident
            cols = ", ".join(quote_ident(c.name) for c in t.columns)
            for row in self.execute(f"SELECT * FROM {quote_ident(t.name)}"):
                yield f"INSERT INTO {quote_ident(t.name)} ({cols}) VALUES ({', '.join(literal(v) for v in row)});"
            for ix in t.indexes:
                if not ix.auto and ix.sql:
                    yield ix.sql + ";"
        yield "COMMIT;"

    # ------------------------------------------------------------------ #
    # Maintenance                                                         #
    # ------------------------------------------------------------------ #

    def integrity_check(self) -> List[str]:
        """Verify every B+tree and index; return a list of problems (empty when healthy)."""
        from .encoding import decode_rowid, encode_key, encode_rowid

        problems: List[str] = []
        try:
            self.catalog.tree.check()
        except DatabaseError as e:
            problems.append(f"catalog: {e}")
        for t in self.catalog.tables.values():
            tree = self.catalog.tree_for(t)
            try:
                tree.check()
            except DatabaseError as e:
                problems.append(f"table {t.name}: {e}")
                continue
            rows = {}
            for key, raw in tree.seek(b""):
                rowid = decode_rowid(key)
                rows[rowid] = self.executor.decode_row(t, rowid, raw)[1:]
            for ix in t.indexes:
                itree = self.catalog.tree_for(ix)
                try:
                    itree.check()
                except DatabaseError as e:
                    problems.append(f"index {ix.name}: {e}")
                    continue
                n = 0
                for key, _ in itree.seek(b""):
                    n += 1
                    rowid = decode_rowid(key[-8:])
                    if rowid not in rows:
                        problems.append(f"index {ix.name}: entry points at missing rowid {rowid}")
                if n != len(rows):
                    problems.append(f"index {ix.name}: {n} entries for {len(rows)} rows")
                for rowid, values in rows.items():
                    vals = [values[t.column_index(c)] for c in ix.columns]  # type: ignore[index]
                    if not itree.contains(encode_key(vals) + encode_rowid(rowid)):
                        problems.append(f"index {ix.name}: row {rowid} is missing")
                        break
        return problems

    def vacuum(self) -> None:
        """Rebuild the database file compactly (drops free pages, re-packs B+trees)."""
        if self.pager.in_memory:
            return
        path = self.path
        tmp = path + ".vacuum"
        for p in (tmp, tmp + "-wal"):
            if os.path.exists(p):
                os.remove(p)
        new = Connection(tmp, page_size=self.pager.page_size, fsync=self.pager.fsync_enabled)
        try:
            new.pager.begin_statement()
            for stmt in self.iterdump():
                if stmt in ("BEGIN;", "COMMIT;"):
                    continue
                new.executor.execute(parse(stmt), {})
                new.catalog.sync_roots()
            new.pager.end_statement()
            new.pager.commit()
        except Exception:
            new.close()
            for p in (tmp, tmp + "-wal"):
                if os.path.exists(p):
                    os.remove(p)
            raise
        new.close()
        self.pager.close()
        os.replace(tmp, path)
        if os.path.exists(path + "-wal"):
            os.remove(path + "-wal")
        if os.path.exists(tmp + "-wal"):
            os.remove(tmp + "-wal")
        self.pager = Pager(path, page_size=self.pager.page_size, fsync=self.pager.fsync_enabled)
        self.catalog = Catalog(self.pager)
        self.executor = Executor(self.catalog)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.pager.close()

    def __del__(self) -> None:  # pragma: no cover
        try:
            self.close()
        except Exception:
            pass


def _bind(params: Params) -> Dict[Any, Any]:
    if params is None:
        return {}
    if isinstance(params, dict):
        out: Dict[Any, Any] = {}
        for k, v in params.items():
            out[k] = v
            if isinstance(k, str) and k[:1] in ":@$":
                out[k[1:]] = v
        return out
    if isinstance(params, (str, bytes)):
        raise ProgrammingError("parameters must be a sequence or a mapping")
    return {i + 1: v for i, v in enumerate(params)}


def connect(path: str = ":memory:", page_size: int = 4096, fsync: bool = True) -> Connection:
    """Open (or create) a database.  Use ``":memory:"`` for a transient database."""
    return Connection(path, page_size=page_size, fsync=fsync)


__all__ = ["connect", "Connection", "Cursor", "DatabaseError"]
