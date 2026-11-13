import streamlit as st
import pandas as pd
from ics import Calendar
from datetime import datetime, timezone, date, timedelta
from collections import defaultdict
import re
import json
import os
import requests
import calendar
from calendar_store import load_cached_events, update_event_store, expand_calendar_events, decode_ics_response, is_free_event
from utils import NOT_TIME_UID_PREFIXES, select_month_range, normalize_time, ensure_ics_content, drop_free_events, render_health_banner
from calendar_health import FeedError, check_ics_text, feed_stats, fetch_feed, record_check, record_failure

st.set_page_config(page_title="Social Time Analysis", layout="wide")
st.title("👥 Social Time Analysis")
render_health_banner()
st.caption("Analyze who you spend the most time with")

# --- Settings file management ---
SETTINGS_FILE = "social_analysis_settings.json"

def load_settings():
    """Load settings from JSON file, validating types and filling missing keys."""
    default_settings = {
        "known_people": [],
        "exclude_patterns": [],
        "hide_from_discover": [],
        "selected_calendars": [],
        "ignore_partial_names": True,
        "match_calendar_names": True
    }
    if not os.path.exists(SETTINGS_FILE):
        return default_settings
    try:
        with open(SETTINGS_FILE, 'r') as f:
            loaded = json.load(f)
    except Exception as e:
        st.error(f"Error loading settings: {e}")
        return default_settings

    if not isinstance(loaded, dict):
        st.warning("Settings file did not contain an object; falling back to defaults.")
        return default_settings

    merged = dict(default_settings)
    for key, default_value in default_settings.items():
        if key not in loaded:
            continue
        value = loaded[key]
        # Drop values whose type does not match the default; this prevents a
        # corrupted entry (e.g. a string where a list is expected) from
        # propagating into downstream code.
        if not isinstance(value, type(default_value)):
            st.warning(
                f"Settings key '{key}' has unexpected type "
                f"{type(value).__name__}; using default."
            )
            continue
        merged[key] = value
    return merged

def save_settings(settings):
    """Save settings to JSON file."""
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings, f, indent=2)
        return True
    except Exception as e:
        st.error(f"Error saving settings: {e}")
        return False

# Initialize session state
if "settings" not in st.session_state:
    st.session_state.settings = load_settings()

# Initialize events cache in session state
if "loaded_events" not in st.session_state:
    st.session_state.loaded_events = []

# --- Calendar loading functions ---
def load_calendar_urls(calendars_json_file="calendars.json"):
    """Load calendar configurations."""
    if os.path.exists(calendars_json_file):
        with open(calendars_json_file, 'r') as file:
            calendar_data = json.load(file)
        # Tag calendars (type "tags", e.g. Oura tags) hold markers, not meetings
        return [c for c in calendar_data.get('calendars', [])
                if str(c.get("type", "time")).lower() != "tags"]
    return []

def load_events_from_cache(url, calendar_name):
    """Load events from local CSV cache (instant, no network)."""
    cached_df = load_cached_events(url)
    if cached_df.empty:
        return []
    
    if "uid" in cached_df:
        cached_df = cached_df[~cached_df["uid"].astype(str).str.startswith(NOT_TIME_UID_PREFIXES)]
    events = []
    for _, row in cached_df.iterrows():
        # Handle column name - CSV uses 'event_name', Social Analysis uses 'title'
        title = row.get("event_name", row.get("title", "")) if hasattr(row, 'get') else (row["event_name"] if "event_name" in row.index else "")
        events.append({
            "calendar": calendar_name,
            "title": title if pd.notna(title) else "",
            "start": row["start"],
            "end": row["end"],
            "duration_hours": row["duration_hours"],
            "free": row.get("free", False),
        })
    return events

