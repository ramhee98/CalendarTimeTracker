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


# --- Periods (weeks run Monday-Sunday) ---

PERIODS = ("week", "month")


def period_start(period, d):
    """First day of the week (Monday) or month containing date `d`."""
    if period == "month":
        return d.replace(day=1)
    return d - timedelta(days=d.weekday())


def next_period_start(period, ps):
    if period == "month":
        return (ps.replace(day=28) + timedelta(days=4)).replace(day=1)
    return ps + timedelta(days=7)


def shift_periods(period, ps, n):
    """Move the period start `ps` by `n` periods (negative = back in time)."""
    for _ in range(abs(n)):
        ps = next_period_start(period, ps) if n > 0 else period_start(period, ps - timedelta(days=1))
    return ps


def period_label(period, ps):
    if period == "month":
        return ps.strftime("%Y-%m")
    year, week, _ = ps.isocalendar()
    return f"{year}-W{week:02d}"


def hours_by_period(df, period, first, last, group_col=None):
    """Hours per week/month from the period containing `first` to the one
    containing `last`, splitting events that cross a period boundary.

    Returns a DataFrame indexed by period start date with one column per
    group (or a single `hours` column when `group_col` is None). Periods
    without events are included as zeros.
    """
    starts = []
    ps = period_start(period, first)
    while ps <= last:
        starts.append(ps)
        ps = next_period_start(period, ps)
    if not starts:
        return pd.DataFrame()

    tz = df["start"].dt.tz if not df.empty else None
    window = clip_between(df, local_midnight(starts[0], tz),
                          local_midnight(next_period_start(period, starts[-1]), tz))
    records = []
    for ps in starts:
        part = clip_between(window, local_midnight(ps, tz),
                            local_midnight(next_period_start(period, ps), tz))
        if group_col:
            records.append(part.groupby(group_col)["duration_hours"].sum().to_dict())
        else:
            records.append({"hours": part["duration_hours"].sum()})
    out = pd.DataFrame(records, index=pd.Index(starts, name="period_start")).fillna(0.0)
    if group_col is None and "hours" not in out:
        out["hours"] = 0.0
    return out


# --- Goals ---

def goal_met(goal, hours):
    mn, mx = goal.get("min"), goal.get("max")
    return (mn is None or hours >= mn - 1e-9) and (mx is None or hours <= mx + 1e-9)


def goal_target_text(goal):
    mn, mx = goal.get("min"), goal.get("max")
    if mn is not None and mx is not None:
        return f"{mn:g}–{mx:g}h"
    if mn is not None:
        return f"≥ {mn:g}h"
    return f"≤ {mx:g}h"


def goal_status(goal, done, booked):
    """Status of a goal for a period: (key, label).

    `done` = hours already passed, `booked` = done + still scheduled. For a
    finished period both are the same.
    """
    mn, mx = goal.get("min"), goal.get("max")
    if mx is not None and done > mx + 1e-9:
        return "over", "❌ Over budget"
    if mx is not None and booked > mx + 1e-9:
        return "at_risk", "⚠️ Booked over budget"
    if mn is not None:
        if done >= mn - 1e-9:
            return "met", "✅ Reached"
        if booked >= mn - 1e-9:
            return "on_track", "🗓️ On track (booked)"
        return "behind", f"⚠️ {mn - booked:.1f}h short even with booked events"
    return "met", "✅ Within budget"


def goal_events(df, goal):
    return df[df[goal["type"]] == goal["name"]] if goal["type"] in df else df.iloc[0:0]


def evaluate_goal(df, goal, now, history=12):
    """Progress of one goal for the current period plus its recent history.

    `now` is a tz-aware Timestamp. History covers up to `history` complete
    periods before the current one, starting no earlier than the first event
    of the goal's calendar/category (so a new calendar isn't counted as
    weeks of misses).
    """
    period = goal.get("period", "week")
    sub = goal_events(df, goal)
    tz = df["start"].dt.tz if not df.empty else None
    cur = period_start(period, now.date())
    nxt = next_period_start(period, cur)
    lo, hi = local_midnight(cur, tz), local_midnight(nxt, tz)

    booked = clip_between(sub, lo, hi)["duration_hours"].sum()
    done = clip_between(sub, lo, min(now, hi))["duration_hours"].sum()
    status, status_label = goal_status(goal, done, booked)

    first = shift_periods(period, cur, -history)
    if not sub.empty:
        first = max(first, period_start(period, sub["start"].min().date()))
    if sub.empty or first >= cur:
        hist = pd.DataFrame({"hours": []}, index=pd.Index([], name="period_start"))
    else:
        hist = hours_by_period(sub, period, first, cur - timedelta(days=1))
    hist["met"] = [goal_met(goal, h) for h in hist["hours"]]

    streak = 0
    for met in reversed(hist["met"].tolist()):
        if not met:
            break
        streak += 1

    return {
        "period": period,
        "period_start": cur,
        "period_end": nxt - timedelta(days=1),
        "done": done,
        "booked": booked,
        "status": status,
        "status_label": status_label,
        "history": hist,
        "met_count": int(hist["met"].sum()),
        "history_count": len(hist),
        "streak": streak,
    }


# --- Typical week and booked weeks ahead ---

