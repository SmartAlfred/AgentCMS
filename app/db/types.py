"""Custom column types shared by the models.

``UTCDateTime`` exists because a naive ``TIMESTAMP WITHOUT TIME ZONE`` column
cannot faithfully round-trip an *aware* ``datetime``: psycopg serialises the
value with its offset and PostgreSQL casts it into the connection's ``TimeZone``
setting, so the stored wall clock (and therefore any later comparison, such as
"has this trust window expired?") silently depends on the server time zone.

``UTCDateTime`` normalises aware values to naive UTC on the way in and
re-attaches UTC on the way out, which makes the stored instant absolute and
independent of the server's ``TimeZone``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """A ``DateTime`` that always round-trips an absolute UTC instant."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
