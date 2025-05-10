import streamlit as st
import pandas as pd
from ics import Calendar
from calendar_store import update_event_store
from app import load_calendar_urls
import requests
import os

st.set_page_config(page_title="Import ICS File", layout="wide")
st.title("Import ICS File into Existing Calendar")

# --- Load calendar URLs from calendars.txt ---
def get_wr_calname(url):
    try:
        if url.startswith("file://"):
            path = url.replace("file://", "")
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        else:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            content = response.text

        for line in content.splitlines():
            if line.startswith("X-WR-CALNAME:"):
                return line.replace("X-WR-CALNAME:", "").strip()
    except Exception as e:
        print(f"Could not read calendar name for {url}: {e}")
    return url  # fallback label

calendar_data = load_calendar_urls()
https_calendars = [calendar for calendar in calendar_data if calendar["url"].startswith("https://") or calendar["url"].startswith("file://")]

if not https_calendars:
    st.warning("No HTTPS calendars found in calendars.txt")
    st.stop()

# Build label like: "Work Calendar - Project B (https://example.com/calendar.ics)"
calendar_label_map = {}
for calendar in https_calendars:
    calendar_name = calendar["custom_name"]

    # Check if calendar_name is "Unnamed", if so, read it from the ICS content
    if calendar_name == "Unnamed":
        try:
            url = calendar["url"]
            content = ""
            
            if url.startswith("file://"):
                path = url.replace("file://", "")
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            else:
                response = requests.get(url, timeout=5)
                response.raise_for_status()
                content = response.text

            # Attempt to extract calendar name from X-WR-CALNAME
            for line in content.splitlines():
                if line.startswith("X-WR-CALNAME:"):
                    calendar_name = line.replace("X-WR-CALNAME:", "").strip()
                    break
        except Exception as e:
            st.warning(f"Could not retrieve name for {url}, using 'Unnamed'. Error: {e}")
            calendar_name = "Unnamed"  # Fallback to "Unnamed" if it fails

    url = calendar["url"]
    label = f"{calendar_name} ({url})"
    calendar_label_map[label] = url

selected_label = st.selectbox("Select target calendar", list(calendar_label_map.keys()))
target_url = calendar_label_map[selected_label]

# --- Upload section ---
uploaded_file = st.file_uploader("Upload a .ics file", type=["ics"])

if uploaded_file and target_url:
    try:
        content = uploaded_file.read().decode("utf-8")
        cal = Calendar(content)

        events = []
        for event in cal.events:
            try:
                start = event.begin.datetime
                end = event.end.datetime
                duration = (end - start).total_seconds() / 3600
                uid = event.uid
                # Extract name only (before first parenthesis)
                calendar_name = selected_label.split(" (")[0]
                events.append({
                    "calendar": f"[Imported] {calendar_name}",
                    "start": start,
                    "end": end,
                    "duration_hours": duration,
                    "uid": uid
                })
            except Exception as e:
                st.error(f"Skipping event due to error: {e}")

        if events:
            import_df = pd.DataFrame(events)
            updated_df = update_event_store(target_url, import_df)
            st.cache_data.clear() # Clear cache to reflect the imported changes
            st.success(f"✅ Imported {len(import_df)} events into: {selected_label}")
            st.caption("Events are now merged into the existing calendar cache.")
            st.dataframe(import_df.head(10))
        else:
            st.warning("No valid events found in the uploaded file.")

    except Exception as e:
        st.error(f"Failed to parse .ics file: {e}")