def fetch_calendar_events(url, calendar_name):
    """Fetch and parse events from a calendar URL and update CSV cache."""
    try:
        try:
            content, meta = fetch_feed(url)
            check_ics_text(content, meta)
        except FeedError as e:
            record_failure(url, calendar_name, e)
            if e.kind == "http":
                # Server error: fall back to the cached events.
                return load_events_from_cache(url, calendar_name)
            if e.kind == "not_ics":
                ensure_ics_content(content)  # raises the more detailed message
            raise

        try:
            cal = Calendar(content)
        except Exception as e:
            record_failure(url, calendar_name, FeedError("parse", str(e)[:200], meta.get("http_status"), meta.get("ms")))
            raise
        events_for_cache = []  # For updating CSV cache
        errors = []
        expanded = expand_calendar_events(cal.events, errors=errors)
        record_check(url, calendar_name, True, meta, feed_stats(expanded, errors))

        # Expand recurring events into individual occurrences.
        for event, start, end, occ_uid in expanded:
            events_for_cache.append({
                "calendar": calendar_name,
                "event_name": event.name or "",
                "start": start,
                "end": end,
                "duration_hours": (end - start).total_seconds() / 3600,
                "uid": occ_uid,
                "free": is_free_event(event),
            })

        # Update the shared CSV cache so other pages benefit. An empty feed
        # leaves the cache untouched.
        if events_for_cache:
            update_event_store(url, pd.DataFrame(events_for_cache))

        # Return the merged cache, not just the feed, so Sync shows the same
        # history (events older than the feed serves) as Quick Load.
        return load_events_from_cache(url, calendar_name)
    except Exception as e:
        st.error(f"Error loading {calendar_name}: {e}")
        return []

# --- Analysis functions ---
def parse_person_entry(entry):
    """Parse a person entry that may contain aliases.
    
    Format: 'PrimaryName, Alias1, Alias2...' (comma-separated)
    Returns: (primary_name, [all_names_including_primary])
    """
    parts = [p.strip() for p in entry.split(',') if p.strip()]
    if not parts:
        return None, []
    primary = parts[0]
    all_names = parts  # All parts are valid names to match
    return primary, all_names

def build_name_pattern(name_lower):
    """Build a search pattern for a name/alias.

    ``\\b`` only marks a boundary next to a word character, so a purely
    symbolic alias such as an emoji ("❤️") could never match. Apply the
    boundary only on the sides where the name actually starts/ends with a
    word character.
    """
    escaped = re.escape(name_lower)
    prefix = r'\b' if re.match(r'\w', name_lower) else ''
    suffix = r'\b' if re.search(r'\w$', name_lower) else ''
    return prefix + escaped + suffix

def people_matching_calendar(calendar_name, known_people):
    """Return the known-people entries a calendar is named after.

    A calendar dedicated to one person (e.g. one named "Elena") should count
    towards them regardless of what the individual event titles say. The
    calendar name is compared against the primary name and every alias.
    """
    if not isinstance(calendar_name, str):
        return []
    calendar_lower = calendar_name.strip().lower()
    if not calendar_lower:
        return []

    matches = []
    for person_entry in known_people:
        _, all_names = parse_person_entry(person_entry)
        if any(name.strip().lower() == calendar_lower for name in all_names):
            matches.append(person_entry)
    return matches

def extract_people_from_title(title, known_people, ignore_partial_names=False):
    """Extract people mentioned in an event title.
    
    known_people entries can contain aliases: 'Niclas Nic' means both
    'Niclas' and 'Nic' refer to the same person (displayed as 'Niclas').
    """
    title_lower = title.lower()
    found_people = []
    
    for person_entry in known_people:
        primary_name, all_names = parse_person_entry(person_entry)
        if not primary_name:
            continue
        
        # Check if any of the names (primary or aliases) match
        for name in all_names:
            name_lower = name.lower()
            pattern = build_name_pattern(name_lower)
            if re.search(pattern, title_lower):
                # Use the full entry as the key (to keep aliases grouped)
                if person_entry not in found_people:
                    found_people.append(person_entry)
                break  # Found a match for this person, no need to check other aliases
    
    # Filter out partial names if a longer name containing them is also found
    if ignore_partial_names and len(found_people) > 1:
        filtered_people = []
        for person_entry in found_people:
            _, all_names = parse_person_entry(person_entry)
            # Check if any of this person's names is part of another found person's names
            is_partial = False
            for other_entry in found_people:
                if person_entry == other_entry:
                    continue
                _, other_names = parse_person_entry(other_entry)
                for name in all_names:
                    name_lower = name.lower()
                    for other_name in other_names:
                        other_lower = other_name.lower()
                        # Check if this name is a part of a multi-word other name
                        if ' ' in other_entry.lower() and name_lower in other_entry.lower().split():
                            is_partial = True
                            break
                    if is_partial:
                        break
                if is_partial:
                    break
            if not is_partial:
                filtered_people.append(person_entry)
        return filtered_people
    
    return found_people

