"""Hand-written SQL tokenizer."""

from __future__ import annotations

from typing import List, NamedTuple, Optional

from .errors import SQLError

KEYWORDS = {
    "ALL", "AND", "AS", "ASC", "AUTOINCREMENT", "BEGIN", "BETWEEN", "BY", "CASE", "CHECK",
    "COMMIT", "CREATE", "CROSS", "DEFAULT", "DELETE", "DESC", "DISTINCT", "DROP", "ELSE",
    "END", "EXISTS", "EXPLAIN", "FALSE", "FROM", "GROUP", "HAVING", "IF", "IN", "INDEX",
    "INNER", "INSERT", "INTO", "IS", "JOIN", "KEY", "LEFT", "LIKE", "LIMIT", "NOT", "NULL",
    "OFFSET", "ON", "OR", "ORDER", "OUTER", "PRIMARY", "REPLACE", "ROLLBACK", "SELECT",
    "SET", "TABLE", "THEN", "TRANSACTION", "TRUE", "UNIQUE", "UPDATE", "VALUES", "VACUUM",
    "WHEN", "WHERE", "GLOB", "CAST", "OR", "ESCAPE", "VALUES", "USING", "NATURAL", "COLLATE",
    "RETURNING", "UNION", "INTERSECT", "EXCEPT", "CONSTRAINT",
}


class Token(NamedTuple):
    kind: str  # KEYWORD, IDENT, INT, REAL, STRING, BLOB, PARAM, OP, EOF
    value: object
    pos: int

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.kind}({self.value!r})"


_TWO_CHAR_OPS = {"<=", ">=", "<>", "!=", "||"}
_ONE_CHAR_OPS = set("(),;*+-/%<>=.")


def tokenize(sql: str) -> List[Token]:
    tokens: List[Token] = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
        elif c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
        elif c == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j < 0:
                raise SQLError("unterminated block comment")
            i = j + 2
        elif c.isalpha() or c == "_":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            word = sql[i:j]
            upper = word.upper()
            if upper in ("X", "x") and j < n and sql[j] == "'":
                # blob literal X'ABCD'
                k = sql.find("'", j + 1)
                if k < 0:
                    raise SQLError("unterminated blob literal")
                hexstr = sql[j + 1 : k]
                try:
                    tokens.append(Token("BLOB", bytes.fromhex(hexstr), i))
                except ValueError as e:
                    raise SQLError(f"invalid blob literal at {i}") from e
                i = k + 1
                continue
            if upper in KEYWORDS:
                tokens.append(Token("KEYWORD", upper, i))
            else:
                tokens.append(Token("IDENT", word, i))
            i = j
        elif c.isdigit() or (c == "." and i + 1 < n and sql[i + 1].isdigit()):
            j = i
            is_real = False
            if sql.startswith(("0x", "0X"), i):
                j = i + 2
                while j < n and sql[j] in "0123456789abcdefABCDEF":
                    j += 1
                tokens.append(Token("INT", int(sql[i + 2 : j], 16), i))
                i = j
                continue
            while j < n and sql[j].isdigit():
                j += 1
            if j < n and sql[j] == ".":
                is_real = True
                j += 1
                while j < n and sql[j].isdigit():
                    j += 1
            if j < n and sql[j] in "eE":
                k = j + 1
                if k < n and sql[k] in "+-":
                    k += 1
                if k < n and sql[k].isdigit():
                    is_real = True
                    j = k
                    while j < n and sql[j].isdigit():
                        j += 1
            text = sql[i:j]
            tokens.append(Token("REAL", float(text), i) if is_real else Token("INT", int(text), i))
            i = j
        elif c == "'":
            j = i + 1
            buf = []
            while True:
                if j >= n:
                    raise SQLError(f"unterminated string literal at {i}")
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(sql[j])
                j += 1
            tokens.append(Token("STRING", "".join(buf), i))
            i = j + 1
        elif c == '"' or c == "`" or c == "[":
            close = {'"': '"', "`": "`", "[": "]"}[c]
            j = sql.find(close, i + 1)
            if j < 0:
                raise SQLError(f"unterminated quoted identifier at {i}")
            tokens.append(Token("IDENT", sql[i + 1 : j], i))
            i = j + 1
        elif c == "?":
            j = i + 1
            while j < n and sql[j].isdigit():
                j += 1
            idx = int(sql[i + 1 : j]) if j > i + 1 else None
            tokens.append(Token("PARAM", idx, i))
            i = j
        elif c in ":@$" and i + 1 < n and (sql[i + 1].isalpha() or sql[i + 1] == "_"):
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            tokens.append(Token("PARAM", sql[i + 1 : j], i))
            i = j
        elif sql[i : i + 2] in _TWO_CHAR_OPS:
            tokens.append(Token("OP", sql[i : i + 2], i))
            i += 2
        elif c in _ONE_CHAR_OPS:
            tokens.append(Token("OP", c, i))
            i += 1
        else:
            raise SQLError(f"unexpected character {c!r} at position {i}")
    tokens.append(Token("EOF", None, n))
    return tokens


def split_statements(sql: str) -> List[str]:
    """Split a script on top-level semicolons (respecting strings/comments)."""
    out: List[str] = []
    start = 0
    for tok in tokenize(sql):
        if tok.kind == "OP" and tok.value == ";":
            stmt = sql[start : tok.pos].strip()
            if stmt:
                out.append(stmt)
            start = tok.pos + 1
    tail = sql[start:].strip()
    if tail:
        out.append(tail)
    return out


def is_complete(sql: str) -> Optional[bool]:
    """For the REPL: True if the text ends with a top-level ';', None if it cannot be tokenized yet."""
    try:
        toks = tokenize(sql)
    except SQLError:
        return None
    return len(toks) >= 2 and toks[-2].kind == "OP" and toks[-2].value == ";"
