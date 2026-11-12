from datetime import timedelta

import altair as alt
import pandas as pd
import streamlit as st

from analysis import WEEKDAY_ABBR, focus_by_day, is_all_day, local_midnight, period_label, period_start
from utils import build_events_df, load_app_settings, load_events_for_page, select_month_range, slot_parts

st.set_page_config(page_title="Focus", layout="wide")
st.title("🧩 Focus & Fragmentation")
st.caption(
    "How chopped up your days are, not just how full. Free time is awake time without events of blocking "
    "calendars; sleep and the weekly blocked times come from Settings → 💡 Free slots."
)

all_events, _ = load_events_for_page("focus")
# Every event counts here (long and free ones too): the free-slot settings
# decide what blocks time, not the data-accuracy rules.
raw = build_events_df(all_events, {"long_events": "include", "ignore_free_events": False})
settings = load_app_settings()
tz = raw["start"].dt.tz
now = pd.Timestamp.now(tz=tz)

c1, c2, c3 = st.columns([1, 1, 2])
focus_minutes = c1.selectbox("Focus block = free for at least", [45, 60, 90, 120, 180], index=2,
                             format_func=lambda m: f"{m} min")
short_minutes = c2.selectbox("Short gap = free for less than", [15, 30, 45, 60], index=1,
                             format_func=lambda m: f"{m} min")
weekdays = c3.multiselect("Days", WEEKDAY_ABBR, default=WEEKDAY_ABBR)

start_date, end_date = select_month_range(raw, months=3)
if not weekdays:
    st.info("Select at least one day.")
    st.stop()

parts = slot_parts(settings, now, time_events=raw, buffer_minutes=0)


def compute(first, last):
    days = focus_by_day(raw, first, last, tz, parts["busy"], parts["off"], parts["rule_for"],
                        focus_minutes, short_minutes)
    if days.empty:
        return days
    days["weekday"] = [WEEKDAY_ABBR[d.weekday()] for d in days["date"]]
    return days[days["weekday"].isin(weekdays)]


with st.spinner("Measuring your days..."):
    days = compute(start_date, end_date)
    span = end_date - start_date
    # The previous period of the same length, but never before the first event
    # (days without any data would look like pure focus time).
    prev_first = max(start_date - span - timedelta(days=1), raw["start"].min().date())
    prev_last = start_date - timedelta(days=1)
    previous = compute(prev_first, prev_last) if prev_first <= prev_last else pd.DataFrame()

if days.empty:
    st.info("No days in the selected range.")
    st.stop()

sleep_like = sorted(c for c in raw["calendar"].dropna().unique() if any(w in c.casefold() for w in ("sleep", "schlaf")))
if sleep_like and settings.get("slots_sleep_mode", "none") != "calendar":
    st.info(f"**{sleep_like[0]}** looks like a sleep calendar but counts as busy time here. Choose it under "
            "Settings → 💡 Free slots → Sleep comes from → A sleep calendar, so sleep isn't counted as busy.")
if (days["awake_h"] >= 23.9).mean() > 0.5:
    st.warning("Most days count as 24 hours awake, so night-time looks like focus time. Set your sleep or a "
               "night block in Settings → 💡 Free slots.")

# --- Headline ---
st.subheader("Per day on average")
metrics = [
    ("Focus time", "focus_h", "h", "normal", f"Free blocks of {focus_minutes} min or more"),
    ("Longest free block", "longest_h", "h", "normal", "The longest stretch without events while awake"),
    ("Fragmented free time", "fragmented_h", "h", "inverse", f"Free time in blocks under {focus_minutes} min"),
    ("Short gaps", "short_gaps", "", "inverse", f"Gaps under {short_minutes} min between two events"),
    ("Calendar switches", "switches", "", "inverse", "Consecutive events from different calendars"),
]
cols = st.columns(len(metrics))
for col, (label, key, unit, color, help_text) in zip(cols, metrics):
    value = days[key].mean()
    delta = None
    if not previous.empty:
        diff = value - previous[key].mean()
        delta = f"{diff:+.1f}{unit} vs previous period"
    col.metric(label, f"{value:.1f}{unit}", delta, delta_color=color, help=help_text)
