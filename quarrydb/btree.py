"""Disk-backed B+tree with byte-string keys and values.

Every table and every index is one B+tree.  Keys are compared bytewise, so
callers must use the order-preserving encodings in :mod:`quarrydb.encoding`.

Node layout (all integers big-endian)::

    leaf:      [type=1 u8][ncells u16][next_leaf u32] cells...
               cell = [klen u16][vlen u32][key][value]
    internal:  [type=2 u8][ncells u16][rightmost_child u32] cells...
               cell = [klen u16][child u32][key]

In an internal node ``child_i`` holds keys ``< key_i`` (and ``>= key_{i-1}``);
``rightmost_child`` holds keys ``>= key_last``.

Values larger than a quarter of a page are stored in a chain of overflow
pages (``[next u32][len u16][data]``) and the leaf cell holds
``[first_page u32][total_len u32]`` with the high bit of ``vlen`` set.

Deletes merge underfull nodes with a sibling, so the tree stays compact under
churn.  Leaves are linked left-to-right for range scans.
"""

from __future__ import annotations

import struct
from bisect import bisect_left, bisect_right
from typing import Iterator, List, Optional, Tuple

from .pager import CorruptionError, Pager

LEAF = 1
INTERNAL = 2
_HDR = struct.Struct(">BHI")
_LEAF_CELL = struct.Struct(">HI")
_INT_CELL = struct.Struct(">HI")
_OVF_HDR = struct.Struct(">IH")
_OVF_REF = struct.Struct(">II")
OVERFLOW_FLAG = 0x80000000


class Node:
    __slots__ = ("pid", "type", "keys", "vals", "children", "next")

    def __init__(self, pid: int, type_: int) -> None:
        self.pid = pid
        self.type = type_
        self.keys: List[bytes] = []
        self.vals: List[bytes] = []  # leaf: raw cell value bytes (inline or overflow ref)
        self.children: List[int] = []  # internal: len(keys) + 1
        self.next = 0

    @property
    def is_leaf(self) -> bool:
        return self.type == LEAF

    def snapshot(self) -> "Node":
        n = Node(self.pid, self.type)
        n.keys = self.keys[:]
        n.vals = self.vals[:]
        n.children = self.children[:]
        n.next = self.next
        return n

    def size(self) -> int:
        if self.type == LEAF:
            return _HDR.size + 6 * len(self.keys) + sum(map(len, self.keys)) + sum(map(len, self.vals))
        return _HDR.size + 6 * len(self.keys) + sum(map(len, self.keys))


def _is_overflow(raw: bytes) -> bool:
    return len(raw) == 9 and raw[0] == 0xFF


