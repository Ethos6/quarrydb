"""SQL layer tests: DDL, DML, queries, constraints, transactions, persistence."""

import os
import tempfile
import unittest

import quarrydb
from quarrydb import IntegrityError, ProgrammingError, SQLError


class Base(unittest.TestCase):
    def setUp(self):
        self.db = quarrydb.connect(":memory:")

    def q(self, sql, params=None):
        return self.db.execute(sql, params).fetchall()


class DDLTests(Base):
    def test_create_and_introspect(self):
        self.db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE, n INT DEFAULT 7)")
        self.assertEqual(self.db.tables(), ["t"])
        self.assertEqual(self.db.indexes("t"), ["t_email_unique"])
        cols = self.db.columns("t")
        self.assertEqual([c["name"] for c in cols], ["id", "name", "email", "n"])
        self.assertTrue(cols[1]["not_null"])
        self.assertEqual(cols[3]["default"], "7")
        self.assertIn("CREATE TABLE t", self.db.schema("t")[0])

    def test_if_not_exists_and_drop(self):
        self.db.execute("CREATE TABLE t (a)")
        with self.assertRaises(ProgrammingError):
            self.db.execute("CREATE TABLE t (a)")
        self.db.execute("CREATE TABLE IF NOT EXISTS t (a)")
        self.db.execute("DROP TABLE t")
        self.db.execute("DROP TABLE IF EXISTS t")
        with self.assertRaises(ProgrammingError):
            self.db.execute("DROP TABLE t")
        self.assertEqual(self.db.tables(), [])

    def test_create_index_populates_existing_rows(self):
        self.db.execute("CREATE TABLE t (a, b)")
        self.db.executemany("INSERT INTO t VALUES (?, ?)", [(i % 5, i) for i in range(100)])
        self.db.execute("CREATE INDEX ia ON t(a)")
        plan = self.q("EXPLAIN SELECT * FROM t WHERE a = 3")
        self.assertIn("USING INDEX ia", plan[0][0])
        self.assertEqual(self.q("SELECT COUNT(*) FROM t WHERE a = 3"), [(20,)])
        with self.assertRaises(IntegrityError):
            self.db.execute("CREATE UNIQUE INDEX iu ON t(a)")
        self.db.execute("DROP INDEX ia")
        self.assertIn("SCAN TABLE", self.q("EXPLAIN SELECT * FROM t WHERE a = 3")[0][0])

    def test_create_table_as_select(self):
        self.db.execute("CREATE TABLE t (a, b)")
        self.db.execute("INSERT INTO t VALUES (1, 'x'), (2, 'y')")
        self.db.execute("CREATE TABLE u AS SELECT b AS name, a * 10 AS tens FROM t")
        self.assertEqual(self.q("SELECT * FROM u ORDER BY tens"), [("x", 10), ("y", 20)])

    def test_composite_primary_key(self):
        self.db.execute("CREATE TABLE t (a, b, c, PRIMARY KEY (a, b))")
        self.db.execute("INSERT INTO t VALUES (1, 1, 'x'), (1, 2, 'y')")
        with self.assertRaises(IntegrityError):
            self.db.execute("INSERT INTO t VALUES (1, 2, 'z')")
        with self.assertRaises(IntegrityError):
            self.db.execute("INSERT INTO t VALUES (NULL, 3, 'z')")
        self.assertEqual(self.q("SELECT c FROM t WHERE a = 1 AND b = 2"), [("y",)])


