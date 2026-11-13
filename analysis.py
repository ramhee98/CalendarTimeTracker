"""Time-accounting helpers on the events DataFrame.

Pure pandas, no Streamlit, so they can be used from every page and tested in
isolation. Every function expects the columns produced by the pages
(`start`, `end` as tz-aware timestamps, `duration_hours`, `calendar`,
`category`, `event_name`).
"""
import re
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
    if "hours_factor" in out:
        # Only part of the event's time counts (e.g. asleep vs time in bed).
        out["duration_hours"] *= out["hours_factor"].fillna(1.0)
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

PERIODS = ("day", "week", "month")


def period_start(period, d):
    """The day itself, or the first day of the week (Monday) or month
    containing date `d`."""
    if period == "day":
        return d
    if period == "month":
        return d.replace(day=1)
    return d - timedelta(days=d.weekday())


def next_period_start(period, ps):
    if period == "day":
        return ps + timedelta(days=1)
    if period == "month":
        return (ps.replace(day=28) + timedelta(days=4)).replace(day=1)
    return ps + timedelta(days=7)


def shift_periods(period, ps, n):
    """Move the period start `ps` by `n` periods (negative = back in time)."""
    for _ in range(abs(n)):
        ps = next_period_start(period, ps) if n > 0 else period_start(period, ps - timedelta(days=1))
    return ps


def period_label(period, ps):
    if period == "day":
        return ps.strftime("%Y-%m-%d")
    if period == "month":
        return ps.strftime("%Y-%m")
    year, week, _ = ps.isocalendar()
    return f"{year}-W{week:02d}"


