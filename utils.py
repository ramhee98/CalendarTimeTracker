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
from calendar_store import update_event_store, load_cached_events, expand_calendar_events


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


def ensure_ics_content(content):
    """Raise a readable error if a fetched document is not an iCalendar feed.

    A Google Calendar sharing link (.../calendar/u/0?cid=...) serves the
    Calendar web page rather than the feed, which the ics parser reports as a
    contentline syntax error followed by a wall of HTML.
    """
    if "BEGIN:VCALENDAR" not in content:
        raise ValueError(
            "URL did not return an .ics feed. Use the calendar's "
            "\"Secret address in iCal format\" (.../ical/<id>/public/basic.ics), "
            "not a calendar.google.com/calendar/u/0?cid=... link."
        )


def random_distinct_color(index, total_colors):
    hue = (index / total_colors)  # Distribute hues evenly (0 to 1)
    saturation = 0.7  # Maintain vivid colors
    lightness = 0.5  # Keep the colors neither too dark nor too light
    r, g, b = colorsys.hls_to_rgb(hue, lightness, saturation)
    color = "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))
    return color


def fetch_and_parse_ics(url, calendar_name):
    """Fetch an .ics feed, expand its events and merge them into the CSV cache.

    No Streamlit dependencies, so the background refresher can use it too.
    Raises on fetch/parse errors; callers decide how to report them.
    """
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
            # Server error: fall back to the cached events for this calendar.
            return load_cached_events(url).to_dict("records")
        content = response.text

    ensure_ics_content(content)

    # Extract calendar name from raw text if not set by calendars file
    if calendar_name == "Unnamed":
        for line in content.splitlines():
            if line.startswith("X-WR-CALNAME:"):
                calendar_name = line.replace("X-WR-CALNAME:", "").strip()
                break

    # Parse using ics
    ics_cal = Calendar(content)
    events = []
    # Expand recurring events into individual occurrences.
    for event, start, end, occ_uid in expand_calendar_events(ics_cal.events):
        duration = (end - start).total_seconds() / 3600
        events.append({
            "calendar": calendar_name,
            "event_name": event.name or "Untitled Event",
            "start": start,
            "end": end,
            "duration_hours": duration,
            "uid": occ_uid
        })

    new_df = pd.DataFrame(events)
    combined_df = update_event_store(url, new_df)
    return combined_df.to_dict("records")


@st.cache_data(ttl=3600, show_spinner=False)  # Spinner handled manually by callers
def parse_ics_from_url(url, calendar_name):
    try:
        return fetch_and_parse_ics(url, calendar_name)
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
            # Tag calendars (type "tags") hold markers like "☕ Coffee", not
            # time blocks. They take no part in colors/categories so adding
            # one doesn't shift the colors of the time calendars.
            total_calendars = max(sum(1 for c in calendar_data['calendars'] if not is_tag_calendar(c)), 1)

            for index, calendar in enumerate(calendar_data['calendars']):
                url = calendar["url"]
                custom_name = calendar.get("custom_name", "") or "Unnamed"
                if is_tag_calendar(calendar):
                    calendars.append({"url": url, "custom_name": custom_name, "category": None,
                                      "color": None, "type": "tags"})
                    continue
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
                    "color": color,
                    "type": "time",
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

                        calendars.append({"url": url, "custom_name": custom_name, "category": category, "color": category_colors[category], "type": "time"})
            return calendars, "txt"

        return None, None

    except Exception as e:
        st.error(f"An error occurred while loading calendar config {filetype}: {e}")
        return None, None


def is_tag_calendar(calendar):
    """True for calendars configured with "type": "tags" in calendars.json."""
    return str(calendar.get("type", "time")).lower() == "tags"


def load_all_events(kind="time"):
    """Sync every configured calendar and return the events of `kind`
    ("time" or "tags"), so a Sync on any page also refreshes tag calendars."""
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
            if calendar_info["type"] != kind:
                continue
            events = [dict(e) for e in events]  # don't mutate the st.cache_data result
            for event in events:
                event["category"] = category
                event["calendar_name"] = custom_name
                event["color"] = color
            all_events.extend(events)

        return all_events, source_type
    except Exception as e:
        st.error(f"An error occurred while loading events: {e}")
        return None, None


