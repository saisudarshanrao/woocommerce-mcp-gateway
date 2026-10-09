"""Turning "today" into an exact time range in the store's timezone.

A language model is poor at date arithmetic and does not know the store's
timezone, so the tools accept a named period and this module does the maths.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

Period = Literal["today", "yesterday", "last_7_days", "last_30_days", "all"]


@dataclass(frozen=True)
class DateRange:
    """Orders placed at or after `start` and before `end`. None means unbounded."""

    start: datetime | None
    end: datetime | None


def store_timezone(timezone_string: str | None, gmt_offset: float | None) -> tzinfo:
    """WordPress stores either a named zone or, failing that, a fixed UTC offset."""
    if timezone_string:
        try:
            return ZoneInfo(timezone_string)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return timezone(timedelta(hours=gmt_offset or 0))


def period_range(period: Period, now: datetime) -> DateRange:
    """The range a named period covers. `now` must be in the store's timezone."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    match period:
        case "today":
            return DateRange(midnight, None)
        case "yesterday":
            return DateRange(midnight - timedelta(days=1), midnight)
        case "last_7_days":
            # Today and the six days before it.
            return DateRange(midnight - timedelta(days=6), None)
        case "last_30_days":
            return DateRange(midnight - timedelta(days=29), None)
        case "all":
            return DateRange(None, None)


def parse_datetime(value: str, tz: tzinfo) -> datetime:
    """Parses an ISO 8601 date or date-time. Without an offset it is store time."""
    parsed = datetime.fromisoformat(value.strip())
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def to_utc_param(moment: datetime) -> str:
    """Formats a moment the way WooCommerce's `after`/`before` filters expect in UTC."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def from_store_gmt(value: str | None, tz: tzinfo) -> str | None:
    """Converts WooCommerce's "date_created_gmt" style field to store time with an offset."""
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=UTC).astimezone(tz).isoformat(timespec="seconds")