def get_display_name(person_entry, include_aliases=False):
    """Get the display name from a person entry.
    
    If include_aliases=True, returns 'Primary (Alias1, Alias2)' format.
    """
    primary, all_names = parse_person_entry(person_entry)
    if not primary:
        return person_entry
    
    if include_aliases and len(all_names) > 1:
        aliases = all_names[1:]  # Everything after the primary name
        return f"{primary} ({', '.join(aliases)})"
    
    return primary

def should_exclude(title, exclude_patterns):
    """Check if an event should be excluded."""
    if not isinstance(title, str):
        return False  # Can't match patterns on non-string titles
    title_lower = title.lower()
    for pattern in exclude_patterns:
        pattern_lower = pattern.lower().strip()
        if not pattern_lower:
            continue
        if pattern_lower in title_lower:
            return True
    return False

def format_gap(hours_val):
    """Format a time gap (given in hours) showing hours with days in parentheses, e.g. '55h (2.3 days)'."""
    if hours_val is None:
        return None
    days_val = hours_val / 24
    return f"{round(hours_val)}h ({round(days_val, 1)} days)"

def format_days_ago(days):
    """Human-readable 'x days ago' for a non-negative day count."""
    if days is None:
        return None
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"

def format_days_until(days):
    """Human-readable 'in x days' for a non-negative day count."""
    if days is None:
        return None
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"

def is_all_day_event(start, end):
    """Heuristic: all-day events are stored as whole days anchored at midnight
    UTC (older cache entries) or noon UTC (see expand_event_occurrences)."""
    s, e = start.tz_convert("UTC"), end.tz_convert("UTC")
    duration = e - s
    return (
        duration >= pd.Timedelta(days=1)
        and duration % pd.Timedelta(days=1) == pd.Timedelta(0)
        and s.time() == e.time()
        and s.minute == 0 and s.second == 0
        and s.hour in (0, 12)
    )

def event_day_span(start, end):
    """Return (first_date, last_date) of the calendar days an event covers.

    All-day events have an exclusive end date (a Jul 25-30 trip ends Jul 31),
    and a timed event ending exactly at midnight doesn't cover the next day.
    """
    if is_all_day_event(start, end):
        return start.tz_convert("UTC").date(), end.tz_convert("UTC").date() - timedelta(days=1)
    last = (end - pd.Timedelta(microseconds=1)).date() if end > start else start.date()
    return start.date(), last

def format_event_when(evt):
    """'2026-07-25' for single-day events, '2026-07-25 → 2026-07-30' otherwise."""
    first, last = event_day_span(evt['date'], evt['end'])
    if first == last:
        return first.strftime('%Y-%m-%d')
    return f"{first.strftime('%Y-%m-%d')} → {last.strftime('%Y-%m-%d')} ({(last - first).days + 1} days)"

def analyze_time_with_people(df, known_people, exclude_patterns, ignore_partial_names=False,
                             match_calendar_names=False):
    """Analyze time spent with each person."""
    time_per_person = defaultdict(float)
    events_per_person = defaultdict(list)
    
    for _, row in df.iterrows():
        title = row['title']
        if not isinstance(title, str):
            title = str(title) if pd.notna(title) else ""
        duration = row['duration_hours']
        calendar_name = row.get('calendar', 'Unknown')
        
        # Skip excluded events
        if should_exclude(title, exclude_patterns):
            continue
        
        people = extract_people_from_title(title, known_people, ignore_partial_names)

        if match_calendar_names:
            # Every event of a calendar named after someone counts towards
            # them. Skip anyone the title already matched, otherwise the
            # event would be added to their total twice.
            for person in people_matching_calendar(calendar_name, known_people):
                if person not in people:
                    people.append(person)

        for person in people:
            time_per_person[person] += duration
            events_per_person[person].append({
                'title': title,
                'date': row['start'],
                'end': row['end'],
                'duration': duration,
                # Unclipped length, for display when the event extends past the range
                'full_duration': row.get('full_duration_hours', duration),
                'calendar': calendar_name
            })
    
    return time_per_person, events_per_person

