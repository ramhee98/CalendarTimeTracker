import streamlit as st
from utils import load_all_events, load_all_events_from_cache, normalize_calendar_name, normalize_time
import pandas as pd
import altair as alt
from datetime import date, timedelta

st.set_page_config(page_title="Compare Periods", layout="wide")
st.title("Compare Periods")
st.caption("Compare your time spending between two date ranges side by side.")

# --- Cache management ---
col1, col2, col3 = st.columns([2, 1, 1])
with col2:
    if st.button("⚡ Quick Load", help="Load instantly from local cache"):
        st.session_state.force_refresh_compare = False
        st.rerun()
with col3:
    if st.button("🔄 Sync Calendars", help="Fetch latest data from calendar URLs"):
        st.cache_data.clear()
        st.session_state.force_refresh_compare = True
        st.rerun()

# --- Load events ---
force_refresh = st.session_state.get("force_refresh_compare", False)
if force_refresh:
    st.session_state.force_refresh_compare = False
    with st.spinner("Fetching latest calendar data from URLs..."):
        all_events, source_type = load_all_events()
else:
    with st.spinner("Loading calendar data from cache..."):
        all_events, source_type = load_all_events_from_cache()
        if not all_events:
            with st.spinner("No cache found, fetching from URLs..."):
                all_events, source_type = load_all_events()

if not all_events:
    st.warning("No events available.")
    st.stop()

with st.spinner("Processing calendar data..."):
    df = pd.DataFrame(all_events)
    df["calendar"] = df["calendar_name"].apply(normalize_calendar_name)
    df = normalize_time(df, tz="local")

# --- View selector ---
if source_type == "json":
    group_mode = st.selectbox(
        "View data by",
        options=["calendar", "category"],
        index=0,
        format_func=str.title,
        key="compare_view_mode"
    )
else:
    group_mode = "calendar"

df["group"] = df[group_mode]

# --- Preset helpers ---
today = date.today()

presets = {
    "This week vs last week": {
        "a_start": today - timedelta(days=today.weekday(), weeks=1),
        "a_end": today - timedelta(days=today.weekday()) - timedelta(days=1),
        "b_start": today - timedelta(days=today.weekday()),
        "b_end": today,
    },
    "This month vs last month": {
        "a_start": (today.replace(day=1) - timedelta(days=1)).replace(day=1),
        "a_end": today.replace(day=1) - timedelta(days=1),
        "b_start": today.replace(day=1),
        "b_end": today,
    },
    "This month vs same month last year": {
        "a_start": today.replace(day=1, year=today.year - 1),
        "a_end": (today.replace(day=1, year=today.year - 1, month=today.month % 12 + 1)
                  if today.month < 12
                  else today.replace(day=31, year=today.year - 1, month=12)),
        "b_start": today.replace(day=1),
        "b_end": today,
    },
    "Custom": None,
}

preset = st.selectbox("Quick presets", list(presets.keys()), index=0)

min_date = df["start"].min().date()
max_date = df["start"].max().date()

st.subheader("Select Two Periods to Compare")
col_a, col_b = st.columns(2)

defaults = presets[preset]

with col_a:
    st.markdown("**Period A**")
    a_col1, a_col2 = st.columns(2)
    with a_col1:
        a_start = st.date_input(
            "Start",
            value=max(min_date, defaults["a_start"]) if defaults else min_date,
            min_value=min_date,
            max_value=max_date,
            key="a_start",
        )
    with a_col2:
        a_end = st.date_input(
            "End",
            value=min(max_date, defaults["a_end"]) if defaults else max_date,
            min_value=min_date,
            max_value=max_date,
            key="a_end",
        )

with col_b:
    st.markdown("**Period B**")
    b_col1, b_col2 = st.columns(2)
    with b_col1:
        b_start = st.date_input(
            "Start",
            value=max(min_date, defaults["b_start"]) if defaults else min_date,
            min_value=min_date,
            max_value=max_date,
            key="b_start",
        )
    with b_col2:
        b_end = st.date_input(
            "End",
            value=min(max_date, defaults["b_end"]) if defaults else max_date,
            min_value=min_date,
            max_value=max_date,
            key="b_end",
        )

if a_start > a_end or b_start > b_end:
    st.warning("Start date must be before end date for both periods.")
    st.stop()


