# File format (version 1)

A database is one file of fixed-size pages plus, transiently, a `<file>-wal`
log. All integers are big-endian. Page ids are 0-based; page 0 is the header.

## Header page (page 0)

| Offset | Size | Field |
|---:|---:|---|
| 0 | 8 | magic `QUARRYDB` |
| 8 | 2 | format version (`1`) |
| 10 | 2 | page size (512–65536, power of two; default 4096) |
| 12 | 4 | page count (including the header page) |
| 16 | 4 | free-list head page id (0 = none) |
| 20 | 4 | catalog B+tree root page id |
| 24 | 8 | transaction counter |

The rest of the page is zero.

## Free-list pages

A freed page stores the id of the next free page in its first 4 bytes. The
header's free-list head points at the most recently freed page.
`Pager.allocate` pops from this list before growing the file.

## B+tree node pages

```
offset 0   u8   node type: 1 = leaf, 2 = internal
offset 1   u16  cell count
offset 3   u32  leaf: next leaf page id (0 = last)
                internal: rightmost child page id
offset 7        cells
```

Leaf cell: `[key length u16][value length u32][key bytes][value bytes]`.
If bit 31 of the value length is set the value lives in an overflow chain and
the inline payload is 8 bytes: `[first overflow page u32][total length u32]`.

Internal cell: `[key length u16][child page id u32][key bytes]`. `child_i`
holds keys `< key_i` (and `>= key_{i-1}`); the rightmost child in the header
holds keys `>= key_last`.

Keys are at most `page_size/4 - 64` bytes. Values larger than `page_size/4`
go to overflow pages. A node is kept at or above `page_size/4` bytes by
merging with a sibling on delete, except for the root.

## Overflow pages

```
offset 0   u32  next overflow page id (0 = last)
offset 4   u16  bytes used in this page
offset 6        data
```

## Row records (table B+tree values)

```
uvarint   number of values
per value:
  0x00                         NULL
  0x01 uvarint(zigzag(n))      INTEGER
  0x02 8 bytes IEEE-754        REAL
  0x03 uvarint(len) utf-8      TEXT
  0x04 uvarint(len) bytes      BLOB
```

Table B+tree keys are the 8-byte sortable rowid (`int64 + 2^63` as an
unsigned big-endian value).

## Index keys (index B+tree keys; values are empty)

`encode_key(column values) + sortable rowid`. Per value:

```
0x10                                   NULL
0x20 [sortable float 8][kind 1][sortable int 8]   number
0x30 escaped bytes 0x00 0x00           TEXT
0x40 escaped bytes 0x00 0x00           BLOB
```

* sortable float: IEEE-754 bits with the sign bit flipped for non-negatives
  and all bits flipped for negatives.
* kind: `0` for integers (the int part holds the exact value), `1` for reals
  (the int part is zero). So `1 < 1.0 < 2` and integers beyond 2^53 still
  order correctly.
* escaped bytes: every `0x00` becomes `0x00 0xFF`; the terminator `0x00 0x00`
  sorts before any continuation.

## Catalog B+tree

Keys: `T\0<table name lower>` and `I\0<index name lower>`. Values: UTF-8 JSON.

```json
{"name": "users", "root": 7, "rowid_column": 0, "seq": 0, "checks": [],
 "sql": "CREATE TABLE users (...)",
 "columns": [{"name": "id", "type": "INTEGER", "not_null": false, "primary_key": true,
              "unique": false, "default": null, "check": null, "autoincrement": false}, ...]}

{"name": "idx_email", "table": "users", "columns": ["email"], "unique": true,
 "root": 9, "auto": false, "sql": "CREATE UNIQUE INDEX ..."}
```

Automatic indexes created for `PRIMARY KEY` / `UNIQUE` constraints have
`"auto": true` and cannot be dropped directly.

## Write-ahead log (`<file>-wal`)

```
magic  QUARRYWAL
frames, each: [page id u32][crc32 u32][page bytes]
commit frame:  [0xFFFFFFFF u32][crc32 u32][txn id u64, zero padded to a page]
```

Many transactions may follow each other in the log, each terminated by a
commit frame. In normal operation the log is truncated to zero length right
after the main file is synced, so it is non-empty only between the two
syncs of a commit or after a crash. Replay applies every fully committed
transaction in order and stops at the first frame whose CRC does not match or
that is incomplete.
