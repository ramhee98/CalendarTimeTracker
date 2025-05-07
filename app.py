import streamlit as st
import pandas as pd
import requests
from io import StringIO
from datetime import datetime
import matplotlib.pyplot as plt
import altair as alt
import os
import shutil
from pandas import date_range, Period

def load_calendar_urls(file_path="calendars.txt"):
    with open(file_path, "r", encoding="utf-8") as f:
        return [
            line.split("#")[0].strip()
            for line in f
            if line.strip() and not line.strip().startswith("#")
        ]

def extract_calendar_name(lines):
    for line in lines:
        if line.startswith("X-WR-CALNAME:"):
            return line.replace("X-WR-CALNAME:", "").strip()
    return "Unnamed"

@st.cache_data(ttl=3600)  # Cache for 1 hour (3600 seconds)
def parse_ics_from_url(url):
    try:
        response = requests.get(url)
        if response.status_code != 200:
            return []
        lines = response.text.splitlines()
        calendar_name = extract_calendar_name(lines)
        events = []
        current_event = {}
        inside_event = False
        for line in lines:
            line = line.strip()
            if line == "BEGIN:VEVENT":
                current_event = {}
                inside_event = True
            elif line == "END:VEVENT":
                try:
                    start_str = current_event["DTSTART"].replace("Z", "")
                    end_str = current_event["DTEND"].replace("Z", "")
                    start = datetime.strptime(start_str, "%Y%m%dT%H%M%S")
                    end = datetime.strptime(end_str, "%Y%m%dT%H%M%S")
                    duration = (end - start).total_seconds() / 3600
                    events.append({
                        "calendar": calendar_name,
                        "start": start,
                        "end": end,
                        "duration_hours": duration
                    })
                except:
                    pass
                inside_event = False
            elif inside_event:
                if line.startswith("DTSTART:"):
                    current_event["DTSTART"] = line.replace("DTSTART:", "")
                elif line.startswith("DTEND:"):
                    current_event["DTEND"] = line.replace("DTEND:", "")
        return events
    except Exception as e:
        st.error(f"Error loading {url}: {e}")
        return []

# Copy sample file if calendars.txt doesn't exist
if not os.path.exists("calendars.txt") and os.path.exists("calendars.txt.sample"):
    shutil.copy("calendars.txt.sample", "calendars.txt")
    st.warning("No calendars.txt found. A sample file has been copied. Please update it with your calendar URLs and reload the page.")

# Streamlit UI
st.title("CalendarTimeTracker")
st.caption("Analyze time usage from multiple public calendar (.ics) URLs")

# Load calendar URLs
calendar_urls = load_calendar_urls("calendars.txt")
all_events = []

for url in calendar_urls:
    events = parse_ics_from_url(url)
    all_events.extend(events)

# Create DataFrame
if all_events:
    df = pd.DataFrame(all_events)
    df["month"] = df["start"].dt.to_period("M")
    df["weekday"] = df["start"].dt.day_name()
    df["hour"] = df["start"].dt.hour

    # Extract year options from event start times
    df["year"] = df["start"].dt.year
    available_years = sorted(df["year"].unique())

    # Add dropdown to select year
    current_year = datetime.now().year
    default_index = available_years.index(current_year) if current_year in available_years else len(available_years) - 1
    selected_year = st.selectbox("Select year", available_years, index=default_index)

    # Filter DataFrame by selected year
    df = df[df["year"] == selected_year]

    # Summary Table
    st.subheader("Summary Table")
    summary = df.groupby("calendar")["duration_hours"].agg(
        Total_Hours="sum",
        Average_Hours_Per_Event="mean",
        Event_Count="count"
    ).reset_index()
    st.dataframe(summary)

    csv = summary.to_csv(index=False).encode("utf-8")
    st.download_button("Download Summary as CSV", csv, "summary.csv", "text/csv")

    # Generate all months
    all_months = pd.date_range(f"{selected_year}-01-01", f"{selected_year}-12-01", freq="MS").to_period("M")
    calendars = df["calendar"].unique()
    full_index = pd.MultiIndex.from_product([all_months, calendars], names=["month", "calendar"])

    monthly = df.groupby(["month", "calendar"])["duration_hours"].sum().reindex(full_index, fill_value=0).reset_index()
    monthly["month"] = monthly["month"].astype(str)


    # Altair chart with labeled axes
    st.subheader("Total Time per Month (Stacked by Calendar)")
    chart = alt.Chart(monthly).mark_bar().encode(
        x=alt.X("month:N", title="Month", axis=alt.Axis(labelAngle=-45)),
        y=alt.Y("duration_hours:Q", title="Hours"),
        color=alt.Color("calendar:N", title="Calendar"),
        tooltip=["month", "calendar", "duration_hours"]
    ).properties(width=700, height=400).interactive()  # Enables zoom/pan

    st.altair_chart(chart, use_container_width=True)

    # Heatmap: Weekday vs Hour
    st.subheader("Activity Heatmap (Weekday × Hour)")
    heatmap_data = df.groupby(["weekday", "hour"])["duration_hours"].sum().unstack(fill_value=0)
    heatmap_data = heatmap_data.reindex(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])
    st.dataframe(heatmap_data.style.background_gradient(cmap="YlOrRd"))

else:
    st.warning("No events loaded from calendars.")
