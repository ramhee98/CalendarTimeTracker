import streamlit as st
import re
from utils import load_all_events, normalize_calendar_name, normalize_time, select_month_range
import pandas as pd

st.set_page_config(page_title="Search Events", layout="wide")
st.title("🔍 Search Events")
st.caption("Search for events by name and filter by calendar or category.")

# Add performance improvements
if "search_loaded" not in st.session_state:
    st.session_state.search_loaded = False

# --- Cache management ---
col1, col2 = st.columns([3, 1])
with col2:
    if st.button("🔄 Refresh Data", help="Clear cache and reload data"):
        st.cache_data.clear()
        st.session_state.search_loaded = False
        st.rerun()

# --- Load events ---
with st.spinner("Loading calendar data..."):
    all_events, source_type = load_all_events()
    if not all_events:
        st.warning("No events available.")
        st.stop()

# Mark as loaded
st.session_state.search_loaded = True

with st.spinner("Processing calendar data..."):
    df = pd.DataFrame(all_events)
    df["calendar"] = df["calendar_name"].apply(normalize_calendar_name)
    df = normalize_time(df, tz="local")

# --- View selector like app.py ---
if source_type == "json":
    group_mode = st.selectbox(
        "View data by",
        options=["calendar", "category"],
        index=0,
        format_func=str.title,
        key="search_view_mode"
    )
else:
    group_mode = "calendar"

df["group"] = df[group_mode]

# --- Select date range ---
with st.spinner("Applying date filters..."):
    start_date, end_date = select_month_range(df)

    # Filter by date range
    df = df[
        ((df["start"].dt.date >= start_date) & (df["start"].dt.date <= end_date)) |
        ((df["end"].dt.date >= start_date) & (df["end"].dt.date <= end_date)) |
        ((df["start"].dt.date <= start_date) & (df["end"].dt.date >= end_date))
    ]

# --- Search functionality ---
st.subheader("Search")

# Get unique values for filter based on group_mode
filter_column = group_mode  # "calendar" or "category"
filter_label = group_mode.title()  # "Calendar" or "Category"
available_options = sorted(df[filter_column].unique().tolist())

col1, col2 = st.columns([2, 1])
with col1:
    search_query = st.text_input(
        "Search for event name",
        placeholder="Enter event name to search...",
        help="Search is case-insensitive. Use * as wildcard (e.g., 'Z*rich' matches 'Zürich')"
    )
with col2:
    selected_options = st.multiselect(
        f"Filter by {filter_label.lower()}(s)",
        options=available_options,
        default=[],
        help=f"Leave empty to search all {filter_label.lower()}s"
    )

if search_query:
    # Convert wildcard pattern to regex
    # Escape special regex characters except *, then convert * to .*
    pattern = re.escape(search_query).replace(r'\*', '.*')
    
    # Filter events matching the pattern (case-insensitive)
    # Pattern matches anywhere in the event name
    mask = df["event_name"].str.contains(pattern, case=False, na=False, regex=True)
    matching_events = df[mask].copy()
    
    # Apply filter if any options are selected
    if selected_options:
        matching_events = matching_events[matching_events[filter_column].isin(selected_options)]
    
    if matching_events.empty:
        filter_msg = f" in selected {filter_label.lower()}(s)" if selected_options else ""
        st.info(f"No events found matching '{search_query}'{filter_msg}")
    else:
        # Sort by start time (most recent first)
        matching_events = matching_events.sort_values("start", ascending=False)
        
        # Calculate total time
        total_hours = matching_events["duration_hours"].sum()
        total_events = len(matching_events)
        
        # Display summary
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Matching Events", total_events)
        with col2:
            st.metric("Total Hours", f"{total_hours:.2f}")
        with col3:
            avg_duration = total_hours / total_events if total_events > 0 else 0
            st.metric("Avg Duration", f"{avg_duration:.2f} hrs")
        
        # Prepare display dataframe
        display_df = matching_events[[
            "event_name", "calendar", "category", "start", "end", "duration_hours"
        ]].copy()
        
        # Format datetime columns for display
        display_df["start"] = display_df["start"].dt.strftime("%Y-%m-%d %H:%M")
        display_df["end"] = display_df["end"].dt.strftime("%Y-%m-%d %H:%M")
        display_df["duration_hours"] = display_df["duration_hours"].round(2)
        
        # Rename columns for display
        display_df.columns = ["Event Name", "Calendar", "Category", "Start", "End", "Duration (hrs)"]
        
        st.dataframe(display_df, use_container_width=True)
        
        # Download button for search results
        csv = display_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download Search Results as CSV",
            csv,
            f"search_results_{search_query}.csv",
            "text/csv"
        )
else:
    st.info("Enter an event name above to search.")
