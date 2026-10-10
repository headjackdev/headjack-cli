"""Storage backends for the Headjack index."""

from headjack.db.types import IndexConnection, as_sqlite, is_postgres

__all__ = ["IndexConnection", "as_sqlite", "is_postgres"]