st.caption(
    f"{len(days)} days from {start_date} to {end_date}; awake {days['awake_h'].mean():.1f}h and busy "
    f"{days['busy_h'].mean():.1f}h per day. Compared with the {len(previous)} days before."
)

# --- Weekly trend ---
st.subheader("Week by week")
days["week"] = [period_start("week", d) for d in days["date"]]
weekly = days.groupby("week").agg(focus_h=("focus_h", "sum"), fragmented_h=("fragmented_h", "sum"),
                                  switches=("switches", "mean"), short_gaps=("short_gaps", "mean")).reset_index()
weekly["label"] = [period_label("week", w) for w in weekly["week"]]
stack = weekly.melt(id_vars="label", value_vars=["focus_h", "fragmented_h"], var_name="kind", value_name="hours")
stack["kind"] = stack["kind"].map({"focus_h": f"Focus (≥ {focus_minutes} min)", "fragmented_h": "Fragmented"})
kind_scale = alt.Scale(domain=[f"Focus (≥ {focus_minutes} min)", "Fragmented"], range=["#2e7d32", "#f9a825"])
w1, w2 = st.columns([3, 2])
with w1:
    st.altair_chart(alt.Chart(stack).mark_bar().encode(
        x=alt.X("label:N", title="Week", sort=None, axis=alt.Axis(labelAngle=-45)),
        y=alt.Y("hours:Q", title="Free hours"),
        color=alt.Color("kind:N", title="", scale=kind_scale),
        tooltip=["label:N", "kind:N", alt.Tooltip("hours:Q", format=".1f")],
    ).properties(height=280), use_container_width=True)
with w2:
    lines = weekly.melt(id_vars="label", value_vars=["switches", "short_gaps"], var_name="kind", value_name="per day")
    lines["kind"] = lines["kind"].map({"switches": "Switches", "short_gaps": "Short gaps"})
    st.altair_chart(alt.Chart(lines).mark_line(point=True).encode(
        x=alt.X("label:N", title="Week", sort=None, axis=alt.Axis(labelAngle=-45)),
        y=alt.Y("per day:Q", title="Per day"),
        color=alt.Color("kind:N", title=""),
        tooltip=["label:N", "kind:N", alt.Tooltip("per day:Q", format=".1f")],
    ).properties(height=280), use_container_width=True)

# --- Weekday comparison ---
st.subheader("Which days are fragmented")
by_day = days.groupby("weekday").agg(focus_h=("focus_h", "mean"), fragmented_h=("fragmented_h", "mean"),
                                     busy_h=("busy_h", "mean"), switches=("switches", "mean"),
                                     short_gaps=("short_gaps", "mean"), longest_h=("longest_h", "mean"))
by_day = by_day.reindex([d for d in WEEKDAY_ABBR if d in by_day.index])
if len(by_day) > 1:
    best = by_day["focus_h"].idxmax()
    worst = (by_day["short_gaps"] + by_day["switches"]).idxmax()
    st.markdown(
        f"Best for focus: **{best}** ({by_day.loc[best, 'focus_h']:.1f}h of focus time). "
        f"Most fragmented: **{worst}** ({by_day.loc[worst, 'short_gaps']:.1f} short gaps and "
        f"{by_day.loc[worst, 'switches']:.1f} switches per day): worth protecting a block there."
    )
day_stack = by_day.reset_index().melt(id_vars="weekday", value_vars=["focus_h", "fragmented_h", "busy_h"],
                                      var_name="kind", value_name="hours")
day_stack["kind"] = day_stack["kind"].map({"focus_h": f"Focus (≥ {focus_minutes} min)",
                                           "fragmented_h": "Fragmented", "busy_h": "Busy"})