# --- Filter data for each period ---
def filter_period(df, start, end):
    return df[
        ((df["start"].dt.date >= start) & (df["start"].dt.date <= end)) |
        ((df["end"].dt.date >= start) & (df["end"].dt.date <= end)) |
        ((df["start"].dt.date <= start) & (df["end"].dt.date >= end))
    ]


df_a = filter_period(df, a_start, a_end)
df_b = filter_period(df, b_start, b_end)

if df_a.empty and df_b.empty:
    st.info("No events found in either period.")
    st.stop()

# --- Summary metrics ---
st.subheader("Summary")

days_a = max((a_end - a_start).days, 1)
days_b = max((b_end - b_start).days, 1)

total_a = df_a["duration_hours"].sum()
total_b = df_b["duration_hours"].sum()
events_a = len(df_a)
events_b = len(df_b)

m1, m2, m3 = st.columns(3)
m1.metric("Total Hours", f"{total_b:.1f}h", delta=f"{total_b - total_a:+.1f}h vs Period A")
m2.metric("Total Events", events_b, delta=f"{events_b - events_a:+d} vs Period A")
m3.metric(
    "Avg Hours/Day",
    f"{total_b / days_b:.1f}h",
    delta=f"{total_b / days_b - total_a / days_a:+.1f}h vs Period A",
)

# --- Side-by-side bar chart ---
st.subheader(f"Hours by {group_mode.title()}")

agg_a = df_a.groupby("group")["duration_hours"].sum().reset_index()
agg_a["period"] = f"A: {a_start} → {a_end}"

agg_b = df_b.groupby("group")["duration_hours"].sum().reset_index()
agg_b["period"] = f"B: {b_start} → {b_end}"

combined = pd.concat([agg_a, agg_b], ignore_index=True)

if not combined.empty:
    bar_chart = (
        alt.Chart(combined)
        .mark_bar()
        .encode(
            x=alt.X("group:N", title=group_mode.title(), axis=alt.Axis(labelAngle=-45)),
            y=alt.Y("duration_hours:Q", title="Hours"),
            color=alt.Color("period:N", title="Period"),
            xOffset="period:N",
            tooltip=["group:N", "period:N", alt.Tooltip("duration_hours:Q", format=".1f")],
        )
        .properties(width=700, height=400)
    )
    st.altair_chart(bar_chart, use_container_width=True)

# --- Delta table ---
st.subheader("Detailed Comparison")

all_groups = sorted(set(agg_a["group"].tolist() + agg_b["group"].tolist()))

rows = []
for g in all_groups:
    hours_a = agg_a.loc[agg_a["group"] == g, "duration_hours"].sum()
    hours_b = agg_b.loc[agg_b["group"] == g, "duration_hours"].sum()
    diff = hours_b - hours_a
    pct = ((hours_b - hours_a) / hours_a * 100) if hours_a > 0 else None
    rows.append({
        group_mode.title(): g,
        "Period A (h)": round(hours_a, 1),
        "Period B (h)": round(hours_b, 1),
        "Δ Hours": round(diff, 1),
        "Δ %": f"{pct:+.0f}%" if pct is not None else "—",
    })

delta_df = pd.DataFrame(rows)
st.dataframe(delta_df, use_container_width=True, hide_index=True)

# --- Per-day trend within each period ---
with st.expander("📈 Daily breakdown within each period"):
    daily_a = df_a.groupby(df_a["start"].dt.date)["duration_hours"].sum().reset_index()
    daily_a.columns = ["date", "hours"]
    daily_a["period"] = "Period A"

    daily_b = df_b.groupby(df_b["start"].dt.date)["duration_hours"].sum().reset_index()
    daily_b.columns = ["date", "hours"]
    daily_b["period"] = "Period B"

    daily_combined = pd.concat([daily_a, daily_b], ignore_index=True)
    daily_combined["date"] = pd.to_datetime(daily_combined["date"])

    if not daily_combined.empty:
        daily_chart = (
            alt.Chart(daily_combined)
            .mark_line(point=True)
            .encode(
                x=alt.X("date:T", title="Date"),
                y=alt.Y("hours:Q", title="Hours"),
                color=alt.Color("period:N", title="Period"),
                tooltip=["date:T", "hours:Q", "period:N"],
            )
            .properties(width=700, height=300)
        )
        st.altair_chart(daily_chart, use_container_width=True)
    else:
        st.info("No daily data to display.")
