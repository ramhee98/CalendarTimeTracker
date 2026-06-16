import os
import hashlib
import pandas as pd
from datetime import datetime, timedelta, timezone
from dateutil.rrule import rrulestr
from dateutil import parser as dtparser

# Ensure event store directory exists
os.makedirs("data", exist_ok=True)


def expand_event_occurrences(event, horizon_days=400, max_occurrences=10000, now=None):
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
    duration = end_utc - start_utc
    uid = event.uid

    rrule_line = next(
        (c.value for c in getattr(event, "extra", []) if c.name == "RRULE"), None
    )
    if not rrule_line:
        return [(start_utc, end_utc, uid)]

    # Collect EXDATE exclusions (matched by calendar date, best effort).
    exdates = set()
    for c in getattr(event, "extra", []):
        if c.name == "EXDATE":
            for part in c.value.split(","):
                try:
                    exdates.add(dtparser.parse(part).date())
                except Exception:
                    pass

    if now is None:
        now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=horizon_days)

    try:
        rule = rrulestr(rrule_line, dtstart=start_utc)
    except Exception:
        # Malformed rule: fall back to a single occurrence rather than crashing.
        return [(start_utc, end_utc, uid)]

    occurrences = []
    for occ_start in rule:
        if occ_start > horizon or len(occurrences) >= max_occurrences:
            break
        if occ_start.date() in exdates:
            continue
        occ_uid = f"{uid}-{occ_start.date().isoformat()}" if uid else None
        occurrences.append((occ_start, occ_start + duration, occ_uid))

    # If the rule produced nothing in range, keep the master occurrence.
    return occurrences or [(start_utc, end_utc, uid)]

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
            old_df = old_df[~old_df["uid"].isin(uids_to_keep)]
        else:
            old_df = pd.DataFrame()

        combined_df = pd.concat([old_df, new_events_df], ignore_index=True)
    else:
        combined_df = new_events_df  # fallback

    save_events_to_cache(url, combined_df)
    return combined_df