class DMLTests(Base):
    def setUp(self):
        super().setUp()
        self.db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, score REAL)")

    def test_insert_variants(self):
        cur = self.db.execute("INSERT INTO t (name, score) VALUES ('a', 1.5)")
        self.assertEqual(cur.lastrowid, 1)
        self.assertEqual(cur.rowcount, 1)
        cur = self.db.execute("INSERT INTO t VALUES (10, 'b', 2), (NULL, 'c', 3)")
        self.assertEqual(cur.rowcount, 2)
        self.assertEqual(self.q("SELECT id FROM t ORDER BY id"), [(1,), (10,), (11,)])
        self.db.execute("INSERT INTO t DEFAULT VALUES")
        self.assertEqual(self.q("SELECT id, name FROM t WHERE id = 12"), [(12, None)])
        self.db.execute("INSERT INTO t (name) SELECT name || '!' FROM t WHERE name IS NOT NULL")
        self.assertEqual(self.q("SELECT COUNT(*) FROM t"), [(7,)])

    def test_parameters(self):
        self.db.execute("INSERT INTO t (name, score) VALUES (?, ?)", ("x", 1))
        self.db.execute("INSERT INTO t (name, score) VALUES (:n, :s)", {"n": "y", "s": 2})
        self.db.execute("INSERT INTO t (name, score) VALUES (?2, ?1)", (3, "z"))
        self.assertEqual(self.q("SELECT name, score FROM t ORDER BY id"), [("x", 1.0), ("y", 2.0), ("z", 3.0)])
        with self.assertRaises(ProgrammingError):
            self.db.execute("INSERT INTO t (name) VALUES (?)")
        self.assertEqual(self.q("SELECT name FROM t WHERE score > ?", [1.5]), [("y",), ("z",)])

    def test_update_and_delete(self):
        self.db.executemany("INSERT INTO t (name, score) VALUES (?, ?)", [("a", 1), ("b", 2), ("c", 3)])
        self.assertEqual(self.db.execute("UPDATE t SET score = score * 10 WHERE score >= 2").rowcount, 2)
        self.assertEqual(self.q("SELECT score FROM t ORDER BY id"), [(1.0,), (20.0,), (30.0,)])
        self.db.execute("UPDATE t SET id = 99 WHERE name = 'a'")
        self.assertEqual(self.q("SELECT id FROM t WHERE name = 'a'"), [(99,)])
        with self.assertRaises(IntegrityError):
            self.db.execute("UPDATE t SET id = 2 WHERE id = 99")
        self.assertEqual(self.db.execute("DELETE FROM t WHERE score > 25").rowcount, 1)
        self.assertEqual(self.db.execute("DELETE FROM t").rowcount, 2)
        self.assertEqual(self.q("SELECT COUNT(*) FROM t"), [(0,)])
        self.db.execute("INSERT INTO t (name) VALUES ('new')")
        self.assertEqual(self.q("SELECT id FROM t"), [(1,)])

    def test_or_replace_and_ignore(self):
        self.db.execute("CREATE TABLE u (k TEXT UNIQUE, v)")
        self.db.execute("INSERT INTO u VALUES ('a', 1)")
        self.db.execute("INSERT OR IGNORE INTO u VALUES ('a', 2)")
        self.assertEqual(self.q("SELECT v FROM u"), [(1,)])
        self.db.execute("INSERT OR REPLACE INTO u VALUES ('a', 3)")
        self.assertEqual(self.q("SELECT v FROM u"), [(3,)])
        self.db.execute("REPLACE INTO u VALUES ('a', 4)")
        self.assertEqual(self.q("SELECT COUNT(*), MAX(v) FROM u"), [(1, 4)])

    def test_autoincrement_never_reuses_ids(self):
        self.db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY AUTOINCREMENT, x)")
        self.db.execute("INSERT INTO a (x) VALUES (1), (2), (3)")
        self.db.execute("DELETE FROM a WHERE id = 3")
        self.db.execute("INSERT INTO a (x) VALUES (4)")
        self.assertEqual(self.q("SELECT id FROM a ORDER BY id"), [(1,), (2,), (4,)])
        self.db.execute("CREATE TABLE b (id INTEGER PRIMARY KEY, x)")
        self.db.execute("INSERT INTO b (x) VALUES (1), (2), (3)")
        self.db.execute("DELETE FROM b WHERE id = 3")
        self.db.execute("INSERT INTO b (x) VALUES (4)")
        self.assertEqual(self.q("SELECT id FROM b ORDER BY id"), [(1,), (2,), (3,)])

    def test_type_affinity(self):
        self.db.execute("CREATE TABLE a (i INTEGER, r REAL, t TEXT, b BLOB, x)")
        self.db.execute("INSERT INTO a VALUES ('12', 3, 45, 'bytes', '7')")
        self.assertEqual(self.q("SELECT typeof(i), typeof(r), typeof(t), typeof(b), typeof(x) FROM a"),
                         [("integer", "real", "text", "text", "text")])
        self.assertEqual(self.q("SELECT i + 1, r, t FROM a"), [(13, 3.0, "45")])


