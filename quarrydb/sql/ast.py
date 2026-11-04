"""AST node definitions for the supported SQL dialect."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple, Union


# --------------------------------------------------------------------------- #
# Expressions                                                                 #
# --------------------------------------------------------------------------- #


class Expr:
    __slots__ = ()


@dataclass(frozen=True)
class Literal(Expr):
    value: Any


@dataclass(frozen=True)
class Param(Expr):
    key: Any  # int (positional, 1-based) or str (named) or None (next positional)


@dataclass(frozen=True)
class Column(Expr):
    table: Optional[str]
    name: str


@dataclass(frozen=True)
class Star(Expr):
    table: Optional[str] = None


@dataclass(frozen=True)
class Unary(Expr):
    op: str
    operand: Expr


@dataclass(frozen=True)
class Binary(Expr):
    op: str
    left: Expr
    right: Expr


@dataclass(frozen=True)
class IsNull(Expr):
    operand: Expr
    negated: bool = False


@dataclass(frozen=True)
class In(Expr):
    operand: Expr
    items: Union[Tuple[Expr, ...], "Select"]
    negated: bool = False


@dataclass(frozen=True)
class Between(Expr):
    operand: Expr
    low: Expr
    high: Expr
    negated: bool = False


@dataclass(frozen=True)
class Like(Expr):
    operand: Expr
    pattern: Expr
    escape: Optional[Expr] = None
    negated: bool = False
    glob: bool = False


@dataclass(frozen=True)
class Call(Expr):
    name: str
    args: Tuple[Expr, ...]
    distinct: bool = False


@dataclass(frozen=True)
class Case(Expr):
    operand: Optional[Expr]
    whens: Tuple[Tuple[Expr, Expr], ...]
    else_: Optional[Expr]


@dataclass(frozen=True)
class Cast(Expr):
    operand: Expr
    type_name: str


@dataclass(frozen=True)
class Exists(Expr):
    query: "Select"
    negated: bool = False


@dataclass(frozen=True)
class Subquery(Expr):
    query: "Select"


# --------------------------------------------------------------------------- #
# Table references                                                            #
# --------------------------------------------------------------------------- #


@dataclass
class TableRef:
    name: str
    alias: Optional[str] = None


@dataclass
class SubqueryRef:
    query: "Select"
    alias: str


@dataclass
class Join:
    left: Any
    right: Any
    kind: str  # INNER, LEFT, CROSS
    on: Optional[Expr]


# --------------------------------------------------------------------------- #
# Statements                                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class SelectItem:
    expr: Expr
    alias: Optional[str] = None


@dataclass
class Select:
    items: List[SelectItem]
    source: Any = None  # TableRef | SubqueryRef | Join | None
    where: Optional[Expr] = None
    group_by: List[Expr] = field(default_factory=list)
    having: Optional[Expr] = None
    order_by: List[Tuple[Expr, bool, Optional[str]]] = field(default_factory=list)  # (expr, descending, NULLS FIRST/LAST)
    limit: Optional[Expr] = None
    offset: Optional[Expr] = None
    distinct: bool = False
    compound: List[Tuple[str, "Select"]] = field(default_factory=list)  # (UNION/UNION ALL/..., select)


@dataclass
class ColumnDef:
    name: str
    type_name: Optional[str]
    not_null: bool = False
    primary_key: bool = False
    unique: bool = False
    default: Optional[Expr] = None
    check: Optional[Expr] = None
    autoincrement: bool = False
    pk_desc: bool = False


@dataclass
class TableConstraint:
    kind: str  # PRIMARY KEY, UNIQUE, CHECK
    columns: List[str] = field(default_factory=list)
    expr: Optional[Expr] = None
    name: Optional[str] = None


@dataclass
class CreateTable:
    name: str
    columns: List[ColumnDef]
    constraints: List[TableConstraint]
    if_not_exists: bool = False
    as_select: Optional[Select] = None


@dataclass
class DropTable:
    name: str
    if_exists: bool = False


@dataclass
class CreateIndex:
    name: str
    table: str
    columns: List[str]
    unique: bool = False
    if_not_exists: bool = False


@dataclass
class DropIndex:
    name: str
    if_exists: bool = False


@dataclass
class Insert:
    table: str
    columns: Optional[List[str]]
    rows: Optional[List[List[Expr]]]
    query: Optional[Select] = None
    or_replace: bool = False
    or_ignore: bool = False


@dataclass
class Update:
    table: str
    assignments: List[Tuple[str, Expr]]
    where: Optional[Expr] = None


@dataclass
class Delete:
    table: str
    where: Optional[Expr] = None


@dataclass
class Begin:
    pass


@dataclass
class Commit:
    pass


@dataclass
class Rollback:
    pass


@dataclass
class Vacuum:
    pass


@dataclass
class Explain:
    statement: Any


Statement = Union[
    Select, Insert, Update, Delete, CreateTable, DropTable, CreateIndex, DropIndex,
    Begin, Commit, Rollback, Vacuum, Explain,
]