def find_potential_names(df, exclude_patterns, min_occurrences=2):
    """Find potential names in event titles."""
    word_counts = defaultdict(int)
    title_examples = defaultdict(list)
    
    for title in df['title']:
        if should_exclude(title, exclude_patterns):
            continue
        
        # Split on common separators
        words = re.split(r'[\s,;:&+/]+', title)
        for word in words:
            word_clean = word.strip().lower()
            # Filter likely names (reasonable length, alphabetic)
            if len(word_clean) >= 2 and word_clean.isalpha():
                word_counts[word_clean] += 1
                if len(title_examples[word_clean]) < 3:
                    title_examples[word_clean].append(title)
    
    # Return words that appear multiple times
    potential = {word: {"count": count, "examples": title_examples[word]} 
                 for word, count in word_counts.items() 
                 if count >= min_occurrences}
    return dict(sorted(potential.items(), key=lambda x: x[1]["count"], reverse=True))

# --- UI Layout ---

# Sidebar for settings
with st.sidebar:
    st.header("⚙️ Settings")
    
    # --- Known People Management ---
    st.subheader("👤 Known People")
    st.caption("Names to track (one per line). Add aliases with comma: 'Niclas, Nic'")
    
    known_people_text = st.text_area(
        "People to track",
        value="\n".join(sorted(st.session_state.settings.get("known_people", []), key=str.lower)),
        height=150,
        key="known_people_input",
        label_visibility="collapsed"
    )
    
    # --- Exclude Patterns Management ---
    st.subheader("🚫 Exclude Patterns")
    st.caption("Event titles containing these will be excluded")
    
    exclude_patterns_text = st.text_area(
        "Patterns to exclude",
        value="\n".join(sorted(st.session_state.settings.get("exclude_patterns", []), key=str.lower)),
        height=100,
        key="exclude_patterns_input",
        label_visibility="collapsed"
    )
    
    # --- Hide from Discover Management ---
    st.subheader("👁️ Hide from Discover")
    st.caption("Words to hide from discovery (not excluded from analysis)")
    
    hide_from_discover_text = st.text_area(
        "Words to hide",
        value="\n".join(sorted(st.session_state.settings.get("hide_from_discover", []), key=str.lower)),
        height=100,
        key="hide_from_discover_input",
        label_visibility="collapsed"
    )
    
    # --- Ignore Partial Names Setting ---
    st.subheader("🔤 Name Matching")
    ignore_partial_names = st.checkbox(
        "Ignore partial names",
        value=st.session_state.settings.get("ignore_partial_names", True),
        help="If 'john doe' and 'john' are both tracked, events with 'john doe' won't count for 'john'",
        key="ignore_partial_names_input"
    )
    match_calendar_names = st.checkbox(
        "Count calendars named after a person",
        value=st.session_state.settings.get("match_calendar_names", True),
        help="If a calendar's name matches a tracked person (or one of their aliases), every event in it counts towards them - events whose title also names them are still counted once",
        key="match_calendar_names_input"
    )
    
    # Save button
    if st.button("💾 Save Settings", use_container_width=True):
        # Parse the text areas into lists
        new_known_people = [p.strip() for p in known_people_text.split("\n") if p.strip()]
        new_exclude_patterns = [p.strip() for p in exclude_patterns_text.split("\n") if p.strip()]
        new_hide_from_discover = [p.strip() for p in hide_from_discover_text.split("\n") if p.strip()]
        
        st.session_state.settings["known_people"] = new_known_people
        st.session_state.settings["exclude_patterns"] = new_exclude_patterns
        st.session_state.settings["hide_from_discover"] = new_hide_from_discover
        st.session_state.settings["ignore_partial_names"] = ignore_partial_names
        st.session_state.settings["match_calendar_names"] = match_calendar_names
        
        if save_settings(st.session_state.settings):
            st.success("Settings saved!")
            st.rerun()