class ConstraintTests(Base):
    def test_not_null_and_check(self):
        self.db.execute("CREATE TABLE t (a NOT NULL, b CHECK (b > 0), c, CHECK (c <> 'bad'))")
        with self.assertRaises(IntegrityError):
            self.db.execute("INSERT INTO t VALUES (NULL, 1, 'x')")
        with self.assertRaises(IntegrityError):
            self.db.execute("INSERT INTO t VALUES (1, 0, 'x')")
        with self.assertRaises(IntegrityError):
            self.db.execute("INSERT INTO t VALUES (1, 1, 'bad')")
        self.db.execute("INSERT INTO t VALUES (1, 1, 'ok')")
        with self.assertRaises(IntegrityError):
            self.db.execute("UPDATE t SET b = -1")
        self.assertEqual(self.q("SELECT b FROM t"), [(1,)])

    def test_unique_allows_multiple_nulls(self):
        self.db.execute("CREATE TABLE t (a UNIQUE)")
        self.db.execute("INSERT INTO t VALUES (NULL), (NULL), (1)")
        with self.assertRaises(IntegrityError):
            self.db.execute("INSERT INTO t VALUES (1)")
        self.assertEqual(self.q("SELECT COUNT(*) FROM t"), [(3,)])

    def test_failed_statement_keeps_earlier_index_updates(self):
        # Regression: undoing a failed statement must not drop changes made by
        # earlier statements of the same transaction to untouched index pages.
        self.db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a, b)")
        self.db.execute("CREATE INDEX ia ON t(a)")
        self.db.execute("CREATE INDEX ib ON t(b, a)")
        self.db.execute("INSERT INTO t VALUES (1, 'x', 'y')")
        self.db.execute("BEGIN")
        self.db.execute("INSERT INTO t (a, b) VALUES ('p', 'q')")
        with self.assertRaises(IntegrityError):
            self.db.execute("INSERT INTO t (id, a) VALUES (1, 'dup')")
        self.db.execute("INSERT INTO t (a, b) VALUES ('r', 's')")
        self.db.execute("COMMIT")
        self.assertEqual(self.db.integrity_check(), [])
        self.assertEqual(self.q("SELECT id FROM t WHERE a = 'p'"), [(2,)])
        self.assertEqual(self.q("SELECT COUNT(*) FROM t WHERE b >= 'q'"), [(3,)])

    def test_failed_statement_does_not_poison_transaction(self):
        self.db.execute("CREATE TABLE t (a UNIQUE)")
        self.db.execute("BEGIN")
        self.db.execute("INSERT INTO t VALUES (1)")
        with self.assertRaises(IntegrityError):
            self.db.execute("INSERT INTO t VALUES (2), (1)")  # second row fails: whole statement undone
        self.db.execute("INSERT INTO t VALUES (3)")
        self.db.execute("COMMIT")
        self.assertEqual(self.q("SELECT a FROM t ORDER BY a"), [(1,), (3,)])


