import streamlit as st
import pandas as pd
import requests
from datetime import datetime, date, timedelta, timezone
import altair as alt
import os
import shutil
from calendar_store import update_event_store
from ics import Calendar
import calendar
import json
import colorsys
from urllib.parse import urlparse
import tzlocal

# Streamlit UI
st.set_page_config(page_title="CalendarTimeTracker", layout="wide", initial_sidebar_state='collapsed')
st.title("CalendarTimeTracker")
st.caption("Analyze time usage from multiple public calendar (.ics) URLs")

def random_distinct_color(index, total_colors):
    hue = (index / total_colors)  # Distribute hues evenly (0 to 1)
    saturation = 0.7  # Maintain vivid colors
    lightness = 0.5  # Keep the colors neither too dark nor too light
    r, g, b = colorsys.hls_to_rgb(hue, lightness, saturation)
    color = "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))
    return color

@st.cache_data(ttl=3600)  # Cache for 1 hour
def parse_ics_from_url(url, calendar_name):
    try:
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
                start = event.begin.datetime.astimezone(timezone.utc)
                end = event.end.datetime.astimezone(timezone.utc)
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
        category_colors = {}  # To keep track of category colors
        calendar_colors = {}  # To store colors from JSON for calendars

        # Try loading the JSON calendar file first
        if os.path.exists(calendars_json_file):
            filetype = calendars_json_file
            with open(calendars_json_file, 'r') as file:
                calendar_data = json.load(file)
            calendars = []
            distinct_color_index = 0
            total_calendars = len(calendar_data['calendars'])

            for index, calendar in enumerate(calendar_data['calendars']):
                url = calendar["url"]
                custom_name = calendar.get("custom_name", "") or "Unnamed"
                category = calendar.get("category", custom_name) # Default category to custom_name if not provided
                color_from_json = calendar.get("color")

                # Store color from JSON if available, or generate if missing
                if color_from_json:
                    calendar_colors[custom_name] = color_from_json
                    color = color_from_json
                else:
                    color = random_distinct_color(distinct_color_index, total_calendars)
                    calendar_colors[custom_name] = color
                    distinct_color_index += 1

                # Assign color to category if not already assigned
                if category not in category_colors:
                    # If a specific color is defined for a calendar, use that for its category (if category is same as custom_name)
                    if category == custom_name and color_from_json:
                        category_colors[category] = color_from_json
                    else:
                        category_colors[category] = random_distinct_color(distinct_color_index, total_calendars)
                        distinct_color_index += 1

                calendars.append({
                    "url": url,
                    "custom_name": custom_name,
                    "category": category,
                    "color": color
                })
            return calendars, "json"

        # If the JSON config is not found, fall back to reading from the txt file
        elif os.path.exists(txt_file):
            filetype = txt_file
            calendars = []
            with open(txt_file, "r", encoding="utf-8") as f:
                total_colors = sum(1 for line in f if line.strip() and not line.startswith("#"))  # Count total calendars
                f.seek(0)  # Reset file pointer to the beginning
                for index, line in enumerate(f):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        parts = line.split("#")
                        url = parts[0].strip()
                        custom_name = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "Unnamed"
                        category = custom_name  # Use custom_name as category for .txt

                        # Assign color to category if not already assigned
                        if category not in category_colors:
                            category_colors[category] = random_distinct_color(index, total_colors)

                        calendars.append({"url": url, "custom_name": custom_name, "category": category, "color": category_colors[category]})
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
        for calendar_info in calendar_data:
            url = calendar_info["url"]
            custom_name = calendar_info["custom_name"]
            category = calendar_info["category"] # Ensure category is always taken from calendar_data
            color = calendar_info["color"]
            events = parse_ics_from_url(url, custom_name)
            for event in events:
                event["category"] = category
                event["calendar_name"] = custom_name
                event["color"] = color  # Add color to each event
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
        df[start_col] = df[start_col].dt.tz_convert("UTC")
        df[end_col] = df[end_col].dt.tz_convert("UTC")
    elif tz == "naive":
        df[start_col] = df[start_col].dt.tz_localize(None)
        df[end_col] = df[end_col].dt.tz_localize(None)
    elif tz == "local":
        local_tz = tzlocal.get_localzone()
        df[start_col] = df[start_col].dt.tz_convert(local_tz)
        df[end_col] = df[end_col].dt.tz_convert(local_tz)
    return df

def normalize_calendar_name(name):
    if name.startswith("[Imported] "):
        return name.replace("[Imported] ", "").strip()
    return name.strip()

