"""Differential tests: the same statements must give the same answers as SQLite.

sqlite3 ships with CPython so this adds no dependency, and it is the best
oracle available for a SQLite-flavoured SQL dialect.
"""

import os
import random
import sqlite3
import tempfile
import unittest

import quarrydb

FIXTURE = [
    "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, age INTEGER, city TEXT DEFAULT 'nowhere', score REAL)",
    "CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount REAL, note TEXT, UNIQUE(user_id, note))",
    "CREATE INDEX idx_orders_user ON orders(user_id)",
    "CREATE INDEX idx_users_age ON users(age, name)",
    "INSERT INTO users (name, age, city, score) VALUES ('ada', 36, 'london', 9.5), ('linus', 55, 'helsinki', 7.25), "
    "('grace', 85, NULL, 10), ('alan', 41, 'london', NULL), ('ken', 79, 'murray hill', 8)",
    "INSERT INTO users (id, name, age) VALUES (10, 'zed', NULL)",
    "INSERT INTO orders (user_id, amount, note) VALUES (1, 10.5, 'a'), (1, 20, 'b'), (2, 5, 'c'), (3, 100, NULL), (3, 1, NULL), (10, 0.5, 'z')",
]

QUERIES = [
    "SELECT * FROM users ORDER BY id",
    "SELECT * FROM users ORDER BY age DESC, name",
    "SELECT name, age FROM users WHERE age BETWEEN 40 AND 80 ORDER BY age",
    "SELECT name FROM users WHERE city = 'london' ORDER BY 1",
    "SELECT name FROM users WHERE city IS NULL",
    "SELECT name FROM users WHERE age > 50 AND city LIKE '%l%' ORDER BY name",
    "SELECT name FROM users WHERE name LIKE 'a_a%' OR name GLOB 'k*' ORDER BY name",
    "SELECT city, COUNT(*), AVG(age), SUM(score), MIN(name), MAX(age) FROM users GROUP BY city ORDER BY city",
    "SELECT city, COUNT(*) AS n FROM users GROUP BY city HAVING n > 1 ORDER BY n DESC, city",
    "SELECT u.name, o.amount FROM users u JOIN orders o ON o.user_id = u.id ORDER BY u.name, o.amount",
    "SELECT u.name, COUNT(o.id), TOTAL(o.amount) FROM users u LEFT JOIN orders o ON o.user_id = u.id GROUP BY u.id ORDER BY u.id",
    "SELECT u.name FROM users u LEFT JOIN orders o ON o.user_id = u.id WHERE o.id IS NULL ORDER BY u.name",
    "SELECT name FROM users WHERE id IN (SELECT user_id FROM orders WHERE amount > 5) ORDER BY name",
    "SELECT name, (SELECT COUNT(*) FROM orders o WHERE o.user_id = users.id) AS n FROM users ORDER BY n DESC, name",
    "SELECT name FROM users u WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id AND o.amount >= 100)",
    "SELECT DISTINCT city FROM users ORDER BY city",
    "SELECT name FROM users ORDER BY age LIMIT 2 OFFSET 1",
    "SELECT name, CASE WHEN age < 40 THEN 'young' WHEN age < 60 THEN 'mid' ELSE 'senior' END AS band FROM users ORDER BY name",
    "SELECT name, COALESCE(city, 'unknown'), IFNULL(score, -1), NULLIF(age, 55) FROM users ORDER BY id",
    "SELECT UPPER(name), LENGTH(name), SUBSTR(name, 2, 2), name || '!' FROM users ORDER BY id",
    "SELECT 1 + 2 * 3, 7 / 2, 7.0 / 2, 7 % 3, -7 / 2, -7 % 3, 1 / 0, 'a' < 'b', 1 < 'a', NULL = NULL, NULL IS NULL",
    "SELECT TYPEOF(1), TYPEOF(1.5), TYPEOF('x'), TYPEOF(NULL), TYPEOF(X'00'), ABS(-3), ROUND(3.14159, 2), HEX('ab')",
    "SELECT name FROM users WHERE age = 36.0",
    "SELECT name FROM users WHERE id = 1.0",
    "SELECT name FROM users WHERE id = '2'",
    "SELECT name FROM users WHERE age >= 41 AND age < 80 ORDER BY age",
    "SELECT name FROM users WHERE age > 40.5 ORDER BY age",
    "SELECT * FROM orders WHERE user_id = 1 AND note = 'b'",
    "SELECT * FROM orders WHERE rowid > 3 ORDER BY rowid",
    "SELECT COUNT(DISTINCT city) FROM users",
    "SELECT GROUP_CONCAT(name, '|') FROM (SELECT name FROM users ORDER BY name)",
    "SELECT name FROM users UNION SELECT note FROM orders ORDER BY 1",
    "SELECT age FROM users WHERE age IS NOT NULL EXCEPT SELECT 36 ORDER BY 1",
    "SELECT x.n, x.c FROM (SELECT city AS c, COUNT(*) AS n FROM users GROUP BY city) x WHERE x.n > 1",
    "SELECT SUM(amount) FROM orders WHERE user_id = 99",
    "SELECT name FROM users WHERE age NOT IN (36, 55) ORDER BY name",
    "SELECT CAST('12' AS INTEGER) + 1, CAST(3.7 AS INT), CAST(5 AS TEXT), CAST('x' AS REAL)",
    "SELECT u.name, o2.amount FROM users u JOIN orders o1 ON o1.user_id = u.id JOIN orders o2 ON o2.user_id = o1.user_id AND o2.id <> o1.id ORDER BY u.name, o2.amount",
    "SELECT 'it''s', X'0102', 1e3, .5, 0x1F",
    "SELECT -age, +age, NOT age FROM users ORDER BY id",
    "SELECT 3 AND 0, 3 OR NULL, NULL AND 0, NULL OR 1, NOT NULL",
    "SELECT users.*, orders.amount FROM users JOIN orders USING (id) ORDER BY users.id",
    "SELECT name AS n FROM users WHERE n > 'g' ORDER BY n",
    "SELECT age, COUNT(*) FROM users GROUP BY age ORDER BY 2 DESC, 1",
    "SELECT name FROM users ORDER BY LENGTH(name), name",
    "SELECT min(3, 1, 2), max('a', 'b'), iif(1 > 2, 'y', 'n'), unicode('A'), char(72, 105), quote('it''s'), quote(X'ff'), quote(NULL)",
    "VALUES (1, 'a'), (2, 'b')",
    "SELECT * FROM (VALUES (1, 'a'), (2, 'b')) ORDER BY 1 DESC",
    "SELECT 5 = 5.0, 5 IS 5.0, '5' = 5, 1 = 1 AND 2 = 2",
    "SELECT 10 / 4, 10 / 4.0, 10 % 4, -10 % 4, 10 % -4, 2 * 3.5",
    "SELECT 'abc' || 1 || 2.5 || NULL",
    "SELECT name FROM users ORDER BY city NULLS LAST, name",
]

