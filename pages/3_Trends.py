import streamlit as st
from app import load_all_events, normalize_calendar_name, normalize_time, select_month_range
import pandas as pd
import altair as alt
from datetime import date

st.set_page_config(page_title="Trend Charts", layout="wide")
st.title("Calendar Trends Overview")
st.caption("Visualize how your calendar or category usage changes over time.")

# Add performance improvements
if "trends_loaded" not in st.session_state:
    st.session_state.trends_loaded = False

# --- Cache management ---
col1, col2 = st.columns([3, 1])
with col2:
    if st.button("🔄 Refresh Data", help="Clear cache and reload data"):
        st.cache_data.clear()
        st.session_state.trends_loaded = False
        st.rerun()

# --- Load events ---
with st.spinner("Loading calendar data for trends analysis..."):
    all_events, source_type = load_all_events()
    if not all_events:
        st.warning("No events available.")
        st.stop()

# Mark as loaded
st.session_state.trends_loaded = True

with st.spinner("Processing calendar data..."):
    df = pd.DataFrame(all_events)
    df["calendar"] = df["calendar_name"].apply(normalize_calendar_name)
    df = normalize_time(df, tz="local")

# --- View selector like app.py, shown on top ---
if source_type == "json":
    group_mode = st.selectbox(
        "View data by",
        options=["calendar", "category"],
        index=0,
        format_func=str.title,
        key="trend_view_mode"
    )
else:
    group_mode = "calendar"

df["group"] = df[group_mode]

# --- Select date range after group mode ---
with st.spinner("Applying date filters..."):
    start_date, end_date = select_month_range(df)

    # Filter by date range
    df = df[
        ((df["start"].dt.date >= start_date) & (df["start"].dt.date <= end_date)) |
        ((df["end"].dt.date >= start_date) & (df["end"].dt.date <= end_date)) |
        ((df["start"].dt.date <= start_date) & (df["end"].dt.date >= end_date))
    ]

# --- Granularity selector ---
granularity = st.radio("Time granularity:", ["Month", "Week"], horizontal=True)

if granularity == "Month":
    df["period"] = df["start"].dt.to_period("M").astype(str)
else:
    df["period"] = df["start"].dt.to_period("W").apply(lambda r: r.start_time.date().strftime("%Y-%m-%d"))

# --- Aggregate durations ---
trend_data = (
    df.groupby(["period", "group"])["duration_hours"]
    .sum()
    .reset_index()
)

# --- Optional: Show table
with st.expander("📋 Show trend data table"):
    pivot = trend_data.pivot(index="period", columns="group", values="duration_hours").fillna(0)
    st.dataframe(pivot)

# --- Chart ---
group_label = group_mode.title()
st.subheader(f"Total Duration Over Time by {group_label}")
line_chart = alt.Chart(trend_data).mark_line(point=True).encode(
    x=alt.X("period:N", title=granularity, axis=alt.Axis(labelAngle=-45)),
    y=alt.Y("duration_hours:Q", title="Duration (hours)"),
    color=alt.Color("group:N", title=group_label),
    tooltip=["period:N", "group:N", "duration_hours:Q"]
).properties(width=800, height=400)

st.altair_chart(line_chart, use_container_width=True)
