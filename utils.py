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
from calendar_store import update_event_store, load_cached_events, expand_calendar_events, decode_ics_response, is_free_event


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


def fetch_and_parse_ics(url, calendar_name, source="sync"):
    """Fetch an .ics feed, expand its events and merge them into the CSV cache.

    No Streamlit dependencies, so the background refresher can use it too.
    Every fetch is recorded on the Calendar Health page (`source`: "sync",
    "background" or "check"). An HTTP error falls back to the cached events;
    other fetch/parse errors raise and callers decide how to report them.
    """
    from calendar_health import FeedError, check_ics_text, feed_stats, fetch_feed, record_check, record_failure

    try:
        content, meta = fetch_feed(url)
        check_ics_text(content, meta)
    except FeedError as e:
        record_failure(url, calendar_name, e, source)
        if e.kind == "http":
            # Server error: fall back to the cached events for this calendar.
            return load_cached_events(url).to_dict("records")
        if e.kind == "not_ics":
            ensure_ics_content(content)  # raises the existing, more detailed message
        raise

    # Extract calendar name from raw text if not set by calendars file
    if calendar_name == "Unnamed":
        for line in content.splitlines():
            if line.startswith("X-WR-CALNAME:"):
                calendar_name = line.replace("X-WR-CALNAME:", "").strip()
                break

    # Parse using ics
    try:
        ics_cal = Calendar(content)
    except Exception as e:
        record_failure(url, calendar_name, FeedError("parse", str(e)[:200], meta.get("http_status"), meta.get("ms")),
                       source)
        raise
    events = []
    errors = []
    expanded = expand_calendar_events(ics_cal.events, errors=errors)
    record_check(url, calendar_name, True, meta, feed_stats(expanded, errors), source=source)
    # Expand recurring events into individual occurrences.
    for event, start, end, occ_uid in expanded:
        duration = (end - start).total_seconds() / 3600
        events.append({
            "calendar": calendar_name,
            "event_name": event.name or "Untitled Event",
            "start": start,
            "end": end,
            "duration_hours": duration,
            "uid": occ_uid,
            "free": is_free_event(event),
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


def load_all_events(kind="time", sync_all=None):
    """Sync calendars and return the events of `kind` ("time" or "tags").

    By default every configured calendar is synced, so a Sync on any page
    also refreshes the others. With the "sync_scope" setting "page", only
    calendars of `kind` are fetched. `sync_all=True` (Home) always syncs
    every calendar.
    """
    try:
        calendar_data, source_type = load_calendar_urls()
        if not calendar_data:
            return None, None

        if sync_all is None:
            sync_all = load_app_settings().get("sync_scope", "all") != "page"
        all_events = []
        for calendar_info in calendar_data:
            if calendar_info["type"] != kind and not sync_all:
                continue
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


def select_month_range(df, months=None):
    """Start/end date pickers. `months` overrides the default range length
    from the Settings page (0 = all data)."""
    min_date = df["start"].min().date()
    max_date = df["start"].max().date()

    now = datetime.now().date()
    # Default range length comes from the Settings page (12 months unless changed)
    if months is None:
        months = load_app_settings().get("default_range_months", 12)
    months = int(months or 0)
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
    # Drop events marked as free / available (iCal TRANSP:TRANSPARENT).
    "ignore_free_events": False,
    # Initial "View data by" choice on every page (calendars.json only).
    "default_view": "calendar",
    # Initial length of the date range pickers in months (0 = all data).
    "default_range_months": 12,
    # Tags page: preselected chart labels ("top" = the N most frequent,
    # "fixed" = tags_chart_fixed in that order).
    "tags_chart_mode": "top",
    "tags_chart_top_n": 8,
    "tags_chart_fixed": [],
    # Replace fixed labels without entries in the range by top labels.
    "tags_chart_fill": True,
    # Keep the chart selection when switching view or date range.
    "tags_keep_selection": True,
    # Initial "Show" view: "grouped", "groups" or "tags".
    "tags_default_view": "grouped",
    # Initial "Count per": "Day", "Week" or "Month".
    "tags_default_granularity": "Week",
    # Tags left out of the Tags page entirely.
    "tags_hidden": [],
    # Date range of the Tags page in months (None = use default_range_months, 0 = all data).
    "tags_range_months": None,
    # Tag calendars preselected when there are several (empty = all).
    "tags_default_calendars": [],
    # Merge "☕ Coffee" and "☕️ Coffee" (emoji variation selector, spacing).
    "tags_merge_variants": True,
    # 🔄 Sync Calendars on a page: "all" calendars, or only those the page
    # shows ("page": time calendars on time pages, tag calendars on Tags).
    # Home always syncs all.
    "sync_scope": "all",

    # --- Calendar Health page ---
    "health_fail_threshold": 2,  # failed fetches in a row before a calendar counts as failing
    "health_drop_percent": 50,   # warn when the event count drops by this much (%)
    "health_frozen_days": 45,    # warn when nothing in a feed changed for this long (0 = off)
    "health_slow_seconds": 10,   # warn when a fetch takes longer
    "health_stale_hours": 48,    # warn when a calendar wasn't checked for this long
    "health_banner": True,       # show failing calendars at the top of every page

    # --- Free-slot suggestions (Goals page) ---
    # "all": every time calendar blocks (also ones added later); events
    # marked as free only block with slots_free_blocks.
    # "custom": per calendar in slots_calendars ({name: {"block", "free"}});
    # calendars not listed there (added later) follow slots_new_calendars_block.
    "slots_blocking_mode": "all",
    "slots_free_blocks": False,
    "slots_calendars": {},
    "slots_new_calendars_block": True,
    # All-day events (trips, holidays) block the whole day.
    "slots_all_day_blocks": False,
    # Blocked every week; day = Mon..Sun, "Weekdays", "Weekend" or "Every
    # day"; a block whose "to" isn't after "from" runs past midnight.
    "slots_schedule": [{"day": "Every day", "from": "22:00", "to": "07:00", "label": "Night"}],
    # Sleep: "none", "calendar" (events of slots_sleep_calendar) or
    # "markers" (events containing slots_bed_text, ended by slots_wake_text
    # or the fixed slots_wake_times).
    "slots_sleep_mode": "none",
    "slots_sleep_calendar": "",
    "slots_marker_calendar": "",  # "" = any calendar
    "slots_bed_text": "going to bed",
    "slots_wake_mode": "event",  # "event" or "fixed"
    "slots_wake_text": "waking up",
    "slots_wake_times": {d: "07:00" for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")},
    # Blocked before going to bed (time to do nothing).
    "slots_winddown_minutes": 30,
    # Nights without sleep data use the usual times of the last 4 weeks.
    "slots_sleep_fallback": True,
    "slots_min_minutes": 30,
    "slots_buffer_minutes": 15,
    "slots_max_per_goal": 3,
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
        settings["ignore_free_events"] = st.session_state[f"{page_key}_ignore_free"]
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
        st.checkbox(
            "Ignore events marked as free",
            value=bool(settings.get("ignore_free_events")),
            key=f"{page_key}_ignore_free",
            on_change=_save,
            help="Leave out events whose availability is \"Free\" instead of "
                 "\"Busy\". Sync once after updating so the cache knows which "
                 "events are free.",
        )
    return settings


def drop_free_events(df, settings=None):
    """Drop events marked as free if the user chose to ignore them.

    Events cached before the "free" column existed count as busy.
    """
    settings = settings or load_app_settings()
    if settings.get("ignore_free_events") and not df.empty and "free" in df:
        free = df["free"].map(lambda v: str(v).strip().lower() == "true")
        df = df[~free]
    return df


def apply_accuracy_settings(df, settings=None):
    """Drop long events from `df` if the user chose to exclude them, and
    free events if the user chose to ignore them."""
    from analysis import long_event_mask

    settings = settings or load_app_settings()
    df = drop_free_events(df, settings)
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
    """Goals as a list of {type, name, period, min, max} dicts.

    type: "calendar", "category", "tag" or "tag_group";
    period: "day", "week" or "month".
    """
    from analysis import GOAL_TYPES, PERIODS

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
        if goal["name"] and goal["type"] in GOAL_TYPES \
                and goal["period"] in PERIODS \
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
    render_health_banner()
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


GOAL_TYPE_LABELS = {
    "calendar": ("📅", "Calendar"),
    "category": ("🗂️", "Category"),
    "tag": ("🏷️", "Tag"),
    "tag_group": ("🔖", "Tag group"),
}


def goal_type_label(goal_type):
    """"📅 Calendar", "🏷️ Tag", ... for a goal type."""
    icon, name = GOAL_TYPE_LABELS.get(goal_type, ("", goal_type))
    return f"{icon} {name}".strip()


def this_period_text(period):
    return "today" if period == "day" else f"this {period}"


def is_sleep_goal(goal, settings=None):
    """Calendar goals for the sleep calendar (Settings → 💡 Free slots) or a
    calendar named like one ("sleep", "schlaf")."""
    if goal.get("type") != "calendar":
        return False
    settings = load_app_settings() if settings is None else settings
    sleep_calendar = settings.get("slots_sleep_calendar") if settings.get("slots_sleep_mode") == "calendar" else None
    return goal["name"] == sleep_calendar or any(w in goal["name"].casefold() for w in ("sleep", "schlaf"))


def goal_estimate_kind(df, goal, settings=None):
    """How the rest of a goal's period is estimated: "sleep" (usual nights),
    "activity" (usual hours per weekday, for oura-activity-ical calendars)
    or None for goals that only count recorded and booked events."""
    if goal.get("type") != "calendar":
        return None
    if is_sleep_goal(goal, settings):
        return "sleep"
    from analysis import is_oura_activity
    if is_oura_activity(df[df["calendar"] == goal["name"]]):
        return "activity"
    return None


def goal_frame(df, goal, kind):
    """`df` as a goal of `kind` counts it: activity goals count only timed
    workouts, not oura-activity-ical's all-day daily and monthly summaries."""
    if kind != "activity":
        return df
    from analysis import is_all_day
    return df[~((df["calendar"] == goal["name"]) & is_all_day(df["start"], df["end"]))]


def goal_estimate(df, goal, kind, first, end, now):
    """Estimated amount not recorded yet for a sleep or activity goal from
    date `first` up to date `end` (exclusive), to add to done + booked.
    `df` is the goal's frame (`goal_frame`). Returns {"hours", "note"} or
    None without past data to estimate from."""
    from analysis import (WEEKDAY_ABBR, expected_daily, expected_sleep, goal_events, local_midnight,
                          sleep_from_calendar, usual_sleep_text)

    sub = goal_events(df, goal)
    tz = now.tz
    if kind == "sleep":
        pairs = sleep_from_calendar(sub)
        if not pairs:
            return None
        hours, nights = expected_sleep(pairs, local_midnight(first, tz), local_midnight(end, tz), now, tz)
        note = (f"😴 **{goal['name']}**: {len(nights)} night(s) not recorded yet are estimated (+{hours:.1f}h) "
                f"from your usual sleep, {usual_sleep_text(pairs, now, tz)}.") if nights else ""
        return {"hours": hours, "note": note}
    if kind == "activity":
        hours, usual = expected_daily(sub, first, end, now)
        if not usual:
            return None
        days = (end - max(first, now.date())).days
        note = ""
        if days > 0:
            per_day = ", ".join(f"{WEEKDAY_ABBR[wd]} {usual[wd]:.1f}h" for wd in range(7))
            note = (f"👟 **{goal['name']}**: the rest of today and {days - 1} more day(s) are estimated "
                    f"(+{hours:.1f}h) from your average of the last 4 weeks: {per_day}.")
        return {"hours": hours, "note": note}
    return None


def evaluate_goals(df, goals, now, history=12, tags=None, groups=()):
    """[(goal, result)] for the current period of every goal, as on the Goals
    page and Home: `evaluate_goal` plus, for sleep and oura-activity-ical
    goals, the estimate of what isn't recorded yet (result["expected"],
    result["estimate_note"])."""
    from analysis import estimated_goal_status, evaluate_goal

    settings = load_app_settings()
    results = []
    for goal in goals:
        kind = goal_estimate_kind(df, goal, settings)
        frame = goal_frame(df, goal, kind)
        r = evaluate_goal(frame, goal, now, history, tags, groups)
        if kind:
            est = goal_estimate(frame, goal, kind, r["period_start"], r["period_end"] + timedelta(days=1), now)
            if est is not None:
                r["expected"] = r["booked"] + est["hours"]
                r["status"], r["status_label"] = estimated_goal_status(goal, r["done"], r["expected"])
                r["estimate_note"] = est["note"]
        results.append((goal, r))
    return results


def render_goal_progress(goal, result):
    """One progress bar for a goal's current period (Home and Goals page)."""
    from analysis import format_goal_amount, goal_target_text, is_tag_goal

    icon, type_name = GOAL_TYPE_LABELS.get(goal["type"], ("", goal["type"]))
    target = goal["min"] if goal["min"] is not None else goal["max"]
    fraction = min(result["done"] / target, 1.0) if target else 0.0
    if is_tag_goal(goal):
        amounts = f"{format_goal_amount(goal, result['done'])}"
        if result["booked"] != result["done"]:
            amounts += f" ({format_goal_amount(goal, result['booked'])} incl. planned)"
        amounts += " of"
    elif "expected" in result:
        amounts = (f"{format_goal_amount(goal, result['done'])} done, "
                   f"~{format_goal_amount(goal, result['expected'])} expected of")
    else:
        amounts = (f"{format_goal_amount(goal, result['done'])} done, "
                   f"{format_goal_amount(goal, result['booked'])} booked of")
    st.progress(
        fraction,
        text=(
            f"{icon} **{goal['name']}** ({type_name.lower()}) · {amounts} "
            f"{goal_target_text(goal)} {this_period_text(result['period'])}"
            f" — {result['status_label']}"
        ),
    )


def load_tag_occurrences():
    """Tag occurrences of all tag calendars from the local cache, with tag
    names as on the Tags page (variants merged, hidden tags left out).

    Used by tag goals. Empty DataFrame when there are no tag events.
    """
    from analysis import TAG_COLUMNS, drop_tags, merge_tag_variants, tag_occurrences

    events, _ = load_all_events_from_cache("tags")
    if not events:
        return pd.DataFrame(columns=TAG_COLUMNS)
    df = pd.DataFrame(events)
    df["calendar"] = df["calendar_name"].apply(normalize_calendar_name)
    df = normalize_time(df, tz="local")
    occ = tag_occurrences(df)
    settings = load_app_settings()
    if settings.get("tags_merge_variants", True):
        occ = merge_tag_variants(occ)
    return drop_tags(occ, settings.get("tags_hidden", []))


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


def write_file_with_backup(path, text):
    """Write `text` to `path` atomically, keeping the previous version as
    `<path>.bak`."""
    if os.path.exists(path):
        import shutil
        shutil.copy2(path, path + ".bak")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write_calendars_config(data, path=CALENDARS_JSON):
    """Write calendars.json, keeping the previous version as calendars.json.bak."""
    write_file_with_backup(path, json.dumps(data, indent=4, ensure_ascii=False) + "\n")


# --- Tag groups (tag_groups.json) ---

TAG_GROUPS_FILE = "tag_groups.json"


def _clean_group(group):
    """A valid tag group dict, or None."""
    if not isinstance(group, dict):
        return None
    name = str(group.get("name", "")).strip()
    tags = [str(t) for t in group.get("tags", []) if str(t).strip()]
    keywords = [str(k).strip() for k in group.get("keywords", []) if str(k).strip()]
    if not name or not (tags or keywords):
        return None
    return {"name": name, "tags": tags, "keywords": keywords}


def load_tag_groups():
    """Tag groups as a list of {name, tags, keywords} dicts, in saved order."""
    try:
        with open(TAG_GROUPS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f).get("groups", [])
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return []
    return [g for g in (_clean_group(g) for g in raw) if g]


def save_tag_groups(groups):
    with open(TAG_GROUPS_FILE, "w", encoding="utf-8") as f:
        json.dump({"groups": [g for g in (_clean_group(g) for g in groups) if g]},
                  f, indent=2, ensure_ascii=False)


# --- Free-slot suggestions (Goals page, Settings) ---

def load_raw_events(kind="time"):
    """Events of `kind` from the cache with local times and display calendar
    names, without the data-accuracy rules (long and free events kept)."""
    events, _ = load_all_events_from_cache(kind)
    if not events:
        return pd.DataFrame(columns=["calendar", "event_name", "start", "end", "free"])
    df = pd.DataFrame(events)
    df["calendar"] = df["calendar_name"].apply(normalize_calendar_name)
    return normalize_time(df, tz="local")


def slot_block_rule(settings):
    """Function calendar name -> None (doesn't block), "busy" (only events
    marked busy) or "all" (events marked as free block too)."""
    if settings.get("slots_blocking_mode", "all") != "custom":
        rule = "all" if settings.get("slots_free_blocks") else "busy"
        return lambda calendar: rule
    per_calendar = settings.get("slots_calendars", {})
    new_rule = "busy" if settings.get("slots_new_calendars_block", True) else None

    def rule_for(calendar):
        cfg = per_calendar.get(calendar)
        if cfg is None:
            return new_rule
        if not cfg.get("block"):
            return None
        return "all" if cfg.get("free") else "busy"
    return rule_for


def sleep_pairs(settings, time_events=None, tag_events=None):
    """(bed, wake) pairs from the configured sleep source, [] if none."""
    from analysis import sleep_from_calendar, sleep_from_markers

    mode = settings.get("slots_sleep_mode", "none")
    if mode not in ("calendar", "markers"):
        return []
    frames = [f for f in (time_events, tag_events) if f is not None and not f.empty]
    if not frames:
        return []
    events = pd.concat([f[["calendar", "event_name", "start", "end"]] for f in frames], ignore_index=True)
    if mode == "calendar":
        return sleep_from_calendar(events[events["calendar"] == settings.get("slots_sleep_calendar")])
    marker_calendar = settings.get("slots_marker_calendar") or ""
    if marker_calendar:
        events = events[events["calendar"] == marker_calendar]
    fixed = settings.get("slots_wake_mode") == "fixed"
    return sleep_from_markers(
        events,
        settings.get("slots_bed_text", ""),
        wake_text=None if fixed else settings.get("slots_wake_text"),
        wake_times=settings.get("slots_wake_times") if fixed else None,
        tz=events["start"].dt.tz,
    )


def slot_parts(settings, now, time_events=None, tag_events=None, buffer_minutes=None):
    """The pieces of blocked time for the free-slot settings, as functions
    of (lo, hi): {"busy": events of blocking calendars (widened by the
    buffer), "off": weekly blocked times + sleep, "nights": (night, bed,
    wake, source) rows for display}. `buffer_minutes` overrides the setting."""
    from analysis import busy_intervals, merge_intervals, schedule_intervals, sleep_intervals

    time_events = load_raw_events("time") if time_events is None else time_events
    needs_tags = settings.get("slots_sleep_mode") in ("calendar", "markers")
    if tag_events is None and needs_tags:
        tag_events = load_raw_events("tags")
    tz = now.tz
    base_rule = slot_block_rule(settings)
    sleep_calendar = settings.get("slots_sleep_calendar") if settings.get("slots_sleep_mode") == "calendar" else None

    def rule_for(calendar):
        # The sleep calendar is sleep (with its wind-down), not busy time.
        return None if sleep_calendar and calendar == sleep_calendar else base_rule(calendar)

    pairs = sleep_pairs(settings, time_events, tag_events)
    fixed_wake = settings.get("slots_wake_times") if (
        settings.get("slots_sleep_mode") == "markers" and settings.get("slots_wake_mode") == "fixed") else None
    buffer = settings.get("slots_buffer_minutes", 0) if buffer_minutes is None else buffer_minutes

    def sleep_for(lo, hi):
        if settings.get("slots_sleep_mode", "none") == "none":
            return [], []
        return sleep_intervals(pairs, lo, hi, tz, now, settings.get("slots_winddown_minutes", 0),
                               settings.get("slots_sleep_fallback", True), fixed_wake)

    return {
        "busy": lambda lo, hi: busy_intervals(time_events, lo, hi, rule_for,
                                              settings.get("slots_all_day_blocks", False), buffer),
        "off": lambda lo, hi: merge_intervals(schedule_intervals(lo, hi, settings.get("slots_schedule", []), tz)
                                              + sleep_for(lo, hi)[0]),
        "nights": lambda lo, hi: sleep_for(lo, hi)[1],
        "rule_for": rule_for,
    }


def slot_blocker(settings, now, time_events=None, tag_events=None):
    """(blocked_for, sleep_nights_for) for the free-slot settings.

    blocked_for(lo, hi) -> merged blocked intervals; sleep_nights_for(lo, hi)
    -> (night, bed, wake, source) rows for display.
    """
    from analysis import merge_intervals

    parts = slot_parts(settings, now, time_events, tag_events)
    return (lambda lo, hi: merge_intervals(parts["busy"](lo, hi) + parts["off"](lo, hi))), parts["nights"]


# --- Calendar health (Calendar Health page, banner) ---

def health_thresholds(settings=None):
    settings = settings or load_app_settings()
    return {
        "fail_threshold": int(settings.get("health_fail_threshold", 2)),
        "drop_percent": float(settings.get("health_drop_percent", 50)),
        "frozen_days": int(settings.get("health_frozen_days", 45)),
        "slow_seconds": float(settings.get("health_slow_seconds", 10)),
        "stale_hours": float(settings.get("health_stale_hours", 48)),
    }


def calendar_health_rows(settings=None):
    """(calendar info, status) for every configured calendar, in config order."""
    from calendar_health import calendar_status, load_health
    from calendar_store import hash_url

    calendars, _ = load_calendar_urls()
    data = load_health()["calendars"]
    thresholds = health_thresholds(settings)
    return [(c, calendar_status(data.get(hash_url(c["url"]), {}).get("checks", []), thresholds=thresholds))
            for c in (calendars or [])]


def render_health_banner():
    """A warning at the top of a page when calendars are failing."""
    settings = load_app_settings()
    if not settings.get("health_banner", True):
        return
    try:
        failing = [c["custom_name"] for c, status in calendar_health_rows(settings) if status["level"] == "failing"]
    except Exception:
        return  # never break a page over the health banner
    if failing:
        names = ", ".join(failing[:3]) + (f" and {len(failing) - 3} more" if len(failing) > 3 else "")
        st.warning(f"🩺 {len(failing)} calendar(s) can't be fetched: {names}. Their cached events are still "
                   "used. Details on the Calendar Health page.")

