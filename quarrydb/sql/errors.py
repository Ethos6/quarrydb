from ..pager import DatabaseError


class SQLError(DatabaseError):
    """Syntax error or unsupported SQL."""


class ProgrammingError(DatabaseError):
    """Semantic error: unknown table/column, wrong parameter count, ..."""


class IntegrityError(DatabaseError):
    """Constraint violation: NOT NULL, UNIQUE, PRIMARY KEY, CHECK."""
