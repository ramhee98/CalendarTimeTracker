"""
Shared utility functions for CalendarTimeTracker.
This module contains functions that are used by both the main app and the pages.
"""

import streamlit as st
import pandas as pd
import requests
from datetime import datetime, date, timedelta, timezone
import os
import json
import colorsys
from urllib.parse import urlparse
import tzlocal
from ics import Calendar
import calendar as cal_module
from calendar_store import update_event_store


def get_version():
    """Read version from version.txt file"""
    try:
        with open("version.txt", "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "Unknown"


@st.cache_data(ttl=3600, show_spinner="Fetching latest version from GitHub...")
def get_latest_github_version():
    """Fetch the latest version from GitHub's version.txt file"""
    try:
        url = "https://raw.githubusercontent.com/ramhee98/CalendarTimeTracker/main/version.txt"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.text.strip()
        else:
            return "Unknown"
    except Exception as e:
        print(f"Error fetching GitHub version: {e}")
        return "Unknown"


def random_distinct_color(index, total_colors):
    hue = (index / total_colors)  # Distribute hues evenly (0 to 1)
    saturation = 0.7  # Maintain vivid colors
    lightness = 0.5  # Keep the colors neither too dark nor too light
    r, g, b = colorsys.hls_to_rgb(hue, lightness, saturation)
    color = "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))
    return color


@st.cache_data(ttl=3600, show_spinner="Loading calendar data...")
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
        ics_cal = Calendar(content)
        events = []
        for event in ics_cal.events:
            try:
                start = event.begin.datetime.astimezone(timezone.utc)
                end = event.end.datetime.astimezone(timezone.utc)
                duration = (end - start).total_seconds() / 3600
                uid = event.uid
                name = event.name or "Untitled Event"

                events.append({
                    "calendar": calendar_name,
                    "event_name": name,
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
        category_colors = {}
        calendar_colors = {}

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
                category = calendar.get("category", custom_name)
                color_from_json = calendar.get("color")

                if color_from_json:
                    calendar_colors[custom_name] = color_from_json
                    color = color_from_json
                else:
                    color = random_distinct_color(distinct_color_index, total_calendars)
                    calendar_colors[custom_name] = color
                    distinct_color_index += 1

                if category not in category_colors:
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

        elif os.path.exists(txt_file):
            filetype = txt_file
            calendars = []
            with open(txt_file, "r", encoding="utf-8") as f:
                total_colors = sum(1 for line in f if line.strip() and not line.startswith("#"))
                f.seek(0)
                for index, line in enumerate(f):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        parts = line.split("#")
                        url = parts[0].strip()
                        custom_name = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "Unnamed"
                        category = custom_name

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
            category = calendar_info["category"]
            color = calendar_info["color"]
            events = parse_ics_from_url(url, custom_name)
            for event in events:
                event["category"] = category
                event["calendar_name"] = custom_name
                event["color"] = color
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

    # Default to last 12 months
    twelve_months_ago = now - timedelta(days=365)
    start_month_default = twelve_months_ago.month
    start_year_default = twelve_months_ago.year
    end_month_default = now.month
    end_year_default = now.year

    st.subheader("Select Month Range")

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        start_month = st.selectbox("Start Month", months, index=start_month_default - 1, format_func=lambda m: cal_module.month_name[m])
    with col2:
        start_year = st.selectbox("Start Year", years, index=years.index(start_year_default))
    with col3:
        end_month = st.selectbox("End Month", months, index=end_month_default - 1, format_func=lambda m: cal_module.month_name[m])
    with col4:
        end_year = st.selectbox("End Year", years, index=years.index(end_year_default))

    try:
        start_date = date(start_year, start_month, 1)
        end_day = cal_module.monthrange(end_year, end_month)[1]
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


def preprocess_dataframe(all_events, select_month_range_func):
    df = pd.DataFrame(all_events)
    df["calendar"] = df["calendar_name"].apply(normalize_calendar_name)
    df = normalize_time(df, tz="local")
    start_date, end_date = select_month_range_func(df)
    df = df[
        ((df["start"].dt.date >= start_date) & (df["start"].dt.date <= end_date)) |
        ((df["end"].dt.date >= start_date) & (df["end"].dt.date <= end_date)) |
        ((df["start"].dt.date <= start_date) & (df["end"].dt.date >= end_date))
    ]
    df["month"] = df["start"].dt.to_period("M")
    df["weekday"] = df["start"].dt.day_name()
    df["hour"] = df["start"].dt.hour
    return df, start_date, end_date
