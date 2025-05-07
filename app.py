import streamlit as st
import pandas as pd
import requests
from datetime import datetime, date
import altair as alt
import os
import shutil
from calendar_store import update_event_store
from ics import Calendar
import calendar

def load_calendar_urls(file_path="calendars.txt"):
    with open(file_path, "r", encoding="utf-8") as f:
        return [
            line.split("#")[0].strip()
            for line in f
            if line.strip() and not line.strip().startswith("#")
        ]

@st.cache_data(ttl=3600)  # Cache for 1 hour (3600 seconds)
def parse_ics_from_url(url):
    try:
        from urllib.parse import urlparse

        # Fetch .ics content
        parsed_url = urlparse(url)
        # Check if it's a local file URL
        if parsed_url.scheme == "file":
            path = parsed_url.path
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        else:
            response = requests.get(url)
            if response.status_code != 200:
                return []
            content = response.text

        # Extract calendar name from raw text
        calendar_name = "Unnamed"
        for line in content.splitlines():
            if line.startswith("X-WR-CALNAME:"):
                calendar_name = line.replace("X-WR-CALNAME:", "").strip()
                break

        # Parse using ics
        cal = Calendar(content)
        events = []
        for event in cal.events:
            try:
                start = event.begin.datetime
                end = event.end.datetime
                duration = (end - start).total_seconds() / 3600
                events.append({
                    "calendar": calendar_name,
                    "start": start,
                    "end": end,
                    "duration_hours": duration
                })
            except Exception as e:
                print(f"Skipping event: {e}")
                continue

        new_df = pd.DataFrame(events)
        combined_df = update_event_store(url, new_df)
        return combined_df.to_dict("records")

    except Exception as e:
        st.error(f"Error loading {url}: {e}")
        return []

def select_month_range(df):
    min_date = df["start"].min().date()
    max_date = df["start"].max().date()

    years = list(range(min_date.year, max_date.year + 1))
    months = list(range(1, 13))
    now = datetime.now()

    start_month_default = 1
    end_month_default = 12
    start_year_default = end_year_default = now.year

    st.subheader("Select Month Range")

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        start_month = st.selectbox("Start Month", months, index=start_month_default - 1, format_func=lambda m: calendar.month_name[m])
    with col2:
        start_year = st.selectbox("Start Year", years, index=years.index(start_year_default))
    with col3:
        end_month = st.selectbox("End Month", months, index=end_month_default - 1, format_func=lambda m: calendar.month_name[m])
    with col4:
        end_year = st.selectbox("End Year", years, index=years.index(end_year_default))

    try:
        start_date = date(start_year, start_month, 1)
        end_day = calendar.monthrange(end_year, end_month)[1]
        end_date = date(end_year, end_month, end_day)

        if start_date > end_date:
            st.warning("Start must be before end.")
            st.stop()

        return start_date, end_date

    except Exception as e:
        st.error(f"Invalid date range: {e}")
        st.stop()

def normalize_time(df, start_col="start", end_col="end", tz="local"):
    df[start_col] = pd.to_datetime(df[start_col], errors="coerce")
    df[end_col] = pd.to_datetime(df[end_col], errors="coerce")

    if tz == "utc":
        df[start_col] = df[start_col].dt.tz_convert("UTC") if df[start_col].dt.tz is not None else df[start_col]
        df[end_col] = df[end_col].dt.tz_convert("UTC") if df[end_col].dt.tz is not None else df[end_col]
    elif tz == "naive":
        df[start_col] = df[start_col].dt.tz_localize(None)
        df[end_col] = df[end_col].dt.tz_localize(None)
    return df

# Copy sample file if calendars.txt doesn't exist
if not os.path.exists("calendars.txt") and os.path.exists("calendars.txt.sample"):
    shutil.copy("calendars.txt.sample", "calendars.txt")
    st.warning("No calendars.txt found. A sample file has been copied. Please update it with your calendar URLs and reload the page.")