DML = [
    "UPDATE users SET age = age + 1, city = 'moved' WHERE age > 50",
    "UPDATE orders SET amount = amount * 2 WHERE user_id = (SELECT id FROM users WHERE name = 'ada')",
    "DELETE FROM orders WHERE amount > 100",
    "INSERT INTO users (name, age) SELECT name || '2', age FROM users WHERE age < 50",
    "INSERT OR REPLACE INTO users (id, name, age) VALUES (1, 'ada-new', 1)",
    "INSERT OR IGNORE INTO orders (user_id, note, amount) VALUES (1, 'b', 999)",
    "UPDATE users SET id = 100 WHERE id = 10",
    "DELETE FROM users WHERE id NOT IN (SELECT user_id FROM orders)",
]

ERRORS = [
    "INSERT INTO users (name) VALUES (NULL)",
    "INSERT INTO users (id, name) VALUES (1, 'dup')",
    "SELECT nope FROM users",
    "SELECT * FROM nope",
    "INSERT INTO orders (user_id, note) VALUES (1, 'b')",
]


def _norm(rows):
    return [tuple(round(v, 9) if isinstance(v, float) else v for v in r) for r in rows]


class DifferentialTests(unittest.TestCase):
    def setUp(self):
        self.q = quarrydb.connect(":memory:")
        self.s = sqlite3.connect(":memory:", isolation_level=None)
        for stmt in FIXTURE:
            self.q.execute(stmt)
            self.s.execute(stmt)

    def both(self, sql, params=None, unordered=False):
        try:
            a, ea = self.q.execute(sql, params).fetchall(), None
        except Exception as e:  # noqa: BLE001
            a, ea = None, e
        try:
            b, eb = self.s.execute(sql, params or []).fetchall(), None
        except Exception as e:  # noqa: BLE001
            b, eb = None, e
        self.assertEqual(ea is None, eb is None, f"{sql}\n quarrydb: {ea!r}\n sqlite: {eb!r}")
        if ea is not None:
            return
        na, nb = _norm(a), _norm(b)
        if unordered:
            na, nb = sorted(na, key=repr), sorted(nb, key=repr)
        self.assertEqual(na, nb, sql)

    def test_queries(self):
        for sql in QUERIES:
            with self.subTest(sql=sql):
                self.both(sql)

    def test_dml_sequence(self):
        for sql in DML:
            with self.subTest(sql=sql):
                self.both(sql)
                self.both("SELECT * FROM users ORDER BY id")
                self.both("SELECT * FROM orders ORDER BY id")
        for sql in ERRORS:
            with self.subTest(sql=sql):
                self.both(sql)
        self.assertEqual(self.q.integrity_check(), [])


