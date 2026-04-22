"""Utilities for parsing iCal feeds and computing multi-person availability."""

from datetime import datetime, timedelta, time
import requests
from icalendar import Calendar
from dateutil.rrule import rrulestr
import pytz


def fetch_ical(url: str) -> Calendar:
    """Fetch and parse an iCal feed from a secret Google Calendar URL."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "")
    if "text/html" in content_type or resp.content.strip().startswith(b"<!"):
        raise ValueError(
            "One of the calendar URLs returned an HTML page instead of iCal data. "
            "Make sure you're using the 'Secret address in iCal format' from Google Calendar Settings — "
            "not a regular calendar page link."
        )
    return Calendar.from_ical(resp.content)


def get_busy_times(cal: Calendar, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Extract busy time ranges from a parsed iCal calendar within a date range.

    Handles single events and recurring events. Expands recurrences that fall
    within the given window.
    """
    busy = []
    tz_utc = pytz.UTC

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        # Skip all-day events (they have date, not datetime)
        dtstart = component.get("dtstart")
        if dtstart is None:
            continue
        dtstart_val = dtstart.dt

        # All-day events use date objects, not datetime — treat as busy for full day
        if not isinstance(dtstart_val, datetime):
            from datetime import date as date_type
            if isinstance(dtstart_val, date_type):
                dtend = component.get("dtend")
                if dtend and not isinstance(dtend.dt, datetime):
                    day_start = datetime.combine(dtstart_val, time.min).replace(tzinfo=tz_utc)
                    day_end = datetime.combine(dtend.dt, time.min).replace(tzinfo=tz_utc)
                else:
                    day_start = datetime.combine(dtstart_val, time.min).replace(tzinfo=tz_utc)
                    day_end = day_start + timedelta(days=1)
                if day_start < end and day_end > start:
                    busy.append((max(day_start, start), min(day_end, end)))
            continue

        # Ensure timezone-aware
        if dtstart_val.tzinfo is None:
            dtstart_val = tz_utc.localize(dtstart_val)
        else:
            dtstart_val = dtstart_val.astimezone(tz_utc)

        dtend = component.get("dtend")
        duration = component.get("duration")

        if dtend:
            dtend_val = dtend.dt
            if not isinstance(dtend_val, datetime):
                continue
            if dtend_val.tzinfo is None:
                dtend_val = tz_utc.localize(dtend_val)
            else:
                dtend_val = dtend_val.astimezone(tz_utc)
            event_duration = dtend_val - dtstart_val
        elif duration:
            event_duration = duration.dt
            dtend_val = dtstart_val + event_duration
        else:
            # Default 1 hour if no end/duration
            event_duration = timedelta(hours=1)
            dtend_val = dtstart_val + event_duration

        # Handle recurring events
        rrule = component.get("rrule")
        if rrule:
            rule_str = rrule.to_ical().decode("utf-8")
            try:
                rule = rrulestr(f"RRULE:{rule_str}", dtstart=dtstart_val)
                occurrences = rule.between(start - event_duration, end, inc=True)
                for occ in occurrences:
                    occ_end = occ + event_duration
                    if occ < end and occ_end > start:
                        busy.append((max(occ, start), min(occ_end, end)))
            except (ValueError, TypeError):
                # If rrule parsing fails, just use the single instance
                if dtstart_val < end and dtend_val > start:
                    busy.append((max(dtstart_val, start), min(dtend_val, end)))
        else:
            if dtstart_val < end and dtend_val > start:
                busy.append((max(dtstart_val, start), min(dtend_val, end)))

    return sorted(busy, key=lambda x: x[0])


def merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Merge overlapping time intervals."""
    if not intervals:
        return []
    sorted_intervals = sorted(intervals, key=lambda x: x[0])
    merged = [sorted_intervals[0]]
    for current_start, current_end in sorted_intervals[1:]:
        last_start, last_end = merged[-1]
        if current_start <= last_end:
            merged[-1] = (last_start, max(last_end, current_end))
        else:
            merged.append((current_start, current_end))
    return merged


def _to_time(hour_value):
    """Convert an hour expressed as int or float (e.g. 8.5 → 08:30) to a time()."""
    h = int(hour_value)
    m = int(round((float(hour_value) - h) * 60))
    if m == 60:
        h += 1
        m = 0
    return time(max(0, min(23, h)), max(0, min(59, m)))


def find_available_slots(
    ical_urls: list[str],
    search_start: datetime,
    search_end: datetime,
    meeting_duration_minutes: int = 60,
    work_hours_start: float = 9,
    work_hours_end: float = 17,
    timezone_str: str = "America/New_York",
    allowed_weekdays: set[int] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Find time slots where ALL people are available.

    Args:
        ical_urls: List of secret iCal URLs for each person.
        search_start: Start of the search window (timezone-aware).
        search_end: End of the search window (timezone-aware).
        meeting_duration_minutes: Desired meeting length in minutes.
        work_hours_start: Start of work hours (hour, may be fractional — 8.5 = 8:30).
        work_hours_end: End of work hours (hour, may be fractional).
        timezone_str: Timezone for work hours (e.g. "America/New_York").
        allowed_weekdays: Iterable of weekday ints (Mon=0 … Sun=6) that are
            bookable. Default: Mon-Fri ({0,1,2,3,4}).

    Returns:
        Tuple of (slots, fetch_report). Slots are dicts with 'start' and 'end'.
        fetch_report entries have 'events', 'ok', and 'error' keys.
    """
    if allowed_weekdays is None:
        allowed_weekdays = {0, 1, 2, 3, 4}
    else:
        allowed_weekdays = set(allowed_weekdays)
    tz = pytz.timezone(timezone_str)
    tz_utc = pytz.UTC

    # Ensure search bounds are timezone-aware
    if search_start.tzinfo is None:
        search_start = tz.localize(search_start)
    if search_end.tzinfo is None:
        search_end = tz.localize(search_end)

    search_start_utc = search_start.astimezone(tz_utc)
    search_end_utc = search_end.astimezone(tz_utc)

    # Collect all busy times — each calendar fetched independently
    all_busy = []
    fetch_report = []
    for url in ical_urls:
        try:
            cal = fetch_ical(url)
            busy = get_busy_times(cal, search_start_utc, search_end_utc)
            all_busy.extend(busy)
            fetch_report.append({"events": len(busy), "ok": True, "error": None})
        except Exception as e:
            fetch_report.append({"events": 0, "ok": False, "error": str(e)})

    # Merge all busy intervals into one unified busy timeline
    merged_busy = merge_intervals(all_busy)

    # Build free slots by finding gaps between busy intervals
    meeting_duration = timedelta(minutes=meeting_duration_minutes)
    available_slots = []

    # Generate candidate slots day by day within work hours
    current_day = search_start.astimezone(tz).date()
    end_day = search_end.astimezone(tz).date()

    start_t = _to_time(work_hours_start)
    end_t = _to_time(work_hours_end)

    while current_day <= end_day:
        # Skip days the host doesn't offer
        if current_day.weekday() not in allowed_weekdays:
            current_day += timedelta(days=1)
            continue

        day_start = tz.localize(datetime.combine(current_day, start_t))
        day_end = tz.localize(datetime.combine(current_day, end_t))

        # Clamp to search window
        day_start = max(day_start, search_start.astimezone(tz))
        day_end = min(day_end, search_end.astimezone(tz))

        if day_start >= day_end:
            current_day += timedelta(days=1)
            continue

        day_start_utc = day_start.astimezone(tz_utc)
        day_end_utc = day_end.astimezone(tz_utc)

        # Find free windows within this day's work hours
        free_start = day_start_utc
        for busy_start, busy_end in merged_busy:
            if busy_end <= day_start_utc:
                continue
            if busy_start >= day_end_utc:
                break

            # There's a gap before this busy block
            if free_start < busy_start:
                gap_end = min(busy_start, day_end_utc)
                if gap_end - free_start >= meeting_duration:
                    available_slots.append({
                        "start": free_start.astimezone(tz).isoformat(),
                        "end": gap_end.astimezone(tz).isoformat(),
                    })
            free_start = max(free_start, busy_end)

        # Check remaining time after last busy block
        if free_start < day_end_utc and day_end_utc - free_start >= meeting_duration:
            available_slots.append({
                "start": free_start.astimezone(tz).isoformat(),
                "end": day_end_utc.astimezone(tz).isoformat(),
            })

        current_day += timedelta(days=1)

    return available_slots, fetch_report
