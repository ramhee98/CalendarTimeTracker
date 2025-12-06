import streamlit as st
import pandas as pd
from ics import Calendar
from datetime import datetime, timezone, date
from collections import defaultdict
import re
import json
import os
import requests
import calendar

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
        "selected_calendars": []
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

def fetch_calendar_events(url, calendar_name):
    """Fetch and parse events from a calendar URL."""
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
        
        for event in cal.events:
            try:
                start = event.begin.datetime.astimezone(timezone.utc)
                end = event.end.datetime.astimezone(timezone.utc)
                duration = (end - start).total_seconds() / 3600
                
                events.append({
                    "calendar": calendar_name,
                    "title": event.name or "",
                    "start": start,
                    "end": end,
                    "duration_hours": duration,
                })
            except Exception:
                continue
        
        return events
    except Exception as e:
        st.error(f"Error loading {calendar_name}: {e}")
        return []

# --- Analysis functions ---
def extract_people_from_title(title, known_people):
    """Extract people mentioned in an event title."""
    title_lower = title.lower()
    found_people = []
    
    for person in known_people:
        person_lower = person.lower().strip()
        if not person_lower:
            continue
        # Check for whole word match
        pattern = r'\b' + re.escape(person_lower) + r'\b'
        if re.search(pattern, title_lower):
            found_people.append(person)
    
    return found_people

def should_exclude(title, exclude_patterns):
    """Check if an event should be excluded."""
    title_lower = title.lower()
    for pattern in exclude_patterns:
        pattern_lower = pattern.lower().strip()
        if not pattern_lower:
            continue
        if pattern_lower in title_lower:
            return True
    return False

def analyze_time_with_people(df, known_people, exclude_patterns):
    """Analyze time spent with each person."""
    time_per_person = defaultdict(float)
    events_per_person = defaultdict(list)
    
    for _, row in df.iterrows():
        title = row['title']
        duration = row['duration_hours']
        
        # Skip excluded events
        if should_exclude(title, exclude_patterns):
            continue
        
        people = extract_people_from_title(title, known_people)
        
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
    st.caption("Names to track (one per line)")
    
    known_people_text = st.text_area(
        "People to track",
        value="\n".join(st.session_state.settings.get("known_people", [])),
        height=150,
        key="known_people_input",
        label_visibility="collapsed"
    )
    
    # --- Exclude Patterns Management ---
    st.subheader("🚫 Exclude Patterns")
    st.caption("Event titles containing these will be excluded")
    
    exclude_patterns_text = st.text_area(
        "Patterns to exclude",
        value="\n".join(st.session_state.settings.get("exclude_patterns", [])),
        height=100,
        key="exclude_patterns_input",
        label_visibility="collapsed"
    )
    
    # --- Hide from Discover Management ---
    st.subheader("👁️ Hide from Discover")
    st.caption("Words to hide from discovery (not excluded from analysis)")
    
    hide_from_discover_text = st.text_area(
        "Words to hide",
        value="\n".join(st.session_state.settings.get("hide_from_discover", [])),
        height=100,
        key="hide_from_discover_input",
        label_visibility="collapsed"
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
    
    if selected_calendar_names and st.button("🔄 Load Selected Calendars"):
        with st.spinner("Fetching calendar data..."):
            loaded_events = []
            for name in selected_calendar_names:
                cal_config = calendar_options[name]
                events = fetch_calendar_events(cal_config["url"], name)
                loaded_events.extend(events)
            
            if loaded_events:
                st.session_state.loaded_events = loaded_events
                st.success(f"Loaded {len(loaded_events)} events from {len(selected_calendar_names)} calendar(s)")

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
        
        # Month range selection (same as main app)
        min_date = df['start'].min().date()
        max_date = df['start'].max().date()
        
        years = list(range(min_date.year, max_date.year + 1))
        months = list(range(1, 13))
        now = datetime.now()
        
        start_month_default = 1
        end_month_default = now.month
        start_year_default = end_year_default = now.year
        
        st.subheader("Select Month Range")
        
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            start_month = st.selectbox("Start Month", months, index=start_month_default - 1, format_func=lambda m: calendar.month_name[m])
        with col2:
            start_year = st.selectbox("Start Year", years, index=years.index(start_year_default) if start_year_default in years else 0)
        with col3:
            end_month = st.selectbox("End Month", months, index=end_month_default - 1, format_func=lambda m: calendar.month_name[m])
        with col4:
            end_year = st.selectbox("End Year", years, index=years.index(end_year_default) if end_year_default in years else len(years) - 1)
        
        try:
            start_date = date(start_year, start_month, 1)
            end_day = calendar.monthrange(end_year, end_month)[1]
            end_date = date(end_year, end_month, end_day)
            
            if start_date > end_date:
                st.warning("Start must be before end.")
                st.stop()
        except Exception as e:
            st.error(f"Invalid date range: {e}")
            st.stop()
        
        # Filter by date
        mask = (df['start'].dt.date >= start_date) & (df['start'].dt.date <= end_date)
        df_filtered = df[mask]
        
        st.info(f"Analyzing {len(df_filtered)} events from {start_date} to {end_date}")
        
        # Get current settings
        known_people = st.session_state.settings.get("known_people", [])
        exclude_patterns = st.session_state.settings.get("exclude_patterns", [])
        hide_from_discover = st.session_state.settings.get("hide_from_discover", [])
        
        # Time Spent with People section
        st.subheader("⏱️ Time Spent with People")
        
        if not known_people:
            st.warning("Add people to track in the sidebar settings")
        else:
            time_per_person, events_per_person = analyze_time_with_people(
                df_filtered, known_people, exclude_patterns
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
                    results.append({
                        "Person": person.title(),
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
                for person in sorted(time_per_person.keys(), key=lambda x: x.lower()):
                    with st.expander(f"{person.title()} - {len(events_per_person[person])} events"):
                        person_events = events_per_person[person]
                        for evt in sorted(person_events, key=lambda x: x['date'], reverse=True):
                            st.write(f"• **{evt['title']}** - {evt['date'].strftime('%Y-%m-%d')} ({evt['duration']:.1f}h)")
        
        st.divider()
        
        # Discover, Quick Add, and Sample Events section
        st.subheader("🔍 Discover People")
        st.caption("Words appearing frequently in your events")
        
        min_occurrences = st.slider("Minimum occurrences", 2, 10, 2)
        
        potential_names = find_potential_names(df_filtered, exclude_patterns, min_occurrences)
        
        if potential_names:
            # Filter out already known people and hidden words
            known_lower = [p.lower() for p in known_people]
            hidden_lower = [h.lower() for h in hide_from_discover]
            new_potentials = {k: v for k, v in potential_names.items() 
                            if k.lower() not in known_lower and k.lower() not in hidden_lower}
            
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
