"""
Cron parsing and the "when does this next fire" question.

Written rather than depended on: `croniter` is not installed, and the only
cron package that is (`python-crontab`) manages crontab *files* — it does not
answer this question. Five standard fields, the `*/n`, `a-b`, `a,b` and `a-b/n`
forms, and nothing else. Names for months and weekdays are accepted because
`0 9 * * MON` is what people write.

A schedule is evaluated in *its own* timezone and stored in UTC. `next_run_after`
takes an IANA zone name and walks the cron fields against local wall-clock time,
then converts the answer back to UTC for `Trigger.next_due_at` — so the column
still means one thing on every row, while "every day at 9" means nine in the
morning where the owner lives. Omit the zone and the old behaviour is kept
exactly: the walk happens in whatever timezone the caller's datetimes carry.

The two daylight-saving edge cases are decided here rather than left to chance.
A wall-clock time that does not exist (the spring-forward gap) is **skipped** —
the firing moves to the schedule's next match, because inventing an instant for
02:30 on a day with no 02:30 would fire at an hour the user never asked for. A
time that happens twice (the autumn fold) fires on the **first** occurrence, and
only once, because the second is filtered out by the strictly-increasing check
every caller already depends on.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def _walk(expression: str, start: datetime):
    """Yield every minute strictly after `start` that the expression matches.

    Naive about timezones on purpose — the caller decides whether these are
    UTC instants or local wall-clock times, which is the whole trick that makes
    per-schedule zones possible without a second cron implementation.

    Steps by the coarsest unit that can be ruled out — a whole month, then a
    day, then an hour — instead of minute by minute, which would be half a
    million iterations for a yearly schedule.
    """
    minute, hour, day, month, weekday, day_any, weekday_any = parse_cron(expression)

    moment = (start + timedelta(minutes=1)).replace(second=0, microsecond=0)
    limit = start + HORIZON

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

        yield moment
        moment += timedelta(minutes=1)


def zone_is_valid(name: str) -> bool:
    """Whether this is an IANA zone the running system actually knows."""
    if not name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False
    return True


def _zone(tz: str | None) -> ZoneInfo | None:
    """The zone to walk in, or None to keep the caller's own timezone.

    An unknown zone name resolves to None rather than raising: a stored row
    naming a zone this machine's tzdata has since dropped must still schedule
    *something*, and falling back to UTC is a visible hour offset rather than a
    trigger that stops firing with no log line.
    """
    if not tz or tz.upper() == 'UTC':
        return None
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None


def _localise(naive: datetime, zone: ZoneInfo) -> datetime | None:
    """A local wall-clock time as a UTC instant, or None if it never happens.

    `fold=0` picks the *first* of a repeated hour. The nonexistent hour is
    detected by round-tripping: `replace(tzinfo=...)` on a time inside the
    spring-forward gap happily produces an instant, but converting it back
    yields a different wall clock — which is the definition of "this time is
    not on that day's clock".
    """
    aware = naive.replace(tzinfo=zone, fold=0)
    as_utc = aware.astimezone(dt_timezone.utc)
    if as_utc.astimezone(zone).replace(tzinfo=None) != naive:
        return None
    return as_utc


def next_runs(expression: str, after: datetime, tz: str | None = None,
              count: int = 5) -> list[datetime]:
    """
    The next `count` firings, as instants.

    This is what the schedule editor previews. A schedule whose consequences
    the user cannot see is one they have to deploy in order to test, and the
    cost of guessing wrong is a run they are billed for at 3am.
    """
    # Parsed here, not inside the loop: `_walk` is a generator, so wrapping the
    # call site in `try` catches nothing — the CronError surfaces at the first
    # `next()`, which is inside the caller's `for`. That turned "return [] for a
    # malformed expression" into an exception escaping into the sweep.
    try:
        parse_cron(expression)
    except CronError:
        return []

    zone = _zone(tz)
    walker = _walk(expression, after if zone is None
                   else after.astimezone(zone).replace(tzinfo=None))

    out: list[datetime] = []
    for naive in walker:
        if zone is None:
            moment = naive
        else:
            moment = _localise(naive, zone)
            if moment is None or moment <= after:
                # Either the spring-forward gap, or the second half of a
                # repeated hour, whose instant is not later than the last one.
                continue
            if out and moment <= out[-1]:
                continue
        out.append(moment)
        if len(out) >= count:
            break
    return out


def next_run_after(expression: str, after: datetime,
                   tz: str | None = None) -> datetime | None:
    """
    The first minute strictly after `after` that the expression matches, read
    in the schedule's own timezone and returned as a UTC instant.

    Returns None for a schedule that cannot occur (`0 0 30 2 *`) rather than
    raising, so one impossible row cannot stop a sweep that is walking every
    trigger in the table. Callers are expected to notice the None and say so —
    `agents/sweep.py` disables the trigger and records why, because a row whose
    `next_due_at` is quietly NULL for ever is indistinguishable from one that
    is working.
    """
    runs = next_runs(expression, after, tz, count=1)
    return runs[0] if runs else None


# ---------------------------------------------------------------- description

_DAY_NAMES = ('Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday',
              'Friday', 'Saturday')
_MONTH_NAMES = ('', 'January', 'February', 'March', 'April', 'May', 'June',
                'July', 'August', 'September', 'October', 'November', 'December')

#: How many distinct clock times are worth spelling out before a list stops
#: being a reading and starts being a table. Past this the phrase says how the
#: schedule *works* ("every 30 minutes between 09:00 and 17:00") rather than
#: enumerating what it produces.
MAX_LISTED_TIMES = 6


def _join(words: list[str]) -> str:
    if not words:
        return ''
    if len(words) == 1:
        return words[0]
    return f"{', '.join(words[:-1])} and {words[-1]}"


def _ordinal(n: int) -> str:
    suffix = 'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f'{n}{suffix}'


def _weekday_order(day: int) -> int:
    """Monday first. Cron numbers weeks from Sunday, but "Saturday and Sunday"
    is how people say the weekend — sorting by the raw cron number renders it
    "Sunday and Saturday", which reads like a mistake."""
    return (day + 6) % 7


def _step_of(token: str) -> int | None:
    """The `n` in `*/n`, or None. Read from the raw token rather than inferred
    from the expanded set, because `*/2` and an explicit list of the same 30
    values mean the same firings but not the same *intent*, and the intent is
    what the reading is for."""
    if token.startswith('*/') and token[2:].isdigit():
        n = int(token[2:])
        return n if n > 1 else None
    return None


def _hours_clause(hour: set[int]) -> str:
    """Which part of the day a sub-hourly schedule runs in.

    Two forms, because one does not cover both cases: a contiguous run reads as
    a span ("between 09:00 and 17:00"), while a scattered set has to name the
    hours ("during the 09:00 and 17:00 hours"). Writing the scattered case as a
    span would claim the schedule fires all afternoon; writing it as "at 09:00
    and 17:00" would claim it fires twice.
    """
    lo, hi = min(hour), max(hour)
    if len(hour) == 1:
        return f'between {lo:02d}:00 and {lo:02d}:59'
    if len(hour) == hi - lo + 1:
        return f'between {lo:02d}:00 and {hi:02d}:00'
    return ('during the ' + _join([f'{h:02d}:00' for h in sorted(hour)])
            + ' hours')


def _time_phrase(minute: set[int], hour: set[int],
                 minute_tok: str, hour_tok: str) -> tuple[str, bool]:
    """
    When in the day this fires, and whether that phrase is an *interval*.

    The flag matters for composition: "Every 15 minutes" already says it
    happens all day, so appending "every day" is noise, while "at 09:00" says
    nothing about which days and needs one.

    Every branch here has to agree word for word with `lib/cron.ts`, because
    the client renders its own reading while typing and this one replaces it
    when the preview lands. Two readings of the same schedule, 350ms apart, is
    worse than one imperfect reading.
    """
    every_minute = len(minute) == 60
    every_hour = len(hour) == 24
    minute_step = _step_of(minute_tok)
    hour_step = _step_of(hour_tok)
    one_minute = len(minute) == 1
    m = min(minute)

    if every_minute and every_hour:
        return 'Every minute', True
    if every_hour and minute_step:
        return f'Every {minute_step} minutes', True
    if every_minute:
        # The whole of one or more hours. "Every minute of 09:00" reads as a
        # single instant, which is the opposite of what it means.
        return f'Every minute {_hours_clause(hour)}', True
    if every_hour and len(minute) <= MAX_LISTED_TIMES:
        # Unrestricted hours: naming them adds "between 00:00 and 23:00", which
        # is every hour there is.
        return 'Every hour at ' + _join([f':{mm:02d}' for mm in sorted(minute)]), True
    if hour_step and one_minute:
        # The shape the "every few hours" picker produces. Reading it back as
        # the six clock times it expands to is technically true and answers a
        # question the user did not ask.
        return f'Every {hour_step} hours, at :{m:02d}', True
    if minute_step:
        # A minute step inside a restricted set of hours: "every 30 minutes
        # between 09:00 and 17:00".
        return f'Every {minute_step} minutes {_hours_clause(hour)}', True

    times = [f'{h:02d}:{mm:02d}' for h in sorted(hour) for mm in sorted(minute)]
    if len(times) > MAX_LISTED_TIMES:
        # Never "at 18 times a day" — a count says how often it lands and
        # nothing about when, which is the only thing the reader is checking.
        # Say how the schedule is *shaped* instead.
        if hour_step:
            return (f'Every {hour_step} hours, at '
                    + _join([f':{mm:02d}' for mm in sorted(minute)]), True)
        past = _join([f':{mm:02d}' for mm in sorted(minute)])
        if every_hour:
            return f'Every hour at {past}', True
        return f'at {past} past the hour, {_hours_clause(hour)}', False
    return 'at ' + _join(times), False


def _day_phrase(day: set[int], weekday: set[int], month: set[int],
                day_any: bool, weekday_any: bool,
                month_any: bool) -> tuple[str, bool, bool]:
    """
    Which days this fires on: the phrase, whether the month is already folded
    into it, and whether the time must be stated *first* to stay unambiguous.
    """
    if day_any and weekday_any:
        return 'every day', False, False

    if day_any:
        if weekday == {1, 2, 3, 4, 5}:
            return 'every weekday', False, False
        names = [_DAY_NAMES[d] for d in sorted(weekday, key=_weekday_order)]
        return 'every ' + _join(names), False, False

    if weekday_any:
        days = _join([_ordinal(d) for d in sorted(day)])
        if not month_any and len(day) == 1 and len(month) == 1:
            # "On 25 December" beats "on the 25th … in December".
            return f'on {min(day)} {_MONTH_NAMES[min(month)]}', True, False
        if month_any:
            return f'on the {days} of every month', False, False
        return f'on the {days} of ' + _join([_MONTH_NAMES[m] for m in sorted(month)]), True, False

    # Both restricted: cron fires if *either* matches. Stating the time first
    # is the only way "on the 13th, or any Friday" cannot be misread as the
    # time attaching to the Friday alone.
    days = _join([_ordinal(d) for d in sorted(day)])
    names = _join([_DAY_NAMES[d] for d in sorted(weekday, key=_weekday_order)])
    return f'on the {days} of the month, or any {names}', False, True


def describe(expression: str, tz: str | None = None) -> str:
    """
    A cron expression in words: "Every weekday at 09:00 (Asia/Kolkata)".

    Not decoration. The failure this prevents is the silent one — `0 9 * * 1`
    and `9 0 * * 1` are both valid, both plausible, and nine hours apart. A
    reading the user can check against their intent catches that before the
    schedule is saved; nothing downstream ever can.

    Two rules the wording follows, both learned from readings that were true
    and useless. It never enumerates more than `MAX_LISTED_TIMES` clock times —
    "at 18 times a day" is not a sentence, and the six times `0 */4 * * *`
    expands to answer a question nobody asked when the user picked "every 4
    hours". And it never appends a day clause to a phrase that already covers
    every day, so `* * * * *` is "Every minute", not "Every minute, every day".

    Returns '' for an unparseable expression rather than raising, so a caller
    rendering a stored row never has to guard the call.
    """
    try:
        minute, hour, day, month, weekday, day_any, weekday_any = parse_cron(expression)
    except CronError:
        return ''

    minute_tok, hour_tok = expression.split()[0], expression.split()[1]
    month_any = len(month) == 12

    when, interval = _time_phrase(minute, hour, minute_tok, hour_tok)
    days, month_folded, time_first = _day_phrase(
        day, weekday, month, day_any, weekday_any, month_any)

    if time_first:
        text = f'{when}, {days}'
    elif interval and days == 'every day':
        # "Every 15 minutes" — the day clause would say nothing.
        text = when
    elif interval:
        text = f'{when}, {days}'
    else:
        text = f'{days} {when}'

    if not month_any and not month_folded:
        text += ', in ' + _join([_MONTH_NAMES[m] for m in sorted(month)])
    if tz:
        text += f' ({tz})'
    return text[0].upper() + text[1:] if text else ''
