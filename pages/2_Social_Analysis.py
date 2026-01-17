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
from calendar_store import load_cached_events, update_event_store
from utils import select_month_range

st.set_page_config(page_title="Social Time Analysis", layout="wide")
st.title("👥 Social Time Analysis")
st.caption("Analyze who you spend the most time with")

# --- Settings file management ---
SETTINGS_FILE = "social_analysis_settings.json"

def load_settings():
    """Load settings from JSON file."""
    default_settings = {
        "known_people": [],
        "exclude_patterns": [],
        "hide_from_discover": [],
        "selected_calendars": [],
        "ignore_partial_names": True
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                settings = json.load(f)
                # Merge with defaults for any missing keys
                for key in default_settings:
                    if key not in settings:
                        settings[key] = default_settings[key]
                return settings
        except Exception as e:
            st.error(f"Error loading settings: {e}")
    return default_settings

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
        return calendar_data.get('calendars', [])
    return []

def load_events_from_cache(url, calendar_name):
    """Load events from local CSV cache (instant, no network)."""
    cached_df = load_cached_events(url)
    if cached_df.empty:
        return []
    
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
        })
    return events

def fetch_calendar_events(url, calendar_name):
    """Fetch and parse events from a calendar URL and update CSV cache."""
    try:
        if url.startswith("file://"):
            path = url.replace("file://", "")
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        else:
            response = requests.get(url, timeout=30)
            if response.status_code != 200:
                return []
            content = response.text
        
        cal = Calendar(content)
        events = []
        events_for_cache = []  # For updating CSV cache
        
        for event in cal.events:
            try:
                start = event.begin.datetime.astimezone(timezone.utc)
                end = event.end.datetime.astimezone(timezone.utc)
                duration = (end - start).total_seconds() / 3600
                uid = getattr(event, 'uid', None)
                event_name = event.name or ""
                
                events.append({
                    "calendar": calendar_name,
                    "title": event_name,
                    "start": start,
                    "end": end,
                    "duration_hours": duration,
                })
                
                # Also prepare for CSV cache update
                events_for_cache.append({
                    "calendar": calendar_name,
                    "event_name": event_name,
                    "start": start,
                    "end": end,
                    "duration_hours": duration,
                    "uid": uid,
                })
            except Exception:
                continue
        
        # Update the shared CSV cache so other pages benefit
        if events_for_cache:
            new_df = pd.DataFrame(events_for_cache)
            update_event_store(url, new_df)
        
        return events
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
            pattern = r'\b' + re.escape(name_lower) + r'\b'
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

def analyze_time_with_people(df, known_people, exclude_patterns, ignore_partial_names=False):
    """Analyze time spent with each person."""
    time_per_person = defaultdict(float)
    events_per_person = defaultdict(list)
    
    for _, row in df.iterrows():
        title = row['title']
        if not isinstance(title, str):
            title = str(title) if pd.notna(title) else ""
        duration = row['duration_hours']
        
        # Skip excluded events
        if should_exclude(title, exclude_patterns):
            continue
        
        people = extract_people_from_title(title, known_people, ignore_partial_names)
        
        for person in people:
            time_per_person[person] += duration
            events_per_person[person].append({
                'title': title,
                'date': row['start'],
                'duration': duration,
                'calendar': row.get('calendar', 'Unknown')
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
        default=st.session_state.settings.get("selected_calendars", []),
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

        st.info(f"Analyzing {len(df_filtered)} events from {start_date} to {end_date}")        
        # Get current settings
        known_people = st.session_state.settings.get("known_people", [])
        exclude_patterns = st.session_state.settings.get("exclude_patterns", [])
        hide_from_discover = st.session_state.settings.get("hide_from_discover", [])
        ignore_partial_names = st.session_state.settings.get("ignore_partial_names", True)
        
        # Time Spent with People section
        st.subheader("⏱️ Time Spent with People")
        
        if not known_people:
            st.warning("Add people to track in the sidebar settings")
        else:
            time_per_person, events_per_person = analyze_time_with_people(
                df_filtered, known_people, exclude_patterns, ignore_partial_names
            )
            
            if not time_per_person:
                st.info("No matches found. Check your known people list or try different names.")
            else:
                # Create a dataframe for display
                results = []
                for person, hours in sorted(time_per_person.items(), key=lambda x: x[1], reverse=True):
                    event_count = len(events_per_person[person])
                    # Find most recent event date
                    person_events = events_per_person[person]
                    last_seen = max(evt['date'] for evt in person_events).strftime('%Y-%m-%d')
                    # Calculate median duration
                    durations = [evt['duration'] for evt in person_events]
                    median_duration = sorted(durations)[len(durations) // 2] if durations else 0
                    display_name = get_display_name(person, include_aliases=True)
                    results.append({
                        "Person": display_name.title(),
                        "Hours": round(hours, 1),
                        "Events": event_count,
                        "Avg Hours/Event": round(hours / event_count, 1) if event_count > 0 else 0,
                        "Median Hours/Event": round(median_duration, 1),
                        "Last Seen": last_seen
                    })
                
                results_df = pd.DataFrame(results)
                
                # Bar chart
                st.bar_chart(results_df.set_index("Person")["Hours"])
                
                # Table
                st.dataframe(results_df, use_container_width=True, hide_index=True)
                
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
                    with st.expander(f"{display_name.title()} - {len(person_events)} events ({calendar_breakdown})"):
                        for evt in sorted(person_events, key=lambda x: x['date'], reverse=True):
                            st.write(f"• **{evt['title']}** - {evt['date'].strftime('%Y-%m-%d')} ({evt['duration']:.1f}h) - *{evt['calendar']}*")
        
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
                if person_lower and re.search(r'\b' + re.escape(person_lower) + r'\b', title_lower):
                    return True
            return False
        
        sample_df = df_filtered[~df_filtered['title'].apply(contains_known_person)]
        # Show unique titles only
        sample_events = sample_df.drop_duplicates(subset=['title'])[['title', 'start', 'duration_hours']].head(20)
        sample_events['start'] = sample_events['start'].dt.strftime('%Y-%m-%d')
        sample_events.columns = ['Title', 'Date', 'Hours']
        st.dataframe(sample_events, use_container_width=True, hide_index=True)

else:
    st.info("👆 Select calendars above to start analyzing")