# --- Main content ---

# Data source selection
st.subheader("📅 Select Calendars")

calendars = load_calendar_urls()

if not calendars:
    st.warning("No calendars configured in calendars.json")
else:
    # Filter to show only calendars (optionally filter by category)
    calendar_options = {f"{c.get('custom_name', 'Unnamed')}": c for c in calendars}
    
    selected_calendar_names = st.multiselect(
        "Select calendars to analyze",
        options=list(calendar_options.keys()),
        # Drop saved calendars that are no longer configured (renamed or
        # removed); st.multiselect raises on defaults missing from options.
        default=[
            name for name in st.session_state.settings.get("selected_calendars", [])
            if name in calendar_options
        ],
        help="Select one or more calendars"
    )
    
    # Save selected calendars to settings
    if selected_calendar_names != st.session_state.settings.get("selected_calendars", []):
        st.session_state.settings["selected_calendars"] = selected_calendar_names
        save_settings(st.session_state.settings)
    
    # Auto-load from cache on page load if calendars selected and no events loaded
    if selected_calendar_names and not st.session_state.loaded_events:
        loaded_events = []
        for name in selected_calendar_names:
            cal_config = calendar_options[name]
            events = load_events_from_cache(cal_config["url"], name)
            loaded_events.extend(events)
        if loaded_events:
            st.session_state.loaded_events = loaded_events
    
    col1, col2, col3 = st.columns([2, 1, 1])
    with col2:
        if selected_calendar_names and st.button("⚡ Quick Load selected", help="Load selected calendars instantly from local cache"):
            loaded_events = []
            for name in selected_calendar_names:
                cal_config = calendar_options[name]
                events = load_events_from_cache(cal_config["url"], name)
                loaded_events.extend(events)
            
            if loaded_events:
                st.session_state.loaded_events = loaded_events
                st.success(f"Loaded {len(loaded_events)} events from cache")
            else:
                st.warning("No cached data found. Click 'Sync Calendars' to fetch data.")
    
    with col3:
        if selected_calendar_names and st.button("🔄 Sync selected Calendars", help="Fetch latest data from selected calendar URLs"):
            with st.spinner("Fetching calendar data from URLs..."):
                loaded_events = []
                for name in selected_calendar_names:
                    cal_config = calendar_options[name]
                    events = fetch_calendar_events(cal_config["url"], name)
                    loaded_events.extend(events)
                
                if loaded_events:
                    st.session_state.loaded_events = loaded_events
                    # Clear Streamlit cache so other pages see fresh data
                    st.cache_data.clear()
                    st.success(f"Synced {len(loaded_events)} events from {len(selected_calendar_names)} calendar(s)")

# Use events from session state
all_events = st.session_state.loaded_events