def preprocess_dataframe(all_events, normalize_calendar_name, normalize_time, select_month_range):
    df = pd.DataFrame(all_events)
    df["calendar"] = df["calendar_name"].apply(normalize_calendar_name)
    # Normalize time BEFORE filtering
    df = normalize_time(df, tz="local")  # or tz="utc"
    # Filter by date range
    start_date, end_date = select_month_range(df)
    # Update day/week/month distribution calculations to handle multi-day events properly
    # For filtering, we use a more inclusive approach to catch all events that span the date range
    df = df[
        ((df["start"].dt.date >= start_date) & (df["start"].dt.date <= end_date)) |  # Events that start in range
        ((df["end"].dt.date >= start_date) & (df["end"].dt.date <= end_date)) |      # Events that end in range
        ((df["start"].dt.date <= start_date) & (df["end"].dt.date >= end_date))      # Events that span the entire range
    ]
    df["month"] = df["start"].dt.to_period("M")
    df["weekday"] = df["start"].dt.day_name()
    df["hour"] = df["start"].dt.hour
    return df, start_date, end_date

def show_summary_table(df, start_date, end_date):
    with st.expander("📋 Show Summary Table"):
        st.caption(f"Showing events from {start_date} to {end_date}")

        # Calculate total days and weeks in the selected range
        days_span = (end_date - start_date).days + 1
        weeks_span = days_span / 7

        # Group and aggregate
        summary = (
            df.groupby("group")["duration_hours"]
            .agg(Total_Hours="sum", Average_Hours_Per_Event="mean", Event_Count="count")
            .reset_index()
        )

        # Add percent share and per-day/week averages
        summary["Percent"] = (summary["Total_Hours"] / summary["Total_Hours"].sum() * 100).round(1)
        summary["Avg_Per_Day"] = (summary["Total_Hours"] / days_span).round(2)
        summary["Avg_Per_Week"] = (summary["Total_Hours"] / weeks_span).round(2)

        # Reorder columns
        summary = summary[[
            "group",
            "Percent",
            "Total_Hours",
            "Average_Hours_Per_Event",
            "Avg_Per_Day",
            "Avg_Per_Week",
            "Event_Count"
        ]]
        summary.rename(columns={"group": "Group"}, inplace=True)

        # Show table and download button
        st.dataframe(summary)
        csv = summary.to_csv(index=False).encode("utf-8")
        st.download_button("Download Summary as CSV", csv, "summary.csv", "text/csv")

        # Optional caption for context
        st.caption(f"Range covers {days_span} days ≈ {weeks_span:.1f} weeks.")