class QueryTests(Base):
    def setUp(self):
        super().setUp()
        self.db.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, city TEXT, age INT);
            CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INT, amount REAL);
            CREATE INDEX io ON orders(user_id);
            INSERT INTO users VALUES (1, 'ada', 'london', 36), (2, 'linus', 'helsinki', 55),
                                     (3, 'grace', NULL, 85), (4, 'alan', 'london', 41);
            INSERT INTO orders (user_id, amount) VALUES (1, 10), (1, 20), (2, 5), (3, 100);
            """
        )

    def test_joins(self):
        self.assertEqual(
            self.q("SELECT u.name, SUM(o.amount) FROM users u JOIN orders o ON o.user_id = u.id GROUP BY u.id ORDER BY u.name"),
            [("ada", 30.0), ("grace", 100.0), ("linus", 5.0)],
        )
        self.assertEqual(
            self.q("SELECT u.name FROM users u LEFT JOIN orders o ON o.user_id = u.id WHERE o.id IS NULL"),
            [("alan",)],
        )
        self.assertEqual(self.q("SELECT COUNT(*) FROM users, orders"), [(16,)])
        plan = "\n".join(r[0] for r in self.q("EXPLAIN SELECT * FROM users u JOIN orders o ON o.user_id = u.id"))
        self.assertIn("USING INDEX io", plan)

    def test_subqueries(self):
        self.assertEqual(self.q("SELECT name FROM users WHERE id IN (SELECT user_id FROM orders WHERE amount >= 20) ORDER BY 1"),
                         [("ada",), ("grace",)])
        self.assertEqual(self.q("SELECT name, (SELECT COUNT(*) FROM orders o WHERE o.user_id = users.id) n FROM users ORDER BY n DESC, name LIMIT 2"),
                         [("ada", 2), ("grace", 1)])
        self.assertEqual(self.q("SELECT name FROM users u WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id)"), [("alan",)])
        self.assertEqual(self.q("SELECT MAX(n) FROM (SELECT user_id, COUNT(*) AS n FROM orders GROUP BY user_id)"), [(2,)])

    def test_aggregates_and_grouping(self):
        self.assertEqual(self.q("SELECT city, COUNT(*), AVG(age) FROM users GROUP BY city ORDER BY city"),
                         [(None, 1, 85.0), ("helsinki", 1, 55.0), ("london", 2, 38.5)])
        self.assertEqual(self.q("SELECT city, COUNT(*) AS n FROM users GROUP BY city HAVING n > 1"), [("london", 2)])
        self.assertEqual(self.q("SELECT COUNT(*), COUNT(city), COUNT(DISTINCT city), GROUP_CONCAT(name, '+') FROM users"),
                         [(4, 3, 2, "ada+linus+grace+alan")])
        self.assertEqual(self.q("SELECT SUM(amount) FROM orders WHERE user_id = 42"), [(None,)])
        self.assertEqual(self.q("SELECT COUNT(*) FROM orders WHERE user_id = 42"), [(0,)])

    def test_ordering_limits_distinct(self):
        self.assertEqual(self.q("SELECT name FROM users ORDER BY age DESC LIMIT 2"), [("grace",), ("linus",)])
        self.assertEqual(self.q("SELECT name FROM users ORDER BY city, name"), [("grace",), ("linus",), ("ada",), ("alan",)])
        self.assertEqual(self.q("SELECT name FROM users ORDER BY city NULLS LAST, name"), [("linus",), ("ada",), ("alan",), ("grace",)])
        self.assertEqual(self.q("SELECT DISTINCT city FROM users ORDER BY 1"), [(None,), ("helsinki",), ("london",)])
        self.assertEqual(self.q("SELECT id FROM users ORDER BY id LIMIT 2 OFFSET 1"), [(2,), (3,)])
        self.assertEqual(self.q("SELECT id FROM users LIMIT 1, 2"), [(2,), (3,)])

    def test_expressions(self):
        self.assertEqual(self.q("SELECT 1 + 2 * 3, 7 / 2, 7.0 / 2, 7 % 3, 1 / 0, 'a' || 'b', -(-1)"), [(7, 3, 3.5, 1, None, "ab", 1)])
        self.assertEqual(self.q("SELECT NULL = NULL, NULL IS NULL, 1 IS NOT NULL, 1 = 1.0, '1' = 1, 1 < 'a'"), [(None, 1, 1, 1, 0, 1)])
        self.assertEqual(self.q("SELECT CASE WHEN 1 > 2 THEN 'a' WHEN 2 > 1 THEN 'b' END, CASE 3 WHEN 3 THEN 'x' ELSE 'y' END"), [("b", "x")])
        self.assertEqual(self.q("SELECT 'abc' LIKE 'A%', 'abc' GLOB 'A*', 'a_c' LIKE 'a\\_c' ESCAPE '\\', 'abc' NOT LIKE '%z%'"), [(1, 0, 1, 1)])
        self.assertEqual(self.q("SELECT 5 BETWEEN 1 AND 5, 6 NOT BETWEEN 1 AND 5, 3 IN (1, 2, 3), 3 NOT IN (1, NULL)"), [(1, 1, 1, None)])
        self.assertEqual(self.q("SELECT CAST('42abc' AS INTEGER), CAST(3.9 AS INT), CAST(7 AS TEXT), typeof(CAST('x' AS BLOB))"), [(42, 3, "7", "blob")])
        self.assertEqual(self.q("SELECT COALESCE(NULL, NULL, 3), IFNULL(NULL, 'd'), NULLIF(1, 1), IIF(0, 'y', 'n')"), [(3, "d", None, "n")])
        self.assertEqual(self.q("SELECT UPPER('ab'), LENGTH('héllo'), SUBSTR('hello', 2, 3), SUBSTR('hello', -3), INSTR('hello', 'l'), REPLACE('aXa', 'X', '-'), TRIM('  x ')"),
                         [("AB", 5, "ell", "llo", 3, "a-a", "x")])
        self.assertEqual(self.q("SELECT ABS(-2), ROUND(2.567, 2), MIN(3, 1, 2), MAX('a', 'b'), HEX('A'), SIGN(-9)"), [(2, 2.57, 1, "b", "41", -1)])
        self.assertEqual(self.q("SELECT X'0102', 0x1F, 1e3, .5, 'it''s'"), [(b"\x01\x02", 31, 1000.0, 0.5, "it's")])
        self.assertEqual(self.q("SELECT LENGTH(DATE('now')), LENGTH(DATETIME('now')), DATE('2024-02-28', '+2 days'), STRFTIME('%Y', '2020-05-06')"),
                         [(10, 19, "2024-03-01", "2020")])

    def test_compound_selects(self):
        self.assertEqual(self.q("SELECT 1 UNION ALL SELECT 1 UNION SELECT 2 ORDER BY 1"), [(1,), (2,)])
        self.assertEqual(self.q("SELECT age FROM users INTERSECT SELECT 36"), [(36,)])
        self.assertEqual(self.q("SELECT name FROM users EXCEPT SELECT 'ada' ORDER BY name LIMIT 2"), [("alan",), ("grace",)])
        self.assertEqual(self.q("VALUES (1, 'a'), (2, 'b')"), [(1, "a"), (2, "b")])
        with self.assertRaises(ProgrammingError):
            self.q("SELECT 1 UNION SELECT 1, 2")

    def test_aliases_everywhere(self):
        self.assertEqual(self.q("SELECT age * 2 AS double FROM users WHERE double > 100"), [(110,), (170,)])
        self.assertEqual(self.q("SELECT city AS c, COUNT(*) AS n FROM users GROUP BY c HAVING n = 2"), [("london", 2)])
        self.assertEqual(self.q("SELECT u.name FROM users AS u WHERE u.id = 1"), [("ada",)])
        self.assertEqual(self.q("SELECT rowid, id FROM users WHERE rowid = 2"), [(2, 2)])
        self.assertEqual(self.q("SELECT users.*, 1 FROM users WHERE id = 1"), [(1, "ada", "london", 36, 1)])

    def test_index_usage_semantics(self):
        # Mixed int/real comparisons must still find rows through the index.
        self.db.execute("CREATE TABLE n (v INTEGER)")
        self.db.execute("CREATE INDEX iv ON n(v)")
        self.db.execute("INSERT INTO n VALUES (1), (2), (3), (10)")
        self.assertEqual(self.q("SELECT v FROM n WHERE v = 2.0"), [(2,)])
        self.assertEqual(self.q("SELECT v FROM n WHERE v > 1.5 AND v <= 3.0 ORDER BY v"), [(2,), (3,)])
        self.assertEqual(self.q("SELECT v FROM n WHERE v BETWEEN 2 AND 10 ORDER BY v DESC"), [(10,), (3,), (2,)])
        self.assertEqual(self.q("SELECT v FROM n WHERE v = '3'"), [(3,)])
        self.assertEqual(self.q("SELECT v FROM n WHERE v = NULL"), [])
        # The ORDER BY on an indexed column is served from the index (no sort).
        plan = "\n".join(r[0] for r in self.q("EXPLAIN SELECT v FROM n WHERE v > 1 ORDER BY v"))
        self.assertIn("satisfied by index", plan)

    def test_errors(self):
        with self.assertRaises(ProgrammingError):
            self.q("SELECT nope FROM users")
        with self.assertRaises(ProgrammingError):
            self.q("SELECT * FROM nope")
        with self.assertRaises(SQLError):
            self.q("SELEC 1")
        with self.assertRaises(SQLError):
            self.q("SELECT 'unterminated")
        with self.assertRaises(ProgrammingError):
            self.q("SELECT COUNT(*) FROM users WHERE COUNT(*) > 1")
        with self.assertRaises(ProgrammingError):
            self.q("SELECT name FROM users u, users v")  # ambiguous


class TransactionAndFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "db.qdb")

    def tearDown(self):
        self.dir.cleanup()

    def test_persistence_across_reopen(self):
        db = quarrydb.connect(self.path, fsync=False)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        db.executemany("INSERT INTO t (v) VALUES (?)", [("row %d" % i,) for i in range(5000)])
        db.execute("CREATE INDEX iv ON t(v)")
        db.close()
        db = quarrydb.connect(self.path, fsync=False)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM t").fetchone(), (5000,))
        self.assertEqual(db.execute("SELECT id FROM t WHERE v = 'row 4242'").fetchone(), (4243,))
        self.assertEqual(db.indexes(), ["iv"])
        db.close()

    def test_rollback_restores_schema_and_data(self):
        db = quarrydb.connect(self.path, fsync=False)
        db.execute("CREATE TABLE t (a)")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("BEGIN")
        db.execute("INSERT INTO t VALUES (2)")
        db.execute("CREATE TABLE u (b)")
        db.execute("DROP TABLE t")
        self.assertEqual(db.tables(), ["u"])
        db.execute("ROLLBACK")
        self.assertEqual(db.tables(), ["t"])
        self.assertEqual(db.execute("SELECT * FROM t").fetchall(), [(1,)])
        with db:
            db.execute("INSERT INTO t VALUES (3)")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM t").fetchone(), (2,))
        db.close()

    def test_crash_recovery_through_sql(self):
        db = quarrydb.connect(self.path, fsync=False)
        db.execute("CREATE TABLE t (a)")
        db.execute("INSERT INTO t VALUES (1)")

        def crash():
            raise RuntimeError("power loss")

        db.pager._crash_after_wal = crash
        with self.assertRaises(RuntimeError):
            db.execute("INSERT INTO t VALUES (2)")
        db.pager._fh.close()
        db.pager._wal.close()
        db.closed = True
        db2 = quarrydb.connect(self.path, fsync=False)
        self.assertEqual(db2.execute("SELECT a FROM t ORDER BY a").fetchall(), [(1,), (2,)])
        db2.close()

    def test_vacuum_shrinks_file(self):
        db = quarrydb.connect(self.path, fsync=False)
        db.execute("CREATE TABLE t (v)")
        db.executemany("INSERT INTO t VALUES (?)", [("x" * 500,) for _ in range(2000)])
        db.execute("DELETE FROM t WHERE rowid % 2 = 0")
        before = os.path.getsize(self.path)
        db.execute("VACUUM")
        after = os.path.getsize(self.path)
        self.assertLess(after, before)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM t").fetchone(), (1000,))
        db.close()

    def test_lock(self):
        db = quarrydb.connect(self.path)
        try:
            import fcntl  # noqa: F401
        except ImportError:
            self.skipTest("no fcntl")
        with self.assertRaises(quarrydb.LockError):
            quarrydb.connect(self.path)
        db.close()
        quarrydb.connect(self.path).close()

    def test_dump_roundtrip(self):
        db = quarrydb.connect(":memory:")
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT, b BLOB)")
        db.execute("INSERT INTO t VALUES (1, 'it''s', X'00ff'), (2, NULL, NULL)")
        db.execute("CREATE INDEX iv ON t(v)")
        script = "\n".join(db.iterdump())
        db2 = quarrydb.connect(":memory:")
        db2.executescript(script)
        self.assertEqual(db2.execute("SELECT * FROM t ORDER BY id").fetchall(), [(1, "it's", b"\x00\xff"), (2, None, None)])
        self.assertEqual(db2.indexes(), ["iv"])


if __name__ == "__main__":
    unittest.main()