def typical_week(df, group_col, week, weeks=8):
    """Average hours per group over up to `weeks` complete weeks before the
    week starting on `week` (a Monday). Weeks before the first event are
    skipped so a short history isn't diluted with empty weeks.

    Returns (per-group Series, average total, number of weeks used).
    """
    first = week - timedelta(weeks=weeks)
    if not df.empty:
        first = max(first, period_start("week", df["start"].min().date()))
    if df.empty or first >= week:
        return pd.Series(dtype=float), 0.0, 0
    base = hours_by_period(df, "week", first, week - timedelta(days=1), group_col)
    total = base.sum(axis=1) if group_col else base["hours"]
    per_group = base.mean() if group_col else pd.Series(dtype=float)
    return per_group, float(total.mean()), len(base)


def booked_weeks(df, group_col, today, weeks_ahead=4, baseline_weeks=8, busy_ratio=1.2):
    """Hours already booked for the current week and the following weeks,
    compared with a typical week.

    Returns (per-week-per-group DataFrame, summary DataFrame, typical total,
    number of baseline weeks). The summary has one row per week with the
    booked total, the ratio to a typical week and a `busy` flag.
    """
    cur = period_start("week", today)
    last = cur + timedelta(weeks=weeks_ahead - 1)
    future = hours_by_period(df, "week", cur, last, group_col)
    _, typical_total, n_weeks = typical_week(df, group_col, cur, baseline_weeks)

    booked = future.sum(axis=1) if group_col else future["hours"]
    summary = pd.DataFrame({"booked": booked})
    summary["typical"] = typical_total
    summary["ratio"] = summary["booked"] / typical_total if typical_total else float("nan")
    summary["busy"] = summary["ratio"] > busy_ratio
    return future, summary, typical_total, n_weeks


# --- Tag calendars (markers such as Oura tags) ---

SUMMARY_UID_PREFIX = "oura-tag-summary-"
TAG_COLUMNS = ["tag", "calendar", "date", "hour", "weekday", "all_day", "start"]


def tag_occurrences(df):
    """One row per tag event with its tag label, day, hour and weekday.

    Monthly summary events from oura-tags-ical are dropped. All-day tags use
    their UTC date (they're anchored at noon UTC, see calendar_store) and
    have no hour. A multi-day all-day tag counts once, on its first day.
    """
    if df.empty:
        return pd.DataFrame(columns=TAG_COLUMNS)
    occ = df
    if "uid" in occ:
        occ = occ[~occ["uid"].astype(str).str.startswith(SUMMARY_UID_PREFIX)]
    occ = occ.copy()
    all_day = is_all_day(occ["start"], occ["end"])
    utc_dates = occ["start"].dt.tz_convert("UTC").dt.date if occ["start"].dt.tz is not None else occ["start"].dt.date
    occ["all_day"] = all_day
    occ["date"] = utc_dates.where(all_day, occ["start"].dt.date)
    occ["hour"] = occ["start"].dt.hour.where(~all_day)
    occ["weekday"] = pd.to_datetime(occ["date"]).dt.day_name()
    occ["tag"] = occ["event_name"].fillna("").astype(str).str.strip().replace("", "Untitled")
    if "calendar" not in occ:
        occ["calendar"] = ""
    return occ[TAG_COLUMNS].reset_index(drop=True)


def _longest_gap(dates, start_date, end_date):
    """Longest run of days in [start_date, end_date] without any of `dates`."""
    if end_date < start_date:
        return 0
    points = [start_date - timedelta(days=1)] + sorted(set(dates)) + [end_date + timedelta(days=1)]
    return max((b - a).days - 1 for a, b in zip(points, points[1:]))


def tag_summary(occ, start_date, end_date, today):
    """Per-tag statistics for the inclusive date range.

    `Days since last` is counted up to `today` over all data, so it reads as
    the current streak without the tag (e.g. 12 alcohol-free days).
    """
    columns = ["Tag", "Total", "Avg / week", "Days with tag", "% of days",
               "Last seen", "Days since last", "Longest gap (days)"]
    in_range = occ[(occ["date"] >= start_date) & (occ["date"] <= end_date)]
    if in_range.empty:
        return pd.DataFrame(columns=columns)
    days = (end_date - start_date).days + 1
    gap_end = min(end_date, today)
    past = occ[occ["date"] <= today]
    rows = []
    for tag, group in in_range.groupby("tag"):
        seen = past.loc[past["tag"] == tag, "date"]
        last = seen.max() if not seen.empty else None
        rows.append({
            "Tag": tag,
            "Total": len(group),
            "Avg / week": round(len(group) / (days / 7), 2),
            "Days with tag": group["date"].nunique(),
            "% of days": round(group["date"].nunique() / days * 100, 1),
            "Last seen": last,
            "Days since last": (today - last).days if last is not None else None,
            "Longest gap (days)": _longest_gap(group["date"], start_date, gap_end),
        })
    return pd.DataFrame(rows, columns=columns).sort_values(["Total", "Tag"], ascending=[False, True]).reset_index(drop=True)


def tag_counts_by_period(occ, period, start_date, end_date):
    """Tag counts per day, week or month (zero-filled), one column per tag.

    Indexed by the start date of each period.
    """
    in_range = occ[(occ["date"] >= start_date) & (occ["date"] <= end_date)]
    if period == "day":
        periods = [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)]
        keys = in_range["date"]
    else:
        periods = []
        ps = period_start(period, start_date)
        while ps <= end_date:
            periods.append(ps)
            ps = next_period_start(period, ps)
        keys = in_range["date"].map(lambda d: period_start(period, d))
    if in_range.empty:
        return pd.DataFrame(index=pd.Index(periods, name="period_start"))
    counts = in_range.groupby([keys.rename("period_start"), "tag"]).size().unstack(fill_value=0)
    return counts.reindex(periods, fill_value=0).rename_axis("period_start")
