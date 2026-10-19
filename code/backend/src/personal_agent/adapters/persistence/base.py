"""Declarative base, naming convention and the UTC timestamp column type."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import JSON, MetaData, String
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

from personal_agent.domain.common import iso_z, parse_utc

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UtcDateTime(TypeDecorator[datetime]):
    """Store UTC RFC3339 strings instead of SQLite's timezone-less datetime.

    SQLite silently drops offsets, which would make "UTC everywhere" a comment
    rather than a guarantee. Storing the canonical ``...Z`` string keeps the
    value exact, sortable and round-trip safe.
    """

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError("UtcDateTime only accepts datetime values")
        return iso_z(value)

    def process_result_value(self, value: Any, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        return parse_utc(str(value))


class JsonObject(TypeDecorator[dict[str, Any]]):
    """JSON object column; callers must validate against a schema version."""

    impl = JSON
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise TypeError("JsonObject only accepts dictionaries")
        return value

    def process_result_value(self, value: Any, dialect: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        return dict(value)


class JsonValue(TypeDecorator[Any]):
    """Arbitrary JSON value (object, array, scalar or null)."""

    impl = JSON
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        return value

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        return value


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[type[Any], Any]] = {datetime: UtcDateTime}
