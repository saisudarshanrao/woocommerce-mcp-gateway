from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from woo_mcp import dates

KOLKATA = ZoneInfo("Asia/Kolkata")
NEW_YORK = ZoneInfo("America/New_York")


def test_today_starts_at_local_midnight_and_has_no_end():
    now = datetime(2026, 10, 9, 10, 30, tzinfo=KOLKATA)

    window = dates.period_range("today", now)

    assert window.start == datetime(2026, 10, 9, 0, 0, tzinfo=KOLKATA)
    assert window.end is None
    # Midnight in Kolkata is 18:30 the previous day in UTC: the reason "today"
    # cannot be computed without knowing the store's timezone.
    assert dates.to_utc_param(window.start) == "2026-10-08T18:30:00"


def test_yesterday_is_one_full_local_day():
    window = dates.period_range("yesterday", datetime(2026, 10, 9, 0, 5, tzinfo=KOLKATA))

    assert window.start == datetime(2026, 10, 8, tzinfo=KOLKATA)
    assert window.end == datetime(2026, 10, 9, tzinfo=KOLKATA)


@pytest.mark.parametrize(("period", "days_back"), [("last_7_days", 6), ("last_30_days", 29)])
def test_rolling_periods_include_today(period, days_back):
    window = dates.period_range(period, datetime(2026, 10, 9, 23, 59, tzinfo=KOLKATA))

    assert window.start == datetime(2026, 10, 9, tzinfo=KOLKATA) - timedelta(days=days_back)
    assert window.end is None


def test_all_is_unbounded():
    assert dates.period_range("all", datetime(2026, 10, 9, tzinfo=KOLKATA)) == dates.DateRange(None, None)


def test_yesterday_is_23_hours_long_when_the_clocks_go_forward():
    # US clocks went forward on 8 March 2026, so that local day lasted 23 hours.
    window = dates.period_range("yesterday", datetime(2026, 3, 9, 12, 0, tzinfo=NEW_YORK))

    assert window.start == datetime(2026, 3, 8, tzinfo=NEW_YORK)
    assert window.end.astimezone(UTC) - window.start.astimezone(UTC) == timedelta(hours=23)


def test_named_timezone_wins_and_offset_is_the_fallback():
    assert dates.store_timezone("Asia/Kolkata", 0) == KOLKATA
    assert dates.store_timezone("", 5.5) == timezone(timedelta(hours=5, minutes=30))
    assert dates.store_timezone("Not/AZone", -8) == timezone(timedelta(hours=-8))
    assert dates.store_timezone(None, None) == timezone(timedelta(0))


def test_a_date_without_an_offset_is_read_as_store_time():
    assert dates.parse_datetime("2026-10-01", KOLKATA) == datetime(2026, 10, 1, tzinfo=KOLKATA)
    assert dates.parse_datetime(" 2026-10-01T09:00:00 ", KOLKATA) == datetime(2026, 10, 1, 9, tzinfo=KOLKATA)


def test_an_explicit_offset_is_kept():
    parsed = dates.parse_datetime("2026-10-01T09:00:00+00:00", KOLKATA)

    assert dates.to_utc_param(parsed) == "2026-10-01T09:00:00"


def test_invalid_dates_are_rejected():
    with pytest.raises(ValueError):
        dates.parse_datetime("next tuesday", KOLKATA)


def test_store_timestamps_are_shown_in_store_time_with_their_offset():
    assert dates.from_store_gmt("2026-10-08T17:18:01", KOLKATA) == "2026-10-08T22:48:01+05:30"
    assert dates.from_store_gmt(None, KOLKATA) is None
    assert dates.from_store_gmt("", KOLKATA) is None
