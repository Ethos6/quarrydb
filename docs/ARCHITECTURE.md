# Architecture

This document follows one statement through the engine, then zooms into the
three mechanisms that make the engine a database rather than a script:
durable commits, statement-level undo, and index selection.

## 1. The life of a statement

```python
db.execute("SELECT u.name, COUNT(*) FROM users u JOIN orders o ON o.user_id = u.id "
           "WHERE u.age > ? GROUP BY u.id HAVING COUNT(*) > 1 ORDER BY 2 DESC LIMIT 5", [30])
```

1. **`Connection._run`** looks the SQL up in a small statement cache; on a miss
   it calls `sql.parser.parse`, which drives `sql.lexer.tokenize` and returns
   an AST (`sql/ast.py` dataclasses). Parameters are bound into a dict
   (`{1: 30}` here, or `{"name": value}` for named ones).

2. **`Pager.begin_statement`** opens a *statement journal*: from now on the
   previous content of every page and every unflushed B+tree node the
   statement touches is remembered, so the statement can be undone on error
   without touching the rest of the transaction.

3. **`Executor.execute`** dispatches on the AST type. For a `SELECT` it builds
   a `SelectPlan` (see §4) and runs it. Rows are produced lazily by a pipeline
   of generators: `_produce` → `_join` per table → `_fetch` per access path.
   Each `_fetch` yields a *segment* `[rowid, col1, col2, ...]` that is spliced
   into a flat row list covering every table in scope. Expressions were
   compiled once, at plan time, into closures that index into that flat row.

4. Aggregation groups rows by the compiled `GROUP BY` keys into
   `(first_row, rows)` pairs; item expressions compiled in *group mode* read
   plain columns from `first_row` and fold aggregates over `rows`. `HAVING`
   filters groups, `ORDER BY` sorts the output (or is skipped when the access
   path already yields the order), `LIMIT/OFFSET` slices.

5. **`Catalog.sync_roots`** persists any B+tree whose root page moved during
   the statement (a split or merge at the top of a tree changes its root; the
   catalog entry stores the root page id).

6. On success `Pager.end_statement` drops the journal and, outside an explicit
   transaction, `Pager.commit` makes everything durable (§2). On any exception
   `Pager.undo_statement` restores the journaled pages and nodes, the catalog
   is reloaded from disk state, and outside a transaction the whole pager is
   rolled back.

## 2. Durable commits

The pager keeps all modifications in memory until commit:

* `_dirty`: page id → new page bytes (pages written by `write`, `allocate`, `free`)
* `dirty_nodes`: page id → parsed `Node` objects modified by the B+tree and
  not yet serialised. Serialising a node costs a full page build, so the tree
  marks nodes dirty and the pager serialises each one exactly once at flush
  time instead of once per insert. This is the single biggest performance
  win in the engine.

`commit()`:

```
flush_nodes()                          serialise dirty nodes into _dirty
txn_id += 1; header = pack_header()
frames = dirty pages + header page
append frames to <db>-wal              [pid u32][crc32 u32][page bytes] each
append commit frame                    [0xFFFFFFFF][crc32][txn_id ...]
fsync(wal)                             <- point of durability
write frames into the main file
fsync(main)
truncate wal to 0                      (checkpoint)
```

Recovery on open reads the log frame by frame, verifying each CRC. Frames are
buffered until a commit frame is seen, then applied to the main file. A bad
CRC, a truncated frame, or a missing commit frame ends replay: everything from
there on belongs to a transaction that never committed and is discarded. The
log is then truncated. Because the main file is only ever written after the
log is complete and synced, it always reflects a committed state, possibly an
older one that replay brings forward.

The header page (page 0) is part of every commit: it carries the page count,
the free-list head, the catalog root and the transaction counter, so the
whole file state moves atomically with the log.

`rollback()` simply clears `_dirty` and `dirty_nodes`, drops the parsed-node
cache and restores the header fields. Nothing was written to disk.

In-memory databases (`":memory:"`) use the same code paths with a dict in
place of the file and no log.

## 3. Statement-level undo

A transaction may contain many statements, and SQL requires that a failing
statement (a `UNIQUE` violation on the third row of a multi-row `INSERT`, say)
leaves no trace while the earlier statements of the transaction survive.
Without savepoints the naive answer is to copy the dirty set before each
statement, which is quadratic for long transactions. The pager instead keeps
two small journals per statement:

* **page journal**: on the first `write`/`allocate`/`free` of a page in this
  statement, record the page's previous dirty entry (or "not dirty").
* **node journal**: when the B+tree loads a node that is already in
  `dirty_nodes`, take a snapshot of its lists before the statement can mutate
  it; when a node becomes dirty for the first time in this statement, record
  "was clean".

`undo_statement()` restores previous page entries, restores node snapshots,
drops nodes that only became dirty in this statement, resets the header
fields and clears the parsed-node cache. Nodes untouched by the statement are
kept as they are. The cost is proportional to what the statement touched, not
to the transaction size.

## 4. Query planning

`plan_select` turns a `SELECT` into a `SelectPlan` with one `SourceStep` per
table in left-to-right join order. Planning is cheap and rule based; there are
no statistics.

**Predicate push-down.** `WHERE` and every `ON` are split into AND-conjuncts.
A conjunct is attached to the *last* table it references, so it is evaluated
as soon as that table's row is available. A conjunct whose last table is the
right side of a `LEFT JOIN` is deferred until after all joins, otherwise it
would turn the outer join into an inner one. Conjuncts containing subqueries
are deferred as well.

**Access path selection** for a table, given its conjuncts, looks for
predicates of the shape `col op expr` where `expr` only references earlier
tables or constants (so the lookup key can be computed per outer row, which is
what makes index-driven joins work):

1. `rowid = expr` (or the `INTEGER PRIMARY KEY` alias): one `BTree.get`.
2. Index equality on the longest prefix of an index's columns, preferring a
   fully covered unique index; an optional range on the next column.
3. `rowid` range.
4. Index range on the first column of an index.
5. Full scan.

Equality lookups on numeric keys are widened to a tiny range covering both
`5` and `5.0`, and the original predicate is kept as a filter, so answers are
exact without the index having to know about affinity. Index entries are
`key_encode(column values) + rowid`, which makes them unique, orders ties by
rowid, and lets the executor fetch the row with one more `BTree.get`.

**Ordering.** If the chosen path yields rows in the order `ORDER BY` asks for
(ascending rowid for scans and rowid ranges, index column order for index
paths) the sort is skipped and `LIMIT` stops the pipeline early.

**Subqueries.** A subquery is planned with the enclosing scope as its parent.
Column references that do not resolve locally are compiled to read from the
parent's current row through a shared cell, which the executor fills before
running the subquery for each outer row. A subquery that never touched its
parent is evaluated once and cached.

`EXPLAIN` prints the chosen paths and filters:

```
SEARCH TABLE users USING INDEX idx_users_age (age>?)
  FILTER u.age > ?
INNER JOIN SEARCH TABLE orders USING INDEX idx_orders_user (user_id=?)
  FILTER o.user_id = u.id
AGGREGATE GROUP BY 1 expr(s)
ORDER BY 1 term(s) via sort
LIMIT 5 OFFSET 0
```

## 5. Rows, keys and types

* A table is a B+tree keyed by the 8-byte sortable rowid; the value is the
  row record (`encoding.encode_record`): a varint count followed by
  tag-prefixed values. `INTEGER PRIMARY KEY` columns are aliases for the
  rowid and are patched in when a row is decoded.
* An index is a B+tree whose keys are `encode_key(values) + rowid` and whose
  values are empty. `encode_key` is order preserving across types
  (`NULL < numbers < text < blob`), within numbers (integers and reals
  interleave by value, `1 < 1.0 < 2`), and within strings (prefix sorts
  first, embedded zero bytes allowed).
* Values stored into a column pass through SQLite-style *affinity*
  (`INTEGER`, `REAL`, `TEXT`, `BLOB`, or none), and comparisons between a
  typed column and an untyped operand apply the column's affinity to the
  operand, so `WHERE id = '2'` works the way users expect.

## 6. Where to extend

* New scalar function: add an entry to `SCALARS` in `sql/functions.py`.
* New aggregate: subclass `Aggregate` and register it in `AGGREGATES`.
* New statement: a parser method producing an AST node, and a branch in
  `Executor.execute`.
* New access path: extend `_plan_access` and `_fetch`.
* Concurrency, savepoints, `ALTER TABLE`, views and foreign keys are the
  obvious next steps and are intentionally left out to keep the core small.