# --- Analysis Section ---
if all_events:
    df = pd.DataFrame(all_events)
    # Convert UTC timestamps to local time so date filtering, "Last Seen" and
    # cadence match the other pages (which all normalize to local time).
    df = normalize_time(df, tz="local")
    df = drop_free_events(df)

    # Check for "Busy" events
    unique_titles = df['title'].unique()
    busy_only = len(unique_titles) == 1 and unique_titles[0].lower() == 'busy'
    
    if busy_only:
        st.error("""
        ⚠️ **All events are titled 'Busy'**
        
        This means you're using a public calendar URL. To see actual event names:
        1. **Export your calendar** as .ics file from Google Calendar settings, OR
        2. Use your **secret calendar URL** (found in Google Calendar settings)
        """)
    else:
        st.divider()
        
        # Date range selection (shared utility)
        start_date, end_date = select_month_range(df)

        # Inclusive filter to capture events that start, end, or span the range
        mask = (
            ((df['start'].dt.date >= start_date) & (df['start'].dt.date <= end_date)) |
            ((df['end'].dt.date >= start_date) & (df['end'].dt.date <= end_date)) |
            ((df['start'].dt.date <= start_date) & (df['end'].dt.date >= end_date))
        )
        df_filtered = df[mask]

        # Clip durations to the selected range so a multi-day event that
        # straddles the boundary only counts its in-range hours (same as the
        # Home page). Events with no in-range time (e.g. an all-day event whose
        # exclusive end falls on the range's first day) are dropped.
        df_people = df_filtered.copy()
        tz = df['start'].dt.tz
        period_start = pd.Timestamp(start_date).tz_localize(tz, nonexistent="shift_forward", ambiguous="NaT")
        period_end = pd.Timestamp(end_date + timedelta(days=1)).tz_localize(tz, nonexistent="shift_forward", ambiguous="NaT")
        df_people['full_duration_hours'] = df_people['duration_hours']
        if pd.notna(period_start) and pd.notna(period_end):
            in_range = (
                df_people['end'].clip(upper=period_end) - df_people['start'].clip(lower=period_start)
            ).dt.total_seconds() / 3600
            df_people['duration_hours'] = in_range.clip(lower=0)
            df_people = df_people[(df_people['duration_hours'] > 0) | (df_people['full_duration_hours'] == 0)]

        st.info(f"Analyzing {len(df_filtered)} events from {start_date} to {end_date}")        
        # Get current settings
        known_people = st.session_state.settings.get("known_people", [])
        exclude_patterns = st.session_state.settings.get("exclude_patterns", [])
        hide_from_discover = st.session_state.settings.get("hide_from_discover", [])
        ignore_partial_names = st.session_state.settings.get("ignore_partial_names", True)
        match_calendar_names = st.session_state.settings.get("match_calendar_names", True)
        
        # Time Spent with People section
        st.subheader("⏱️ Time Spent with People")
        
        if not known_people:
            st.warning("Add people to track in the sidebar settings")
        else:
            time_per_person, events_per_person = analyze_time_with_people(
                df_people, known_people, exclude_patterns, ignore_partial_names,
                match_calendar_names
            )
            
            if not time_per_person:
                st.info("No matches found. Check your known people list or try different names.")
            else:
                now = pd.Timestamp.now(tz=df['start'].dt.tz)
                today = now.normalize()
                # Upcoming events come from all loaded events (not just the selected
                # range), so "Next" works even when the range ends this month.
                _, upcoming_per_person = analyze_time_with_people(
                    df[df['start'] > now], known_people, exclude_patterns, ignore_partial_names,
                    match_calendar_names
                )

                # Create a dataframe for display
                results = []
                last_seen_by_person = {}
                next_by_person = {}
                for person, hours in sorted(time_per_person.items(), key=lambda x: x[1], reverse=True):
                    event_count = len(events_per_person[person])
                    person_events = events_per_person[person]
                    # Last seen = last day covered by an event that has already
                    # started, capped at today (an ongoing multi-day event = today)
                    past_days = [min(event_day_span(evt['date'], evt['end'])[1], today.date())
                                 for evt in person_events if evt['date'] <= now]
                    last_seen_dt = max(past_days) if past_days else None
                    days_ago = (today.date() - last_seen_dt).days if last_seen_dt is not None else None
                    # Next = earliest upcoming event
                    upcoming = [evt['date'] for evt in upcoming_per_person.get(person, [])]
                    next_dt = min(upcoming) if upcoming else None
                    days_until = (next_dt.normalize() - today).days if next_dt is not None else None
                    last_seen_by_person[person] = (last_seen_dt, days_ago)
                    next_by_person[person] = (next_dt, days_until)
                    # Calculate median duration
                    durations = [evt['duration'] for evt in person_events]
                    median_duration = sorted(durations)[len(durations) // 2] if durations else 0
                    # Calculate cadence: time between consecutive events (in hours)
                    event_times = sorted({evt['date'] for evt in person_events})
                    gaps_hours = [(event_times[i + 1] - event_times[i]).total_seconds() / 3600
                                  for i in range(len(event_times) - 1)]
                    if gaps_hours:
                        gaps_series = pd.Series(gaps_hours)
                        avg_between = gaps_series.mean()
                        min_between = gaps_series.min()
                        q1_between = gaps_series.quantile(0.25)
                        median_between = gaps_series.quantile(0.5)
                        q3_between = gaps_series.quantile(0.75)
                        max_between = gaps_series.max()
                    else:
                        avg_between = min_between = q1_between = None
                        median_between = q3_between = max_between = None
                    display_name = get_display_name(person, include_aliases=True)
                    results.append({
                        "Person": display_name.title(),
                        "Last Seen": days_ago,
                        "Next": days_until,
                        "Hours": round(hours, 1),
                        "Events": event_count,
                        "Avg Hours/Event": round(hours / event_count, 1) if event_count > 0 else 0,
                        "Median Hours/Event": round(median_duration, 1),
                        "Avg Between": format_gap(avg_between),
                        "Min Between": format_gap(min_between),
                        "Q1 Between": format_gap(q1_between),
                        "Median Between": format_gap(median_between),
                        "Q3 Between": format_gap(q3_between),
                        "Max Between": format_gap(max_between),
                    })
                
                results_df = pd.DataFrame(results)
                
                # Bar chart
                st.bar_chart(results_df.set_index("Person")["Hours"])
                
                # Table
                # Keep Last Seen / Next numeric so they sort correctly; format for display
                st.dataframe(
                    results_df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Last Seen": st.column_config.NumberColumn(
                            "Last Seen", format="%d days ago",
                            help="Days since the most recent past event in the selected range",
                        ),
                        "Next": st.column_config.NumberColumn(
                            "Next", format="in %d days",
                            help="Days until the next scheduled event (any future date)",
                        ),
                    },
                )
                
                # Expandable details per person
                st.subheader("📋 Event Details")
                for person in sorted(time_per_person.keys(), key=lambda x: get_display_name(x).lower()):
                    display_name = get_display_name(person, include_aliases=True)
                    person_events = events_per_person[person]
                    # Count events per calendar
                    calendar_counts = defaultdict(int)
                    for evt in person_events:
                        calendar_counts[evt['calendar']] += 1
                    calendar_breakdown = ", ".join([f"{cal}: {count}" for cal, count in sorted(calendar_counts.items())])
                    last_seen_dt, days_ago = last_seen_by_person[person]
                    next_dt, days_until = next_by_person[person]
                    when_parts = []
                    if last_seen_dt is not None:
                        when_parts.append(f"last seen {format_days_ago(days_ago)} ({last_seen_dt.strftime('%Y-%m-%d')})")
                    if next_dt is not None:
                        when_parts.append(f"next {format_days_until(days_until)} ({next_dt.strftime('%Y-%m-%d')})")
                    when_str = f" · {' · '.join(when_parts)}" if when_parts else ""
                    with st.expander(f"{display_name.title()} - {len(person_events)} events ({calendar_breakdown}){when_str}"):
                        for evt in sorted(person_events, key=lambda x: x['date'], reverse=True):
                            if abs(evt['full_duration'] - evt['duration']) > 0.01:
                                hours_str = f"{evt['duration']:.1f}h in range of {evt['full_duration']:.1f}h"
                            else:
                                hours_str = f"{evt['duration']:.1f}h"
                            st.write(f"• **{evt['title']}** - {format_event_when(evt)} ({hours_str}) - *{evt['calendar']}*")
        
        st.divider()
        
        # Discover, Quick Add, and Sample Events section
        st.subheader("🔍 Discover People")
        st.caption("Words appearing frequently in your events")
        
        min_occurrences = st.slider("Minimum occurrences", 2, 10, 2)
        
        potential_names = find_potential_names(df_filtered, exclude_patterns, min_occurrences)
        
        if potential_names:
            # Build list of all known names including aliases
            all_known_names_lower = []
            for person_entry in known_people:
                _, all_names = parse_person_entry(person_entry)
                all_known_names_lower.extend([n.lower() for n in all_names])
            
            hidden_lower = [h.lower() for h in hide_from_discover]
            
            # Option to ignore partial names that are part of known full names
            hide_partial_names = st.checkbox(
                "Hide partial names", 
                value=True,
                help="Hide words that are part of a known person's name (e.g., hide 'John' if 'John Doe' is tracked)"
            )
            
            def is_partial_of_known(word):
                """Check if word is a part of any known person's full name."""
                word_lower = word.lower()
                for known in all_known_names_lower:
                    # Check if word is a part of a multi-word name (but not the full name itself)
                    if ' ' in known and word_lower in known.split() and word_lower != known:
                        return True
                return False
            
            new_potentials = {k: v for k, v in potential_names.items() 
                            if k.lower() not in all_known_names_lower 
                            and k.lower() not in hidden_lower
                            and not (hide_partial_names and is_partial_of_known(k))}
            
            if new_potentials:
                # Use a form to batch add people without reloading
                st.caption("Click to add people to track, or hide from this list:")
                
                # Create checkboxes for each potential name
                selected_to_add = []
                selected_to_hide = []
                for word, data in list(new_potentials.items())[:15]:
                    col_check, col_info, col_hide = st.columns([1, 3, 1])
                    with col_check:
                        if st.checkbox(word, key=f"check_{word}"):
                            selected_to_add.append(word)
                    with col_info:
                        st.caption(f"{data['count']} times · e.g. {data['examples'][0][:30]}...")
                    with col_hide:
                        if st.checkbox("🙈", key=f"hide_{word}", help=f"Hide '{word}' from discover"):
                            selected_to_hide.append(word)
                
                col_add, col_hide_btn = st.columns(2)
                with col_add:
                    if selected_to_add:
                        if st.button(f"➕ Add {len(selected_to_add)} selected", type="primary"):
                            for word in selected_to_add:
                                if word not in st.session_state.settings["known_people"]:
                                    st.session_state.settings["known_people"].append(word)
                            save_settings(st.session_state.settings)
                            st.success(f"Added: {', '.join(selected_to_add)}")
                            st.rerun()
                with col_hide_btn:
                    if selected_to_hide:
                        if st.button(f"🙈 Hide {len(selected_to_hide)} selected"):
                            for word in selected_to_hide:
                                if word not in st.session_state.settings["hide_from_discover"]:
                                    st.session_state.settings["hide_from_discover"].append(word)
                            save_settings(st.session_state.settings)
                            st.success(f"Hidden: {', '.join(selected_to_hide)}")
                            st.rerun()
            else:
                st.success("All frequent words are already in your known people list!")
        else:
            st.info("No frequently occurring words found")
        
        # Quick add input
        st.divider()
        st.subheader("✏️ Quick Add")
        new_person = st.text_input("Add a person manually", placeholder="Enter name...")
        if new_person and st.button("Add", key="quick_add_btn"):
            new_person = new_person.strip()
            if new_person and new_person.lower() not in [p.lower() for p in st.session_state.settings["known_people"]]:
                st.session_state.settings["known_people"].append(new_person)
                save_settings(st.session_state.settings)
                st.success(f"Added: {new_person}")
                st.rerun()
            elif new_person:
                st.warning(f"'{new_person}' is already in your list")
        
        # Sample events section - filter out events with known people
        st.divider()
        st.subheader("📅 Sample Events")
        
        # Filter out events that contain known people
        def contains_known_person(title):
            title_lower = title.lower()
            for person in known_people:
                person_lower = person.lower().strip()
                if person_lower and re.search(build_name_pattern(person_lower), title_lower):
                    return True
            return False
        
        attributed = df_filtered['title'].apply(contains_known_person)
        if match_calendar_names:
            # Events already attributed through their calendar name are not
            # unidentified either, so keep them out of the samples.
            attributed = attributed | df_filtered['calendar'].apply(
                lambda c: bool(people_matching_calendar(c, known_people))
            )
        sample_df = df_filtered[~attributed]
        # Show unique titles only
        sample_events = sample_df.drop_duplicates(subset=['title'])[['title', 'start', 'duration_hours']].head(20)
        sample_events['start'] = sample_events['start'].dt.strftime('%Y-%m-%d')
        sample_events.columns = ['Title', 'Date', 'Hours']
        st.dataframe(sample_events, use_container_width=True, hide_index=True)

else:
    st.info("👆 Select calendars above to start analyzing")
