from datetime import date

import altair as alt
import pandas as pd
import streamlit as st

from analysis import period_label, tag_counts_by_period, tag_occurrences, tag_summary
from utils import (
    load_calendar_urls, load_events_for_page, normalize_calendar_name, normalize_time, select_month_range,
)

st.set_page_config(page_title="Tags", layout="wide")
st.title("🏷️ Tags")
st.caption("Count markers from tag calendars, such as Oura tags (☕ Coffee, 🍺 Beer). "
           "Tag calendars are never counted as time.")

calendars, _ = load_calendar_urls()
tag_calendars = [c for c in (calendars or []) if c.get("type") == "tags"]
if not tag_calendars:
    st.info(
        "No tag calendars configured yet. Add a calendar of markers, for example from "
        "[oura-tags-ical](https://github.com/ramhee98/oura-tags-ical), and set its **Type** to `tags` "
        "on the Settings page (or add `\"type\": \"tags\"` to it in `calendars.json`)."
    )
    st.stop()

all_events, _ = load_events_for_page("tags", kind="tags", stop_if_empty=False)
if not all_events:
    st.warning("No tag events cached yet. Press 🔄 Sync Calendars to fetch them.")
    st.stop()

with st.spinner("Processing tags..."):
    df = pd.DataFrame(all_events)
    df["calendar"] = df["calendar_name"].apply(normalize_calendar_name)
    df = normalize_time(df, tz="local")
    occ = tag_occurrences(df)

if occ.empty:
    st.warning("The tag calendars contain no tag events.")
    st.stop()

if len(tag_calendars) > 1:
    names = sorted(occ["calendar"].unique())
    chosen = st.multiselect("Tag calendars", names, default=names)
    occ = occ[occ["calendar"].isin(chosen)]
    if occ.empty:
        st.info("Select at least one tag calendar.")
        st.stop()

start_date, end_date = select_month_range(occ)  # summary events excluded
today = date.today()
in_range = occ[(occ["date"] >= start_date) & (occ["date"] <= end_date)]
if in_range.empty:
    st.info("No tags in the selected date range.")
    st.stop()

summary = tag_summary(occ, start_date, end_date, today)
by_count = summary["Tag"].tolist()

c1, c2 = st.columns([3, 1])
with c1:
    selected = st.multiselect("Tags in the charts", by_count, default=by_count[:8])
with c2:
    granularity = st.radio("Count per", ["Day", "Week", "Month"], index=1, horizontal=True)

# --- Summary table ---
st.subheader("Summary")
shown = summary.copy()
shown["Last seen"] = [f"{d:%Y-%m-%d}" if d is not None and pd.notna(d) else "—" for d in shown["Last seen"]]
st.dataframe(
    shown,
    use_container_width=True,
    hide_index=True,
    column_config={
        "% of days": st.column_config.ProgressColumn("% of days", min_value=0, max_value=100, format="%.1f%%"),
        "Days since last": st.column_config.NumberColumn(
            "Days since last", help="Days since the tag was last logged, up to today: the current streak without it"),
        "Longest gap (days)": st.column_config.NumberColumn(
            "Longest gap (days)", help="Longest run of days without the tag inside the selected range"),
    },
)
st.download_button("Download summary as CSV", shown.to_csv(index=False).encode("utf-8"),
                   f"tags_{start_date}_{end_date}.csv", "text/csv")
st.caption(f"{len(in_range)} tags from {start_date} to {end_date}. A multi-day tag counts once, on its first day.")

if not selected:
    st.info("Select tags above to see their charts.")
    st.stop()

sel = in_range[in_range["tag"].isin(selected)]
tag_scale = alt.Scale(domain=selected)

# --- Trend ---
st.subheader(f"Count per {granularity.lower()}")
period = granularity.lower()
counts = tag_counts_by_period(sel, period, start_date, end_date)
counts = counts.reindex(columns=[t for t in selected if t in counts.columns], fill_value=0)
trend = counts.reset_index().melt(id_vars="period_start", var_name="tag", value_name="count")
if period == "day":
    trend["label"] = pd.to_datetime(trend["period_start"])
    x = alt.X("label:T", title="Day")
else:
    trend["label"] = [period_label(period, d) for d in trend["period_start"]]
    x = alt.X("label:N", title=granularity, sort=None, axis=alt.Axis(labelAngle=-45))
st.altair_chart(
    alt.Chart(trend).mark_line(point=True).encode(
        x=x,
        y=alt.Y("count:Q", title="Count"),
        color=alt.Color("tag:N", title="Tag", scale=tag_scale),
        tooltip=[alt.Tooltip("label", title=granularity), "tag:N", "count:Q"],
    ).properties(height=320).interactive(),
    use_container_width=True,
)
with st.expander("📋 Show counts table"):
    table = counts.copy()
    table.index = [d.strftime("%Y-%m-%d") if period == "day" else period_label(period, d) for d in table.index]
    st.dataframe(table, use_container_width=True)

# --- Weekday pattern ---
weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
st.subheader("Weekday pattern")
by_weekday = sel.groupby(["tag", "weekday"]).size().reset_index(name="count")
base = alt.Chart(by_weekday).encode(
    x=alt.X("weekday:N", sort=weekdays, title=""),
    y=alt.Y("tag:N", sort=selected, title=""),
)
st.altair_chart(
    (base.mark_rect().encode(color=alt.Color("count:Q", title="Count", scale=alt.Scale(scheme="oranges")),
                             tooltip=["tag:N", "weekday:N", "count:Q"])
     + base.mark_text(fontSize=11).encode(text="count:Q"))
    .properties(height=max(120, 32 * len(selected))),
    use_container_width=True,
)

# --- Time of day ---
st.subheader("Time of day")
timed = sel[~sel["all_day"]].dropna(subset=["hour"]).copy()
if timed.empty:
    st.caption("None of the selected tags has a time of day (all-day tags only).")
else:
    timed["hour"] = timed["hour"].astype(int)
    by_hour = timed.groupby(["tag", "hour"]).size().reset_index(name="count")
    base = alt.Chart(by_hour).encode(
        x=alt.X("hour:O", title="Hour of day", scale=alt.Scale(domain=list(range(24)))),
        y=alt.Y("tag:N", sort=selected, title=""),
    )
    st.altair_chart(
        (base.mark_rect().encode(color=alt.Color("count:Q", title="Count", scale=alt.Scale(scheme="blues")),
                                 tooltip=["tag:N", "hour:O", "count:Q"])
         + base.mark_text(fontSize=11).encode(text="count:Q"))
        .properties(height=max(120, 32 * timed["tag"].nunique())),
        use_container_width=True,
    )
    all_day_tags = sorted(set(sel.loc[sel["all_day"], "tag"]))
    st.caption("All-day tags have no time of day and are left out here"
               + (f": {', '.join(all_day_tags)}." if all_day_tags else "."))
