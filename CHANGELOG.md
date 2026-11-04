# Changelog

## 0.1.0

First release.

* Page file with header, free list, cache and advisory lock.
* Write-ahead log with CRC-checked frames, commit markers, crash recovery.
* B+tree with range scans, merges on delete and overflow pages.
* Order-preserving key encoding; compact row records.
* SQL: tables, indexes, constraints, INSERT/UPDATE/DELETE, SELECT with joins,
  grouping, ordering, subqueries, compound selects, EXPLAIN, VACUUM.
* Transactions with statement-level atomicity.
* Python API, interactive shell, integrity check, benchmark.
* Test suite including differential and randomized tests against SQLite.
