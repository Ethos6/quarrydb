"""Binary encodings used on disk.

Two different encodings live here:

* **Record encoding** (`encode_record` / `decode_record`) packs a tuple of SQL
  values into a compact byte string.  It is used for table rows.  It is *not*
  order preserving; it is optimised for size and decode speed.

* **Key encoding** (`encode_key` / `decode_key`) packs a tuple of SQL values
  into a byte string whose *bytewise* order equals the SQL order of the tuple.
  It is used for index keys and row ids so the B+tree can compare keys with
  plain ``bytes`` comparison and still return rows in the right order.

SQL values are represented in Python as ``None`` (NULL), ``int``, ``float``,
``str`` and ``bytes``.  ``bool`` is treated as an integer.
"""

from __future__ import annotations

import struct
from typing import Any, List, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Varints                                                                     #
# --------------------------------------------------------------------------- #


def encode_uvarint(n: int) -> bytes:
    """LEB128 unsigned varint."""
    if n < 0:
        raise ValueError("uvarint must be non-negative")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def decode_uvarint(buf: bytes, pos: int) -> Tuple[int, int]:
    """Return (value, new_pos)."""
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def zigzag(n: int) -> int:
    """Map signed ints to unsigned so small magnitudes stay small (any size)."""
    return (n << 1) if n >= 0 else ((-n) << 1) - 1


def unzigzag(z: int) -> int:
    return (z >> 1) if not (z & 1) else -((z + 1) >> 1)


# --------------------------------------------------------------------------- #
# Record encoding (rows)                                                      #
# --------------------------------------------------------------------------- #

T_NULL = 0
T_INT = 1
T_REAL = 2
T_TEXT = 3
T_BLOB = 4

_F64 = struct.Struct(">d")


def encode_record(values: Sequence[Any]) -> bytes:
    out = bytearray(encode_uvarint(len(values)))
    for v in values:
        if v is None:
            out.append(T_NULL)
        elif isinstance(v, bool):
            out.append(T_INT)
            out += encode_uvarint(zigzag(int(v)))
        elif isinstance(v, int):
            out.append(T_INT)
            out += encode_uvarint(zigzag(v))
        elif isinstance(v, float):
            out.append(T_REAL)
            out += _F64.pack(v)
        elif isinstance(v, str):
            b = v.encode("utf-8")
            out.append(T_TEXT)
            out += encode_uvarint(len(b))
            out += b
        elif isinstance(v, (bytes, bytearray, memoryview)):
            b = bytes(v)
            out.append(T_BLOB)
            out += encode_uvarint(len(b))
            out += b
        else:
            raise TypeError(f"unsupported value type: {type(v).__name__}")
    return bytes(out)


def decode_record(buf: bytes) -> List[Any]:
    n, pos = decode_uvarint(buf, 0)
    values: List[Any] = []
    for _ in range(n):
        tag = buf[pos]
        pos += 1
        if tag == T_NULL:
            values.append(None)
        elif tag == T_INT:
            z, pos = decode_uvarint(buf, pos)
            values.append(unzigzag(z))
        elif tag == T_REAL:
            values.append(_F64.unpack_from(buf, pos)[0])
            pos += 8
        elif tag == T_TEXT:
            ln, pos = decode_uvarint(buf, pos)
            values.append(buf[pos : pos + ln].decode("utf-8"))
            pos += ln
        elif tag == T_BLOB:
            ln, pos = decode_uvarint(buf, pos)
            values.append(bytes(buf[pos : pos + ln]))
            pos += ln
        else:
            raise ValueError(f"corrupt record: unknown tag {tag}")
    return values


# --------------------------------------------------------------------------- #
# Order-preserving key encoding (indexes, row ids)                            #
# --------------------------------------------------------------------------- #
#
# Type order: NULL < INTEGER/REAL (numeric) < TEXT < BLOB.
# Within a type:
#   * integers: 8-byte big-endian two's complement with the sign bit flipped
#   * reals: IEEE-754 bits, with the sign bit flipped for positives and all
#     bits flipped for negatives (the classic sortable-double trick)
#   * text/blob: bytes with 0x00 escaped as 0x00 0xFF, terminated by 0x00 0x00
#     so that a prefix sorts before its extensions
#
# Integers and reals share the same tag so a mixed index still sorts
# numerically: every integer that fits in an int64 is encoded as a real *and*
# an int component (see _encode_number).

