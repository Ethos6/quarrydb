"""Storage layer tests: encoding, pager, B+tree, write-ahead log, crash recovery."""

import os
import random
import tempfile
import unittest

from quarrydb.btree import BTree
from quarrydb.encoding import (
    decode_key,
    decode_record,
    decode_rowid,
    encode_key,
    encode_record,
    encode_rowid,
    key_successor,
)
from quarrydb.pager import CorruptionError, Pager


class EncodingTests(unittest.TestCase):
    def test_record_roundtrip(self):
        for vals in (
            [None, 1, -1, 2**40, -(2**70), 1.5, "héllo\x00x", b"\x00\xff"],
            [],
            [True, False],
            [0, 0.0, "", b""],
        ):
            expected = [int(v) if isinstance(v, bool) else v for v in vals]
            self.assertEqual(decode_record(encode_record(vals)), expected)

    def test_key_order_matches_sql_order(self):
        samples = [None, -10, -1, 0, 1, 2, 2.5, 3, 1e300, -1e300, 2**62, "", "a", "a\x00", "a\x00b", "ab", "b",
                   b"", b"\x00", b"a"]

        def sql_key(v):
            if v is None:
                return (0,)
            if isinstance(v, (int, float)):
                return (1, v)
            if isinstance(v, str):
                return (2, v.encode())
            return (3, v)

        by_bytes = sorted(samples, key=lambda v: encode_key([v]))
        self.assertEqual([sql_key(v) for v in by_bytes], sorted(sql_key(v) for v in samples))
        for v in samples:
            self.assertEqual(decode_key(encode_key([v])), [v])

    def test_int_and_float_distinct_but_adjacent(self):
        self.assertLess(encode_key([1]), encode_key([1.0]))
        self.assertLess(encode_key([1.0]), encode_key([2]))
        self.assertLess(encode_key([1.5]), encode_key([2]))

    def test_composite_keys(self):
        self.assertEqual(decode_key(encode_key([1, "x", None, 2.0, b"z"])), [1, "x", None, 2.0, b"z"])
        self.assertLess(encode_key(["a", 2]), encode_key(["a", 10]))
        self.assertLess(encode_key(["a", 10]), encode_key(["b", 1]))

    def test_rowid(self):
        for n in (0, 1, -1, 2**62, -(2**62)):
            self.assertEqual(decode_rowid(encode_rowid(n)), n)
        self.assertLess(encode_rowid(-1), encode_rowid(0))

    def test_successor(self):
        self.assertEqual(key_successor(b"ab"), b"ac")
        self.assertEqual(key_successor(b"a\xff"), b"b")
        self.assertEqual(key_successor(b"\xff"), b"")


class BTreeTests(unittest.TestCase):
    def setUp(self):
        self.pager = Pager(":memory:")
        self.tree = BTree(self.pager)

    def test_insert_get_delete(self):
        t = self.tree
        self.assertFalse(t.insert(b"k", b"v"))
        self.assertEqual(t.get(b"k"), b"v")
        self.assertTrue(t.insert(b"k", b"w"))
        self.assertEqual(t.get(b"k"), b"w")
        self.assertTrue(t.insert(b"k", b"x", replace=False))
        self.assertEqual(t.get(b"k"), b"w")
        self.assertTrue(t.delete(b"k"))
        self.assertFalse(t.delete(b"k"))
        self.assertIsNone(t.get(b"k"))

    def test_random_against_dict(self):
        rnd = random.Random(7)
        ref = {}
        t = self.tree
        for i in range(8000):
            k = encode_rowid(rnd.randrange(0, 5000))
            v = bytes(rnd.randrange(256) for _ in range(rnd.choice([0, 1, 30, 300, 2000, 6000])))
            if rnd.random() < 0.3 and ref:
                dk = rnd.choice(list(ref))
                self.assertTrue(t.delete(dk))
                del ref[dk]
            else:
                t.insert(k, v)
                ref[k] = v
        t.check()
        self.assertEqual(list(t.seek(b"")), sorted(ref.items()))
        self.assertGreater(t.depth(), 1)
        for k in rnd.sample(list(ref), 200):
            self.assertEqual(t.get(k), ref[k])
        self.assertEqual(t.last()[0], max(ref))
        self.assertEqual(t.first()[0], min(ref))
        for k in list(ref):
            self.assertTrue(t.delete(k))
        t.check()
        self.assertEqual(list(t.seek(b"")), [])
        self.assertEqual(t.depth(), 1)
        self.assertIsNone(t.last())
        # Freed pages are recycled: the tree should not keep growing.
        self.assertTrue(self.pager.freelist_head)

    def test_range_scan_and_prefix(self):
        t = self.tree
        for i in range(100):
            t.insert(b"k%03d" % i, b"%d" % i)
        self.assertEqual([k for k, _ in t.scan(b"k010", b"k013")], [b"k010", b"k011", b"k012"])
        self.assertEqual([k for k, _ in t.scan(b"k010", b"k013", include_hi=True)], [b"k010", b"k011", b"k012", b"k013"])
        self.assertEqual(len(list(t.prefix(b"k09"))), 10)
        self.assertEqual([k for k, _ in t.seek(b"k098")], [b"k098", b"k099"])

    def test_overflow_values(self):
        t = self.tree
        big = os.urandom(50_000)
        t.insert(b"big", big)
        t.insert(b"small", b"s")
        self.assertEqual(t.get(b"big"), big)
        pages_before = self.pager.page_count
        t.insert(b"big", b"tiny")  # frees the overflow chain
        self.assertEqual(t.get(b"big"), b"tiny")
        for _ in range(3):
            t.insert(b"big2", os.urandom(20_000))
        self.assertLess(self.pager.page_count, pages_before + 10)  # reuse via free list

    def test_key_too_large(self):
        with self.assertRaises(ValueError):
            self.tree.insert(b"x" * 5000, b"")

    def test_drop_frees_pages(self):
        t = self.tree
        for i in range(2000):
            t.insert(b"%05d" % i, b"x" * 100)
        t.drop()
        # Every page except the header should now be on the free list.
        free = 0
        pid = self.pager.freelist_head
        import struct
        while pid:
            free += 1
            pid = struct.unpack_from(">I", self.pager.read(pid), 0)[0]
        self.assertEqual(free, self.pager.page_count - 1)


class PagerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "t.qdb")

    def tearDown(self):
        self.dir.cleanup()

    def _open(self):
        return Pager(self.path, fsync=False)

    def test_persist_and_reopen(self):
        p = self._open()
        t = BTree(p)
        for i in range(500):
            t.insert(b"%d" % i, b"v%d" % i)
        p.catalog_root = t.root
        p.commit()
        p.close()
        p = self._open()
        t = BTree(p, p.catalog_root)
        self.assertEqual(t.get(b"42"), b"v42")
        self.assertEqual(len(t), 500)
        p.close()

    def test_rollback_discards_changes(self):
        p = self._open()
        t = BTree(p)
        t.insert(b"a", b"1")
        p.catalog_root = t.root
        p.commit()
        root = t.root
        t.insert(b"b", b"2")
        p.rollback()
        t = BTree(p, root)
        self.assertIsNone(t.get(b"b"))
        self.assertEqual(t.get(b"a"), b"1")
        p.close()

    def test_crash_after_wal_is_replayed(self):
        p = self._open()
        t = BTree(p)
        t.insert(b"k1", b"v1")
        p.catalog_root = t.root
        p.commit()

        class Boom(Exception):
            pass

        def boom():
            raise Boom

        p._crash_after_wal = boom
        t.insert(b"k2", b"v2")
        with self.assertRaises(Boom):
            p.commit()
        self.assertGreater(os.path.getsize(self.path + "-wal"), 0)
        p._fh.close()
        p._wal.close()  # simulate process death without cleanup
        p = self._open()
        t = BTree(p, p.catalog_root)
        self.assertEqual(t.get(b"k2"), b"v2")
        self.assertEqual(os.path.getsize(self.path + "-wal"), 0)
        p.close()

    def test_torn_wal_is_discarded(self):
        p = self._open()
        t = BTree(p)
        t.insert(b"k1", b"v1")
        p.catalog_root = t.root
        p.commit()

        class Boom(Exception):
            pass

        def torn(i):
            if i == 1:
                raise Boom

        p._crash_during_wal = torn
        for j in range(50):
            t.insert(b"z%d" % j, b"x" * 100)
        with self.assertRaises(Boom):
            p.commit()
        p._fh.close()
        p._wal.close()
        p = self._open()
        t = BTree(p, p.catalog_root)
        self.assertIsNone(t.get(b"z0"))
        self.assertEqual(t.get(b"k1"), b"v1")
        t.check()
        p.close()

    def test_corrupt_wal_frame_stops_replay(self):
        p = self._open()
        t = BTree(p)
        t.insert(b"k1", b"v1")
        p.catalog_root = t.root
        p.commit()
        p._crash_after_wal = lambda: (_ for _ in ()).throw(RuntimeError("crash"))
        t.insert(b"k2", b"v2")
        with self.assertRaises(RuntimeError):
            p.commit()
        p._fh.close()
        p._wal.close()
        # Flip a byte inside the first frame's payload.
        with open(self.path + "-wal", "r+b") as fh:
            fh.seek(len(b"QUARRYWAL") + 8 + 10)
            b = fh.read(1)
            fh.seek(-1, os.SEEK_CUR)
            fh.write(bytes([b[0] ^ 0xFF]))
        p = self._open()
        t = BTree(p, p.catalog_root)
        self.assertIsNone(t.get(b"k2"))
        self.assertEqual(t.get(b"k1"), b"v1")
        p.close()

    def test_bad_magic(self):
        with open(self.path, "wb") as fh:
            fh.write(b"not a database" * 100)
        with self.assertRaises(CorruptionError):
            Pager(self.path)

    def test_statement_journal(self):
        p = self._open()
        t = BTree(p)
        t.insert(b"a", b"1")
        p.catalog_root = t.root
        p.commit()
        root = t.root
        p.begin_statement()
        for i in range(300):
            t.insert(b"j%d" % i, b"x" * 200)
        p.undo_statement()
        t = BTree(p, root)
        self.assertEqual(list(t.seek(b"")), [(b"a", b"1")])
        self.assertEqual(p.dirty_count, 0)
        p.close()


if __name__ == "__main__":
    unittest.main()
