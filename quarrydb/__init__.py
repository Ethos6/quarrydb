"""quarrydb - an embedded SQL database written from scratch in pure Python.

    >>> import quarrydb
    >>> db = quarrydb.connect(":memory:")
    >>> db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    >>> db.execute("INSERT INTO t (name) VALUES (?), (?)", ["ada", "linus"])
    >>> db.execute("SELECT * FROM t ORDER BY name").fetchall()
    [(1, 'ada'), (2, 'linus')]
"""

from .pager import CorruptionError, DatabaseError, LockError
from .sql.errors import IntegrityError, ProgrammingError, SQLError
from .connection import Connection, Cursor, connect

__version__ = "0.1.0"
__all__ = [
    "connect",
    "Connection",
    "Cursor",
    "DatabaseError",
    "CorruptionError",
    "LockError",
    "SQLError",
    "ProgrammingError",
    "IntegrityError",
    "__version__",
]
