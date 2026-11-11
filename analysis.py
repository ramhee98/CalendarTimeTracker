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
    return tags[tags["tag"].isin(tag_group_members(group, tags["tag"].unique()))]


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
        return float(mask.sum())
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
    counts = sub["date"].map(lambda d: period_start(period, d)).value_counts()
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


def resolve_tag_groups(groups, tags):
    """{display label: sorted member tags} for groups matching at least one
    of `tags`. A group named like a real tag that isn't one of its members
    is labelled "<name> (group)" so the two never merge."""
    tags = set(tags)
    resolved = {}
    for group in groups:
        members = tag_group_members(group, tags)
        if not members:
            continue
        label = group["name"]
        if label in tags and label not in members:
            label += " (group)"
        resolved[label] = sorted(members)
    return resolved


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
    for label, members in resolve_tag_groups(groups, occ["tag"].unique()).items():
        grouped_tags |= set(members)
        part = occ[occ["tag"].isin(members)].copy()
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
