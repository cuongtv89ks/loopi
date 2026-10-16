import datetime as dt
from zoneinfo import ZoneInfo

from loopi_brain.days import day_bounds_utc, hm, local_day, local_now

SEOUL = ZoneInfo("Asia/Seoul")


def test_a_seoul_day_starts_at_15_utc_the_day_before():
    assert day_bounds_utc(dt.date(2026, 10, 15), SEOUL) == (
        dt.datetime(2026, 10, 14, 15, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 10, 15, 15, 0, tzinfo=dt.UTC),
    )


def test_a_dst_day_is_25_hours_long():
    start, end = day_bounds_utc(dt.date(2026, 10, 25), ZoneInfo("Europe/Berlin"))  # clocks go back
    assert end - start == dt.timedelta(hours=25)


def test_local_day_and_now_follow_the_zone():
    late_utc = dt.datetime(2026, 10, 14, 23, 30, tzinfo=dt.UTC)  # 08:30 on Oct 15 in Seoul
    assert local_day(late_utc, SEOUL) == dt.date(2026, 10, 15)
    assert local_now(SEOUL, late_utc).hour == 8
    assert local_now(SEOUL).tzinfo == SEOUL


def test_hm_is_the_local_clock_time_zero_padded():
    assert hm(dt.datetime(2026, 10, 14, 23, 5, tzinfo=dt.UTC), SEOUL) == "08:05"
    assert hm(dt.datetime(2026, 10, 15, 6, 45, tzinfo=dt.UTC), ZoneInfo("Europe/Berlin")) == "08:45"
