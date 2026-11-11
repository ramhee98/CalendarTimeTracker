import os
import re
import hashlib
import pandas as pd
from datetime import datetime, timedelta, timezone
from dateutil.rrule import rrulestr
from dateutil import parser as dtparser
from dateutil import tz as dttz

# Ensure event store directory exists
os.makedirs("data", exist_ok=True)


def expand_event_occurrences(event, horizon_days=400, max_occurrences=10000, now=None, overridden=None):
    """Expand a single ics Event into its individual occurrences.

    The ``ics`` 0.7.2 library does not expand recurrence rules, so a weekly
    meeting would otherwise be counted only once. This reads the raw RRULE
    (and best-effort EXDATE) from ``event.extra`` and materialises each
    occurrence using dateutil. Non-recurring events yield a single occurrence.

    Returns a list of ``(start_utc, end_utc, uid)`` tuples. Recurring
    occurrences get a date-suffixed uid so each occurrence is a stable,
    distinct row in the CSV cache.
    """
    start_utc = event.begin.datetime.astimezone(timezone.utc)
    end_utc = event.end.datetime.astimezone(timezone.utc)
    if getattr(event, "all_day", False):
        # All-day events arrive at UTC midnight. Converting midnight to a
        # local timezone west of UTC rolls the event onto the previous
        # calendar day. Anchor at noon UTC instead so the date is stable
        # under any realistic timezone offset.
        start_utc = start_utc.replace(hour=12, minute=0, second=0, microsecond=0)
        end_utc = end_utc.replace(hour=12, minute=0, second=0, microsecond=0)
    uid = event.uid

    # Expand in the event's own timezone so a 09:00 series stays at 09:00
    # local time across DST changes (and BYDAY is evaluated on local
    # weekdays). All-day events keep their noon-UTC anchor.
    if getattr(event, "all_day", False):
        dtstart = start_utc
        duration = end_utc - start_utc
    else:
        dtstart = event.begin.datetime
        local_end = event.end.datetime.astimezone(dtstart.tzinfo)
        # Wall-clock duration, added to each occurrence in local time.
        duration = local_end.replace(tzinfo=None) - dtstart.replace(tzinfo=None)

    rrule_line = next(
        (c.value for c in getattr(event, "extra", []) if c.name == "RRULE"), None
    )
    if not rrule_line:
        return [(start_utc, end_utc, uid)]

    if now is None:
        now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=horizon_days)

    # dateutil rejects a naive UNTIL when DTSTART is timezone-aware, which is
    # always the case here. All-day series commonly use a DATE UNTIL
    # (e.g. UNTIL=20250127), so make it UTC: a DATE is inclusive of that whole
    # day, a floating DATE-TIME is treated as UTC (best effort).
    rrule_line = re.sub(r"UNTIL=(\d{8})(?=;|$)", r"UNTIL=\1T235959Z", rrule_line)
    rrule_line = re.sub(r"UNTIL=(\d{8}T\d{6})(?=;|$)", r"UNTIL=\1Z", rrule_line)

    # Collect EXDATE exclusions, matched by calendar date in the event's own
    # timezone (honouring the EXDATE's TZID) so exclusions near midnight
    # don't miss or hit the wrong occurrence.
    exdates = set()
    for c in getattr(event, "extra", []):
        if c.name == "EXDATE":
            for part in c.value.split(","):
                try:
                    ex = parse_ical_value(part, c.params, dtstart.tzinfo)
                    if isinstance(ex, datetime):
                        ex = ex.astimezone(dtstart.tzinfo).date()
                    exdates.add(ex)
                except Exception:
                    pass

    try:
        rule = rrulestr(rrule_line, dtstart=dtstart)
    except Exception:
        # Malformed rule: fall back to a single occurrence rather than crashing.
        return [(start_utc, end_utc, uid)]

    occurrences = []
    for occ_local in rule:
        occ_start = occ_local.astimezone(timezone.utc)
        if occ_start > horizon or len(occurrences) >= max_occurrences:
            break
        if occ_local.date() in exdates:
            continue
        if overridden and occ_start.date().isoformat() in overridden:
            continue  # Replaced by a RECURRENCE-ID override event
        occ_end = (occ_local + duration).astimezone(timezone.utc)
        occ_uid = f"{uid}-{occ_start.date().isoformat()}" if uid else None
        occurrences.append((occ_start, occ_end, occ_uid))

    # If the rule produced nothing in range, keep the master occurrence.
    return occurrences or [(start_utc, end_utc, uid)]

