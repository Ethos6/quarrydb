"""Micro-benchmark: quarrydb vs the C-implemented sqlite3 module.

Run:  python bench/bench.py [rows]

This is not a fair fight (pure Python vs C), it is here to give an honest
order-of-magnitude picture and to catch performance regressions.
"""

from __future__ import annotations

import os
import random
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import quarrydb  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 50_000
rnd = random.Random(1)
ROWS = [(i, f"user{i}", rnd.randint(18, 90), rnd.random() * 1000) for i in range(N)]
LOOKUPS = [rnd.randrange(N) for _ in range(2000)]
NAMES = [f"user{rnd.randrange(N)}" for _ in range(2000)]


def timed(label, fn):
    t0 = time.perf_counter()
    r = fn()
    dt = time.perf_counter() - t0
    print(f"  {label:<44} {dt * 1000:9.1f} ms")
    return r


def run(name, db, is_sqlite):
    print(name)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, age INT, score REAL)")
    if is_sqlite:
        db.execute("BEGIN")
    else:
        db.execute("BEGIN")
    timed(f"insert {N} rows (one transaction)", lambda: db.executemany("INSERT INTO t VALUES (?, ?, ?, ?)", ROWS))
    db.execute("COMMIT")
    timed("create index on name", lambda: db.execute("CREATE INDEX i_name ON t(name)"))
    timed("2000 point lookups by primary key", lambda: [db.execute("SELECT * FROM t WHERE id = ?", (k,)).fetchall() for k in LOOKUPS])
    timed("2000 point lookups by indexed text column", lambda: [db.execute("SELECT * FROM t WHERE name = ?", (k,)).fetchall() for k in NAMES])
    timed("range scan id BETWEEN 1000 AND 6000", lambda: db.execute("SELECT COUNT(*) FROM t WHERE id BETWEEN 1000 AND 6000").fetchall())
    timed("full scan with filter (age > 50)", lambda: db.execute("SELECT COUNT(*) FROM t WHERE age > 50").fetchall())
    timed("group by age", lambda: db.execute("SELECT age, COUNT(*), AVG(score) FROM t GROUP BY age").fetchall())
    timed("order by score limit 10", lambda: db.execute("SELECT * FROM t ORDER BY score DESC LIMIT 10").fetchall())
    timed("update 10% of rows", lambda: db.execute("UPDATE t SET score = score + 1 WHERE age = 30"))
    timed("delete 10% of rows", lambda: db.execute("DELETE FROM t WHERE age = 40"))
    timed("1000 autocommit inserts (fsync each)", lambda: [db.execute("INSERT INTO t (name, age, score) VALUES (?, ?, ?)", (f"x{i}", 1, 1.0)) for i in range(1000)])
    print()


with tempfile.TemporaryDirectory() as d:
    q = quarrydb.connect(os.path.join(d, "q.qdb"))
    run(f"quarrydb {quarrydb.__version__} (pure Python, file-backed, fsync on)", q, False)
    q.close()
    s = sqlite3.connect(os.path.join(d, "s.db"), isolation_level=None)
    s.execute("PRAGMA synchronous=FULL")
    run(f"sqlite3 {sqlite3.sqlite_version} (C, file-backed, synchronous=FULL)", s, True)
    s.close()
