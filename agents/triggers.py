"""
Cron parsing and the "when does this next fire" question.

Written rather than depended on: `croniter` is not installed, and the only
cron package that is (`python-crontab`) manages crontab *files* — it does not
answer this question. Five standard fields, the `*/n`, `a-b`, `a,b` and `a-b/n`
forms, and nothing else. Names for months and weekdays are accepted because
`0 9 * * MON` is what people write.

Everything here works in whatever timezone the caller's datetimes carry.
`Trigger.next_due_at` is stored in UTC like every other timestamp, which means
a `0 9 * * *` schedule fires at 09:00 UTC rather than 09:00 wherever its owner
is. Per-user schedule timezones are a real feature and a separate one; doing it
implicitly here would make the stored column mean different things per row.
"""
from __future__ import annotations

from datetime import datetime, timedelta

#: (name, low, high) per field, in cron order.
FIELDS = (
    ('minute', 0, 59),
    ('hour', 0, 23),
    ('day', 1, 31),
    ('month', 1, 12),
    # 7 is accepted alongside 0 for Sunday, as every cron implementation
    # people have used does. It is normalised to 0 at the end of `_parse_field`
    # — *after* ranges are expanded, never during parsing, or the common
    # "1-7" ("every day") becomes the range 1-0 and is rejected as backwards.
    ('weekday', 0, 7),
)

MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}
WEEKDAYS = {
    'sun': 0, 'mon': 1, 'tue': 2, 'wed': 3, 'thu': 4, 'fri': 5, 'sat': 6,
}

#: How far ahead to look before giving up. A schedule such as `0 0 30 2 *`
#: (30 February) never matches, and the search has to end rather than spin.
HORIZON = timedelta(days=366 * 4)


class CronError(ValueError):
    """The expression is not five parseable fields."""


def _alias(token: str, index: int) -> str:
    if index == 3:
        return str(MONTHS.get(token.lower(), token))
    if index == 4:
        return str(WEEKDAYS.get(token.lower(), token))
    return token


def _parse_field(raw: str, index: int) -> set[int]:
    name, low, high = FIELDS[index]
    values: set[int] = set()

    for part in raw.split(','):
        part = part.strip()
        if not part:
            raise CronError(f'Empty value in the {name} field.')

        step = 1
        if '/' in part:
            part, _, step_raw = part.partition('/')
            if not step_raw.isdigit() or int(step_raw) < 1:
                raise CronError(f'Bad step "{step_raw}" in the {name} field.')
            step = int(step_raw)

        if part in ('*', ''):
            start, end = low, high
        elif '-' in part.lstrip('-'):
            start_raw, _, end_raw = part.partition('-')
            start, end = _to_int(start_raw, index, name), _to_int(end_raw, index, name)
        else:
            start = end = _to_int(part, index, name)

        if start > end:
            raise CronError(f'Range {start}-{end} is backwards in the {name} field.')
        if start < low or end > high:
            raise CronError(
                f'{name} must be between {low} and {high}; got {start}-{end}.'
            )
        values.update(range(start, end + 1, step))

    if index == 4 and 7 in values:
        # Both 0 and 7 mean Sunday. Folded here, once the range has been
        # expanded, so `1-7` and `0-6` and `7` all end up as the same set.
        values.discard(7)
        values.add(0)

    if not values:
        raise CronError(f'The {name} field matches nothing.')
    return values


def _to_int(token: str, index: int, name: str) -> int:
    token = _alias(token.strip(), index)
    try:
        value = int(token)
    except ValueError:
        raise CronError(f'"{token}" is not a number in the {name} field.') from None
    return value


def parse_cron(expression: str) -> tuple[set[int], set[int], set[int], set[int], set[int], bool, bool]:
    """
    Five fields -> the values each matches, plus whether day/weekday were `*`.

    Those two flags are load-bearing: when both day-of-month and day-of-week
    are restricted, cron matches a day if *either* does — but if one is `*`,
    it is an AND. Losing that distinction turns `0 9 13 * FRI` from "Friday the
    13th, or any Friday" into something else entirely.
    """
    parts = (expression or '').split()
    if len(parts) != 5:
        raise CronError(
            f'Expected five cron fields, got {len(parts)}. For example "0 9 * * 1".'
        )

    minute, hour, day, month, weekday = (
        _parse_field(raw, i) for i, raw in enumerate(parts)
    )
    return (
        minute, hour, day, month, weekday,
        parts[2].strip() == '*', parts[4].strip() == '*',
    )


def is_valid(expression: str) -> bool:
    try:
        parse_cron(expression)
    except CronError:
        return False
    return True


def _day_matches(moment: datetime, days: set[int], weekdays: set[int],
                 day_any: bool, weekday_any: bool) -> bool:
    # Python's Monday=0 vs cron's Sunday=0.
    dow = (moment.weekday() + 1) % 7
    by_day = moment.day in days
    by_weekday = dow in weekdays

    if day_any and weekday_any:
        return True
    if day_any:
        return by_weekday
    if weekday_any:
        return by_day
    return by_day or by_weekday


def next_run_after(expression: str, after: datetime) -> datetime | None:
    """
    The first minute strictly after `after` that the expression matches.

    Returns None for a schedule that cannot occur (`0 0 30 2 *`) rather than
    raising, so one impossible row cannot stop a sweep that is walking every
    trigger in the table.

    Steps by the coarsest unit that can be ruled out — a whole month, then a
    day, then an hour — instead of minute by minute, which would be half a
    million iterations for a yearly schedule.
    """
    try:
        minute, hour, day, month, weekday, day_any, weekday_any = parse_cron(expression)
    except CronError:
        return None

    moment = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    limit = after + HORIZON

    while moment <= limit:
        if moment.month not in month:
            # First minute of the next month.
            moment = (
                moment.replace(year=moment.year + 1, month=1, day=1,
                               hour=0, minute=0)
                if moment.month == 12 else
                moment.replace(month=moment.month + 1, day=1, hour=0, minute=0)
            )
            continue

        if not _day_matches(moment, day, weekday, day_any, weekday_any):
            moment = (moment + timedelta(days=1)).replace(hour=0, minute=0)
            continue

        if moment.hour not in hour:
            moment = (moment + timedelta(hours=1)).replace(minute=0)
            continue

        if moment.minute not in minute:
            moment += timedelta(minutes=1)
            continue

        return moment

    return None