def load_all_events_from_cache(kind="time"):
    """Load events of `kind` ("time" or "tags") from the local CSV cache only
    (instant, no network requests)."""
    try:
        calendar_data, source_type = load_calendar_urls()
        if not calendar_data:
            return None, None

        all_events = []
        for calendar_info in calendar_data:
            if calendar_info["type"] != kind:
                continue
            url = calendar_info["url"]
            custom_name = calendar_info["custom_name"]
            category = calendar_info["category"]
            color = calendar_info["color"]
            
            # Load from local CSV cache (no URL fetching)
            cached_df = load_cached_events(url)
            if not cached_df.empty:
                cached_df["category"] = category
                cached_df["calendar_name"] = custom_name
                cached_df["color"] = color
                all_events.extend(cached_df.to_dict("records"))

        return all_events if all_events else None, source_type
    except Exception as e:
        st.error(f"An error occurred while loading cached events: {e}")
        return None, None


def select_month_range(df):
    min_date = df["start"].min().date()
    max_date = df["start"].max().date()

    now = datetime.now().date()
    # Default range length comes from the Settings page (12 months unless changed)
    months = int(load_app_settings().get("default_range_months", 12) or 0)
    range_start = (pd.Timestamp(now) - pd.DateOffset(months=months)).date() if months else min_date
    # Clamp both defaults into the data range: st.date_input raises if the
    # default lies outside [min_value, max_value], e.g. when all events are
    # older than the range or all in the future.
    default_start = min(max(min_date, range_start), max_date)
    default_end = max(min(max_date, now), min_date)

    st.subheader("Select Date Range")

    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input(
            "Start Date",
            value=default_start,
            min_value=min_date,
            max_value=max_date,
            help="Select the start date for filtering events (day & month enabled)",
        )
    with col2:
        end_date = st.date_input(
            "End Date",
            value=default_end,
            min_value=min_date,
            max_value=max_date,
            help="Select the end date for filtering events (day & month enabled)",
        )

    try:
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


# --- App-wide settings (app_settings.json) ---

APP_SETTINGS_FILE = "app_settings.json"
DEFAULT_APP_SETTINGS = {
    # "include" counts all-day / multi-day events fully, "exclude" drops them.
    "long_events": "include",
    # Events longer than this (hours) count as long even if not all-day.
    "long_event_hours": 16,
    # Initial "View data by" choice on every page (calendars.json only).
    "default_view": "calendar",
    # Initial length of the date range pickers in months (0 = all data).
    "default_range_months": 12,
}


def load_app_settings():
    settings = dict(DEFAULT_APP_SETTINGS)
    try:
        with open(APP_SETTINGS_FILE, "r", encoding="utf-8") as f:
            settings.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return settings


def save_app_settings(settings):
    with open(APP_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, sort_keys=True)


def render_accuracy_settings(page_key):
    """Sidebar controls for how all-day / multi-day events are counted.

    The choice is stored in app_settings.json so every page uses the same rule.
    """
    settings = load_app_settings()
    options = ["include", "exclude"]
    labels = {"include": "Count fully", "exclude": "Exclude"}

    def _save():
        settings["long_events"] = st.session_state[f"{page_key}_long_events"]
        settings["long_event_hours"] = st.session_state[f"{page_key}_long_event_hours"]
        save_app_settings(settings)

    with st.sidebar.expander("⚖️ Data accuracy"):
        st.radio(
            "All-day & multi-day events",
            options,
            index=options.index(settings["long_events"]) if settings["long_events"] in options else 0,
            format_func=labels.get,
            key=f"{page_key}_long_events",
            on_change=_save,
            help="A 3-day trip counts as 72 hours when counted fully. "
                 "Exclude them to only count timed events.",
        )
        st.number_input(
            "Treat events longer than (hours) as long",
            min_value=1,
            max_value=168,
            value=int(settings["long_event_hours"]),
            step=1,
            key=f"{page_key}_long_event_hours",
            on_change=_save,
        )
    return settings


def apply_accuracy_settings(df, settings=None):
    """Drop long events from `df` if the user chose to exclude them."""
    from analysis import long_event_mask

    settings = settings or load_app_settings()
    if settings.get("long_events") == "exclude" and not df.empty:
        df = df[~long_event_mask(df, settings.get("long_event_hours", 16))]
    return df