def parse_ical_value(value, params, default_tz):
    """Parse an iCal DATE or DATE-TIME property value.

    Returns a ``date`` for DATE values and an aware ``datetime`` otherwise. A
    floating DATE-TIME uses its TZID parameter, else ``default_tz``.
    """
    value = value.strip()
    if len(value) == 8:
        return datetime.strptime(value, "%Y%m%d").date()
    dt = dtparser.parse(value)
    if dt.tzinfo is None:
        tzid = (params or {}).get("TZID")
        tzinfo = dttz.gettz(tzid[0]) if tzid else None
        dt = dt.replace(tzinfo=tzinfo or default_tz)
    return dt


def recurrence_id_key(event):
    """Occurrence date key (as used in occurrence uids) that an override
    event replaces, or None if the event is not a RECURRENCE-ID override."""
    for c in getattr(event, "extra", []):
        if c.name == "RECURRENCE-ID":
            rid = parse_ical_value(c.value, c.params, event.begin.datetime.tzinfo)
            if isinstance(rid, datetime):
                rid = rid.astimezone(timezone.utc).date()
            return rid.isoformat()
    return None


def expand_calendar_events(events, errors=None, **kwargs):
    """Expand all events of a calendar into ``(event, start, end, uid)``.

    Handles RECURRENCE-ID overrides: the override replaces the master
    series' occurrence for that date (and takes over its uid), and a
    cancelled override just removes it. Events that fail to parse are skipped
    and their exception appended to ``errors`` if given.
    """
    events = list(events)
    overrides = {}
    keys = {}
    for event in events:
        try:
            keys[id(event)] = recurrence_id_key(event)
        except Exception:
            keys[id(event)] = None
        if keys[id(event)] and event.uid:
            overrides.setdefault(event.uid, set()).add(keys[id(event)])

    result = []
    for event in events:
        try:
            key = keys[id(event)]
            if key and event.uid:
                if (getattr(event, "status", None) or "").upper() == "CANCELLED":
                    continue
                for start, end, _ in expand_event_occurrences(event, **kwargs):
                    result.append((event, start, end, f"{event.uid}-{key}"))
            else:
                for start, end, uid in expand_event_occurrences(
                    event, overridden=overrides.get(event.uid), **kwargs
                ):
                    result.append((event, start, end, uid))
        except Exception as e:
            print(f"Skipping event: {e}")
            if errors is not None:
                errors.append(e)
    return result


_OCCURRENCE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def hash_url(url):
    return hashlib.md5(url.encode()).hexdigest()

def get_cache_file_path(url):
    return f"data/{hash_url(url)}.csv"

def load_cached_events(url):
    path = get_cache_file_path(url)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            return pd.read_csv(path, parse_dates=["start", "end"])
        except pd.errors.EmptyDataError:
            return pd.DataFrame()  # Recover gracefully if file is corrupt or empty
    return pd.DataFrame()

def save_events_to_cache(url, df):
    if df.empty or df.columns.empty:
        return  # Prevent saving if no usable data
    path = get_cache_file_path(url)
    df.to_csv(path, index=False)

def update_event_store(url, new_events_df, cutoff_days=30, additive=False):
    """Merge new events into the cached event store for ``url``.

    Two modes:

    * Feed refresh (``additive=False``, default): the URL is authoritative for
      the recent window, so cached events newer than ``cutoff_days`` are dropped
      and replaced by whatever the feed currently contains. Events not present
      in the feed anymore are removed from the recent window.
    * Additive merge (``additive=True``, used by the manual ICS import): keep
      ALL existing cached events, replacing only those whose uid also appears in
      the new set. This never deletes the recent window, so importing a partial
      .ics file adds to the calendar instead of wiping unrelated events.
    """
    cached_df = load_cached_events(url)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(cutoff_days)

    if "uid" in new_events_df.columns:
        if not cached_df.empty and "uid" in cached_df.columns:
            if additive:
                # Keep everything; only matching UIDs get replaced below.
                old_df = cached_df
            else:
                # Only keep old events that are outside the replacement range
                old_df = cached_df[cached_df["start"] < cutoff]
            # Replace events by dropping any matching UIDs
            uids_to_keep = set(new_events_df["uid"])
            # Rows cached under a series' bare uid (e.g. RECURRENCE-ID
            # overrides from older versions) are superseded by the series'
            # date-suffixed occurrence rows.
            uids_to_keep |= {
                _OCCURRENCE_SUFFIX.sub("", u) for u in uids_to_keep
                if isinstance(u, str) and _OCCURRENCE_SUFFIX.search(u)
            }
            old_df = old_df[~old_df["uid"].isin(uids_to_keep)]
        else:
            old_df = pd.DataFrame()

        combined_df = pd.concat([old_df, new_events_df], ignore_index=True)
    elif new_events_df.empty:
        # Feed returned no events (e.g. a transient empty response): keep
        # serving the cache instead of making the calendar disappear.
        return cached_df
    else:
        combined_df = new_events_df  # fallback

    save_events_to_cache(url, combined_df)
    return combined_df
