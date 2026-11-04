"""The schema catalog: tables, columns and indexes.

Every table and index is a B+tree; the catalog records each tree's root page
along with its SQL definition.  The catalog itself is a B+tree (its root is
in the file header) whose keys are ``T\\0<name>`` / ``I\\0<name>`` and whose
values are JSON documents.  Names are case-insensitive.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from .btree import BTree
from .pager import Pager
from .sql.errors import ProgrammingError

AFFINITIES = ("INTEGER", "REAL", "TEXT", "BLOB", "ANY")


def affinity_of(type_name: Optional[str]) -> str:
    """Map a declared type to a storage affinity (same rules as SQLite)."""
    if not type_name:
        return "ANY"
    t = type_name.upper()
    if "INT" in t or "BOOL" in t:
        return "INTEGER"
    if "CHAR" in t or "CLOB" in t or "TEXT" in t or "STRING" in t or "JSON" in t:
        return "TEXT"
    if "BLOB" in t:
        return "BLOB"
    if "REAL" in t or "FLOA" in t or "DOUB" in t or "NUM" in t or "DEC" in t:
        return "REAL"
    return "ANY"


class ColumnInfo:
    __slots__ = ("name", "type_name", "affinity", "not_null", "primary_key", "unique",
                 "default_sql", "check_sql", "autoincrement")

    def __init__(self, name: str, type_name: Optional[str] = None, not_null: bool = False,
                 primary_key: bool = False, unique: bool = False, default_sql: Optional[str] = None,
                 check_sql: Optional[str] = None, autoincrement: bool = False) -> None:
        self.name = name
        self.type_name = type_name
        self.affinity = affinity_of(type_name)
        self.not_null = not_null
        self.primary_key = primary_key
        self.unique = unique
        self.default_sql = default_sql
        self.check_sql = check_sql
        self.autoincrement = autoincrement

    def to_json(self) -> dict:
        return {
            "name": self.name, "type": self.type_name, "not_null": self.not_null,
            "primary_key": self.primary_key, "unique": self.unique, "default": self.default_sql,
            "check": self.check_sql, "autoincrement": self.autoincrement,
        }

    @classmethod
    def from_json(cls, d: dict) -> "ColumnInfo":
        return cls(d["name"], d.get("type"), d.get("not_null", False), d.get("primary_key", False),
                   d.get("unique", False), d.get("default"), d.get("check"), d.get("autoincrement", False))


class IndexInfo:
    __slots__ = ("name", "table", "columns", "unique", "root", "auto", "sql")

    def __init__(self, name: str, table: str, columns: List[str], unique: bool, root: int,
                 auto: bool = False, sql: Optional[str] = None) -> None:
        self.name = name
        self.table = table
        self.columns = columns
        self.unique = unique
        self.root = root
        self.auto = auto  # created implicitly by PRIMARY KEY / UNIQUE
        self.sql = sql

    def to_json(self) -> dict:
        return {"name": self.name, "table": self.table, "columns": self.columns, "unique": self.unique,
                "root": self.root, "auto": self.auto, "sql": self.sql}

    @classmethod
    def from_json(cls, d: dict) -> "IndexInfo":
        return cls(d["name"], d["table"], d["columns"], d["unique"], d["root"], d.get("auto", False), d.get("sql"))


class TableInfo:
    __slots__ = ("name", "columns", "root", "rowid_column", "checks", "sql", "seq", "indexes", "_colmap")

    def __init__(self, name: str, columns: List[ColumnInfo], root: int, rowid_column: Optional[int],
                 checks: List[str], sql: Optional[str], seq: int = 0) -> None:
        self.name = name
        self.columns = columns
        self.root = root
        self.rowid_column = rowid_column  # index of the INTEGER PRIMARY KEY column, or None
        self.checks = checks  # table-level CHECK constraint SQL
        self.sql = sql
        self.seq = seq  # AUTOINCREMENT high-water mark
        self.indexes: List[IndexInfo] = []
        self._colmap: Dict[str, int] = {c.name.lower(): i for i, c in enumerate(columns)}

    def column_index(self, name: str) -> Optional[int]:
        return self._colmap.get(name.lower())

    @property
    def column_names(self) -> List[str]:
        return [c.name for c in self.columns]

    def to_json(self) -> dict:
        return {
            "name": self.name, "columns": [c.to_json() for c in self.columns], "root": self.root,
            "rowid_column": self.rowid_column, "checks": self.checks, "sql": self.sql, "seq": self.seq,
        }

    @classmethod
    def from_json(cls, d: dict) -> "TableInfo":
        return cls(d["name"], [ColumnInfo.from_json(c) for c in d["columns"]], d["root"],
                   d.get("rowid_column"), d.get("checks", []), d.get("sql"), d.get("seq", 0))


class Catalog:
    def __init__(self, pager: Pager) -> None:
        self.pager = pager
        if pager.catalog_root == 0:
            self.tree = BTree(pager)
            pager.catalog_root = self.tree.root
            pager.commit()
        else:
            self.tree = BTree(pager, pager.catalog_root)
        self.tables: Dict[str, TableInfo] = {}
        self.indexes: Dict[str, IndexInfo] = {}
        self._trees: Dict[int, BTree] = {}
        self.reload()

    # ------------------------------------------------------------------ #
    # Loading / persistence                                               #
    # ------------------------------------------------------------------ #

    def reload(self) -> None:
        self.tables.clear()
        self.indexes.clear()
        self._trees.clear()
        self.tree = BTree(self.pager, self.pager.catalog_root)
        for key, value in self.tree.seek(b""):
            doc = json.loads(value.decode("utf-8"))
            if key.startswith(b"T\x00"):
                t = TableInfo.from_json(doc)
                self.tables[t.name.lower()] = t
            elif key.startswith(b"I\x00"):
                ix = IndexInfo.from_json(doc)
                self.indexes[ix.name.lower()] = ix
        for ix in self.indexes.values():
            self.tables[ix.table.lower()].indexes.append(ix)

    def _put(self, kind: bytes, name: str, doc: dict) -> None:
        self.tree.insert(kind + name.lower().encode("utf-8"), json.dumps(doc, separators=(",", ":")).encode("utf-8"))
        self.pager.catalog_root = self.tree.root

    def _del(self, kind: bytes, name: str) -> None:
        self.tree.delete(kind + name.lower().encode("utf-8"))
        self.pager.catalog_root = self.tree.root

    def save_table(self, t: TableInfo) -> None:
        self._put(b"T\x00", t.name, t.to_json())

    def save_index(self, ix: IndexInfo) -> None:
        self._put(b"I\x00", ix.name, ix.to_json())

    def sync_roots(self) -> None:
        """Persist any B+tree root that moved during the last statement."""
        for t in self.tables.values():
            tree = self._trees.get(id(t))
            if tree is not None and tree.root != t.root:
                t.root = tree.root
                self.save_table(t)
        for ix in self.indexes.values():
            tree = self._trees.get(id(ix))
            if tree is not None and tree.root != ix.root:
                ix.root = tree.root
                self.save_index(ix)

    def tree_for(self, obj) -> BTree:
        tree = self._trees.get(id(obj))
        if tree is None:
            tree = BTree(self.pager, obj.root)
            self._trees[id(obj)] = tree
        return tree

    # ------------------------------------------------------------------ #
    # Lookup                                                              #
    # ------------------------------------------------------------------ #

    def get_table(self, name: str) -> TableInfo:
        t = self.tables.get(name.lower())
        if t is None:
            raise ProgrammingError(f"no such table: {name}")
        return t

    def has_table(self, name: str) -> bool:
        return name.lower() in self.tables

    def get_index(self, name: str) -> IndexInfo:
        ix = self.indexes.get(name.lower())
        if ix is None:
            raise ProgrammingError(f"no such index: {name}")
        return ix

    def has_index(self, name: str) -> bool:
        return name.lower() in self.indexes

    # ------------------------------------------------------------------ #
    # DDL                                                                 #
    # ------------------------------------------------------------------ #

    def create_table(self, t: TableInfo) -> TableInfo:
        if t.name.lower() in self.tables or t.name.lower() in self.indexes:
            raise ProgrammingError(f"table {t.name} already exists")
        tree = BTree(self.pager)
        t.root = tree.root
        self.tables[t.name.lower()] = t
        self._trees[id(t)] = tree
        self.save_table(t)
        return t

    def drop_table(self, name: str) -> None:
        t = self.get_table(name)
        for ix in list(t.indexes):
            self.drop_index(ix.name)
        self.tree_for(t).drop()
        self._trees.pop(id(t), None)
        del self.tables[name.lower()]
        self._del(b"T\x00", name)

    def create_index(self, ix: IndexInfo) -> IndexInfo:
        if ix.name.lower() in self.indexes or ix.name.lower() in self.tables:
            raise ProgrammingError(f"index {ix.name} already exists")
        t = self.get_table(ix.table)
        for c in ix.columns:
            if t.column_index(c) is None:
                raise ProgrammingError(f"no such column: {c}")
        tree = BTree(self.pager)
        ix.root = tree.root
        self.indexes[ix.name.lower()] = ix
        t.indexes.append(ix)
        self._trees[id(ix)] = tree
        self.save_index(ix)
        return ix

    def drop_index(self, name: str) -> None:
        ix = self.get_index(name)
        self.tree_for(ix).drop()
        self._trees.pop(id(ix), None)
        t = self.tables[ix.table.lower()]
        t.indexes = [i for i in t.indexes if i is not ix]
        del self.indexes[name.lower()]
        self._del(b"I\x00", name)

    def rollback(self) -> None:
        """Forget in-memory state after the pager rolled back."""
        self.reload()
