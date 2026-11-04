"""Page file, free list and write-ahead log.

The database is a single file made of fixed-size pages (4 KiB by default).
Page 0 is the header; every other page belongs to a B+tree node, an overflow
chain, or the free list.

Durability model
----------------
All writes go to an in-memory dirty set until :meth:`Pager.commit`.  A commit:

1. appends every dirty page (plus the header) to the ``<file>-wal`` log as
   CRC-checked frames, followed by a commit frame, and fsyncs the log;
2. writes the pages into the main file and fsyncs it;
3. truncates the log (checkpoint).

If the process dies before step 1 finishes, the partial transaction has no
commit frame and is discarded on the next open.  If it dies during step 2, the
log is complete and is replayed on the next open.  Either way the main file is
always a consistent snapshot of some committed transaction.

A rollback simply drops the dirty set.
"""

from __future__ import annotations

import os
import struct
import zlib
from typing import Any, Callable, Dict, List, Optional

try:  # POSIX advisory locking
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore

MAGIC = b"QUARRYDB"
FORMAT_VERSION = 1
DEFAULT_PAGE_SIZE = 4096
MIN_PAGE_SIZE = 512
MAX_PAGE_SIZE = 65536

# magic, version, page_size, page_count, freelist_head, catalog_root, txn_id
_HEADER = struct.Struct(">8sHHIIIQ")
_FRAME = struct.Struct(">II")  # page id, crc32
COMMIT_FRAME_ID = 0xFFFFFFFF
WAL_MAGIC = b"QUARRYWAL"


class DatabaseError(Exception):
    """Base class for all quarrydb errors."""


class CorruptionError(DatabaseError):
    pass


class LockError(DatabaseError):
    pass


