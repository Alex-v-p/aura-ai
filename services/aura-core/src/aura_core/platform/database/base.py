"""Shared SQLAlchemy declarative base.

Domain-specific mappings live with the domain that owns their data.  The
generic database platform intentionally exposes only engine, session,
transaction, and base primitives.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
