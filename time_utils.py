"""One time convention: UTC in SQLite, Asia/Kolkata for clinic inputs/display.

Naive datetimes mean clinic local time. Date arguments are local dates, not UTC
calendar dates. Fixed UTC+05:30 avoids a Windows tzdata dependency for this demo.
"""
from datetime import date, datetime, time, timedelta, timezone

from errors import ValidationError

UTC = timezone.utc
CLINIC_TZ = timezone(timedelta(hours=5, minutes=30), name="Asia/Kolkata")
CLINIC_TIMEZONE_NAME = "Asia/Kolkata (UTC+05:30)"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def as_utc(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (ValueError, TypeError) as exc:
            raise ValidationError("Use a valid ISO datetime, for example 2030-01-09T09:00:00+05:30.") from exc
        # fromisoformat accepts date-only strings; do not silently invent midnight.
        if "T" not in value and " " not in value:
            raise ValidationError("A date alone is not an appointment time; include hours and minutes.")
    else:
        raise ValidationError("Appointment time must be a datetime or ISO datetime string.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CLINIC_TZ)
    return parsed.astimezone(UTC)


def utc_now(now=None):
    # Keep a single second-resolution convention in all comparisons and storage.
    return (as_utc(now) if now is not None else datetime.now(UTC)).replace(microsecond=0)


def to_storage(value):
    return as_utc(value).replace(microsecond=0).strftime(TIMESTAMP_FORMAT)


def to_local(value):
    return as_utc(value).astimezone(CLINIC_TZ)


def display_datetime(value):
    return to_local(value).strftime("%Y-%m-%d %H:%M") if value else ""


def parse_date(value):
    if isinstance(value, datetime):
        return to_local(value).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError("Use a valid clinic date in YYYY-MM-DD format.") from exc
    raise ValidationError("Date must be a date object or YYYY-MM-DD string.")


def clinic_datetime(day, clock):
    day = parse_date(day)
    if isinstance(clock, str):
        try:
            clock = time.fromisoformat(clock)
        except ValueError as exc:
            raise ValidationError("Clinic availability must use HH:MM times.") from exc
    if not isinstance(clock, time):
        raise ValidationError("A valid clinic time is required.")
    return datetime.combine(day, clock.replace(tzinfo=None), tzinfo=CLINIC_TZ)


def validate_duration(value, default=30):
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int) or not 15 <= value <= 180 or value % 15:
        raise ValidationError("Duration must be an integer multiple of 15 minutes, from 15 to 180.")
    return value


def validate_future_start(value, now=None):
    start = as_utc(value)
    if start.second or start.microsecond:
        raise ValidationError("Appointment starts must use whole minutes.")
    if start <= utc_now(now):
        raise ValidationError("Appointment time must be in the future.")
    return start


def overlaps(start_a, duration_a, start_b, duration_b):
    a, b = as_utc(start_a), as_utc(start_b)
    return a < b + timedelta(minutes=duration_b) and b < a + timedelta(minutes=duration_a)


def next_demo_day(now=None):
    day = to_local(utc_now(now)).date() + timedelta(days=1)
    while day.weekday() == 6:
        day += timedelta(days=1)
    return day
