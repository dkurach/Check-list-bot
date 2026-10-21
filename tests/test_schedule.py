from datetime import date, datetime, time

import pytest

from checklist_bot.schedule import (
    ScheduleError,
    business_day,
    describe,
    occurrences,
    parse_times,
    parse_weekdays,
    parse_window,
)

from .conftest import TZ

MONDAY = date(2026, 10, 19)
SATURDAY = date(2026, 10, 24)


def hhmm(values):
    return [v.strftime("%H:%M") for v in values]


def test_parse_times_sorts_and_dedupes():
    assert [t.strftime("%H:%M") for t in parse_times("18:00, 9.00 13:00;18:00")] == ["09:00", "13:00", "18:00"]


@pytest.mark.parametrize("bad", ["", "25:00", "9", "09:61", "утром"])
def test_parse_times_rejects_garbage(bad):
    with pytest.raises(ScheduleError):
        parse_times(bad)


def test_parse_window():
    number, start, end = parse_window("120 10:00-20:00")
    assert (number, start.hour, end.hour) == (120, 10, 20)
    number, start, end = parse_window("45 14:45-02:45")  # через полночь
    assert (start.hour, end.hour) == (14, 2)
    with pytest.raises(ScheduleError):
        parse_window("120 10:00-10:00")
    with pytest.raises(ScheduleError):
        parse_window("0 10:00-12:00")


def test_parse_weekdays():
    assert parse_weekdays("5, 1 3") == "135"
    with pytest.raises(ScheduleError):
        parse_weekdays("пн")


def test_times_respects_weekdays():
    tpl = {"kind": "times", "times": "09:00, 18:00", "weekdays": "12345"}
    assert hhmm(occurrences(tpl, MONDAY, TZ)) == ["09:00", "18:00"]
    assert occurrences(tpl, SATURDAY, TZ) == []


def test_interval_includes_window_end():
    tpl = {"kind": "interval", "start_time": "10:00", "end_time": "20:00", "interval_min": 120, "weekdays": "1234567"}
    assert hhmm(occurrences(tpl, MONDAY, TZ)) == ["10:00", "12:00", "14:00", "16:00", "18:00", "20:00"]


def test_count_spreads_evenly():
    tpl = {"kind": "count", "start_time": "09:00", "end_time": "21:00", "count": 4, "weekdays": "1234567"}
    assert hhmm(occurrences(tpl, MONDAY, TZ)) == ["09:00", "13:00", "17:00", "21:00"]
    tpl["count"] = 1
    assert hhmm(occurrences(tpl, MONDAY, TZ)) == ["09:00"]


def test_once_only_on_its_day():
    tpl = {"kind": "once", "once_at": datetime(2026, 10, 19, 15, 30, tzinfo=TZ).isoformat(), "weekdays": "1234567"}
    assert hhmm(occurrences(tpl, MONDAY, TZ)) == ["15:30"]
    assert occurrences(tpl, SATURDAY, TZ) == []


def test_describe():
    tpl = {"kind": "interval", "start_time": "10:00", "end_time": "20:00", "interval_min": 120, "weekdays": "12345"}
    assert describe(tpl, TZ) == "по будням, каждые 120 мин с 10:00 до 20:00"


def test_window_across_midnight():
    tpl = {"kind": "interval", "start_time": "14:45", "end_time": "02:45", "interval_min": 45, "weekdays": "1"}
    result = occurrences(tpl, MONDAY, TZ, day_start=time(5))
    assert result[0] == datetime(2026, 10, 19, 14, 45, tzinfo=TZ)
    assert result[-1] == datetime(2026, 10, 20, 2, 45, tzinfo=TZ)  # уже вторник по календарю
    assert len(result) == 17
    # Окно принадлежит понедельнику, во вторник по дням недели его нет.
    assert occurrences(tpl, date(2026, 10, 20), TZ, day_start=time(5)) == []


def test_after_midnight_times_belong_to_previous_day():
    tpl = {"kind": "times", "times": "01:00, 23:00", "weekdays": "1234567"}
    assert occurrences(tpl, MONDAY, TZ, day_start=time(5)) == [
        datetime(2026, 10, 19, 23, 0, tzinfo=TZ),
        datetime(2026, 10, 20, 1, 0, tzinfo=TZ),
    ]


def test_business_day():
    assert business_day(datetime(2026, 10, 20, 2, 59, tzinfo=TZ), time(5)) == MONDAY
    assert business_day(datetime(2026, 10, 20, 5, 0, tzinfo=TZ), time(5)) == date(2026, 10, 20)


def test_once_after_midnight_counts_for_previous_business_day():
    tpl = {"kind": "once", "once_at": datetime(2026, 10, 20, 1, 30, tzinfo=TZ).isoformat(), "weekdays": "1234567"}
    assert hhmm(occurrences(tpl, MONDAY, TZ, day_start=time(5))) == ["01:30"]