st.altair_chart(alt.Chart(day_stack).mark_bar().encode(
    x=alt.X("weekday:N", title="", sort=WEEKDAY_ABBR),
    y=alt.Y("hours:Q", title="Hours per day (awake)"),
    color=alt.Color("kind:N", title="", scale=alt.Scale(
        domain=[f"Focus (≥ {focus_minutes} min)", "Fragmented", "Busy"], range=["#2e7d32", "#f9a825", "#90a4ae"])),
    tooltip=["weekday:N", "kind:N", alt.Tooltip("hours:Q", format=".1f")],
).properties(height=280), use_container_width=True)

# --- Day timeline of one week ---
st.subheader("A week at a glance")
weeks = sorted({period_start("week", d) for d in days["date"]})
current = period_start("week", now.date())
week = st.selectbox("Week", weeks, index=weeks.index(current) if current in weeks else len(weeks) - 1,
                    format_func=lambda w: f"{period_label('week', w)} ({w:%d.%m.} – {w + timedelta(days=6):%d.%m.})")

segments = []
week_days = focus_by_day(raw, week, week + timedelta(days=6), tz, parts["busy"], parts["off"], parts["rule_for"],
                         focus_minutes, short_minutes)
timed = raw[(raw["end"] > raw["start"]) & ~is_all_day(raw["start"], raw["end"])]
timed = timed[timed["calendar"].map(parts["rule_for"]).notna()]
for _, row in week_days.iterrows():
    d = row["date"]
    lo, hi = local_midnight(d, tz), local_midnight(d + timedelta(days=1), tz)
    day_label = f"{d:%a %d.%m.}"
    hour = lambda ts: (ts - lo).total_seconds() / 3600
    for a, b in parts["off"](lo, hi):
        segments.append({"day": day_label, "from": hour(a), "to": hour(b), "what": "Sleep / blocked",
                         "detail": f"{a:%H:%M}–{b:%H:%M}"})
    for a, b, kind in row["blocks"]:
        segments.append({"day": day_label, "from": hour(a), "to": hour(b),
                         "what": "Focus block" if kind == "focus" else "Fragment",
                         "detail": f"{a:%H:%M}–{b:%H:%M} free"})
    for _, e in timed[(timed["start"] < hi) & (timed["end"] > lo)].iterrows():
        a, b = max(e["start"], lo), min(e["end"], hi)
        segments.append({"day": day_label, "from": hour(a), "to": hour(b), "what": "Busy",
                         "detail": f"{a:%H:%M}–{b:%H:%M} {e['calendar']}: {e['event_name']}"})
if segments:
    seg = pd.DataFrame(segments)
    order = list(dict.fromkeys(seg["day"]))
    st.altair_chart(alt.Chart(seg).mark_bar(height=18).encode(
        x=alt.X("from:Q", title="Hour of day", scale=alt.Scale(domain=[0, 24]), axis=alt.Axis(values=list(range(0, 25, 2)))),
        x2="to:Q",
        y=alt.Y("day:N", title="", sort=order),
        color=alt.Color("what:N", title="", scale=alt.Scale(
            domain=["Busy", "Focus block", "Fragment", "Sleep / blocked"],
            range=["#5c6bc0", "#2e7d32", "#f9a825", "#e0e0e0"])),
        tooltip=["day:N", "what:N", "detail:N"],
    ).properties(height=alt.Step(26)), use_container_width=True)
    st.caption("Hover a bar for the times. Busy shows events of blocking calendars, overlapping ones on top "
               "of each other.")

# --- Table ---
with st.expander("📋 Day by day"):
    table = days[["date", "weekday", "awake_h", "busy_h", "free_h", "focus_h", "fragmented_h", "longest_h",
                  "short_gaps", "switches", "events"]].copy()
    table.columns = ["Date", "Day", "Awake (h)", "Busy (h)", "Free (h)", "Focus (h)", "Fragmented (h)",
                     "Longest free (h)", "Short gaps", "Switches", "Events"]
    table = table.round(2)
    st.dataframe(table, hide_index=True, use_container_width=True)
    st.download_button("Download as CSV", table.to_csv(index=False).encode("utf-8"),
                       f"focus_{start_date}_{end_date}.csv", "text/csv")
