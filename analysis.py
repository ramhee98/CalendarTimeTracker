"""Time-accounting helpers on the events DataFrame.

Pure pandas, no Streamlit, so they can be used from every page and tested in
isolation. Every function expects the columns produced by the pages
(`start`, `end` as tz-aware timestamps, `duration_hours`, `calendar`,
`category`, `event_name`).
"""
from datetime import timedelta

import pandas as pd

DAY = pd.Timedelta(days=1)


# --- Long (all-day / multi-day) events ---

def is_all_day(starts, ends):
    """Vectorised heuristic for all-day events.

    All-day events are stored as whole days anchored at midnight UTC (older
    cache entries) or noon UTC (see calendar_store.expand_event_occurrences).
    """
    s = starts.dt.tz_convert("UTC") if starts.dt.tz is not None else starts
    e = ends.dt.tz_convert("UTC") if ends.dt.tz is not None else ends
    duration = e - s
    return (
        (duration >= DAY)
        & (duration % DAY == pd.Timedelta(0))
        & s.dt.hour.isin([0, 12])
        & (s.dt.minute == 0)
        & (s.dt.second == 0)
    )


def long_event_mask(df, max_hours=16):
    """True for all-day events and events longer than `max_hours`."""
    if df.empty:
        return pd.Series(False, index=df.index)
    hours = (df["end"] - df["start"]).dt.total_seconds() / 3600
    return is_all_day(df["start"], df["end"]) | (hours > max_hours)


# --- Clipping to a time window ---

def local_midnight(d, tz):
    """Midnight at the start of date `d` in `tz` (DST-safe)."""
    ts = pd.Timestamp(d)
    if tz is not None:
        ts = ts.tz_localize(tz, nonexistent="shift_forward", ambiguous=False)
    return ts


def clip_between(df, lo, hi):
    """Events overlapping [lo, hi) with `duration_hours` trimmed to that window."""
    if df.empty:
        return df.copy()
    overlaps = (df["start"] < hi) & (df["end"] > lo)
    # Zero-length events have no overlap but still belong to the window.
    instant = (df["start"] == df["end"]) & (df["start"] >= lo) & (df["start"] < hi)
    out = df[overlaps | instant].copy()
    clipped = out["end"].clip(upper=hi) - out["start"].clip(lower=lo)
    out["duration_hours"] = (clipped.dt.total_seconds() / 3600).clip(lower=0)
    return out


def clip_to_range(df, start_date, end_date):
    """Events touching the inclusive date range, counting only in-range hours.

    A 4-day trip that starts the day before `start_date` contributes 3 days,
    not 4, so totals match the duration charts on the Home page.
    """
    if df.empty:
        return df.copy()
    tz = df["start"].dt.tz
    lo = local_midnight(start_date, tz)
    hi = local_midnight(end_date + timedelta(days=1), tz)
    return clip_between(df, lo, hi)


# --- Overlapping events ---

def find_overlaps(df):
    """All pairs of events whose time ranges overlap.

    Returns one row per pair with both events and the overlapping hours.
    Long events should be removed first, otherwise a week-long trip overlaps
    with everything inside it.
    """
    columns = ["calendar_a", "event_a", "start_a", "end_a",
               "calendar_b", "event_b", "start_b", "end_b", "overlap_hours"]
    if df.empty:
        return pd.DataFrame(columns=columns)

    ev = df.sort_values("start").reset_index(drop=True)
    starts = ev["start"].tolist()
    ends = ev["end"].tolist()
    pairs = []
    active = []
    for i, s in enumerate(starts):
        active = [j for j in active if ends[j] > s]
        for j in active:
            overlap = min(ends[j], ends[i]) - s
            if overlap > pd.Timedelta(0):
                pairs.append((j, i, overlap.total_seconds() / 3600))
        active.append(i)

    rows = []
    for j, i, hours in pairs:
        a, b = ev.iloc[j], ev.iloc[i]
        rows.append({
            "calendar_a": a["calendar"], "event_a": a.get("event_name", ""),
            "start_a": a["start"], "end_a": a["end"],
            "calendar_b": b["calendar"], "event_b": b.get("event_name", ""),
            "start_b": b["start"], "end_b": b["end"],
            "overlap_hours": hours,
        })
    return pd.DataFrame(rows, columns=columns)


def union_hours(df):
    """Hours covered by at least one event (overlaps counted once)."""
    if df.empty:
        return 0.0
    ev = df.sort_values("start")
    total = pd.Timedelta(0)
    cur_start = cur_end = None
    for s, e in zip(ev["start"], ev["end"]):
        if cur_end is None or s > cur_end:
            if cur_end is not None:
                total += cur_end - cur_start
            cur_start, cur_end = s, e
        elif e > cur_end:
            cur_end = e
    total += cur_end - cur_start
    return total.total_seconds() / 3600
