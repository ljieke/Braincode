from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class CronExpressionError(ValueError):
    pass


def _parse_field(text: str, minimum: int, maximum: int, *, sunday: bool = False) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise CronExpressionError("Cron fields must not contain empty values")
        base, slash, step_text = part.partition("/")
        step = 1
        if slash:
            try:
                step = int(step_text)
            except ValueError as exc:
                raise CronExpressionError(f"Invalid cron step: {step_text}") from exc
            if step <= 0:
                raise CronExpressionError("Cron step must be positive")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            left, right = base.split("-", 1)
            try:
                start, end = int(left), int(right)
            except ValueError as exc:
                raise CronExpressionError(f"Invalid cron range: {base}") from exc
            if start > end:
                raise CronExpressionError(f"Cron range starts after it ends: {base}")
        else:
            try:
                start = end = int(base)
            except ValueError as exc:
                raise CronExpressionError(f"Invalid cron value: {base}") from exc
        if start < minimum or start > maximum or end < minimum or end > maximum:
            raise CronExpressionError(
                f"Cron value {base} is outside {minimum}..{maximum}"
            )
        generated = range(start, end + 1, step)
        values.update(0 if sunday and value == 7 else value for value in generated)
    return values


@dataclass(frozen=True)
class CronExpression:
    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_wildcard: bool
    weekday_wildcard: bool

    @classmethod
    def parse(cls, expression: str) -> CronExpression:
        fields = expression.split()
        if len(fields) != 5:
            raise CronExpressionError("Cron expression must contain exactly 5 fields")
        minute, hour, day, month, weekday = fields
        return cls(
            expression=" ".join(fields),
            minutes=frozenset(_parse_field(minute, 0, 59)),
            hours=frozenset(_parse_field(hour, 0, 23)),
            days=frozenset(_parse_field(day, 1, 31)),
            months=frozenset(_parse_field(month, 1, 12)),
            weekdays=frozenset(_parse_field(weekday, 0, 7, sunday=True)),
            day_wildcard=day == "*",
            weekday_wildcard=weekday == "*",
        )

    def matches(self, value: datetime) -> bool:
        cron_weekday = (value.weekday() + 1) % 7
        day_match = value.day in self.days
        weekday_match = cron_weekday in self.weekdays
        if self.day_wildcard:
            calendar_match = weekday_match
        elif self.weekday_wildcard:
            calendar_match = day_match
        else:
            calendar_match = day_match or weekday_match
        return (
            value.minute in self.minutes
            and value.hour in self.hours
            and value.month in self.months
            and calendar_match
        )

    def next_after(self, value: datetime, timezone: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("value must be timezone-aware")
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise CronExpressionError(f"Unknown timezone: {timezone}") from exc
        candidate = value.astimezone(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = candidate + timedelta(days=366 * 5)
        while candidate <= limit:
            normalized = candidate.astimezone(UTC).astimezone(zone)
            same_wall_time = (
                normalized.year,
                normalized.month,
                normalized.day,
                normalized.hour,
                normalized.minute,
            ) == (
                candidate.year,
                candidate.month,
                candidate.day,
                candidate.hour,
                candidate.minute,
            )
            if same_wall_time and self.matches(normalized):
                return candidate.astimezone(UTC)
            candidate += timedelta(minutes=1)
        raise CronExpressionError("No matching cron time found within 5 years")


def next_cron_run(expression: str, timezone: str, after: datetime) -> datetime:
    return CronExpression.parse(expression).next_after(after, timezone)
