"""The alpha filter DSL.

Alpha listing does not use ordinary query parameters. The comparison operator is part of
the parameter *name*, so a filter is one opaque token::

    ?is.sharpe>=1.58&status!=UNSUBMITTED&name~momentum&settings.region=USA

A normal dict-to-querystring helper produces ``is.sharpe=%3E%3D1.58`` instead, which the
server reads as an equality test against the literal string ``>=1.58`` and quietly returns
nothing. Hence a module of its own.

Three platform behaviours are mirrored deliberately, because not mirroring them changes
which alphas come back: ``hidden`` defaults to false, so omitting it silently excludes
hidden alphas; a comma in a value is the multi-value separator ``%1F``, so a literal comma
matches nothing; and the v4 list refuses a bare date, so a date becomes the start of its
day in ``America/New_York``.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .platform import PLATFORM_TIMEZONE

#: Where the platform's calendar lives, for both quota and date filters.
PLATFORM_TZ = ZoneInfo(PLATFORM_TIMEZONE)


def platform_midnight() -> datetime:
    """The start of today on the platform's clock, which the daily quota resets on."""
    return datetime.now(PLATFORM_TZ).replace(hour=0, minute=0, second=0, microsecond=0)


Operator = Literal["=", "!=", ">", ">=", "<", "<=", "!<", "~"]


@dataclass(frozen=True, slots=True)
class Filter:
    """One comparison. ``field`` and ``op`` are concatenated on the wire."""

    field: str
    op: Operator
    value: Any

    def token(self) -> str:
        return f"{quote(self.field, safe='.')}{self.op}{_render(self.value)}"


def _render(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return quote(moment.astimezone(PLATFORM_TZ).isoformat(), safe="")
    if isinstance(value, date):
        return value.isoformat()
    return quote(str(value), safe="").replace("%2C", "%1F")


@dataclass(slots=True)
class AlphaQuery:
    """A request for a page of alphas."""

    limit: int = 50
    offset: int = 0
    #: ``-dateCreated`` for newest first. The leading minus is the platform's own syntax.
    order: str = "-dateCreated"
    filters: list[Filter] | None = None

    #: Tri-state on purpose. ``False`` excludes hidden alphas, ``True`` returns only
    #: hidden ones, and ``None`` means "do not filter" — which the platform does *not*
    #: default to, so it is sent as both values.
    hidden: bool | None = False

    #: Convenience bounds, normalised to the platform's calendar.
    created_after: date | datetime | None = None
    created_before: date | datetime | None = None

    def path(self) -> str:
        """This query against your own alphas."""
        tokens = [f"limit={self.limit}", f"offset={self.offset}"]
        if self.order:
            tokens.append(f"order={quote(self.order, safe='-.')}")

        tokens.extend(entry.token() for entry in self.filters or [])

        if self.hidden is None:
            tokens.append("hidden=true%1Ffalse")
        else:
            tokens.append(f"hidden={'true' if self.hidden else 'false'}")

        if self.created_after is not None:
            tokens.append(
                Filter("dateCreated", ">=", _bound(self.created_after, next_day=False)).token()
            )
        if self.created_before is not None:
            tokens.append(
                Filter("dateCreated", "<", _bound(self.created_before, next_day=True)).token()
            )

        return f"/users/self/alphas?{'&'.join(tokens)}"


def _bound(value: date | datetime, *, next_day: bool) -> datetime:
    """A datetime as it is; a date as the start of that day, or the next, in Eastern time.

    ``next_day`` is for ``dateCreated<``: without it the named day itself would silently
    drop out of the range, which reads as missing data rather than an off-by-one.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    day = value + timedelta(days=1) if next_day else value
    return datetime(day.year, day.month, day.day, tzinfo=PLATFORM_TZ)