class BTree:
    """A B+tree rooted at ``root`` inside ``pager``.

    Pass ``root=0`` to create a new empty tree; the new root page id is then
    available as ``tree.root`` and must be persisted by the caller (the catalog
    does that).
    """

    def __init__(self, pager: Pager, root: int = 0) -> None:
        self.pager = pager
        self.page_size = pager.page_size
        self.max_key = self.page_size // 4 - 64
        self.overflow_threshold = self.page_size // 4
        self.min_fill = self.page_size // 4
        if root == 0:
            node = Node(pager.allocate(), LEAF)
            self._store(node)
            root = node.pid
        self.root = root

    # ------------------------------------------------------------------ #
    # Node (de)serialisation                                              #
    # ------------------------------------------------------------------ #

    def _load(self, pid: int) -> Node:
        pager = self.pager
        node = pager.dirty_nodes.get(pid)
        if node is not None:
            j = pager._node_journal
            if j is not None and pid not in j:
                j[pid] = node.snapshot()  # statement-level undo for unflushed nodes
            return node
        node = self.pager.obj_cache.get(pid)
        if node is not None:
            return node
        raw = self.pager.read(pid)
        type_, n, link = _HDR.unpack_from(raw, 0)
        if type_ not in (LEAF, INTERNAL):
            raise CorruptionError(f"page {pid} is not a B+tree node (type {type_})")
        node = Node(pid, type_)
        pos = _HDR.size
        if type_ == LEAF:
            node.next = link
            for _ in range(n):
                klen, vlen = _LEAF_CELL.unpack_from(raw, pos)
                pos += _LEAF_CELL.size
                key = raw[pos : pos + klen]
                pos += klen
                if vlen & OVERFLOW_FLAG:
                    val = b"\xff" + raw[pos : pos + 8]
                    pos += 8
                else:
                    val = raw[pos : pos + vlen]
                    pos += vlen
                node.keys.append(key)
                node.vals.append(val)
        else:
            for _ in range(n):
                klen, child = _INT_CELL.unpack_from(raw, pos)
                pos += _INT_CELL.size
                node.keys.append(raw[pos : pos + klen])
                node.children.append(child)
                pos += klen
            node.children.append(link)
        self.pager.obj_cache[pid] = node
        return node

    def _store(self, node: Node) -> None:
        """Mark a node dirty.  Serialisation is deferred until the pager flushes
        (commit / statement boundary), so repeated edits to the same leaf cost
        one page write instead of one per edit."""
        pager = self.pager
        if node.pid not in pager.dirty_nodes:
            j = pager._node_journal
            if j is not None and node.pid not in j:
                j[node.pid] = None  # was clean before this statement: drop on undo
            pager.dirty_nodes[node.pid] = node
        pager.obj_cache[node.pid] = node

    # ------------------------------------------------------------------ #
    # Overflow values                                                     #
    # ------------------------------------------------------------------ #

    def _write_overflow(self, data: bytes) -> bytes:
        chunk = self.page_size - _OVF_HDR.size
        pages = [self.pager.allocate() for _ in range(max(1, (len(data) + chunk - 1) // chunk))]
        for i, pid in enumerate(pages):
            part = data[i * chunk : (i + 1) * chunk]
            page = bytearray(self.page_size)
            nxt = pages[i + 1] if i + 1 < len(pages) else 0
            _OVF_HDR.pack_into(page, 0, nxt, len(part))
            page[_OVF_HDR.size : _OVF_HDR.size + len(part)] = part
            self.pager.write(pid, bytes(page))
        return b"\xff" + _OVF_REF.pack(pages[0], len(data))

    def _read_overflow(self, ref: bytes) -> bytes:
        pid, total = _OVF_REF.unpack(ref[1:])
        out = bytearray()
        while pid:
            page = self.pager.read(pid)
            nxt, ln = _OVF_HDR.unpack_from(page, 0)
            out += page[_OVF_HDR.size : _OVF_HDR.size + ln]
            pid = nxt
        if len(out) != total:
            raise CorruptionError("overflow chain length mismatch")
        return bytes(out)

    def _free_overflow(self, ref: bytes) -> None:
        pid, _ = _OVF_REF.unpack(ref[1:])
        while pid:
            nxt = _OVF_HDR.unpack_from(self.pager.read(pid), 0)[0]
            self.pager.free(pid)
            pid = nxt

    def _unwrap(self, raw: bytes) -> bytes:
        return self._read_overflow(raw) if _is_overflow(raw) else raw

    def _wrap(self, value: bytes) -> bytes:
        if len(value) > self.overflow_threshold:
            return self._write_overflow(value)
        if len(value) == 9 and value[0] == 0xFF:
            # Ambiguous with an overflow reference; force it out of line.
            return self._write_overflow(value)
        return value

    def _release(self, raw: bytes) -> None:
        if _is_overflow(raw):
            self._free_overflow(raw)

    # ------------------------------------------------------------------ #
    # Lookup                                                              #
    # ------------------------------------------------------------------ #

    def _find_leaf(self, key: bytes) -> Tuple[Node, List[Tuple[Node, int]]]:
        """Descend to the leaf for ``key``; return it and the path of (node, child index)."""
        path: List[Tuple[Node, int]] = []
        node = self._load(self.root)
        while not node.is_leaf:
            i = bisect_right(node.keys, key)
            path.append((node, i))
            node = self._load(node.children[i])
        return node, path

    def get(self, key: bytes) -> Optional[bytes]:
        leaf, _ = self._find_leaf(key)
        i = bisect_left(leaf.keys, key)
        if i < len(leaf.keys) and leaf.keys[i] == key:
            return self._unwrap(leaf.vals[i])
        return None

    def contains(self, key: bytes) -> bool:
        leaf, _ = self._find_leaf(key)
        i = bisect_left(leaf.keys, key)
        return i < len(leaf.keys) and leaf.keys[i] == key

    def seek(self, key: bytes = b"") -> Iterator[Tuple[bytes, bytes]]:
        """Yield (key, value) for every entry with key >= ``key`` in order.

        The tree must not be modified while iterating.
        """
        leaf, _ = self._find_leaf(key)
        i = bisect_left(leaf.keys, key)
        while True:
            keys, vals = leaf.keys, leaf.vals
            while i < len(keys):
                yield keys[i], self._unwrap(vals[i])
                i += 1
            if not leaf.next:
                return
            leaf = self._load(leaf.next)
            i = 0

    def scan(self, lo: bytes = b"", hi: bytes = b"", include_hi: bool = False) -> Iterator[Tuple[bytes, bytes]]:
        """Yield entries with lo <= key < hi (or <= hi).  Empty ``hi`` means no upper bound."""
        for k, v in self.seek(lo):
            if hi:
                if include_hi:
                    if k > hi:
                        return
                elif k >= hi:
                    return
            yield k, v

    def prefix(self, prefix: bytes) -> Iterator[Tuple[bytes, bytes]]:
        for k, v in self.seek(prefix):
            if not k.startswith(prefix):
                return
            yield k, v

    def first(self) -> Optional[Tuple[bytes, bytes]]:
        for kv in self.seek(b""):
            return kv
        return None

    def last(self) -> Optional[Tuple[bytes, bytes]]:
        node = self._load(self.root)
        while not node.is_leaf:
            node = self._load(node.children[-1])
        if node.keys:
            return node.keys[-1], self._unwrap(node.vals[-1])
        # Rightmost leaf is empty: either the tree is empty or a degenerate
        # (unmergeable) empty leaf survived; fall back to a full scan.
        last = None
        for kv in self.seek(b""):
            last = kv
        return last

    def __len__(self) -> int:
        return sum(1 for _ in self.seek(b""))

    # ------------------------------------------------------------------ #
    # Insert                                                              #
    # ------------------------------------------------------------------ #

    def insert(self, key: bytes, value: bytes, replace: bool = True) -> bool:
        """Insert or replace.  Returns True if the key already existed."""
        if len(key) > self.max_key:
            raise ValueError(f"key too large ({len(key)} bytes, max {self.max_key})")
        leaf, path = self._find_leaf(key)
        i = bisect_left(leaf.keys, key)
        existed = i < len(leaf.keys) and leaf.keys[i] == key
        if existed:
            if not replace:
                return True
            self._release(leaf.vals[i])
            leaf.vals[i] = self._wrap(value)
        else:
            leaf.keys.insert(i, key)
            leaf.vals.insert(i, self._wrap(value))
        if leaf.size() <= self.page_size:
            self._store(leaf)
        else:
            self._split(leaf, path)
        return existed

    def _split(self, node: Node, path: List[Tuple[Node, int]]) -> None:
        """Split an overfull node and propagate upwards."""
        right = Node(self.pager.allocate(), node.type)
        if node.is_leaf:
            mid = self._split_point(node)
            right.keys, right.vals = node.keys[mid:], node.vals[mid:]
            node.keys, node.vals = node.keys[:mid], node.vals[:mid]
            right.next = node.next
            node.next = right.pid
            sep = right.keys[0]
        else:
            mid = len(node.keys) // 2
            sep = node.keys[mid]
            right.keys = node.keys[mid + 1 :]
            right.children = node.children[mid + 1 :]
            node.keys = node.keys[:mid]
            node.children = node.children[: mid + 1]
        self._store(node)
        self._store(right)

        if not path:
            new_root = Node(self.pager.allocate(), INTERNAL)
            new_root.keys = [sep]
            new_root.children = [node.pid, right.pid]
            self._store(new_root)
            self.root = new_root.pid
            return

        parent, idx = path[-1]
        parent.keys.insert(idx, sep)
        parent.children.insert(idx + 1, right.pid)
        if parent.size() <= self.page_size:
            self._store(parent)
        else:
            self._split(parent, path[:-1])

    def _split_point(self, leaf: Node) -> int:
        """Index at which to split a leaf so both halves are roughly equal in bytes."""
        total = leaf.size()
        acc = _HDR.size
        for i, (k, v) in enumerate(zip(leaf.keys, leaf.vals)):
            acc += 6 + len(k) + len(v)
            if acc >= total // 2:
                return max(1, min(i + 1, len(leaf.keys) - 1))
        return len(leaf.keys) // 2

    # ------------------------------------------------------------------ #
    # Delete                                                              #
    # ------------------------------------------------------------------ #

    def delete(self, key: bytes) -> bool:
        leaf, path = self._find_leaf(key)
        i = bisect_left(leaf.keys, key)
        if i >= len(leaf.keys) or leaf.keys[i] != key:
            return False
        self._release(leaf.vals[i])
        del leaf.keys[i]
        del leaf.vals[i]
        self._store(leaf)
        self._rebalance(leaf, path)
        return True

    def _rebalance(self, node: Node, path: List[Tuple[Node, int]]) -> None:
        if not path:
            # node is the root
            if not node.is_leaf and len(node.children) == 1:
                child = node.children[0]
                self.pager.free(node.pid)
                self.root = child
            return
        if node.size() >= self.min_fill:
            return
        parent, idx = path[-1]
        if len(parent.children) < 2:
            return  # degenerate parent with a single child: nothing to merge with
        # Prefer merging with the left sibling (keeps `next` pointers simple).
        if idx > 0:
            left = self._load(parent.children[idx - 1])
            merged = self._try_merge(left, node, parent, idx - 1)
        else:
            right = self._load(parent.children[idx + 1])
            merged = self._try_merge(node, right, parent, idx)
        if merged:
            self._rebalance(parent, path[:-1])

    def _try_merge(self, left: Node, right: Node, parent: Node, sep_idx: int) -> bool:
        """Merge ``right`` into ``left`` if the result fits in a page."""
        if left.is_leaf:
            combined = left.size() + right.size() - _HDR.size
        else:
            combined = left.size() + right.size() - _HDR.size + 6 + len(parent.keys[sep_idx])
        if combined > self.page_size:
            return False
        if left.is_leaf:
            left.keys += right.keys
            left.vals += right.vals
            left.next = right.next
        else:
            left.keys += [parent.keys[sep_idx]] + right.keys
            left.children += right.children
        del parent.keys[sep_idx]
        del parent.children[sep_idx + 1]
        self._store(left)
        self._store(parent)
        self.pager.free(right.pid)
        return True

    # ------------------------------------------------------------------ #
    # Bulk operations                                                     #
    # ------------------------------------------------------------------ #

    def drop(self) -> None:
        """Free every page of the tree (including overflow chains)."""
        self._drop_node(self.root)
        self.root = 0

    def _drop_node(self, pid: int) -> None:
        node = self._load(pid)
        if node.is_leaf:
            for v in node.vals:
                self._release(v)
        else:
            for c in node.children:
                self._drop_node(c)
        self.pager.free(pid)

    def clear(self) -> None:
        """Remove every entry but keep the tree (and its root page) alive."""
        self._drop_node(self.root)
        node = Node(self.pager.allocate(), LEAF)
        self._store(node)
        self.root = node.pid

    def depth(self) -> int:
        d = 1
        node = self._load(self.root)
        while not node.is_leaf:
            node = self._load(node.children[0])
            d += 1
        return d

    def check(self) -> None:
        """Verify structural invariants; raise CorruptionError if violated."""
        self._check_node(self.root, b"", b"")

    def _check_node(self, pid: int, lo: bytes, hi: bytes) -> None:
        node = self._load(pid)
        keys = node.keys
        for a, b in zip(keys, keys[1:]):
            if a >= b:
                raise CorruptionError(f"page {pid}: keys out of order")
        if keys and lo and keys[0] < lo:
            raise CorruptionError(f"page {pid}: key below lower bound")
        if keys and hi and keys[-1] >= hi:
            raise CorruptionError(f"page {pid}: key above upper bound")
        if node.size() > self.page_size:
            raise CorruptionError(f"page {pid}: node too large")
        if not node.is_leaf:
            if len(node.children) != len(keys) + 1:
                raise CorruptionError(f"page {pid}: child count mismatch")
            bounds = [lo] + keys + [hi]
            for i, c in enumerate(node.children):
                self._check_node(c, bounds[i], bounds[i + 1])


def serialize_node(node: Node, page_size: int) -> bytes:
    """Encode a node into exactly ``page_size`` bytes."""
    parts: List[bytes] = []
    if node.type == LEAF:
        parts.append(_HDR.pack(LEAF, len(node.keys), node.next))
        pack = _LEAF_CELL.pack
        for k, v in zip(node.keys, node.vals):
            if len(v) == 9 and v[0] == 0xFF:
                parts.append(pack(len(k), OVERFLOW_FLAG | 8))
                parts.append(k)
                parts.append(v[1:])
            else:
                parts.append(pack(len(k), len(v)))
                parts.append(k)
                parts.append(v)
    else:
        parts.append(_HDR.pack(INTERNAL, len(node.keys), node.children[-1]))
        pack = _INT_CELL.pack
        for k, c in zip(node.keys, node.children):
            parts.append(pack(len(k), c))
            parts.append(k)
    body = b"".join(parts)
    if len(body) > page_size:
        raise CorruptionError("node does not fit in a page")
    return body + b"\x00" * (page_size - len(body))