# Streamlit UI
st.title("CalendarTimeTracker")
st.caption("Analyze time usage from multiple public calendar (.ics) URLs")

# Load calendar URLs
calendar_urls = load_calendar_urls("calendars.txt")
all_events = []

for url in calendar_urls:
    events = parse_ics_from_url(url)
    all_events.extend(events)

# Create DataFrame
if all_events:
    df = pd.DataFrame(all_events)
    start_date, end_date = select_month_range(df)
    df = df[(df["start"].dt.date >= start_date) & (df["start"].dt.date <= end_date)]
    df = normalize_time(df, tz="naive")  # or tz="utc"
    df["month"] = df["start"].dt.to_period("M")
    df["weekday"] = df["start"].dt.day_name()
    df["hour"] = df["start"].dt.hour

    # Summary Table
    st.subheader("Summary Table")
    st.caption(f"Showing events from {start_date} to {end_date}")
    summary = df.groupby("calendar")["duration_hours"].agg(
        Total_Hours="sum",
        Average_Hours_Per_Event="mean",
        Event_Count="count"
    ).reset_index()
    total_hours = summary["Total_Hours"].sum()
    summary["Percent"] = (summary["Total_Hours"] / total_hours * 100).round(1)
    summary = summary[["calendar", "Percent", "Total_Hours", "Average_Hours_Per_Event", "Event_Count"]]
    st.dataframe(summary)

    csv = summary.to_csv(index=False).encode("utf-8")
    st.download_button("Download Summary as CSV", csv, "summary.csv", "text/csv")

    # Generate all months
    all_months = pd.date_range(start=start_date, end=end_date, freq="MS").to_period("M")
    calendars = df["calendar"].unique()
    full_index = pd.MultiIndex.from_product([all_months, calendars], names=["month", "calendar"])

    monthly = df.groupby(["month", "calendar"])["duration_hours"].sum().reindex(full_index, fill_value=0).reset_index()
    monthly["month"] = monthly["month"].astype(str)
    
    # Normalize durations to 100% per month
    monthly_totals = monthly.groupby("month")["duration_hours"].transform("sum")
    monthly["percent"] = (
        (monthly["duration_hours"] / monthly_totals.replace(0, pd.NA)) * 100
    ).round(1).fillna(0)

    st.subheader("Relative Time per Month (100% Stacked)")
    st.caption(f"Showing events from {start_date} to {end_date}")
    # Normalized altair chart with labeled axes
    chart_percent = alt.Chart(monthly).mark_bar().encode(
        x=alt.X("month:N", title="Month", axis=alt.Axis(labelAngle=-45)),
        y=alt.Y("percent:Q", title="Percentage", stack="normalize"),
        color=alt.Color("calendar:N", title="Calendar"),
        tooltip=["month", "calendar", "duration_hours", "percent"]
    ).properties(width=700, height=400).interactive()

    st.altair_chart(chart_percent, use_container_width=True)

    # Altair chart with labeled axes
    st.subheader("Total Time per Month (Stacked by Calendar)")
    st.caption(f"Showing events from {start_date} to {end_date}")
    chart = alt.Chart(monthly).mark_bar().encode(
        x=alt.X("month:N", title="Month", axis=alt.Axis(labelAngle=-45)),
        y=alt.Y("duration_hours:Q", title="Hours"),
        color=alt.Color("calendar:N", title="Calendar"),
        tooltip=["month", "calendar", "duration_hours"]
    ).properties(width=700, height=400).interactive()  # Enables zoom/pan

    st.altair_chart(chart, use_container_width=True)

    # Heatmap: Weekday vs Hour
    st.subheader("Activity Heatmap (Weekday × Hour)")
    st.caption(f"Showing events from {start_date} to {end_date}")
    heatmap_data = df.groupby(["weekday", "hour"])["duration_hours"].sum().unstack(fill_value=0)
    heatmap_data = heatmap_data.reindex(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])
    st.dataframe(heatmap_data.style.background_gradient(cmap="YlOrRd"))

else:
    st.warning("No events loaded from calendars.")
