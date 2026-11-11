import streamlit as st
import pandas as pd
import requests
from datetime import datetime, date, timedelta, timezone
import altair as alt
import os
import shutil
import calendar
import json
import tzlocal
import threading
import time
from utils import (
    default_view_index,
    load_calendar_urls, load_all_events, load_all_events_from_cache, parse_ics_from_url,
    select_month_range, fetch_and_parse_ics, render_accuracy_settings, apply_accuracy_settings,
    build_events_df, load_goals, render_goal_progress,
)
from analysis import (
    clip_to_range, long_event_mask, find_overlaps, union_hours, evaluate_goal,
    booked_weeks, period_label,
)

# Streamlit UI
st.set_page_config(page_title="CalendarTimeTracker", layout="wide")
st.title("CalendarTimeTracker")
st.caption("Analyze time usage from multiple public calendar (.ics) URLs")

# Add performance improvements
if "main_app_loaded" not in st.session_state:
    st.session_state.main_app_loaded = False

def get_version():
    """Read version from version.txt file"""
    try:
        with open("version.txt", "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "Unknown"

@st.cache_data(ttl=3600, show_spinner="Fetching latest version from GitHub...")  # Cache for 1 hour
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

def _version_tuple(v):
    """Parse a dotted version string into a tuple of ints for comparison."""
    parts = []
    for p in v.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)

def is_update_available(current, latest):
    """True if `latest` is a strictly newer version than `current`.

    Uses numeric component comparison so e.g. 2.10 is correctly newer than
    2.9 (a plain string compare would treat "2.9" > "2.10").
    """
    if current == "Unknown" or latest == "Unknown":
        return False
    try:
        return _version_tuple(current) < _version_tuple(latest)
    except Exception:
        return False

def _fetch_and_parse_ics(url, calendar_name):
    """Core function to fetch and parse ICS - no Streamlit dependencies."""
    try:
        return fetch_and_parse_ics(url, calendar_name)
    except Exception as e:
        print(f"Error loading {url}: {e}")
        return []

# --- Background auto-refresh (runs 5 minutes before 24-hour TTL expires) ---
_CACHE_TTL = 86400  # 24 hours in seconds
_REFRESH_INTERVAL = _CACHE_TTL - (5 * 60)  # Refresh 5 minutes before expiry
_refresh_lock = threading.Lock()
_refresher_started = False

