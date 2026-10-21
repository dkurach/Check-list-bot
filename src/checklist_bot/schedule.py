"""Расчёт, когда публиковать повторяющиеся задачи, и разбор ввода расписания."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

KIND_ONCE = "once"
KIND_TIMES = "times"
KIND_INTERVAL = "interval"
KIND_COUNT = "count"

WEEKDAY_NAMES = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
ALL_DAYS = "1234567"


class ScheduleError(ValueError):
    pass


def parse_hhmm(text: str) -> time:
    match = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", text)
    if not match:
        raise ScheduleError(f"Не понял время «{text.strip()}». Нужно в виде 09:30.")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ScheduleError(f"Нет такого времени: «{text.strip()}».")
    return time(hour, minute)


def fmt_hhmm(value: time) -> str:
    return value.strftime("%H:%M")


def parse_times(text: str) -> list[time]:
    """«09:00, 13:00 18:00» -> отсортированный список без повторов."""
    parts = [part for part in re.split(r"[,;\s]+", text) if part]
    if not parts:
        raise ScheduleError("Укажите хотя бы одно время, например 09:00, 13:00.")
    return sorted({parse_hhmm(part) for part in parts})


def parse_window(text: str) -> tuple[int, time, time]:
    """«120 10:00-20:00» -> (120, 10:00, 20:00). Первое число - интервал в минутах или количество раз.

    Конец раньше начала значит, что окно переходит через полночь («45 14:45-02:45»).
    """
    match = re.fullmatch(r"\s*(\d+)\s+(\d{1,2}[:.]\d{2})\s*[-–—]\s*(\d{1,2}[:.]\d{2})\s*", text)
    if not match:
        raise ScheduleError("Нужно в виде «120 10:00-20:00»: число, пробел, начало-конец.")
    number = int(match.group(1))
    start, end = parse_hhmm(match.group(2)), parse_hhmm(match.group(3))
    if number <= 0:
        raise ScheduleError("Число должно быть больше нуля.")
    if end == start:
        raise ScheduleError("Начало и конец окна совпадают.")
    return number, start, end


def parse_weekdays(text: str) -> str:
    digits = sorted(set(re.findall(r"[1-7]", text)))
    if not digits:
        raise ScheduleError("Укажите дни цифрами 1-7, где 1 - понедельник. Например: 1,3,5.")
    return "".join(digits)


def describe_weekdays(weekdays: str) -> str:
    if weekdays == ALL_DAYS:
        return "каждый день"
    if weekdays == "12345":
        return "по будням"
    if weekdays == "67":
        return "по выходным"
    return ", ".join(WEEKDAY_NAMES[int(d) - 1] for d in weekdays)


def business_day(moment: datetime, day_start: time) -> date:
    """Рабочие сутки, к которым относится момент: ночь до day_start - это ещё предыдущий день."""
    shift = timedelta(hours=day_start.hour, minutes=day_start.minute)
    return (moment - shift).date()


def _at(day: date, value: time, tz: ZoneInfo, day_start: time) -> datetime:
    """Время внутри рабочих суток day: часы до day_start попадают на следующую календарную дату."""
    moment = datetime.combine(day, value, tzinfo=tz)
    return moment + timedelta(days=1) if value < day_start else moment


def occurrences(tpl: Mapping, day: date, tz: ZoneInfo, day_start: time = time(0)) -> list[datetime]:
    """Все моменты публикации задачи в рабочие сутки day."""
    kind = tpl["kind"]
    if kind == KIND_ONCE:
        once_at = datetime.fromisoformat(tpl["once_at"])
        return [once_at] if business_day(once_at.astimezone(tz), day_start) == day else []

    if str(day.isoweekday()) not in tpl["weekdays"]:
        return []

    if kind == KIND_TIMES:
        return sorted(_at(day, t, tz, day_start) for t in parse_times(tpl["times"]))

    start = _at(day, parse_hhmm(tpl["start_time"]), tz, day_start)
    end = _at(day, parse_hhmm(tpl["end_time"]), tz, day_start)
    if end < start:  # окно через полночь
        end += timedelta(days=1)

    if kind == KIND_INTERVAL:
        step = timedelta(minutes=int(tpl["interval_min"]))
        result, current = [], start
        while current <= end:
            result.append(current)
            current += step
        return result

    if kind == KIND_COUNT:
        count = int(tpl["count"])
        if count == 1:
            return [start]
        step = (end - start) / (count - 1)
        # Округляем до минуты, чтобы время в чате было ровным.
        return [(start + step * i).replace(second=0, microsecond=0) for i in range(count)]

    raise ScheduleError(f"Неизвестный тип расписания: {kind}")


def describe(tpl: Mapping, tz: ZoneInfo) -> str:
    kind = tpl["kind"]
    if kind == KIND_ONCE:
        return "один раз, " + datetime.fromisoformat(tpl["once_at"]).astimezone(tz).strftime("%d.%m в %H:%M")
    days = describe_weekdays(tpl["weekdays"])
    if kind == KIND_TIMES:
        times = ", ".join(fmt_hhmm(t) for t in parse_times(tpl["times"]))
        return f"{days} в {times}"
    window = f"с {tpl['start_time']} до {tpl['end_time']}"
    if kind == KIND_INTERVAL:
        return f"{days}, каждые {tpl['interval_min']} мин {window}"
    if kind == KIND_COUNT:
        return f"{days}, {tpl['count']} раз(а) {window}"
    return kind