class Pager:
    def __init__(
        self,
        path: Optional[str],
        page_size: int = DEFAULT_PAGE_SIZE,
        fsync: bool = True,
        cache_pages: int = 4096,
    ) -> None:
        self.in_memory = path is None or path == ":memory:"
        self.path = None if self.in_memory else os.fspath(path)
        self.fsync_enabled = fsync and not self.in_memory
        self._cache_limit = cache_pages

        self._dirty: Dict[int, bytes] = {}
        self._cache: Dict[int, bytes] = {}
        self.obj_cache: Dict[int, Any] = {}  # parsed nodes, owned by btree.py
        self.dirty_nodes: Dict[int, Any] = {}  # modified nodes not yet serialised
        self._mem: Dict[int, bytes] = {}
        self._fh = None
        self._wal = None
        self._lock_fh = None
        self.closed = False
        self._journal: Optional[Dict[int, Optional[bytes]]] = None
        self._node_journal: Optional[Dict[int, Any]] = None
        self._journal_header = (0, 0, 0, 0)

        # Test hooks: called at specific points of commit() to simulate crashes.
        self._crash_after_wal: Optional[Callable[[], None]] = None
        self._crash_during_wal: Optional[Callable[[int], None]] = None

        if self.in_memory:
            self.page_size = page_size
            self.page_count = 1
            self.freelist_head = 0
            self.catalog_root = 0
            self.txn_id = 0
            self._mem[0] = self._pack_header()
        else:
            self._open_file(page_size)
        self._snapshot_header()

    # ------------------------------------------------------------------ #
    # File open / header                                                  #
    # ------------------------------------------------------------------ #

    def _open_file(self, page_size: int) -> None:
        assert self.path is not None
        exists = os.path.exists(self.path) and os.path.getsize(self.path) > 0
        self._fh = open(self.path, "r+b" if exists else "w+b")
        self._acquire_lock()
        wal_path = self.wal_path
        if exists:
            self._recover(wal_path)
            self._fh.seek(0)
            raw = self._fh.read(_HEADER.size)
            self._unpack_header(raw)
        else:
            if not MIN_PAGE_SIZE <= page_size <= MAX_PAGE_SIZE or page_size & (page_size - 1):
                raise ValueError("page_size must be a power of two between 512 and 65536")
            self.page_size = page_size
            self.page_count = 1
            self.freelist_head = 0
            self.catalog_root = 0
            self.txn_id = 0
            self._fh.write(self._pack_header())
            self._fh.flush()
            self._sync(self._fh)
            if os.path.exists(wal_path):
                os.remove(wal_path)
        self._wal = open(wal_path, "a+b")

    @property
    def wal_path(self) -> str:
        return (self.path or "") + "-wal"

    def _acquire_lock(self) -> None:
        if fcntl is None or self._fh is None:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:  # pragma: no cover - depends on another process
            raise LockError(f"database is locked by another process: {self.path}") from e

    def _pack_header(self) -> bytes:
        hdr = _HEADER.pack(
            MAGIC,
            FORMAT_VERSION,
            self.page_size,
            self.page_count,
            self.freelist_head,
            self.catalog_root,
            self.txn_id,
        )
        return hdr + b"\x00" * (self.page_size - len(hdr))

    def _unpack_header(self, raw: bytes) -> None:
        if len(raw) < _HEADER.size:
            raise CorruptionError("file too small to be a quarrydb database")
        magic, version, page_size, page_count, freelist_head, catalog_root, txn_id = _HEADER.unpack(
            raw[: _HEADER.size]
        )
        if magic != MAGIC:
            raise CorruptionError("not a quarrydb database (bad magic)")
        if version != FORMAT_VERSION:
            raise CorruptionError(f"unsupported file format version {version}")
        self.page_size = page_size
        self.page_count = page_count
        self.freelist_head = freelist_head
        self.catalog_root = catalog_root
        self.txn_id = txn_id

    def _snapshot_header(self) -> None:
        self._committed = (self.page_count, self.freelist_head, self.catalog_root, self.txn_id)

    def _sync(self, fh) -> None:
        if self.fsync_enabled:
            os.fsync(fh.fileno())

    # ------------------------------------------------------------------ #
    # Page access                                                         #
    # ------------------------------------------------------------------ #

    def read(self, pid: int) -> bytes:
        d = self._dirty.get(pid)
        if d is not None:
            return d
        c = self._cache.get(pid)
        if c is not None:
            return c
        if self.in_memory:
            data = self._mem.get(pid)
            if data is None:
                raise CorruptionError(f"read of unallocated page {pid}")
        else:
            assert self._fh is not None
            self._fh.seek(pid * self.page_size)
            data = self._fh.read(self.page_size)
            if len(data) < self.page_size:
                # A freshly allocated page past EOF that was never written.
                data = data + b"\x00" * (self.page_size - len(data))
        if len(self._cache) >= self._cache_limit:
            # Cheap eviction: drop an arbitrary quarter of the cache (never an unflushed node).
            dirty = self.dirty_nodes
            for k in list(self._cache)[: self._cache_limit // 4]:
                if k in dirty:
                    continue
                self._cache.pop(k, None)
                self.obj_cache.pop(k, None)
        self._cache[pid] = data
        return data

    def flush_nodes(self) -> None:
        """Serialise every modified B+tree node into the dirty page set."""
        if not self.dirty_nodes:
            return
        from .btree import serialize_node  # local import: btree depends on pager

        nodes = self.dirty_nodes
        self.dirty_nodes = {}
        for pid, node in nodes.items():
            self.write(pid, serialize_node(node, self.page_size))
            self.obj_cache[pid] = node

    def _journal_page(self, pid: int) -> None:
        j = self._journal
        if j is not None and pid not in j:
            j[pid] = self._dirty.get(pid)

    def write(self, pid: int, data: bytes) -> None:
        if len(data) != self.page_size:
            raise ValueError(f"page must be exactly {self.page_size} bytes, got {len(data)}")
        if pid <= 0 or pid >= self.page_count:
            raise CorruptionError(f"write to invalid page {pid}")
        self._journal_page(pid)
        self._dirty[pid] = bytes(data)
        self.obj_cache.pop(pid, None)

    def allocate(self) -> int:
        if self.freelist_head:
            pid = self.freelist_head
            page = self.read(pid)
            self.freelist_head = struct.unpack_from(">I", page, 0)[0]
        else:
            pid = self.page_count
            self.page_count += 1
        self._journal_page(pid)
        self._dirty[pid] = b"\x00" * self.page_size
        self.obj_cache.pop(pid, None)
        self.dirty_nodes.pop(pid, None)
        return pid

    def free(self, pid: int) -> None:
        if pid <= 0 or pid >= self.page_count:
            raise CorruptionError(f"free of invalid page {pid}")
        page = bytearray(self.page_size)
        struct.pack_into(">I", page, 0, self.freelist_head)
        self._journal_page(pid)
        self._dirty[pid] = bytes(page)
        self.obj_cache.pop(pid, None)
        self.dirty_nodes.pop(pid, None)
        self.freelist_head = pid

    # ------------------------------------------------------------------ #
    # Statement-level undo (atomic statements inside a transaction)       #
    # ------------------------------------------------------------------ #

    def begin_statement(self) -> None:
        self._journal = {}
        self._node_journal = {}
        self._journal_header = (self.page_count, self.freelist_head, self.catalog_root, self.txn_id)

    def end_statement(self) -> None:
        self._journal = None
        self._node_journal = None

    def undo_statement(self) -> None:
        """Restore the dirty set to what it was at begin_statement()."""
        j = self._journal
        if j is None:
            return
        for pid, prev in j.items():
            if prev is None:
                self._dirty.pop(pid, None)
            else:
                self._dirty[pid] = prev
        self.page_count, self.freelist_head, self.catalog_root, self.txn_id = self._journal_header
        self.obj_cache.clear()
        # Unflushed nodes: restore the pre-statement snapshot, or drop nodes
        # that were not dirty before the statement (their pages were restored above).
        nj = self._node_journal or {}
        restored = {}
        for pid, node in self.dirty_nodes.items():
            if pid in nj:
                if nj[pid] is not None:
                    restored[pid] = nj[pid]  # modified during the statement: restore snapshot
                # else: became dirty during the statement -> drop
            else:
                restored[pid] = node  # untouched by this statement: keep
        self.dirty_nodes = restored
        self._journal = None
        self._node_journal = None

    @property
    def dirty_count(self) -> int:
        return len(self._dirty) + len(self.dirty_nodes)

    # ------------------------------------------------------------------ #
    # Transactions                                                        #
    # ------------------------------------------------------------------ #

    def commit(self) -> None:
        if self.closed:
            raise DatabaseError("pager is closed")
        self.flush_nodes()
        if not self._dirty and self._committed == (
            self.page_count,
            self.freelist_head,
            self.catalog_root,
            self.txn_id,
        ):
            return
        self.txn_id += 1
        header = self._pack_header()
        frames = list(self._dirty.items())
        frames.append((0, header))

        if self.in_memory:
            for pid, data in frames:
                self._mem[pid] = data
        else:
            self._write_wal(frames)
            if self._crash_after_wal is not None:
                self._crash_after_wal()
            self._write_main(frames)
            self._checkpoint()

        for pid, data in frames:
            self._cache[pid] = data
        self._dirty.clear()
        self._snapshot_header()

    def _write_wal(self, frames) -> None:
        assert self._wal is not None
        self._wal.seek(0, os.SEEK_END)
        if self._wal.tell() == 0:
            self._wal.write(WAL_MAGIC)
        for i, (pid, data) in enumerate(frames):
            if self._crash_during_wal is not None:
                self._crash_during_wal(i)
            self._wal.write(_FRAME.pack(pid, zlib.crc32(data)))
            self._wal.write(data)
        commit_payload = struct.pack(">Q", self.txn_id) + b"\x00" * (self.page_size - 8)
        self._wal.write(_FRAME.pack(COMMIT_FRAME_ID, zlib.crc32(commit_payload)))
        self._wal.write(commit_payload)
        self._wal.flush()
        self._sync(self._wal)

    def _write_main(self, frames) -> None:
        assert self._fh is not None
        for pid, data in frames:
            self._fh.seek(pid * self.page_size)
            self._fh.write(data)
        self._fh.flush()
        self._sync(self._fh)

    def _checkpoint(self) -> None:
        assert self._wal is not None
        self._wal.truncate(0)
        self._wal.flush()
        self._sync(self._wal)

    def rollback(self) -> None:
        self._dirty.clear()
        self.obj_cache.clear()
        self.dirty_nodes.clear()
        self.page_count, self.freelist_head, self.catalog_root, self.txn_id = self._committed

    def _recover(self, wal_path: str) -> None:
        """Replay committed transactions from the WAL, then truncate it."""
        if not os.path.exists(wal_path) or os.path.getsize(wal_path) == 0:
            return
        with open(wal_path, "rb") as wal:
            blob = wal.read()
        if not blob.startswith(WAL_MAGIC):
            os.truncate(wal_path, 0)
            return
        # The page size is in the main file header, which is always valid.
        assert self._fh is not None
        self._fh.seek(0)
        self._unpack_header(self._fh.read(_HEADER.size))
        ps = self.page_size
        pos = len(WAL_MAGIC)
        pending: List[tuple] = []
        applied = 0
        while pos + _FRAME.size + ps <= len(blob):
            pid, crc = _FRAME.unpack_from(blob, pos)
            data = blob[pos + _FRAME.size : pos + _FRAME.size + ps]
            pos += _FRAME.size + ps
            if zlib.crc32(data) != crc:
                break  # torn write: everything from here on is garbage
            if pid == COMMIT_FRAME_ID:
                for p, d in pending:
                    self._fh.seek(p * ps)
                    self._fh.write(d)
                pending = []
                applied += 1
            else:
                pending.append((pid, data))
        if applied:
            self._fh.flush()
            self._sync(self._fh)
        os.truncate(wal_path, 0)

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        if self.closed:
            return
        self.rollback()
        self.closed = True
        if self._wal is not None:
            self._wal.close()
            self._wal = None
        if self._fh is not None:
            if fcntl is not None:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                except OSError:  # pragma: no cover
                    pass
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "Pager":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