class RandomisedTests(unittest.TestCase):
    """Random inserts/updates/deletes/queries against SQLite, with reopen and VACUUM."""

    WORDS = ["x", "y", "z", "ab", "abc", "Ab", "b%", "_", "", "ñ", "q'q"]

    def run_seed(self, seed, steps=250):
        rnd = random.Random(seed)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f.qdb")
            q = quarrydb.connect(path, fsync=False)
            s = sqlite3.connect(":memory:", isolation_level=None)
            for ddl in ("CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b TEXT, c REAL, d)",
                        "CREATE TABLE u (k TEXT PRIMARY KEY, v INTEGER, w TEXT)",
                        "CREATE INDEX ia ON t(a)", "CREATE INDEX ib ON t(b, a)", "CREATE UNIQUE INDEX iu ON u(v, w)"):
                q.execute(ddl)
                s.execute(ddl)

            def val(kind):
                r = rnd.random()
                if r < 0.1:
                    return None
                if kind == "a":
                    return rnd.randint(-5, 20)
                if kind == "b":
                    return rnd.choice(self.WORDS)
                if kind == "c":
                    return rnd.choice([0.5, 1.0, 2.25, -3.5, 1e10, 7])
                return rnd.choice([1, 2.0, "s", b"\x00\x01", 3, None, 2])

            def lit(v):
                if v is None:
                    return "NULL"
                if isinstance(v, bytes):
                    return "X'" + v.hex() + "'"
                if isinstance(v, str):
                    return "'" + v.replace("'", "''") + "'"
                return repr(v)

            def cond(depth=0):
                r = rnd.random()
                c = rnd.choice(["a", "b", "c", "d", "id"])
                kind = "a" if c == "id" else c
                if r < 0.4:
                    return f"{c} {rnd.choice(['=', '<>', '<', '<=', '>', '>='])} {lit(val(kind))}"
                if r < 0.5:
                    return f"{c} IS NULL"
                if r < 0.6:
                    return f"{c} BETWEEN {lit(val(kind))} AND {lit(val(kind))}"
                if r < 0.7:
                    return f"{c} IN ({lit(val(kind))}, {lit(val(kind))})"
                if r < 0.8 and c == "b":
                    return f"{c} LIKE {lit(rnd.choice(['a%', '%b%', '_', '%', 'a_c']))}"
                if depth < 2:
                    return f"({cond(depth + 1)} {rnd.choice(['AND', 'OR'])} {cond(depth + 1)})"
                return f"{c} > {lit(val(kind))}"

            def both(sql, params=None):
                try:
                    a, ea = q.execute(sql, params).fetchall(), None
                except Exception as e:  # noqa: BLE001
                    a, ea = None, e
                try:
                    b, eb = s.execute(sql, params or []).fetchall(), None
                except Exception as e:  # noqa: BLE001
                    b, eb = None, e
                self.assertEqual(ea is None, eb is None, f"seed {seed}: {sql} {params}\n {ea!r}\n {eb!r}")
                if ea is None:
                    self.assertEqual(sorted(_norm(a), key=repr), sorted(_norm(b), key=repr), f"seed {seed}: {sql} {params}")

            for step in range(steps):
                r = rnd.random()
                if r < 0.3:
                    rows = [(val("a"), val("b"), val("c"), val("d")) for _ in range(rnd.randint(1, 3))]
                    both("INSERT INTO t (a,b,c,d) VALUES " + ",".join("(?,?,?,?)" for _ in rows), [x for row in rows for x in row])
                elif r < 0.4:
                    both(f"INSERT OR {rnd.choice(['REPLACE', 'IGNORE'])} INTO u (k, v, w) VALUES (?, ?, ?)",
                         [rnd.choice(self.WORDS) + str(rnd.randint(0, 5)), rnd.randint(0, 3), rnd.choice(self.WORDS[:4])])
                elif r < 0.5:
                    both(f"UPDATE t SET {rnd.choice(['a = a + 1', 'b = b || ''!''', 'c = c * 2', 'd = NULL', 'a = NULL', 'id = id + 100'])} WHERE {cond()}")
                elif r < 0.58:
                    both(f"DELETE FROM t WHERE {cond()}")
                elif r < 0.6:
                    both("DELETE FROM u WHERE v = ?", [rnd.randint(0, 3)])
                else:
                    sel = rnd.choice(["*", "id", "a, b", "COUNT(*)", "SUM(a), MIN(b), MAX(c)", "a, COUNT(*) AS n", "DISTINCT a"])
                    sql = f"SELECT {sel} FROM t WHERE {cond()}"
                    if "AS n" in sel:
                        sql += " GROUP BY a"
                    elif rnd.random() < 0.3 and "(" not in sel:
                        sql += f" ORDER BY {rnd.choice(['id', 'a, id', 'b DESC, id', 'c, id'])} LIMIT {rnd.randint(0, 5)}"
                    both(sql)
                    if rnd.random() < 0.3:
                        both(f"SELECT t.id, u.k FROM t JOIN u ON u.v = t.a WHERE {cond()}")
                        both(f"SELECT t.id, u.k FROM t LEFT JOIN u ON u.v = t.a AND u.w = t.b WHERE {cond()}")
                if step % 60 == 59:
                    q.close()
                    q = quarrydb.connect(path, fsync=False)
                    both("SELECT * FROM t")
                    both("SELECT * FROM u")
            # transactions: rollback, and a failing statement inside a committed transaction
            q.execute("BEGIN"); s.execute("BEGIN")
            both("INSERT INTO t (a) VALUES (999)"); both("DELETE FROM u")
            q.execute("ROLLBACK"); s.execute("ROLLBACK")
            both("SELECT * FROM t"); both("SELECT * FROM u")
            q.execute("BEGIN"); s.execute("BEGIN")
            both("INSERT INTO t (a) VALUES (998)")
            both("INSERT INTO t (id, a) VALUES (1, 1)")  # may or may not conflict; both must agree
            both("INSERT INTO t (a) VALUES (997)")
            q.execute("COMMIT"); s.execute("COMMIT")
            both("SELECT * FROM t"); both("SELECT * FROM u")
            self.assertEqual(q.integrity_check(), [], f"seed {seed}")
            q.execute("VACUUM")
            both("SELECT * FROM t"); both("SELECT * FROM u")
            self.assertEqual(q.integrity_check(), [], f"seed {seed} after vacuum")
            q.close()

    def test_seeds(self):
        for seed in range(1, 5):
            with self.subTest(seed=seed):
                self.run_seed(seed)


if __name__ == "__main__":
    unittest.main()