# --- Goals (goals.json) ---

GOALS_FILE = "goals.json"


def _clean_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def load_goals():
    """Goals as a list of {type, name, period, min, max} dicts."""
    try:
        with open(GOALS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f).get("goals", [])
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return []
    goals = []
    for g in raw:
        goal = {
            "type": g.get("type", "calendar"),
            "name": str(g.get("name", "")).strip(),
            "period": g.get("period", "week"),
            "min": _clean_number(g.get("min")),
            "max": _clean_number(g.get("max")),
        }
        if goal["name"] and goal["type"] in ("calendar", "category") \
                and goal["period"] in ("week", "month") \
                and (goal["min"] is not None or goal["max"] is not None):
            goals.append(goal)
    return goals


def save_goals(goals):
    with open(GOALS_FILE, "w", encoding="utf-8") as f:
        json.dump({"goals": goals}, f, indent=2, ensure_ascii=False)


# --- Shared page helpers ---

def load_events_for_page(page_key, kind="time", stop_if_empty=True):
    """Quick Load / Sync buttons plus cache-first loading, as on every page.

    `kind` selects time calendars or tag calendars. Stops the page when no
    events are available (unless `stop_if_empty` is False).
    """
    flag = f"force_refresh_{page_key}"
    _, col2, col3 = st.columns([2, 1, 1])
    with col2:
        if st.button("⚡ Quick Load", help="Load instantly from local cache", key=f"{page_key}_quick"):
            st.session_state[flag] = False
            st.rerun()
    with col3:
        if st.button("🔄 Sync Calendars", help="Fetch latest data from calendar URLs", key=f"{page_key}_sync"):
            st.cache_data.clear()
            st.session_state[flag] = True
            st.rerun()

    if st.session_state.get(flag, False):
        st.session_state[flag] = False
        with st.spinner("Fetching latest calendar data from URLs..."):
            all_events, source_type = load_all_events(kind)
    else:
        with st.spinner("Loading calendar data from cache..."):
            all_events, source_type = load_all_events_from_cache(kind)
            if not all_events:
                with st.spinner("No cache found, fetching from URLs..."):
                    all_events, source_type = load_all_events(kind)

    if not all_events and stop_if_empty:
        st.warning("No events available.")
        st.stop()
    return all_events, source_type


def build_events_df(all_events, accuracy=None):
    """Events DataFrame with local times, display calendar names and the
    data-accuracy rule applied."""
    df = pd.DataFrame(all_events)
    df["calendar"] = df["calendar_name"].apply(normalize_calendar_name)
    df = normalize_time(df, tz="local")
    return apply_accuracy_settings(df, accuracy)


def default_view_index():
    """Index of the default "View data by" option set on the Settings page."""
    return 1 if load_app_settings().get("default_view") == "category" else 0


def select_group_mode(source_type, key):
    if source_type != "json":
        return "calendar"
    return st.selectbox(
        "View data by",
        options=["calendar", "category"],
        index=default_view_index(),
        format_func=str.title,
        key=key,
    )


def render_goal_progress(goal, result):
    """One progress bar for a goal's current period (Home and Goals page)."""
    from analysis import goal_target_text

    target = goal["min"] if goal["min"] is not None else goal["max"]
    fraction = min(result["done"] / target, 1.0) if target else 0.0
    st.progress(
        fraction,
        text=(
            f"**{goal['name']}** ({goal['type']}) · {result['done']:.1f}h done, "
            f"{result['booked']:.1f}h booked of {goal_target_text(goal)} this {result['period']}"
            f" — {result['status_label']}"
        ),
    )


# --- calendars.json editing (Settings page) ---

CALENDARS_JSON = "calendars.json"


def read_calendars_config(path=CALENDARS_JSON):
    """Raw calendars.json content ({"calendars": [...]}, other keys kept)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"calendars": []}
    if not isinstance(data, dict) or not isinstance(data.get("calendars"), list):
        raise ValueError(f"{path} must contain an object with a \"calendars\" list")
    return data


def write_calendars_config(data, path=CALENDARS_JSON):
    """Write calendars.json, keeping the previous version as calendars.json.bak."""
    if os.path.exists(path):
        import shutil
        shutil.copy2(path, path + ".bak")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
