"""Interactive shell:  ``python -m quarrydb [database]`` or ``quarrydb [database]``."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
from typing import Any, List, Optional, Sequence

from . import __version__, connect
from .pager import DatabaseError
from .sql.lexer import is_complete, split_statements
from .sql.values import to_text

HELP = """\
.help              Show this message
.tables            List tables
.schema [TABLE]    Show CREATE statements
.indexes [TABLE]   List indexes
.mode MODE         Output mode: table (default), csv, json, line, list
.headers on|off    Show column headers in csv/list modes
.timer on|off      Print elapsed time per statement
.dump              Dump the database as SQL
.read FILE         Execute SQL from a file
.open FILE         Open another database
.stats             Page count, free list, transaction id
.check             Verify B+tree and index integrity
.quit / .exit      Leave the shell
Statements end with ';' and may span several lines.
"""


def format_value(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (bytes, bytearray)):
        return "X'" + bytes(v).hex().upper() + "'"
    if isinstance(v, float):
        return to_text(v)
    return str(v)


def render_table(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    cells = [[format_value(v) for v in r] for r in rows]
    widths = [len(c) for c in columns]
    for r in cells:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    numeric = [all(isinstance(r[i], (int, float)) or r[i] is None for r in rows) and bool(rows) for i in range(len(columns))]

    def fmt_row(vals: Sequence[str]) -> str:
        parts = []
        for i, v in enumerate(vals):
            parts.append(v.rjust(widths[i]) if numeric[i] else v.ljust(widths[i]))
        return "| " + " | ".join(parts) + " |"

    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    out = [sep, fmt_row(list(columns)), sep]
    out.extend(fmt_row(r) for r in cells)
    out.append(sep)
    return "\n".join(out)


class Shell:
    def __init__(self, path: str, out=None) -> None:
        self.path = path
        self.db = connect(path)
        self.out = out or sys.stdout
        self.mode = "table"
        self.headers = True
        self.timer = False

    # ------------------------------------------------------------------ #

    def write(self, s: str = "") -> None:
        self.out.write(s + "\n")

    def run_sql(self, sql: str) -> None:
        for stmt in split_statements(sql):
            t0 = time.perf_counter()
            try:
                cur = self.db.execute(stmt)
            except DatabaseError as e:
                self.write(f"Error: {e}")
                continue
            dt = time.perf_counter() - t0
            if cur.description:
                self.show(cur.columns, cur.fetchall())
            elif cur.rowcount >= 0:
                self.write(f"{cur.rowcount} row(s) affected")
            if self.timer:
                self.write(f"Run time: {dt * 1000:.2f} ms")

    def show(self, columns: List[str], rows: List[tuple]) -> None:
        if self.mode == "table":
            self.write(render_table(columns, rows))
            self.write(f"{len(rows)} row(s)")
        elif self.mode == "csv":
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            if self.headers:
                w.writerow(columns)
            for r in rows:
                w.writerow(["" if v is None else (bytes(v).hex() if isinstance(v, (bytes, bytearray)) else v) for v in r])
            self.out.write(buf.getvalue())
        elif self.mode == "json":
            def conv(v):
                return bytes(v).hex() if isinstance(v, (bytes, bytearray)) else v
            self.write(json.dumps([dict(zip(columns, map(conv, r))) for r in rows], ensure_ascii=False, indent=2))
        elif self.mode == "line":
            width = max((len(c) for c in columns), default=0)
            for r in rows:
                for c, v in zip(columns, r):
                    self.write(f"{c.rjust(width)} = {format_value(v)}")
                self.write()
        else:  # list
            if self.headers:
                self.write("|".join(columns))
            for r in rows:
                self.write("|".join(format_value(v) for v in r))

    # ------------------------------------------------------------------ #

    def dot_command(self, line: str) -> bool:
        """Handle a dot-command; return False to exit."""
        parts = line.strip().split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in (".quit", ".exit", ".q"):
            return False
        if cmd == ".help":
            self.out.write(HELP)
        elif cmd == ".tables":
            for t in self.db.tables():
                self.write(t)
        elif cmd == ".schema":
            for s in self.db.schema(arg or None):
                self.write(s + ";")
        elif cmd == ".indexes":
            for ix in self.db.indexes(arg or None):
                info = self.db.catalog.get_index(ix)
                self.write(f"{ix}  ON {info.table}({', '.join(info.columns)}){'  UNIQUE' if info.unique else ''}")
        elif cmd == ".mode":
            if arg.lower() in ("table", "csv", "json", "line", "list"):
                self.mode = arg.lower()
            else:
                self.write("modes: table, csv, json, line, list")
        elif cmd == ".headers":
            self.headers = arg.lower() in ("on", "1", "true", "yes")
        elif cmd == ".timer":
            self.timer = arg.lower() in ("on", "1", "true", "yes")
        elif cmd == ".dump":
            for s in self.db.iterdump():
                self.write(s)
        elif cmd == ".read":
            if not arg:
                self.write("usage: .read FILE")
            else:
                try:
                    with open(arg, "r", encoding="utf-8") as fh:
                        self.run_sql(fh.read())
                except OSError as e:
                    self.write(f"Error: {e}")
        elif cmd == ".open":
            if not arg:
                self.write("usage: .open FILE")
            else:
                self.db.close()
                self.path = arg
                self.db = connect(arg)
                self.write(f"opened {arg}")
        elif cmd == ".check":
            problems = self.db.integrity_check()
            self.write("ok" if not problems else "\n".join(problems))
        elif cmd == ".stats":
            p = self.db.pager
            self.write(f"file:        {self.path}")
            self.write(f"page size:   {p.page_size}")
            self.write(f"pages:       {p.page_count}")
            self.write(f"txn id:      {p.txn_id}")
            self.write(f"tables:      {len(self.db.catalog.tables)}")
            self.write(f"indexes:     {len(self.db.catalog.indexes)}")
        else:
            self.write(f"unknown command {cmd}; try .help")
        return True

    def repl(self, inp=None) -> None:
        inp = inp or sys.stdin
        interactive = inp is sys.stdin and sys.stdin.isatty()
        if interactive:
            self.write(f"quarrydb {__version__}  (type .help for help, .quit to exit)")
            self.write(f"connected to {self.path}")
        buf = ""
        while True:
            prompt = "quarry> " if not buf else "   ...> "
            try:
                line = input(prompt) if interactive else inp.readline()
            except EOFError:
                break
            if not interactive and line == "":
                break
            line = line.rstrip("\n")
            if not buf and line.strip().startswith("."):
                if not self.dot_command(line):
                    break
                continue
            if not line.strip() and not buf:
                continue
            buf += line + "\n"
            if is_complete(buf):
                self.run_sql(buf)
                buf = ""
        if buf.strip():
            self.run_sql(buf)
        self.db.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="quarrydb", description="quarrydb interactive shell")
    ap.add_argument("database", nargs="?", default=":memory:", help="database file (default: in-memory)")
    ap.add_argument("-c", "--command", action="append", help="run this SQL and exit (repeatable)")
    ap.add_argument("--mode", default="table", choices=["table", "csv", "json", "line", "list"])
    ap.add_argument("--version", action="version", version=f"quarrydb {__version__}")
    args = ap.parse_args(argv)
    try:
        shell = Shell(args.database)
    except DatabaseError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    shell.mode = args.mode
    if args.command:
        for c in args.command:
            shell.run_sql(c)
        shell.db.close()
        return 0
    try:
        shell.repl()
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
