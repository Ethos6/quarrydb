# SQL reference

quarrydb implements a SQLite-flavoured dialect. Where this document is silent,
SQLite's behaviour is the intended one; the differential test suite checks
that assumption against the real `sqlite3` module.

## Statements

```sql
CREATE TABLE [IF NOT EXISTS] name (column-def | table-constraint, ...)
CREATE TABLE [IF NOT EXISTS] name AS select
DROP TABLE [IF EXISTS] name
CREATE [UNIQUE] INDEX [IF NOT EXISTS] name ON table (col, ...)
DROP INDEX [IF EXISTS] name

INSERT [OR REPLACE | OR IGNORE] INTO table [(col, ...)] VALUES (...), (...)
INSERT [OR REPLACE | OR IGNORE] INTO table [(col, ...)] select
INSERT INTO table DEFAULT VALUES
REPLACE INTO table ...
UPDATE table SET col = expr, ... [WHERE expr]
DELETE FROM table [WHERE expr]

SELECT [DISTINCT] item, ... [FROM source] [WHERE expr] [GROUP BY expr, ...] [HAVING expr]
       [compound-op select ...] [ORDER BY expr [ASC|DESC] [NULLS FIRST|LAST], ...]
       [LIMIT n [OFFSET m] | LIMIT m, n]
VALUES (...), (...)

BEGIN [TRANSACTION] | COMMIT | END | ROLLBACK
EXPLAIN statement
VACUUM
```

`column-def`: `name [type] [PRIMARY KEY [ASC|DESC] [AUTOINCREMENT]] [NOT NULL] [UNIQUE]
[DEFAULT literal | DEFAULT (expr)] [CHECK (expr)] [COLLATE x]`

`table-constraint`: `[CONSTRAINT name] PRIMARY KEY (cols) | UNIQUE (cols) | CHECK (expr)`

`source`: tables and derived tables joined with `,` / `CROSS JOIN` /
`[INNER] JOIN ... ON | USING (cols)` / `LEFT [OUTER] JOIN ... ON | USING (cols)`,
with optional aliases (`AS` optional) and parenthesised sub-selects.

`compound-op`: `UNION`, `UNION ALL`, `INTERSECT`, `EXCEPT` (evaluated left to right).

Identifiers may be quoted with `"..."`, `` `...` `` or `[...]`. Comments:
`-- line` and `/* block */`. Multiple statements separated by `;` are accepted
by `executescript` and the shell.

## Types and affinity

Declared types are free text; the affinity is derived as in SQLite:
`INT`→INTEGER, `CHAR/CLOB/TEXT`→TEXT, `BLOB`→BLOB, `REAL/FLOA/DOUB/NUM/DEC`→REAL,
otherwise none. Values are coerced on storage (`'12'` into an INTEGER column
becomes `12`; `3` into a REAL column becomes `3.0`; numbers into TEXT become
text). Comparisons between a column with numeric affinity and a text or
untyped operand convert the operand to a number; a TEXT column converts an
untyped operand to text.

A single-column `PRIMARY KEY` declared `INTEGER`/`INT`/`BIGINT` is an alias
for the rowid; `rowid`, `_rowid_` and `oid` are always available. Other
primary keys and `UNIQUE` constraints create automatic unique indexes.
`PRIMARY KEY` implies `NOT NULL`. `UNIQUE` allows multiple `NULL`s.

## Expressions

Literals: integers, reals (`1.5`, `1e3`, `.5`), hex (`0x1F`), strings (`'it''s'`),
blobs (`X'00FF'`), `NULL`, `TRUE`/`FALSE` (1/0). Parameters: `?`, `?N`, `:name`, `@name`, `$name`.

Operators by precedence (highest first): `||`; `* / %`; `+ -`; `< <= > >=`;
`= == <> != IS [NOT] IN LIKE GLOB BETWEEN`; `NOT`; `AND`; `OR`. Unary `-`, `+`, `NOT`.

* `/` on two integers is integer division truncating toward zero; division by
  zero yields `NULL`. `%` follows the sign of the dividend.
* `||` concatenates text (blobs concatenate as blobs); `NULL` propagates.
* Comparisons use three-valued logic. Mixed types order `NULL < numbers < text < blob`;
  `IS` / `IS NOT` treat `NULL` as a value.
* `LIKE` is case-insensitive (`%`, `_`, optional `ESCAPE`); `GLOB` is
  case-sensitive (`*`, `?`, `[...]`).
* `CASE WHEN ... THEN ... [ELSE] END` and `CASE x WHEN v THEN ... END`.
* `CAST(expr AS type)`.
* `EXISTS (select)`, `expr IN (select)`, `expr IN (list)`, scalar `(select)`;
  subqueries may reference outer columns.
* Output-column aliases may be used in `WHERE`, `GROUP BY`, `HAVING` and
  `ORDER BY` (SQLite extension). `ORDER BY n` sorts by output position.

## Functions

Scalar: `ABS CEIL CEILING CHAR CLOCK COALESCE CONCAT CONCAT_WS CONTAINS CURRENT_DATE
CURRENT_TIME CURRENT_TIMESTAMP DATE DATETIME ENDSWITH EXP FLOOR FORMAT HEX IF IFNULL IIF
INSTR LENGTH LIKELIHOOD LIKELY LN LOG LOG10 LOG2 LOWER LTRIM MAX(a,b,...) MIN(a,b,...)
NULLIF PI POW POWER PRINTF QUOTE RANDOM RANDOMBLOB REPEAT REPLACE REVERSE ROUND RTRIM
SIGN SQRT STARTSWITH STRFTIME SUBSTR SUBSTRING TIME TRIM TYPEOF UNHEX UNICODE
UNIXEPOCH UNLIKELY UPPER ZEROBLOB`

Date/time functions accept `'now'`, ISO-8601 text, or a Unix epoch number, and
modifiers such as `'+2 days'`, `'-1 month'`, `'start of day'`, `'localtime'`.

Aggregate: `COUNT(*)`, `COUNT(x)`, `SUM`, `TOTAL`, `AVG`, `MIN`, `MAX`,
`GROUP_CONCAT(x [, sep])` / `STRING_AGG`; all accept `DISTINCT`. Bare columns
in an aggregate query take their value from an arbitrary row of the group.

## Transactions

Outside `BEGIN ... COMMIT` every statement is its own transaction and is
durable when `execute` returns. Inside a transaction a failing statement is
undone on its own; the transaction remains open and usable. `ROLLBACK`
restores data and schema. The Python connection is a context manager that
commits on success and rolls back on exception.

## Not supported (yet)

`ALTER TABLE`, views, triggers, foreign keys, window functions, common table
expressions, `RIGHT`/`FULL OUTER JOIN`, `NATURAL JOIN`, collations,
savepoints, `RETURNING`, `UPSERT (ON CONFLICT)`, `ATTACH`, `PRAGMA`
(use `Connection.integrity_check()` / `.check` instead), and concurrent
access from several processes.
