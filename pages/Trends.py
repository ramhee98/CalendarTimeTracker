import streamlit as st
from app import load_all_events, normalize_calendar_name, normalize_time, select_month_range
import pandas as pd
import altair as alt
from datetime import date

st.set_page_config(page_title="Trend Charts", layout="wide")
st.title("Calendar Trends Overview")
st.caption("Visualize how your calendar usage changes over time.")

# --- Load events ---
all_events, source_type = load_all_events()
if not all_events:
    st.warning("No events available.")
    st.stop()

df = pd.DataFrame(all_events)
df["calendar"] = df["calendar_name"].apply(normalize_calendar_name)
df = normalize_time(df, tz="local")

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
    df.groupby(["period", "calendar"])["duration_hours"]
    .sum()
    .reset_index()
)

# --- Optional: Show table
with st.expander("📋 Show trend data table"):
    pivot = trend_data.pivot(index="period", columns="calendar", values="duration_hours").fillna(0)
    st.dataframe(pivot)

# --- Chart ---
st.subheader("Total Duration Over Time by Calendar")
line_chart = alt.Chart(trend_data).mark_line(point=True).encode(
    x=alt.X("period:N", title=granularity, axis=alt.Axis(labelAngle=-45)),
    y=alt.Y("duration_hours:Q", title="Duration (hours)"),
    color=alt.Color("calendar:N", title="Calendar"),
    tooltip=["period:N", "calendar:N", "duration_hours:Q"]
).properties(width=800, height=400)

st.altair_chart(line_chart, use_container_width=True)