def _load_calendar_urls_no_streamlit(calendars_json_file="calendars.json", txt_file="calendars.txt"):
    """Load calendar URLs without Streamlit dependencies (for background thread)."""
    try:
        if os.path.exists(calendars_json_file):
            with open(calendars_json_file, 'r') as file:
                calendar_data = json.load(file)
            return [(c["url"], c.get("custom_name", "") or "Unnamed") for c in calendar_data['calendars']]
        elif os.path.exists(txt_file):
            calendars = []
            with open(txt_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        parts = line.split("#")
                        url = parts[0].strip()
                        custom_name = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "Unnamed"
                        calendars.append((url, custom_name))
            return calendars
        return []
    except Exception as e:
        print(f"[CalendarTimeTracker] Error loading calendar config: {e}")
        return []

def _refresh_calendar_cache_once():
    """Run a single cache-refresh pass."""
    try:
        print(f"[CalendarTimeTracker] Auto-refresh started at {datetime.now()}")
        # Fetch calendars and update local CSV cache (bypasses Streamlit cache)
        calendars = _load_calendar_urls_no_streamlit()
        for url, custom_name in calendars:
            _fetch_and_parse_ics(url, custom_name)  # Updates local CSV cache
        # Clear Streamlit cache so next user request gets fresh data
        parse_ics_from_url.clear()
        print(f"[CalendarTimeTracker] Auto-refresh completed at {datetime.now()}")
    except Exception as e:
        print(f"[CalendarTimeTracker] Auto-refresh failed: {e}")

_refresher_lock = threading.Lock()
_refresher_stop = None

def _start_thread(stop_event: threading.Event):
    def worker():
        # Single loop that uses the stop_event for sleeping so the thread
        # exits promptly when the event is set (e.g. on Streamlit reload).
        while not stop_event.wait(_REFRESH_INTERVAL):
            _refresh_calendar_cache_once()
    t = threading.Thread(target=worker, daemon=True)
    t.start()

@st.cache_resource
def start_background_refresher_once():
    global _refresher_stop

    with _refresher_lock:
        # If a previous refresher exists in this process, stop it (covers code reload edge cases)
        if _refresher_stop is not None and not _refresher_stop.is_set():
            _refresher_stop.set()

        stop_event = threading.Event()
        _refresher_stop = stop_event
        _start_thread(stop_event)
        return stop_event

# Start background refresher on module load
start_background_refresher_once()


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
    st.subheader("Summary Table")
    with st.expander("📋 Show Summary Table"):
        st.caption(f"Showing events from {start_date} to {end_date}")

        # Calculate total days and weeks in the selected range
        days_span = (end_date - start_date).days + 1
        weeks_span = days_span / 7

        # Only the in-range part of events that straddle the boundary (or span
        # multiple days) counts, consistent with the duration charts.
        clipped = clip_to_range(df, start_date, end_date)
        summary = (
            clipped.groupby("group")["duration_hours"]
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

    # Vectorized time proportion calculation
    def calculate_time_proportions_vectorized(df, periods_df, period_start_col, period_end_col, label_col):
        """Calculate time proportions using vectorized operations instead of nested loops."""
        results = []
        tz = df['start'].dt.tz
        
        for _, period in periods_df.iterrows():
            period_start = pd.Timestamp(period[period_start_col])
            period_end = pd.Timestamp(period[period_end_col])
            
            # Make timezone aware if needed. Use shift_forward / NaT to survive
            # DST transitions where midnight may be ambiguous or non-existent
            # (events near these boundaries were silently dropped before).
            if tz is not None:
                if period_start.tz is None:
                    period_start = period_start.tz_localize(
                        tz, nonexistent='shift_forward', ambiguous='NaT'
                    )
                if period_end.tz is None:
                    period_end = period_end.tz_localize(
                        tz, nonexistent='shift_forward', ambiguous='NaT'
                    )
                # If localization produced NaT (ambiguous DST midnight), skip
                # this period rather than crashing on the comparison below.
                if pd.isna(period_start) or pd.isna(period_end):
                    continue
            
            # Set end of period to end of day
            period_end = period_end.replace(hour=23, minute=59, second=59)
            
            # Vectorized overlap calculation
            event_starts = df['start'].clip(lower=period_start)
            event_ends = df['end'].clip(upper=period_end)
            
            # Calculate durations (only positive overlaps)
            durations = (event_ends - event_starts).dt.total_seconds() / 3600
            durations = durations.clip(lower=0)
            
            # Filter events with positive duration and aggregate
            mask = durations > 0
            if mask.any():
                period_df = df.loc[mask, ['group']].copy()
                period_df['duration_hours'] = durations[mask]
                period_df['time_label'] = period[label_col]
                results.append(period_df)
        
        if results:
            return pd.concat(results, ignore_index=True)
        return pd.DataFrame()

    # Create time-based data frames based on the selected date_option
    time_aggregation = pd.DataFrame()

    if date_option == "week":
        # Generate all weeks in the selected range
        weeks = []
        # Snap to the Monday of the start week so the +7 day stride lands on
        # every ISO week up to and including the week containing end_date.
        # (Striding from an arbitrary weekday could skip the trailing week.)
        current_date = start_date - timedelta(days=start_date.weekday())
        seen_weeks = set()
        while current_date <= end_date:
            year, week, _ = current_date.isocalendar()
            week_key = (year, week)
            if week_key not in seen_weeks:
                seen_weeks.add(week_key)
                weeks.append({
                    'year': year, 
                    'week': week, 
                    'week_start': datetime.fromisocalendar(year, week, 1),
                    'week_end': datetime.fromisocalendar(year, week, 7),
                    'time_label': f"{year}-W{week:02d}"
                })
            current_date += timedelta(days=7)

        weeks_df = pd.DataFrame(weeks)
        time_aggregation = calculate_time_proportions_vectorized(
            df, weeks_df, 'week_start', 'week_end', 'time_label'
        )

    elif date_option == "day":
        # Generate all days in the selected range
        days = []
        current_date = start_date
        while current_date <= end_date:
            days.append({
                'day_start': datetime.combine(current_date, datetime.min.time()),
                'day_end': datetime.combine(current_date, datetime.min.time()),
                'time_label': current_date.strftime('%Y-%m-%d')
            })
            current_date += timedelta(days=1)

        days_df = pd.DataFrame(days)
        time_aggregation = calculate_time_proportions_vectorized(
            df, days_df, 'day_start', 'day_end', 'time_label'
        )

    else:  # Month
        # Generate all months in the selected range
        months = []
        current_date = date(start_date.year, start_date.month, 1)
        while current_date <= end_date:
            month_end = date(current_date.year, current_date.month, 
                            calendar.monthrange(current_date.year, current_date.month)[1])
            months.append({
                'month_start': current_date,
                'month_end': month_end,
                'time_label': f"{current_date.year}-{current_date.month:02d}"
            })
            # Move to next month
            if current_date.month == 12:
                current_date = date(current_date.year + 1, 1, 1)
            else:
                current_date = date(current_date.year, current_date.month + 1, 1)

        months_df = pd.DataFrame(months)
        time_aggregation = calculate_time_proportions_vectorized(
            df, months_df, 'month_start', 'month_end', 'time_label'
        )

    if time_aggregation.empty:
        st.info(f"No data to display for the selected time range.")
        return

    # Aggregate by time_label and group
    time_aggregation = time_aggregation.groupby(['time_label', 'group']).agg(
        duration_hours=('duration_hours', 'sum')
    ).reset_index()

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

def show_upcoming_weeks(full_df, group_mode, today, weeks_ahead=4):
    """Hours already booked this week and the next weeks vs a typical week."""
    future, summary, typical_total, n_weeks = booked_weeks(full_df, "group", today, weeks_ahead)
    if future.empty or summary["booked"].sum() == 0:
        return

    labels = {
        ws: ("This week" if i == 0 else "Next week" if i == 1 else f"{period_label('week', ws)} ({ws:%d %b})")
        for i, ws in enumerate(summary.index)
    }
    busy = [labels[ws] for ws in summary.index[summary["busy"]]]
    title = "🔮 Upcoming Weeks"
    if busy:
        title += f" — {busy[0].lower() if busy[0] in ('This week', 'Next week') else busy[0]} looks busy"

    with st.expander(title, expanded=bool(busy)):
        st.caption(
            "Hours already in your calendars. Future weeks are usually incomplete, so read this as "
            f"\"booked so far\", not a prediction. Typical week = average of the last {n_weeks} complete weeks."
        )
        cols = st.columns(len(summary))
        for col, (ws, row) in zip(cols, summary.iterrows()):
            delta = f"{row['ratio'] * 100:.0f}% of typical" if typical_total else None
            col.metric(
                labels[ws], f"{row['booked']:.1f}h", delta=delta,
                delta_color="inverse" if row["busy"] else "off",
            )

        long = future.reset_index().melt(id_vars="period_start", var_name="group", value_name="hours")
        long = long[long["hours"] > 0]
        long["week"] = long["period_start"].map(labels)
        color_mapping = full_df.groupby("group")["color"].first().to_dict()
        bars = alt.Chart(long).mark_bar().encode(
            x=alt.X("week:N", title="", sort=[labels[ws] for ws in summary.index], axis=alt.Axis(labelAngle=0)),
            y=alt.Y("hours:Q", title="Hours booked"),
            color=alt.Color(
                "group:N", title=group_mode.title(),
                scale=alt.Scale(domain=list(color_mapping.keys()), range=list(color_mapping.values())),
            ),
            tooltip=["week:N", alt.Tooltip("group:N", title=group_mode.title()), alt.Tooltip("hours:Q", format=".1f")],
        )
        layers = [bars]
        if typical_total:
            layers.append(
                alt.Chart(pd.DataFrame({"y": [typical_total]}))
                .mark_rule(strokeDash=[4, 4], color="#555")
                .encode(y="y:Q", tooltip=[alt.Tooltip("y:Q", title="Typical week (h)", format=".1f")])
            )
        st.altair_chart(alt.layer(*layers).properties(height=280), use_container_width=True)


def show_data_quality(df_all_events, df_counted, start_date, end_date, accuracy):
    """Flag long events and overlapping events that distort the totals."""
    max_hours = accuracy.get("long_event_hours", 16)
    in_range = clip_to_range(df_all_events, start_date, end_date)
    long_mask = long_event_mask(in_range, max_hours)
    long_events = in_range[long_mask]
    timed = clip_to_range(df_counted, start_date, end_date)
    timed = timed[~long_event_mask(timed, max_hours)]
    overlaps = find_overlaps(timed)
    raw_hours = ((timed["end"] - timed["start"]).dt.total_seconds() / 3600).sum()
    double_counted = max(raw_hours - union_hours(timed), 0.0)

    excluded = accuracy.get("long_events") == "exclude"
    label = "🩺 Data Quality"
    if not long_events.empty or double_counted > 0.05:
        label += " — check your totals"
    with st.expander(label):
        total_hours = in_range["duration_hours"].sum()
        c1, c2, c3 = st.columns(3)
        c1.metric(
            "Long events",
            f"{len(long_events)}",
            help=f"All-day events and events longer than {max_hours}h.",
        )
        c2.metric(
            "Hours in long events",
            f"{long_events['duration_hours'].sum():.1f}h",
            delta=("excluded" if excluded else
                   f"{long_events['duration_hours'].sum() / total_hours * 100:.0f}% of total" if total_hours else None),
            delta_color="off",
        )
        c3.metric(
            "Double-counted hours",
            f"{double_counted:.1f}h",
            help="Hours where two or more (non-long) events overlap and are counted more than once.",
        )
        st.caption(
            "Long events are currently "
            + ("**excluded** from all totals." if excluded else "**counted fully**.")
            + " Change this under ⚖️ Data accuracy in the sidebar."
        )

        if not long_events.empty:
            st.markdown("**Long events in range** (hours inside the selected range)")
            show = long_events.sort_values("duration_hours", ascending=False)[
                ["event_name", "calendar", "start", "end", "duration_hours"]
            ].copy()
            show["start"] = show["start"].dt.strftime("%Y-%m-%d %H:%M")
            show["end"] = show["end"].dt.strftime("%Y-%m-%d %H:%M")
            show["duration_hours"] = show["duration_hours"].round(1)
            show.columns = ["Event", "Calendar", "Start", "End", "Hours"]
            st.dataframe(show, use_container_width=True, hide_index=True)

        if not overlaps.empty:
            st.markdown("**Overlapping events by calendar pair**")
            pair = overlaps.apply(
                lambda r: " ↔ ".join(sorted([r["calendar_a"], r["calendar_b"]])), axis=1
            )
            by_pair = (
                overlaps.assign(pair=pair)
                .groupby("pair")["overlap_hours"]
                .agg(Overlaps="count", Hours="sum")
                .sort_values("Hours", ascending=False)
                .reset_index()
                .rename(columns={"pair": "Calendars"})
            )
            by_pair["Hours"] = by_pair["Hours"].round(1)
            st.dataframe(by_pair, use_container_width=True, hide_index=True)
            st.caption(
                "Some overlap is expected (e.g. a commute calendar next to work). "
                "The same event in two calendars is counted twice."
            )

            show = overlaps.sort_values("start_b", ascending=False).head(200).copy()
            show["When"] = show["start_b"].dt.strftime("%Y-%m-%d %H:%M")
            show["overlap_hours"] = show["overlap_hours"].round(2)
            show = show[["When", "calendar_a", "event_a", "calendar_b", "event_b", "overlap_hours"]]
            show.columns = ["When", "Calendar A", "Event A", "Calendar B", "Event B", "Overlap (h)"]
            st.markdown("**Most recent overlaps**")
            st.dataframe(show, use_container_width=True, hide_index=True)

        if long_events.empty and overlaps.empty:
            st.success("No long or overlapping events in the selected range.")


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
    st.markdown("### About")
    version = get_version()
    latest_version = get_latest_github_version()
    
    st.markdown(f"**Current Version:** {version}")
    st.markdown(f"**Latest Version:** {latest_version}")
    
    update_available = is_update_available(version, latest_version)
    
    if update_available:
        st.info("🔄 A newer version is available!")
        if st.button("⬇️ Update Now", help="Pull latest version from GitHub"):
            import subprocess
            result = subprocess.run(["git", "pull"], capture_output=True, text=True)
            if result.returncode == 0:
                st.success("✅ Updated! Please restart the app.")
                st.code(result.stdout)
            else:
                st.error(f"Update failed: {result.stderr}")
    else:
        if st.button("🔄 Check for Updates", help="Check GitHub for new releases"):
            get_latest_github_version.clear()
            # Fetch immediately after clearing
            new_version = get_latest_github_version()
            if is_update_available(version, new_version):
                st.info(f"🔄 Update available: v{new_version}")
            else:
                st.success("✅ You're on the latest version!")
    
    st.markdown("🔗 [GitHub Repository](https://github.com/ramhee98/CalendarTimeTracker)")

# --- Cache management ---
col1, col2, col3 = st.columns([2, 1, 1])
with col2:
    if st.button("⚡ Quick Load", help="Load instantly from local cache"):
        st.session_state.main_app_loaded = False
        st.session_state.force_refresh = False
        st.rerun()
with col3:
    if st.button("🔄 Sync Calendars", help="Fetch latest data from calendar URLs"):
        st.cache_data.clear()
        st.session_state.main_app_loaded = False
        st.session_state.force_refresh = True
        st.rerun()

# Load events - from cache (instant) or URLs (on refresh)
force_refresh = st.session_state.get("force_refresh", False)
if force_refresh:
    st.session_state.force_refresh = False  # Reset flag
    with st.spinner("Fetching latest calendar data from URLs..."):
        all_events, source_type = load_all_events(sync_all=True)
else:
    with st.spinner("Loading calendar data from cache..."):
        all_events, source_type = load_all_events_from_cache()
        if not all_events:
            # No cache exists yet, fetch from URLs
            with st.spinner("No cache found, fetching from URLs..."):
                all_events, source_type = load_all_events(sync_all=True)

# Mark main app as loaded
st.session_state.main_app_loaded = True

if all_events:
    # Optional selector (only if JSON is used)
    if source_type == "json":
        group_mode = st.selectbox(
            "View data by",
            options=["calendar", "category"],
            index=default_view_index(),
            format_func=str.title,
            key="view_mode"
        )
    else:
        group_mode = "calendar"
    
    accuracy = render_accuracy_settings("home")

    # Goals and upcoming weeks are about now, independent of the date range below.
    full_df = build_events_df(all_events, accuracy)
    full_df["group"] = full_df[group_mode]
    now = pd.Timestamp.now(tz=full_df["start"].dt.tz)
    goals = load_goals()
    if goals:
        st.subheader("🎯 Goals")
        goal_cols = st.columns(2)
        for i, goal in enumerate(goals):
            with goal_cols[i % 2]:
                render_goal_progress(goal, evaluate_goal(full_df, goal, now, history=0))
        st.caption("Edit goals and see their history on the Goals page.")
    show_upcoming_weeks(full_df, group_mode, now.date())

    # Process data with progress indicator
    with st.spinner("Processing and filtering calendar data..."):
        df, start_date, end_date = preprocess_dataframe(all_events, normalize_calendar_name, normalize_time, select_month_range)
        df["group"] = df[group_mode]
        df_all_events = df
        df = apply_accuracy_settings(df, accuracy)

    show_summary_table(df, start_date, end_date)
    show_data_quality(df_all_events, df, start_date, end_date, accuracy)

    st.subheader(f"Relative Time Charts")
    date_option = st.radio(
        "Show duration charts by:",
        options=["week", "day", "month"],
        index=0,
        horizontal=True
    )
    
    # Show charts with progress indicator
    with st.spinner("Generating charts..."):
        show_duration_charts(df, start_date, end_date, group_mode, date_option)
        in_range = clip_to_range(df, start_date, end_date)
        show_weekday_hour_heatmap(in_range, start_date, end_date)
        show_calendar_distribution_pie_chart(in_range, group_mode)

else:
    st.warning("No events loaded from calendars.")