K_NULL = 0x10
K_NUM = 0x20
K_TEXT = 0x30
K_BLOB = 0x40

_I64 = struct.Struct(">q")
_U64 = struct.Struct(">Q")

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1


def _sortable_int(n: int) -> bytes:
    if not INT64_MIN <= n <= INT64_MAX:
        raise OverflowError("integer out of 64-bit range for key encoding")
    return _U64.pack((n + (1 << 63)) & 0xFFFFFFFFFFFFFFFF)


def _unsortable_int(b: bytes) -> int:
    return _U64.unpack(b)[0] - (1 << 63)


def _sortable_float(f: float) -> bytes:
    bits = _U64.unpack(_F64.pack(f))[0]
    if bits & (1 << 63):
        bits ^= 0xFFFFFFFFFFFFFFFF
    else:
        bits ^= 1 << 63
    return _U64.pack(bits)


def _unsortable_float(b: bytes) -> float:
    bits = _U64.unpack(b)[0]
    if bits & (1 << 63):
        bits ^= 1 << 63
    else:
        bits ^= 0xFFFFFFFFFFFFFFFF
    return _F64.unpack(_U64.pack(bits))[0]


def _encode_number(v: Any) -> bytes:
    """Numbers are encoded as: [float part 8 bytes][kind 1 byte][int part 8 bytes].

    The float part gives correct ordering between ints and reals.  For
    integers the int part makes values beyond float precision still distinct
    and correctly ordered.  kind=0 for integers, kind=1 for reals, so that an
    integer and a real with the same float value are still distinct keys
    (1 vs 1.0) while adjacent.
    """
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, int):
        return _sortable_float(float(v)) + b"\x00" + _sortable_int(v)
    return _sortable_float(v) + b"\x01" + _sortable_int(0)


def _escape_bytes(b: bytes) -> bytes:
    return b.replace(b"\x00", b"\x00\xff") + b"\x00\x00"


def encode_key(values: Sequence[Any]) -> bytes:
    out = bytearray()
    for v in values:
        if v is None:
            out.append(K_NULL)
        elif isinstance(v, (bool, int, float)):
            out.append(K_NUM)
            out += _encode_number(v)
        elif isinstance(v, str):
            out.append(K_TEXT)
            out += _escape_bytes(v.encode("utf-8"))
        elif isinstance(v, (bytes, bytearray, memoryview)):
            out.append(K_BLOB)
            out += _escape_bytes(bytes(v))
        else:
            raise TypeError(f"unsupported key type: {type(v).__name__}")
    return bytes(out)


def decode_key(buf: bytes) -> List[Any]:
    values: List[Any] = []
    pos = 0
    n = len(buf)
    while pos < n:
        tag = buf[pos]
        pos += 1
        if tag == K_NULL:
            values.append(None)
        elif tag == K_NUM:
            kind = buf[pos + 8]
            if kind == 0:
                values.append(_unsortable_int(buf[pos + 9 : pos + 17]))
            else:
                values.append(_unsortable_float(buf[pos : pos + 8]))
            pos += 17
        elif tag in (K_TEXT, K_BLOB):
            raw = bytearray()
            while True:
                b = buf[pos]
                pos += 1
                if b == 0:
                    nxt = buf[pos]
                    pos += 1
                    if nxt == 0:
                        break
                    raw.append(0)
                else:
                    raw.append(b)
            values.append(raw.decode("utf-8") if tag == K_TEXT else bytes(raw))
        else:
            raise ValueError(f"corrupt key: unknown tag {tag}")
    return values


def encode_rowid(rowid: int) -> bytes:
    """Row ids are stored as bare sortable int64 (8 bytes)."""
    return _sortable_int(rowid)


def decode_rowid(b: bytes) -> int:
    return _unsortable_int(b)


def key_successor(prefix: bytes) -> bytes:
    """Smallest byte string greater than every string starting with ``prefix``.

    Returns ``b''`` (meaning "no upper bound") when prefix is all 0xFF.
    """
    b = bytearray(prefix)
    while b and b[-1] == 0xFF:
        b.pop()
    if not b:
        return b""
    b[-1] += 1
    return bytes(b)