def hours_by_period(df, period, first, last, group_col=None):
    """Hours per day/week/month from the period containing `first` to the one
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
#
# Calendar and category goals count hours. Tag and tag group goals count how
# often a tag occurs (tags are markers, not time) and work on the tag
# occurrences from tag_occurrences(), with tag names as shown on the Tags page.

TIME_GOAL_TYPES = ("calendar", "category")
TAG_GOAL_TYPES = ("tag", "tag_group")
GOAL_TYPES = TIME_GOAL_TYPES + TAG_GOAL_TYPES


def is_tag_goal(goal):
    return goal.get("type") in TAG_GOAL_TYPES


def format_goal_amount(goal, value):
    """"2.5h" for time goals, "3×" for tag goals."""
    return f"{value:g}×" if is_tag_goal(goal) else f"{value:.1f}h"


def goal_met(goal, value):
    mn, mx = goal.get("min"), goal.get("max")
    return (mn is None or value >= mn - 1e-9) and (mx is None or value <= mx + 1e-9)


def goal_target_text(goal):
    mn, mx = goal.get("min"), goal.get("max")
    unit = "×" if is_tag_goal(goal) else "h"
    if mn is not None and mx is not None:
        return f"{mn:g}–{mx:g}{unit}"
    if mn is not None:
        return f"≥ {mn:g}{unit}"
    return f"≤ {mx:g}{unit}"


def goal_status(goal, done, booked):
    """Status of a goal for a period: (key, label).

    `done` = hours (or tag count) already passed, `booked` = done + still
    scheduled. For a finished period both are the same.
    """
    mn, mx = goal.get("min"), goal.get("max")
    if mx is not None and done > mx + 1e-9:
        return "over", "❌ Over the limit" if is_tag_goal(goal) else "❌ Over budget"
    if mx is not None and booked > mx + 1e-9:
        return "at_risk", "⚠️ Booked over budget"
    if mn is not None:
        if done >= mn - 1e-9:
            return "met", "✅ Reached"
        if booked >= mn - 1e-9:
            return "on_track", "🗓️ On track (booked)"
        short = mn - booked
        if is_tag_goal(goal):
            return "behind", f"⚠️ {short:g}× to go"
        return "behind", f"⚠️ {short:.1f}h short even with booked events"
    return "met", "✅ Within the limit" if is_tag_goal(goal) else "✅ Within budget"


def estimated_goal_status(goal, done, expected):
    """Like `goal_status`, with `expected` = done + booked + estimated
    (e.g. sleep nights not recorded yet): (key, label)."""
    key, label = goal_status(goal, done, expected)
    mn = goal.get("min")
    if key == "at_risk":
        return key, "⚠️ Expected over budget"
    if key == "on_track":
        return key, "📈 On track (estimated)"
    if key == "behind" and mn is not None:
        return key, f"⚠️ ~{mn - expected:.1f}h short (estimated)"
    return key, label


def tag_goal_occurrences(tags, goal, groups=()):
    """Tag occurrences counted by a tag or tag group goal.

    A tag goal matches its tag ignoring the emoji variation selector and
    spacing; a tag group goal matches the members of the saved group with
    that name (none if the group no longer exists).
    """
    if tags is None or tags.empty:
        return pd.DataFrame(columns=TAG_COLUMNS)
    if goal["type"] == "tag":
        return tags[tags["tag"].map(_variant_key) == _variant_key(goal["name"])]
    group = next((g for g in groups if g["name"] == goal["name"]), None)
    if group is None:
        return tags.iloc[0:0]
    members = weighted_members(group, tags["tag"].unique())
    out = tags[tags["tag"].isin(members)].copy()
    out["weight"] = out["tag"].map(members).astype(float)
    return out


def goal_events(df, goal, tags=None, groups=()):
    """Events (time goals) or tag occurrences (tag goals) a goal counts."""
    if is_tag_goal(goal):
        return tag_goal_occurrences(tags, goal, groups)
    return df[df[goal["type"]] == goal["name"]] if goal["type"] in df else df.iloc[0:0]


def goal_amount(goal, sub, first, end, now=None, tz=None):
    """Hours (time goals) or tag count (tag goals) of `sub` from date `first`
    up to date `end` (exclusive), counting only what happened before `now`
    if given. `tz` is the time zone of the dates (time goals)."""
    if sub.empty:
        return 0.0
    if is_tag_goal(goal):
        mask = (sub["date"] >= first) & (sub["date"] < end)
        if now is not None:
            mask &= sub["date"] <= now.date()
        return float(_weights(sub)[mask].sum())
    hi = local_midnight(end, tz)
    if now is not None:
        hi = min(now, hi)
    return float(clip_between(sub, local_midnight(first, tz), hi)["duration_hours"].sum())


def _goal_history(goal, sub, period, first, last):
    """Hours or tag count per period from the period containing `first` to
    the one containing `last`, as a Series indexed by period start."""
    if not is_tag_goal(goal):
        return hours_by_period(sub, period, first, last)["hours"]
    starts = []
    ps = period_start(period, first)
    while ps <= last:
        starts.append(ps)
        ps = next_period_start(period, ps)
    counts = _weights(sub).groupby(sub["date"].map(lambda d: period_start(period, d))).sum()
    return counts.reindex(starts, fill_value=0).astype(float)


def evaluate_goal(df, goal, now, history=12, tags=None, groups=()):
    """Progress of one goal for the current period plus its recent history.

    `now` is a tz-aware Timestamp. `tags` (tag occurrences) and `groups`
    (tag groups) are only needed for tag goals. History covers up to
    `history` complete periods before the current one, starting no earlier
    than the first event of the goal's calendar/category, or the first tag
    of any kind for tag goals (so a new calendar isn't counted as weeks of
    misses, while days without a tracked tag still count).
    """
    period = goal.get("period", "week")
    sub = goal_events(df, goal, tags, groups)
    tz = df["start"].dt.tz if not df.empty else now.tz
    cur = period_start(period, now.date())
    nxt = next_period_start(period, cur)

    booked = goal_amount(goal, sub, cur, nxt, tz=tz)
    done = goal_amount(goal, sub, cur, nxt, now=now, tz=tz)
    status, status_label = goal_status(goal, done, booked)

    if is_tag_goal(goal):
        tracked = tags if tags is not None and not tags.empty else None
        tracked_from = tracked["date"].min() if tracked is not None else None
    else:
        tracked_from = sub["start"].min().date() if not sub.empty else None

    first = shift_periods(period, cur, -history)
    if tracked_from is not None:
        first = max(first, period_start(period, tracked_from))
    if tracked_from is None or first >= cur:
        values = pd.Series([], dtype=float, index=pd.Index([], name="period_start"))
    else:
        values = _goal_history(goal, sub, period, first, cur - timedelta(days=1))
    hist = pd.DataFrame({"value": values.astype(float)})
    hist.index.name = "period_start"
    hist["met"] = [goal_met(goal, v) for v in hist["value"]]

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
# `weight` is how much an occurrence counts: 1, or a tag group's weight
# for that tag (e.g. a Doppio counting 2 in ☕ Coffee).
TAG_COLUMNS = ["tag", "calendar", "date", "hour", "weekday", "all_day", "start", "weight"]


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
    occ["weight"] = 1.0
    return occ[TAG_COLUMNS].reset_index(drop=True)


def _longest_gap(dates, start_date, end_date):
    """Longest run of days in [start_date, end_date] without any of `dates`."""
    if end_date < start_date:
        return 0
    points = [start_date - timedelta(days=1)] + sorted(set(dates)) + [end_date + timedelta(days=1)]
    return max((b - a).days - 1 for a, b in zip(points, points[1:]))


DAYS_PER_MONTH = 365.25 / 12  # average month length for "per month" averages
TAG_AVERAGES = {"day": "Avg / day", "week": "Avg / week", "month": "Avg / month"}


def tag_summary(occ, start_date, end_date, today):
    """Per-tag statistics for the inclusive date range.

    `Days since last` is counted up to `today` over all data, so it reads as
    the current streak without the tag (e.g. 12 alcohol-free days).
    """
    columns = ["Tag", "Total", "Avg / day", "Avg / week", "Avg / month", "Days with tag", "% of days",
               "Last seen", "Days since last", "Longest gap (days)"]
    in_range = occ[(occ["date"] >= start_date) & (occ["date"] <= end_date)]
    if in_range.empty:
        return pd.DataFrame(columns=columns)
    days = (end_date - start_date).days + 1
    gap_end = min(end_date, today)
    # Totals add up weights; a day counts as "with tag" if its weights add
    # up to more than 0 (a day with only weight-0 tags is a day without).
    past = occ[(occ["date"] <= today) & (_weights(occ) > 0)]
    rows = []
    for tag, group in in_range.groupby("tag"):
        total = float(_weights(group).sum())
        per_day = _weights(group).groupby(group["date"]).sum()
        day_dates = per_day[per_day > 0].index
        seen = past.loc[past["tag"] == tag, "date"]
        last = seen.max() if not seen.empty else None
        rows.append({
            "Tag": tag,
            "Total": int(total) if total == int(total) else round(total, 2),
            "Avg / day": round(total / days, 2),
            "Avg / week": round(total / (days / 7), 2),
            "Avg / month": round(total / (days / DAYS_PER_MONTH), 2),
            "Days with tag": len(day_dates),
            "% of days": round(len(day_dates) / days * 100, 1),
            "Last seen": last,
            "Days since last": (today - last).days if last is not None else None,
            "Longest gap (days)": _longest_gap(day_dates, start_date, gap_end),
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
    counts = _weights(in_range).groupby([keys.rename("period_start"), in_range["tag"]]).sum().unstack(fill_value=0)
    return counts.reindex(periods, fill_value=0).rename_axis("period_start")


# --- Tag groups ---

def _norm_tag(text):
    """Compare tags ignoring case and the emoji variation selector, so
    "☕" matches "☕️ Flat White"."""
    return str(text).replace("️", "").strip().casefold()


def _tag_keys(tag):
    """The names a keyword can match exactly: the full tag, the tag without
    its leading emoji, and the emoji alone ("☕️ Flat White" -> "☕ flat
    white", "flat white", "☕")."""
    keys = {_norm_tag(tag)}
    emoji = _leading_emoji(tag)
    if emoji:
        keys.add(_norm_tag(emoji))
        rest = str(tag).strip().split(" ", 1)
        if len(rest) == 2:
            keys.add(_norm_tag(rest[1]))
    return keys


def tag_group_members(group, tags):
    """Tags (from `tags`) that are listed in the group or match one of its
    keywords exactly (ignoring case). "Flat White" matches "☕️ Flat White"
    but not "☕️ Decaf Flat White"; an emoji keyword such as "☕" matches every
    tag starting with that emoji."""
    listed = {_norm_tag(t) for t in group.get("tags", [])}
    keywords = {_norm_tag(k) for k in group.get("keywords", []) if _norm_tag(k)}
    return {
        t for t in tags
        if _norm_tag(t) in listed or keywords & _tag_keys(t)
    }


def _weights(occ):
    """Weight of each occurrence (1 where the column is missing)."""
    if "weight" in occ:
        return occ["weight"].astype(float).fillna(1.0)
    return pd.Series(1.0, index=occ.index)


def tag_weight(group, tag):
    """How much `tag` counts in `group`: the weight whose key matches the tag
    like a keyword does (full name or name without the emoji, any case),
    else one keyed by its emoji alone, else 1."""
    weights = {_norm_tag(k): float(v) for k, v in (group.get("weights") or {}).items()}
    if not weights:
        return 1.0
    keys = _tag_keys(tag)
    emoji = _leading_emoji(tag)
    emoji_key = _norm_tag(emoji) if emoji else None
    for key in keys - {emoji_key}:
        if key in weights:
            return weights[key]
    if emoji_key in weights:
        return weights[emoji_key]
    return 1.0


def weighted_members(group, tags):
    """{tag: weight} of the group's members among `tags`."""
    return {t: tag_weight(group, t) for t in tag_group_members(group, tags)}


def member_text(members, limit=None):
    """"☕️ Flat White ×2, ☕ Coffee" from {tag: weight}; weight 1 is implicit."""
    names = sorted(members)
    shown = names if limit is None else names[:limit]
    text = ", ".join(t if members[t] == 1 else f"{t} ×{members[t]:g}" for t in shown)
    return text + (f" … ({len(names)})" if limit is not None and len(names) > limit else "")


def _resolve(groups, tags):
    """[(label, group, {member: weight})] for groups matching at least one of
    `tags`; see `resolve_tag_groups` for the labels."""
    tags = set(tags)
    out = []
    for group in groups:
        members = weighted_members(group, tags)
        if not members:
            continue
        label = group["name"]
        if label in tags and label not in members:
            label += " (group)"
        out.append((label, group, members))
    return out


def resolve_tag_groups(groups, tags):
    """{display label: {member tag: weight}} for groups matching at least one
    of `tags`. A group named like a real tag that isn't one of its members
    is labelled "<name> (group)" so the two never merge."""
    return {label: members for label, _, members in _resolve(groups, tags)}


def apply_tag_groups(occ, groups, mode="grouped"):
    """Relabel tag occurrences by group.

    - "tags": unchanged.
    - "groups": one copy of each member occurrence per group, labelled with
      the group name (a tag in two groups counts in both).
    - "grouped": the groups plus every tag that is in no group, as itself.

    A group whose name equals a real tag that isn't one of its members gets
    " (group)" appended so the two don't merge.
    """
    if mode == "tags" or not groups or occ.empty:
        return occ
    parts, grouped_tags = [], set()
    for label, _, members in _resolve(groups, occ["tag"].unique()):
        grouped_tags |= set(members)
        part = occ[occ["tag"].isin(members)].copy()
        part["weight"] = part["tag"].map(members).astype(float)
        part["tag"] = label
        parts.append(part)
    if mode == "grouped":
        parts.append(occ[~occ["tag"].isin(grouped_tags)])
    if not parts:
        return occ.iloc[0:0]
    return pd.concat(parts, ignore_index=True)


def _leading_emoji(tag):
    """First token of a tag if it holds no letters or digits (an emoji)."""
    first = str(tag).strip().split(" ", 1)[0].replace("️", "")
    return first if first and not any(ch.isalnum() for ch in first) else None


def suggest_tag_groups(tags, existing=()):
    """One keyword group per leading emoji shared by at least two tags,
    skipping emojis already used as a keyword by an existing group."""
    used = {_norm_tag(k) for g in existing for k in g.get("keywords", [])}
    by_emoji = {}
    for tag in sorted(tags):
        emoji = _leading_emoji(tag)
        if emoji:
            by_emoji.setdefault(emoji, []).append(tag)
    return [
        {"name": f"{emoji} group", "tags": [], "keywords": [emoji]}
        for emoji, members in by_emoji.items()
        if len(members) >= 2 and _norm_tag(emoji) not in used
    ]


def _tag_text(tag):
    """Tag name without its leading emoji ("☕️ Flat White" -> "Flat White")."""
    parts = str(tag).strip().split(" ", 1)
    return parts[1].strip() if _leading_emoji(tag) and len(parts) == 2 else str(tag).strip()


def suggest_tag_groups_by_name(tags, existing=()):
    """One group per word or phrase that appears (as whole words) in the
    names of at least two tags, e.g. "Decaf" or "Flat White".

    Phrases with the same members collapse into the longest one ("Flat",
    "White" and "Flat White" -> "Flat White"). Keywords are the members'
    exact names, so the groups work with exact keyword matching and are easy
    to edit. Suggestions whose name or members match an existing group are
    skipped.
    """
    tags = sorted(set(tags))
    phrases = {}  # normalised phrase -> (display phrase, set of tags)
    for tag in tags:
        words = _tag_text(tag).split()
        for n in range(1, len(words) + 1):
            for i in range(len(words) - n + 1):
                phrase = " ".join(words[i:i + n])
                key = _norm_tag(phrase)
                if not any(ch.isalpha() for ch in key):
                    continue
                phrases.setdefault(key, (phrase, set()))[1].add(tag)

    best = {}  # frozenset(members) -> display phrase
    for display, members in phrases.values():
        if len(members) < 2:
            continue
        key = frozenset(members)
        current = best.get(key)
        if current is None or (len(display.split()), len(display), display) > (len(current.split()), len(current), current):
            best[key] = display

    existing_names = {_norm_tag(g.get("name", "")) for g in existing}
    existing_sets = {frozenset(tag_group_members(g, tags)) for g in existing}
    suggestions = []
    for members, display in sorted(best.items(), key=lambda kv: (-len(kv[0]), kv[1].casefold())):
        if _norm_tag(display) in existing_names or members in existing_sets:
            continue
        suggestions.append({
            "name": display,
            "tags": [],
            "keywords": sorted({_tag_text(t) for t in members}, key=str.casefold),
        })
    return suggestions


# --- Tag display settings ---

def _variant_key(tag):
    """Tag name without the emoji variation selector and with single spaces,
    so "☕ Coffee" and "☕️  Coffee" compare equal (case is kept)."""
    return " ".join(str(tag).replace("️", "").split())


def merge_tag_variants(occ):
    """Relabel tags that differ only in the emoji variation selector or in
    spacing with their most common spelling."""
    if occ.empty:
        return occ
    counts = occ["tag"].value_counts()
    canonical = {}
    for tag in sorted(counts.index, key=lambda t: (-counts[t], t)):
        canonical.setdefault(_variant_key(tag), tag)
    out = occ.copy()
    out["tag"] = out["tag"].map(lambda t: canonical[_variant_key(t)])
    return out


def drop_tags(occ, hidden):
    """Occurrences without the `hidden` tags (compared like merge_tag_variants)."""
    keys = {_variant_key(t) for t in hidden or ()}
    if occ.empty or not keys:
        return occ
    return occ[~occ["tag"].map(_variant_key).isin(keys)]


def default_chart_labels(by_count, mode="top", top_n=8, fixed=(), fill=True):
    """Labels preselected for the charts.

    - "top": the `top_n` labels with the highest count (`by_count` is sorted).
    - "fixed": the `fixed` labels in their saved order, skipping those with no
      entries. A group whose label got " (group)" appended still matches.
      With `fill`, skipped ones are replaced by the top remaining labels so
      the charts show as many lines as were picked.
    """
    if mode != "fixed" or not fixed:
        return list(by_count[:top_n])
    available = set(by_count)
    picked = []
    for name in fixed:
        label = name if name in available else f"{name} (group)"
        if label in available and label not in picked:
            picked.append(label)
    if fill:
        rest = [label for label in by_count if label not in picked]
        picked += rest[:max(0, len(fixed) - len(picked))]
    return picked


# --- Free-slot suggestions for goals ---
#
# Blocked time = events of blocking calendars (busy only, or also events
# marked as free, per calendar) + fixed weekly blocked times + sleep (from a
# sleep calendar or from "going to bed" / "waking up" events). Suggestions
# fill the free gaps that remain, preferring the weekdays and hours the goal
# usually happens at.

WEEKDAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_GROUPS = {
    "Every day": WEEKDAY_ABBR,
    "Weekdays": WEEKDAY_ABBR[:5],
    "Weekend": WEEKDAY_ABBR[5:],
}


def schedule_days(day):
    """Weekday abbreviations of a schedule row's day ("Mon", "Weekdays", ...)."""
    return DAY_GROUPS.get(day, [day] if day in WEEKDAY_ABBR else [])


def _minutes(text):
    """Minutes after midnight of "HH:MM"."""
    h, m = str(text).split(":")[:2]
    return int(h) * 60 + int(m)


def _local_time(d, minutes, tz):
    """Timestamp `minutes` after midnight of date `d` in `tz` (DST-safe)."""
    ts = pd.Timestamp(d) + pd.Timedelta(minutes=minutes)
    return ts.tz_localize(tz, nonexistent="shift_forward", ambiguous=False) if tz is not None else ts


def merge_intervals(intervals):
    """Sorted, merged (start, end) intervals; touching ones join."""
    merged = []
    for s, e in sorted(i for i in intervals if i[1] > i[0]):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def schedule_intervals(lo, hi, rows, tz):
    """Intervals of the weekly blocked times overlapping [lo, hi).

    A row is {"day": "Mon" | "Weekdays" | "Weekend" | "Every day",
    "from": "08:00", "to": "10:00"}. When "to" is not after "from" the block
    runs past midnight and belongs to the day it starts on (22:00-07:00).
    """
    out = []
    d = (lo - DAY).date()
    while d <= hi.date():
        for row in rows:
            if WEEKDAY_ABBR[d.weekday()] not in schedule_days(row.get("day")):
                continue
            f, t = _minutes(row["from"]), _minutes(row["to"])
            start = _local_time(d, f, tz)
            end = _local_time(d + timedelta(days=1), t, tz) if t <= f else _local_time(d, t, tz)
            if end > lo and start < hi:
                out.append((max(start, lo), min(end, hi)))
        d += timedelta(days=1)
    return out


def _is_free(values):
    return values.map(lambda v: str(v).strip().lower() == "true")


def busy_intervals(df, lo, hi, rule_for, all_day_blocks=False, buffer_minutes=0):
    """Merged busy time in [lo, hi).

    `rule_for(calendar)` returns None (calendar doesn't block), "busy" (only
    events marked busy block) or "all" (events marked as free block too).
    All-day events only block with `all_day_blocks`; zero-length events never
    do. Each interval is widened by `buffer_minutes` on both sides.
    """
    if df.empty:
        return []
    sub = df[(df["start"] < hi) & (df["end"] > lo) & (df["end"] > df["start"])]
    if sub.empty:
        return []
    rules = sub["calendar"].map(rule_for)
    free = _is_free(sub["free"]) if "free" in sub else pd.Series(False, index=sub.index)
    keep = rules.notna() & ((rules == "all") | ~free)
    if not all_day_blocks:
        keep &= ~is_all_day(sub["start"], sub["end"])
    sub = sub[keep]
    buf = pd.Timedelta(minutes=buffer_minutes)
    return merge_intervals([(s - buf, e + buf) for s, e in zip(sub["start"], sub["end"])])


def free_gaps(lo, hi, blocked):
    """Gaps in [lo, hi) not covered by the merged `blocked` intervals."""
    gaps, cursor = [], lo
    for s, e in blocked:
        if e <= cursor:
            continue
        if s >= hi:
            break
        if s > cursor:
            gaps.append((cursor, min(s, hi)))
        cursor = max(cursor, e)
    if cursor < hi:
        gaps.append((cursor, hi))
    return gaps


# --- Sleep ---

def _contains(names, text):
    text = str(text or "").strip().casefold()
    if not text:
        return pd.Series(False, index=names.index)
    return names.fillna("").astype(str).str.casefold().str.contains(text, regex=False)


def _next_wake_time(bed, wake_times, tz):
    """First fixed wake-up time ({"Mon": "07:00", ...}) after `bed`."""
    for days in range(0, 3):
        d = (bed + pd.Timedelta(days=days)).date()
        text = (wake_times or {}).get(WEEKDAY_ABBR[d.weekday()])
        if not text:
            continue
        wake = _local_time(d, _minutes(text), tz)
        if wake > bed:
            return wake
    return None


def sleep_from_markers(events, bed_text, wake_text=None, wake_times=None, tz=None, max_hours=16, default_hours=8):
    """(bed, wake) pairs from "going to bed" events, ended by the next
    "waking up" event within `max_hours`, else the next fixed wake-up time
    of that day (`wake_times`), else `default_hours` later."""
    if events.empty:
        return []
    names = events["event_name"]
    beds = events.loc[_contains(names, bed_text), "start"].sort_values()
    wakes = events.loc[_contains(names, wake_text), "start"].sort_values() if wake_text else pd.Series(dtype=object)
    wake_list = list(wakes)
    pairs = []
    for bed in beds:
        wake = next((w for w in wake_list if bed < w <= bed + pd.Timedelta(hours=max_hours)), None)
        if wake is None and wake_times:
            wake = _next_wake_time(bed, wake_times, tz)
        if wake is None:
            wake = bed + pd.Timedelta(hours=default_hours)
        pairs.append((bed, wake))
    return pairs


def sleep_from_calendar(events):
    """(start, end) of every timed event of a sleep calendar."""
    if events.empty:
        return []
    sub = events[(events["end"] > events["start"]) & ~is_all_day(events["start"], events["end"])]
    return sorted(zip(sub["start"], sub["end"]))


def _night_of(ts):
    """The date of the evening a sleep belongs to (before noon = night before)."""
    return (ts - pd.Timedelta(hours=12)).date()


NIGHT_MIN_HOURS = 3  # shorter sleeps are naps: they block, but aren't the night


def _is_night(bed, wake):
    return wake - bed >= pd.Timedelta(hours=NIGHT_MIN_HOURS)


SLEEP_MIN_NIGHTS = 3  # nights a weekday needs before its own median is used
WEEKEND_NIGHTS = (4, 5)  # Friday and Saturday evenings


def _noon(d, tz):
    return _local_time(d, 12 * 60, tz)


def usual_sleep(pairs, now, days=28, tz=None):
    """Usual bedtime and wake-up time per night, from the nights (not naps)
    of the last `days` before `now`, or of the latest `days` with sleep data
    if there are none.

    Returns {evening weekday: (bed, wake)} with both as minutes after noon of
    the evening (an 07:00 wake-up is 1140), or {} without data. Each value is
    the median of that weekday if it has at least SLEEP_MIN_NIGHTS nights,
    else of the weekday or weekend nights (Fri/Sat evenings), else of all.
    """
    nights = sorted((b, w) for b, w in pairs if b < now and _is_night(b, w))
    if not nights:
        return {}
    recent = [(b, w) for b, w in nights if b >= now - pd.Timedelta(days=days)]
    if not recent:
        last = nights[-1][0]
        recent = [(b, w) for b, w in nights if b >= last - pd.Timedelta(days=days)]
    tz = tz if tz is not None else recent[0][0].tz
    rows = []
    for b, w in recent:
        noon = _noon(_night_of(b), tz)
        rows.append({"weekday": _night_of(b).weekday(),
                     "bed": (b - noon).total_seconds() / 60, "wake": (w - noon).total_seconds() / 60})
    rows = pd.DataFrame(rows)
    rows["weekend"] = rows["weekday"].isin(WEEKEND_NIGHTS)

    def median_of(sub):
        return float(sub["bed"].median()), float(sub["wake"].median())

    usual = {}
    for wd in range(7):
        own = rows[rows["weekday"] == wd]
        group = rows[rows["weekend"] == (wd in WEEKEND_NIGHTS)]
        sub = own if len(own) >= SLEEP_MIN_NIGHTS else group if len(group) >= SLEEP_MIN_NIGHTS else rows
        usual[wd] = median_of(sub)
    return usual


def usual_night(d, usual, tz, wake_times=None):
    """(bed, wake) of the evening of date `d` from `usual_sleep`; a fixed
    wake-up time (`wake_times`) replaces the usual one."""
    bed_min, wake_min = usual[d.weekday()]
    bed = _local_time(d, 12 * 60 + bed_min, tz)
    wake = _next_wake_time(bed, wake_times, tz) if wake_times else None
    if wake is None or wake - bed > pd.Timedelta(hours=16):
        wake = _local_time(d, 12 * 60 + wake_min, tz)
    return bed, wake


def sleep_intervals(pairs, lo, hi, tz, now, winddown_minutes=0, fallback=True, wake_times=None, plan=None):
    """Blocked sleep time in [lo, hi): every (bed, wake) pair, starting
    `winddown_minutes` before bed. With `fallback`, nights without data use
    the usual bedtime and wake-up time of that night (`usual_sleep`), ending
    at the fixed wake-up time instead if `wake_times` has one. Nights in
    `plan` ({night: (bed, wake)}, see `sleep_plan`) without a recorded sleep
    use the planned night, with or without `fallback`.

    Returns (intervals, nights) where nights lists (night date, bed, wake,
    source) for display; source is "event", "plan" or "usual".
    """
    winddown = pd.Timedelta(minutes=winddown_minutes)
    sleeps = [(_night_of(b), b, w, "event") for b, w in pairs
              if w + winddown > lo - DAY and b - winddown < hi + DAY]
    covered = {d for d, b, w, _ in sleeps if _is_night(b, w)}
    usual = usual_sleep(pairs, now, tz=tz) if fallback else {}
    plan = plan or {}
    if usual or plan:
        d = (lo - DAY).date()
        while d <= hi.date():
            if d not in covered:
                if d in plan:
                    sleeps.append((d, *plan[d], "plan"))
                elif usual:
                    bed, wake = usual_night(d, usual, tz, wake_times)
                    sleeps.append((d, bed, wake, "usual"))
            d += timedelta(days=1)
    intervals = [(max(bed - winddown, lo), min(wake, hi)) for _, bed, wake, _ in sleeps
                 if wake > lo and bed - winddown < hi]
    return merge_intervals(intervals), sorted(sleeps)


GO_TO_BED_UID_PREFIX = "go-to-bed-"
SLEEP_ESTIMATES = {
    "usual": "My usual sleep (last 4 weeks)",
    "plan": "My bedtime plan, else my usual sleep",
    "plan_wake": "Usual bedtime, planned wake-up time",
    "off": "Don't estimate",
}


def is_go_to_bed(events):
    """Whether `events` come from go_to_bed (UIDs go-to-bed-bed-… / -wake-…)."""
    if events.empty or "uid" not in events:
        return False
    return bool(events["uid"].astype(str).str.startswith(GO_TO_BED_UID_PREFIX).any())


_IN_BED = re.compile(r"\((\d+):(\d{2}) in bed\)")


def sleep_plan(events):
    """Planned nights of a go_to_bed calendar: {night date: (bed, wake)}.

    Bedtime is the start of a "go-to-bed-bed-<wake day>" event, wake-up the
    start of the "go-to-bed-wake-<wake day>" event of the same wake day, or
    bedtime plus the "(H:MM in bed)" of its title without one."""
    if events.empty or "uid" not in events:
        return {}
    uid = events["uid"].astype(str)
    beds = events[uid.str.startswith(GO_TO_BED_UID_PREFIX + "bed-")]
    wakes = events[uid.str.startswith(GO_TO_BED_UID_PREFIX + "wake-")]
    wake_of = {u.split("@")[0].rsplit("-wake-", 1)[-1]: t for u, t in zip(wakes["uid"].astype(str), wakes["start"])}
    plan = {}
    for u, bed, title in zip(beds["uid"].astype(str), beds["start"], beds["event_name"].astype(str)):
        wake = wake_of.get(u.split("@")[0].rsplit("-bed-", 1)[-1])
        if wake is None:
            m = _IN_BED.search(title)
            if not m:
                continue
            wake = bed + pd.Timedelta(hours=int(m.group(1)), minutes=int(m.group(2)))
        if bed < wake <= bed + pd.Timedelta(hours=16):
            plan[_night_of(bed)] = (bed, wake)
    return plan


def expected_sleep(pairs, lo, hi, now, tz, wake_times=None, plan=None, mode="usual"):
    """Estimated sleep in [lo, hi) that isn't recorded yet.

    Every night after the last recorded sleep that ended before `now` gets
    an estimate, counted in full: sleep trackers add a night only after
    waking, so last night may still be missing. `mode` (SLEEP_ESTIMATES):
    "usual" = usual bedtime and wake-up time (`usual_sleep`); "plan" = the
    planned night from `plan` ({night: (bed, wake)}, see `sleep_plan`) where
    there is one, else usual; "plan_wake" = usual bedtime, planned wake-up
    time; "off" = no estimate. Returns (hours, nights) with nights as
    (night date, bed, wake, source) and source "usual" or "plan".
    """
    if mode == "off":
        return 0.0, []
    plan = (plan or {}) if mode in ("plan", "plan_wake") else {}
    usual = usual_sleep(pairs, now, tz=tz)
    if not usual and not plan:
        return 0.0, []
    past = [w for b, w in pairs if w <= now]
    last_end = max(past) if past else None
    covered = {_night_of(b) for b, w in pairs if _is_night(b, w)}
    recorded = merge_intervals(list(pairs))
    hours, nights = 0.0, []
    d = (lo - DAY).date()
    while d < hi.date():
        night = None
        if d not in covered:
            if d in plan and mode == "plan":
                night = (*plan[d], "plan")
            elif usual:
                bed, wake = usual_night(d, usual, tz, wake_times)
                if d in plan and plan[d][1] > bed + pd.Timedelta(hours=NIGHT_MIN_HOURS):
                    night = (bed, plan[d][1], "plan")
                else:
                    night = (bed, wake, "usual")
        if night is not None:
            bed, wake, source = night
            if (last_end is None or bed >= last_end) and wake > lo and bed < hi:
                parts = [(max(bed, lo), min(wake, hi))]
                for rs, re_ in recorded:
                    parts = [p for s, e in parts for p in _subtract([(s, e)], rs, re_)]
                part_hours = sum((e - s).total_seconds() for s, e in parts) / 3600
                if part_hours > 0:
                    hours += part_hours
                    nights.append((d, bed, wake, source))
        d += timedelta(days=1)
    return hours, nights


_ASLEEP = re.compile(r"^\s*(?:Sleep|Nap)\s*:\s*(\d+):(\d{2})", re.IGNORECASE)


def asleep_factors(events):
    """Share of each sleep event's time in bed spent asleep, from titles like
    "Sleep: 7:17\\nTib: 7:47" (oura-sleep-ical); 1.0 where the title has no
    sleep time. Returned as a Series aligned with `events`."""
    tib = (events["end"] - events["start"]).dt.total_seconds() / 3600
    asleep = events["event_name"].astype(str).str.extract(_ASLEEP).astype(float)
    hours = asleep[0] + asleep[1] / 60
    factor = (hours / tib.where(tib > 0)).clip(upper=1.0)
    return factor.fillna(1.0)


def usual_efficiency(events, now, days=28):
    """Median asleep share of the nights (not naps) of the last `days` (or the
    latest `days` with data) of sleep `events`; 1.0 without data."""
    nights = events[(events["start"] < now) & (events["end"] - events["start"] >= pd.Timedelta(hours=NIGHT_MIN_HOURS))]
    if nights.empty:
        return 1.0
    recent = nights[nights["start"] >= now - pd.Timedelta(days=days)]
    if recent.empty:
        recent = nights[nights["start"] >= nights["start"].max() - pd.Timedelta(days=days)]
    return float(asleep_factors(recent).median())


def usual_sleep_text(pairs, now, tz):
    """"in bed ~23:10, up ~07:00 on weeknights · ~00:20–08:30 on weekends"
    style summary of `usual_sleep`, or "" without data."""
    usual = usual_sleep(pairs, now, tz=tz)
    if not usual:
        return ""

    def clock(minutes):
        m = int(round(minutes)) + 12 * 60
        return f"{m // 60 % 24:02d}:{m % 60:02d}"

    def span(wds):
        bed = sum(usual[w][0] for w in wds) / len(wds)
        wake = sum(usual[w][1] for w in wds) / len(wds)
        return f"~{clock(bed)}–{clock(wake)}"

    week, weekend = span([0, 1, 2, 3, 6]), span(list(WEEKEND_NIGHTS))
    if week == weekend:
        return f"{week} every night"
    return f"{week} on weeknights (Sun–Thu) · {weekend} on Fri/Sat nights"


OURA_ACTIVITY_UID_PREFIXES = ("oura-workout-", "oura-session-", "oura-daily-activity-", "oura-activity-summary-")


def is_oura_activity(events):
    """Whether `events` (of one calendar) come from oura-activity-ical,
    recognised by its UIDs (oura-workout-…, oura-daily-activity-…)."""
    if events.empty or "uid" not in events:
        return False
    return bool(events["uid"].astype(str).str.startswith(OURA_ACTIVITY_UID_PREFIXES).any())


def usual_daily_hours(sub, now, days=28, min_days=3):
    """Average hours per weekday of `sub` (timed events) over the last `days`
    complete days before today, or the latest `days` up to the last day with
    data if there's none recently. Days without events count as 0; a weekday
    with fewer than `min_days` days uses the average of all days.
    Returns {weekday: hours} or {} without data."""
    today = now.date()
    past = sub[sub["start"] < local_midnight(today, now.tz)]
    if past.empty:
        return {}
    dates = past["start"].dt.date
    last = today - timedelta(days=1)
    if (dates >= today - timedelta(days=days)).sum() == 0:
        last = dates.max()
    first = max(last - timedelta(days=days - 1), dates.min())
    per_day = past[(dates >= first) & (dates <= last)].groupby(dates)["duration_hours"].sum()
    all_days = pd.Series(0.0, index=pd.Index([first + timedelta(days=i) for i in range((last - first).days + 1)]))
    all_days = all_days.add(per_day, fill_value=0)
    weekday = pd.Series([d.weekday() for d in all_days.index], index=all_days.index)
    overall = float(all_days.mean())
    usual = {}
    for wd in range(7):
        vals = all_days[weekday == wd]
        usual[wd] = float(vals.mean()) if len(vals) >= min_days else overall
    return usual


def expected_daily(sub, first, end, now, days=28):
    """Estimated hours of `sub` (timed events, recorded after the fact) for
    the days from today up to date `end` (exclusive), within the period
    starting at date `first`: each future day gets its usual hours
    (`usual_daily_hours`), today its usual hours minus what's recorded today.
    Returns (hours, usual) with usual = {weekday: hours} ({} without data)."""
    usual = usual_daily_hours(sub, now, days)
    if not usual:
        return 0.0, {}
    today = now.date()
    tz = now.tz
    hours = 0.0
    d = max(first, today)
    while d < end:
        expected = usual[d.weekday()]
        if d == today:
            done_today = clip_between(sub, local_midnight(today, tz), now)["duration_hours"].sum()
            expected = max(0.0, expected - float(done_today))
        hours += expected
        d += timedelta(days=1)
    return hours, usual


# --- Goal preferences and slot picking ---

def goal_time_preferences(df, goal, now, weeks=26):
    """When a time goal's events usually happen: counts per weekday, hour and
    (weekday, hour) over the last `weeks`, plus the median session length."""
    sub = goal_events(df, goal)
    sub = sub[(sub["start"] < now) & (sub["start"] >= now - pd.Timedelta(weeks=weeks))]
    if not sub.empty:
        sub = sub[~is_all_day(sub["start"], sub["end"]) & (sub["end"] > sub["start"])]
    minutes = (sub["end"] - sub["start"]).dt.total_seconds() / 60
    return {
        "n": len(sub),
        "weekday": sub["start"].dt.weekday.value_counts().to_dict(),
        "hour": sub["start"].dt.hour.value_counts().to_dict(),
        "both": sub.groupby([sub["start"].dt.weekday, sub["start"].dt.hour]).size().to_dict(),
        "session_minutes": float(minutes.median()) if len(sub) else None,
    }


def _near(counts, key, hour):
    return sum(counts.get(key(h), 0) for h in (hour - 1, hour, hour + 1))


def _slot_score(start, prefs):
    """Higher when the goal usually happens on this weekday around this hour."""
    n = prefs["n"]
    if not n:
        # No history: prefer daytime over the first free minute after waking.
        return 0.05 if 9 <= start.hour < 21 else 0.0
    wd, h = start.weekday(), start.hour
    both = _near(prefs["both"], lambda x: (wd, x), h)
    hour = _near(prefs["hour"], lambda x: x, h)
    return (3 * both + hour + prefs["weekday"].get(wd, 0)) / n


def _slot_reason(start, prefs):
    n = prefs["n"]
    if not n:
        return "Free (no history for this goal yet)"
    wd, h = start.weekday(), start.hour
    reasons = []
    if prefs["weekday"].get(wd, 0) / n >= 0.2:
        reasons.append(f"you often do this on {start.day_name()}s")
    if _near(prefs["hour"], lambda x: x, h) / n >= 0.2:
        reasons.append(f"usually around {h:02d}:00")
    return ("Free, " + " and ".join(reasons)) if reasons else "Free"


def _round_up(ts, step_minutes):
    floored = ts.floor(f"{step_minutes}min", nonexistent="shift_forward", ambiguous=False)
    return floored if floored == ts else floored + pd.Timedelta(minutes=step_minutes)


def _subtract(gaps, start, end):
    """`gaps` without [start, end)."""
    out = []
    for gs, ge in gaps:
        if end <= gs or start >= ge:
            out.append((gs, ge))
            continue
        if gs < start:
            out.append((gs, start))
        if end < ge:
            out.append((end, ge))
    return out


def suggest_slots(gaps, need_minutes, session_minutes, prefs, max_slots=3, min_minutes=30, step=15):
    """Up to `max_slots` non-overlapping slots in `gaps` covering
    `need_minutes`, preferring the goal's usual weekdays and hours and
    different days. Returns (slots, minutes still uncovered)."""
    slots, remaining = [], need_minutes
    while remaining > 1e-6 and len(slots) < max_slots:
        length = min(session_minutes, remaining)
        length = max(min_minutes, int(-(-length // step) * step))  # round up to the step
        span = pd.Timedelta(minutes=length)
        used_days = {s["start"].date() for s in slots}
        best = None
        for gs, ge in gaps:
            t = _round_up(gs, step)
            while t + span <= ge:
                score = _slot_score(t, prefs) - (10 if t.date() in used_days else 0)
                if best is None or score > best[0] + 1e-9:
                    best = (score, t)
                t += pd.Timedelta(minutes=step)
        if best is None:
            break
        start = best[1]
        slots.append({"start": start, "end": start + span, "minutes": length, "reason": _slot_reason(start, prefs)})
        remaining -= length
        gaps = _subtract(gaps, start, start + span)
    return slots, max(0.0, remaining)


def suggest_goal_slots(goal_df, goals, now, blocked_for, plan="current", min_minutes=30,
                       buffer_minutes=15, max_slots=3, step=15):
    """Free-slot suggestions for every time goal with a minimum it won't
    reach in the period.

    `goal_df` = events counted towards goals (as on the Goals page).
    `blocked_for(lo, hi)` returns the merged blocked intervals in [lo, hi).
    `plan` is "current" (rest of this day/week/month) or "next". Slots picked
    for one goal are no longer free for the following goals.
    """
    tz = goal_df["start"].dt.tz if not goal_df.empty else now.tz
    start_at = _round_up(now, step)
    buf = pd.Timedelta(minutes=buffer_minutes)
    taken, plans = [], []
    for goal in goals:
        if is_tag_goal(goal) or goal.get("min") is None:
            continue
        period = goal.get("period", "week")
        cur = period_start(period, now.date())
        if plan == "next":
            cur = next_period_start(period, cur)
        nxt = next_period_start(period, cur)
        p_lo, hi = local_midnight(cur, tz), local_midnight(nxt, tz)
        lo = max(p_lo, start_at)
        booked = goal_amount(goal, goal_events(goal_df, goal), cur, nxt, tz=tz)
        need = goal["min"] - booked
        if need <= 1e-6:
            continue
        gaps = []
        if lo < hi:
            blocked = merge_intervals(blocked_for(lo, hi) + [(s - buf, e + buf) for s, e in taken])
            gaps = [g for g in free_gaps(lo, hi, blocked) if g[1] - g[0] >= pd.Timedelta(minutes=min_minutes)]
        prefs = goal_time_preferences(goal_df, goal, now)
        session = prefs["session_minutes"] or 60
        session = min(120, max(min_minutes, round(session / step) * step))
        slots, missing = suggest_slots(gaps, need * 60, session, prefs, max_slots, min_minutes, step)
        taken += [(s["start"], s["end"]) for s in slots]
        plans.append({
            "goal": goal,
            "period_start": cur,
            "booked": booked,
            "need_hours": need,
            "slots": slots,
            "missing_hours": missing / 60,
            "free_hours": sum((b - a).total_seconds() for a, b in gaps) / 3600,
        })
    return plans


def _ics_text(text):
    return str(text).replace("\\", "\\\\").replace(";", "\;").replace(",", "\\,").replace("\n", "\\n")


def slots_to_ics(items, now=None):
    """iCalendar text with one event per (title, slot) pair, to import the
    suggestions into a calendar app."""
    import hashlib
    stamp = (now or pd.Timestamp.now(tz="UTC")).tz_convert("UTC").strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//CalendarTimeTracker//Free slots//EN",
             "CALSCALE:GREGORIAN"]
    for title, slot in items:
        start = slot["start"].tz_convert("UTC").strftime("%Y%m%dT%H%M%SZ")
        end = slot["end"].tz_convert("UTC").strftime("%Y%m%dT%H%M%SZ")
        uid = hashlib.md5(f"{title}|{start}|{end}".encode()).hexdigest()
        lines += ["BEGIN:VEVENT", f"UID:{uid}@calendartimetracker", f"DTSTAMP:{stamp}",
                  f"DTSTART:{start}", f"DTEND:{end}", f"SUMMARY:{_ics_text(title)}",
                  f"DESCRIPTION:{_ics_text('Suggested by CalendarTimeTracker: ' + slot['reason'])}",
                  "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# --- Focus & fragmentation ---

def focus_day(lo, hi, busy, off, focus_minutes=90, short_minutes=30):
    """How fragmented the awake time in [lo, hi) is.

    `busy` = merged busy intervals (events), `off` = merged sleep and weekly
    blocked times. Free time is awake time that isn't busy. Returns hours of
    awake, busy, free, focus (free blocks of at least `focus_minutes`) and
    fragmented free time, the longest free block, the number of short gaps
    (free blocks under `short_minutes` between two events) and the free
    blocks themselves as (start, end, "focus" | "fragment").
    """
    hours = lambda intervals: sum((b - a).total_seconds() for a, b in intervals) / 3600
    awake = free_gaps(lo, hi, off)
    gaps = free_gaps(lo, hi, merge_intervals(busy + off))
    focus_span = pd.Timedelta(minutes=focus_minutes)
    busy_ends = {e for _, e in busy}
    busy_starts = {s for s, _ in busy}
    short = [g for g in gaps if g[1] - g[0] < pd.Timedelta(minutes=short_minutes)
             and g[0] in busy_ends and g[1] in busy_starts]
    focus = [g for g in gaps if g[1] - g[0] >= focus_span]
    free_h, awake_h = hours(gaps), hours(awake)
    return {
        "awake_h": awake_h,
        "busy_h": awake_h - free_h,
        "free_h": free_h,
        "focus_h": hours(focus),
        "fragmented_h": free_h - hours(focus),
        "longest_h": max(((b - a).total_seconds() / 3600 for a, b in gaps), default=0.0),
        "short_gaps": len(short),
        "blocks": [(a, b, "focus" if b - a >= focus_span else "fragment") for a, b in gaps],
    }


def calendar_switches(events):
    """How often consecutive timed events (sorted by start) belong to a
    different calendar than the one before."""
    if len(events) < 2:
        return 0
    cals = events.sort_values("start")["calendar"].tolist()
    return sum(1 for a, b in zip(cals, cals[1:]) if a != b)


def focus_by_day(events, first, last, tz, busy_for, off_for, rule_for, focus_minutes=90, short_minutes=30):
    """One row of focus_day() metrics plus "switches" and "events" per day
    from date `first` to `last` (inclusive)."""
    timed = events[(events["end"] > events["start"]) & ~is_all_day(events["start"], events["end"])] \
        if not events.empty else events
    if not timed.empty:
        timed = timed[timed["calendar"].map(rule_for).notna()]
    rows = []
    d = first
    while d <= last:
        lo, hi = local_midnight(d, tz), local_midnight(d + timedelta(days=1), tz)
        off = off_for(lo, hi)
        busy = merge_intervals([(max(a, lo), min(b, hi)) for a, b in busy_for(lo, hi)])
        metrics = focus_day(lo, hi, busy, off, focus_minutes, short_minutes)
        day_events = timed[(timed["start"] >= lo) & (timed["start"] < hi)] if not timed.empty else timed
        if len(day_events) and off:
            # Only events while awake count as switches (not a sleep calendar).
            awake = free_gaps(lo, hi, off)
            inside = day_events["start"].map(lambda s: any(a <= s < b for a, b in awake))
            day_events = day_events[inside]
        metrics.update({"date": d, "switches": calendar_switches(day_events), "events": len(day_events)})
        rows.append(metrics)
        d += timedelta(days=1)
    return pd.DataFrame(rows)