def show_duration_charts(df, start_date, end_date, group_mode, date_option):
    group_label = group_mode.title()  # "Calendar" or "Category"

    # Create a color mapping based on the selected group
    color_mapping = df.groupby('group')['color'].first().to_dict()

    # Create a function to calculate the proportion of an event that falls within a specific time period
    def calculate_time_proportion(row, period_start, period_end):
        event_start = max(row['start'], period_start)
        event_end = min(row['end'], period_end)

        # If the event doesn't overlap with this period, return 0
        if event_end <= event_start:
            return 0

        # Calculate the proportion of the event that falls in this period
        event_duration = (row['end'] - row['start']).total_seconds() / 3600
        period_duration = (event_end - event_start).total_seconds() / 3600

        return period_duration

    # Create time-based data frames based on the selected date_option
    time_aggregation = pd.DataFrame()

    if date_option == "week":
        # Generate all weeks in the selected range
        weeks = []
        current_date = start_date
        while current_date <= end_date:
            year, week, _ = current_date.isocalendar()
            weeks.append({'year': year, 'week': week, 
                         'week_start': datetime.fromisocalendar(year, week, 1),
                         'week_end': datetime.fromisocalendar(year, week, 7)})
            current_date += timedelta(days=7)

        weeks_df = pd.DataFrame(weeks)

        # Calculate duration for each event in each week
        results = []
        tz = df['start'].dt.tz
        for _, week_row in weeks_df.iterrows():
            # Ensure period_start and period_end are tz-aware
            period_start = pd.Timestamp(week_row['week_start']).tz_localize(tz)
            period_end = pd.Timestamp(week_row['week_end']).replace(hour=23, minute=59, second=59).tz_localize(tz)

            for _, event_row in df.iterrows():
                duration = calculate_time_proportion(event_row, period_start, period_end)
                if duration > 0:
                    results.append({
                        'year': week_row['year'],
                        'week': week_row['week'],
                        'group': event_row['group'],
                        'duration_hours': duration,
                        'time_label': f"{week_row['year']}-W{week_row['week']:02d}"
                    })

        if results:
            time_aggregation = pd.DataFrame(results)
            time_aggregation = time_aggregation.groupby(['time_label', 'group']).agg(
                duration_hours=('duration_hours', 'sum'),
                year=('year', 'first'),
                week=('week', 'first')
            ).reset_index()

    elif date_option == "day":
        # Generate all days in the selected range
        days = []
        current_date = start_date
        while current_date <= end_date:
            days.append(current_date)
            current_date += timedelta(days=1)

        # Calculate duration for each event in each day
        results = []
        tz = df["start"].dt.tz
        for day in days:
            period_start = pd.Timestamp(day).tz_localize(tz)
            period_end = pd.Timestamp(day).replace(hour=23, minute=59, second=59).tz_localize(tz)

            for _, event_row in df.iterrows():
                duration = calculate_time_proportion(event_row, period_start, period_end)
                if duration > 0:
                    results.append({
                        'date': day,
                        'group': event_row['group'],
                        'duration_hours': duration,
                        'time_label': day.strftime('%Y-%m-%d')
                    })

        if results:
            time_aggregation = pd.DataFrame(results)
            time_aggregation = time_aggregation.groupby(['time_label', 'group']).agg(
                duration_hours=('duration_hours', 'sum'),
                date=('date', 'first')
            ).reset_index()

    else:  # Month
        # Generate all months in the selected range
        months = []
        current_date = date(start_date.year, start_date.month, 1)
        while current_date <= end_date:
            month_end = date(current_date.year, current_date.month, 
                            calendar.monthrange(current_date.year, current_date.month)[1])
            months.append({'month': pd.Period(current_date, freq='M'),
                          'month_start': current_date,
                          'month_end': month_end})
            # Move to next month
            if current_date.month == 12:
                current_date = date(current_date.year + 1, 1, 1)
            else:
                current_date = date(current_date.year, current_date.month + 1, 1)

        months_df = pd.DataFrame(months)
        # Calculate duration for each event in each month
        results = []
        tz = df["start"].dt.tz
        for _, month_row in months_df.iterrows():
            period_start = pd.Timestamp(month_row['month_start']).tz_localize(tz)
            period_end = pd.Timestamp(month_row['month_end']).replace(hour=23, minute=59, second=59).tz_localize(tz)

            for _, event_row in df.iterrows():
                duration = calculate_time_proportion(event_row, period_start, period_end)
                if duration > 0:
                    results.append({
                        'month': month_row['month'],
                        'group': event_row['group'],
                        'duration_hours': duration,
                        'time_label': str(month_row['month'])
                    })

        if results:
            time_aggregation = pd.DataFrame(results)
            time_aggregation = time_aggregation.groupby(['time_label', 'group']).agg(
                duration_hours=('duration_hours', 'sum'),
                month=('month', 'first')
            ).reset_index()

    if time_aggregation.empty:
        st.info(f"No data to display for the selected time range.")
        return

    # Calculate percentages
    time_totals = time_aggregation.groupby('time_label')["duration_hours"].transform("sum")
    time_aggregation["percent"] = ((time_aggregation["duration_hours"] / time_totals.replace(0, pd.NA)) * 100).round(1).fillna(0)

    time_unit = date_option.title()

    st.subheader(f"Relative Time per {time_unit} (100% Stacked)")
    st.caption(f"Showing events from {start_date} to {end_date}")

    # Normalized chart with labeled axes, apply hex color from color_mapping
    chart_percent = alt.Chart(time_aggregation).mark_bar().encode(
        x=alt.X(f"time_label:N", title=time_unit, axis=alt.Axis(labelAngle=-45)),
        y=alt.Y("percent:Q", title="Percentage", stack="normalize"),
        color=alt.Color("group:N", title=group_label, scale=alt.Scale(domain=list(color_mapping.keys()), range=list(color_mapping.values()))),
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
        color=alt.Color("group:N", title=group_label, scale=alt.Scale(domain=list(color_mapping.keys()), range=list(color_mapping.values()))),
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

    # For multi-day events, we need to distribute their time appropriately
    # We'll create a simplified version focusing on the start hour for each event

    # Use the start hour and weekday for the heatmap (simplification)
    heatmap_data = df.groupby(["weekday", "hour"])["duration_hours"].sum().unstack(fill_value=0)
    heatmap_data = heatmap_data.reindex(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])
    st.dataframe(heatmap_data.style.background_gradient(cmap="YlOrRd"))

def show_calendar_distribution_pie_chart(df, group_mode):
    group_label = group_mode.title()  # "Calendar" or "Category"

    # Create a color mapping based on the selected group
    color_mapping = df.groupby('group')['color'].first().to_dict()

    summary = df.groupby("group")["duration_hours"].sum().reset_index()
    total = summary["duration_hours"].sum()
    summary["percentage"] = (summary["duration_hours"] / total) * 100

    chart = alt.Chart(summary).mark_arc().encode(
        theta=alt.Theta(field="duration_hours", type="quantitative"),
        color=alt.Color(
            field="group",
            type="nominal",
            title=group_label,
            scale=alt.Scale(domain=list(color_mapping.keys()), range=list(color_mapping.values()))
        ),
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

with st.sidebar:
    st.markdown("### Options")
    if st.button("Clear Cache"):
        st.cache_data.clear()
        st.success("Cache has been cleared. Please reload the page.")

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