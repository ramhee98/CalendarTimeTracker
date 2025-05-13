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
import json

@st.cache_data(ttl=86400)  # Cache for 24 hour (86400 seconds)
def parse_ics_from_url(url, calendar_name):
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

        # Extract calendar name from raw text if not set by calendars file
        if calendar_name == "Unnamed":
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
                uid = event.uid

                events.append({
                    "calendar": calendar_name,
                    "start": start,
                    "end": end,
                    "duration_hours": duration,
                    "uid": uid
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

def load_calendar_urls(calendars_json_file="calendars.json", txt_file="calendars.txt"):
    try:
        # Try loading the JSON calendar file first
        if os.path.exists(calendars_json_file):
            filetype = calendars_json_file
            with open(calendars_json_file, 'r') as file:
                calendar_data = json.load(file)
            calendars = []
            for calendar in calendar_data['calendars']:
                # If a custom name is provided, use it
                custom_name = calendar.get("custom_name", "") or "Unnamed"
                category = calendar.get("category", "Uncategorized")
                calendars.append({
                    "url": calendar["url"],
                    "custom_name": custom_name,
                    "category": category
                })
            return calendars, "json"
        # If the JSON config is not found, fall back to reading from the txt file
        elif os.path.exists(txt_file):
            filetype = txt_file
            calendars = []
            with open(txt_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        parts = line.split("#")
                        url = parts[0].strip()
                        custom_name = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "Unnamed"
                        calendars.append({"url": url, "custom_name": custom_name})
            return calendars, "txt"
        return None, None
    except Exception as e:
        st.error(f"An error occurred while loading calendar config {filetype}: {e}")
        return None, None

def load_all_events():
    try:
        calendar_data, source_type = load_calendar_urls()
        if not calendar_data:
            return None, None

        all_events = []
        for calendar in calendar_data:
            url = calendar["url"]
            custom_name = calendar["custom_name"]
            category = calendar.get("category", "Uncategorized")
            events = parse_ics_from_url(url, custom_name)
            for event in events:
                event["category"] = category
            all_events.extend(events)

        return all_events, source_type
    except Exception as e:
        st.error(f"An error occurred while loading events: {e}")
        return None, None

def select_month_range(df):
    min_date = df["start"].min().date()
    max_date = df["start"].max().date()

    years = list(range(min_date.year, max_date.year + 1))
    months = list(range(1, 13))
    now = datetime.now()

    start_month_default = 1
    end_month_default = datetime.now().month
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

def normalize_calendar_name(name):
    if name.startswith("[Imported] "):
        return name.replace("[Imported] ", "").strip()
    return name.strip()

def preprocess_dataframe(all_events, normalize_calendar_name, normalize_time, select_month_range):
    df = pd.DataFrame(all_events)
    df["calendar"] = df["calendar"].apply(normalize_calendar_name)
    # Normalize time BEFORE filtering
    df = normalize_time(df, tz="naive")  # or tz="utc"
    # Now filtering by datetimes works safely
    start_date, end_date = select_month_range(df)
    df = df[(df["start"].dt.date >= start_date) & (df["start"].dt.date <= end_date)]
    df["month"] = df["start"].dt.to_period("M")
    df["weekday"] = df["start"].dt.day_name()
    df["hour"] = df["start"].dt.hour
    return df, start_date, end_date

def show_summary_table(df, start_date, end_date):
    st.subheader("Summary Table")
    st.caption(f"Showing events from {start_date} to {end_date}")
    summary = (
        df.groupby("group")["duration_hours"]
        .agg(Total_Hours="sum", Average_Hours_Per_Event="mean", Event_Count="count")
        .reset_index()
    )
    summary["Percent"] = (summary["Total_Hours"] / summary["Total_Hours"].sum() * 100).round(1)
    summary = summary[["group", "Percent", "Total_Hours", "Average_Hours_Per_Event", "Event_Count"]]
    summary.rename(columns={"group": "Group"}, inplace=True)
    st.dataframe(summary)
    csv = summary.to_csv(index=False).encode("utf-8")
    st.download_button("Download Summary as CSV", csv, "summary.csv", "text/csv")

def show_duration_charts(df, start_date, end_date, group_mode, date_option):
    group_label = group_mode.title()  # "Calendar" or "Category"

    # Create 'year', 'week', 'day', or 'month' columns based on the date_option
    if date_option == "week":
        df['year'] = df['start'].dt.year
        df['week'] = df['start'].dt.isocalendar().week
        time_label_base = 'week'
        time_unit = "Week"
        grouping_cols = ['year', 'week', 'group']
    elif date_option == "day":
        df['date'] = df['start'].dt.date
        time_label_base = 'date'
        time_unit = "Day"
        grouping_cols = ['date', 'group']
    else:  # Default to month
        df['month'] = df['start'].dt.to_period('M')
        time_label_base = 'month'
        time_unit = "Month"
        grouping_cols = ['month', 'group']

    groups = df["group"].unique()

    # Aggregate duration
    time_aggregation = df.groupby(grouping_cols)["duration_hours"].sum().reset_index()

    if date_option == "week":
        time_aggregation['time_label'] = time_aggregation['year'].astype(str) + '-W' + time_aggregation['week'].astype(str).str.zfill(2)
        # Filter for weeks within the selected date range
        time_aggregation = time_aggregation[
            time_aggregation.apply(
                lambda row: (datetime.fromisocalendar(row['year'], row['week'], 1).date() >= start_date) and
                            (datetime.fromisocalendar(row['year'], row['week'], 7).date() <= end_date),
                axis=1
            )
        ]
    elif date_option == "day":
        time_aggregation['time_label'] = time_aggregation['date'].astype(str)
        time_aggregation = time_aggregation[
            (time_aggregation['date'] >= start_date) & (time_aggregation['date'] <= end_date)
        ]
    else:  # Month
        time_aggregation['time_label'] = time_aggregation['month'].astype(str)
        time_aggregation = time_aggregation[
            (time_aggregation['month'] >= pd.Period(start_date, freq='M')) &
            (time_aggregation['month'] <= pd.Period(end_date, freq='M'))
        ]

    if time_aggregation.empty:
        st.info(f"No data to display for the selected {time_unit.lower()} range.")
        return

    # Ensure 'time_label' exists even if filtering resulted in an empty DataFrame
    if 'time_label' not in time_aggregation.columns:
        time_aggregation['time_label'] = pd.Series(dtype='str') # Create an empty 'time_label' column

    time_totals = time_aggregation.groupby('time_label')["duration_hours"].transform("sum")
    time_aggregation["percent"] = ((time_aggregation["duration_hours"] / time_totals.replace(0, pd.NA)) * 100).round(1).fillna(0)

    st.subheader(f"Relative Time per {time_unit} (100% Stacked)")
    st.caption(f"Showing events from {start_date} to {end_date}")

    # Normalized chart with labeled axes
    chart_percent = alt.Chart(time_aggregation).mark_bar().encode(
        x=alt.X(f"time_label:N", title=time_unit, axis=alt.Axis(labelAngle=-45)),
        y=alt.Y("percent:Q", title="Percentage", stack="normalize"),
        color=alt.Color("group:N", title=group_label),
        tooltip=[
            alt.Tooltip(f"time_label:N", title=time_unit),
            alt.Tooltip("group:N", title=group_label),
            alt.Tooltip("duration_hours:Q", title="Duration (hours)", format=".2f"),
            alt.Tooltip("percent:Q", title="Percentage", format=".1f")
        ]
    ).properties(width=700, height=400).interactive()

    st.altair_chart(chart_percent, use_container_width=True)

    # Total duration stacked chart
    st.subheader(f"Total Time per {time_unit} (Stacked by " + group_label + ")")
    st.caption(f"Showing events from {start_date} to {end_date}")
    chart = alt.Chart(time_aggregation).mark_bar().encode(
        x=alt.X(f"time_label:N", title=time_unit, axis=alt.Axis(labelAngle=-45)),
        y=alt.Y("duration_hours:Q", title="Hours"),
        color=alt.Color("group:N", title=group_label),
        tooltip=[
            alt.Tooltip(f"time_label:N", title=time_unit),
            alt.Tooltip("group:N", title=group_label),
            alt.Tooltip("duration_hours:Q", title="Duration (hours)", format=".2f")
        ]
    ).properties(width=700, height=400).interactive()

    st.altair_chart(chart, use_container_width=True)

def show_weekday_hour_heatmap(df, start_date, end_date):
    st.subheader("Activity Heatmap (Weekday × Hour)")
    st.caption(f"Showing events from {start_date} to {end_date}")
    heatmap_data = df.groupby(["weekday", "hour"])["duration_hours"].sum().unstack(fill_value=0)
    heatmap_data = heatmap_data.reindex(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])
    st.dataframe(heatmap_data.style.background_gradient(cmap="YlOrRd"))

def show_calendar_distribution_pie_chart(df, group_mode):
    group_label = group_mode.title()  # "Calendar" or "Category"

    summary = df.groupby("group")["duration_hours"].sum().reset_index()
    total = summary["duration_hours"].sum()
    summary["percentage"] = (summary["duration_hours"] / total) * 100

    chart = alt.Chart(summary).mark_arc().encode(
        theta=alt.Theta(field="duration_hours", type="quantitative"),
        color=alt.Color(field="group", type="nominal", title=group_label),
        tooltip=[
            alt.Tooltip("group:N", title=group_label),
            alt.Tooltip("duration_hours:Q", title="Total Duration (hours)", format=".2f"),
            alt.Tooltip("percentage:Q", title="Percentage", format=".2f")
        ]
    ).properties(width=500, height=500)

    st.subheader(f"Time Distribution by {group_label}")
    st.altair_chart(chart, use_container_width=True)

# Copy sample file if calendars.txt doesn't exist
if not os.path.exists("calendars.txt") and os.path.exists("calendars.txt.sample"):
    shutil.copy("calendars.txt.sample", "calendars.txt")
    st.warning("No calendars.txt found. A sample file has been copied. Please update it with your calendar URLs and reload the page.")

# Streamlit UI
st.set_page_config(page_title="CalendarTimeTracker", layout="wide", initial_sidebar_state='collapsed')
st.title("CalendarTimeTracker")
st.caption("Analyze time usage from multiple public calendar (.ics) URLs")

# Load events from all calendar URLs
all_events, source_type = load_all_events()

if all_events:
    # Optional selector (only if JSON is used)
    if source_type == "json":
        group_mode = st.selectbox(
            "View data by",
            options=["calendar", "category"],
            index=0,
            format_func=str.title,
            key="view_mode"
        )
    else:
        group_mode = "calendar"
    df, start_date, end_date = preprocess_dataframe(all_events, normalize_calendar_name, normalize_time, select_month_range)
    df["group"] = df[group_mode]
    show_summary_table(df, start_date, end_date)

    st.subheader(f"Relative Time Charts")
    date_option = st.radio(
        "Show duration charts by:",
        options=["week", "day", "month"],
        index=0,
        horizontal=True
    )
    show_duration_charts(df, start_date, end_date, group_mode, date_option)
    show_weekday_hour_heatmap(df, start_date, end_date)
    show_calendar_distribution_pie_chart(df, group_mode)

else:
    st.warning("No events loaded from calendars.")

st.markdown(
    """
    ---
    🔗 [View project on GitHub](https://github.com/ramhee98/CalendarTimeTracker)
    """,
    unsafe_allow_html=True
)