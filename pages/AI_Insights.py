import streamlit as st
import pandas as pd
from openai import OpenAI
import os
from datetime import date
from app import load_all_events, normalize_calendar_name, normalize_time, select_month_range
from dotenv import load_dotenv

st.set_page_config(page_title="AI Insights", layout="wide")
st.title("Calendar AI Insights (ChatGPT-Powered)")
st.caption("Understand how your time is distributed with AI-generated insights.")

# --- OpenAI API setup ---
env_path = ".env"
# Create .env file with placeholder if it doesn't exist
if not os.path.exists(env_path):
    with open(env_path, "w") as f:
        f.write("OPENAI_API_KEY=your-api-key-here\n")
    print("✅ Created .env file with placeholder API key.")
    st.error("Missing OpenAI API key. Set it in the .env file.")
    st.stop()
else:
    load_dotenv()
    if os.getenv("OPENAI_API_KEY") != "your-api-key-here":
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    else:
        st.warning("⚠️ OpenAI API key not found. The Analyze page will not work.")
        st.stop()

# --- Load calendar data ---
all_events, source_type = load_all_events()

if not all_events:
    st.warning("No events available to analyze.")
    st.stop()

# --- Choose grouping mode ---
if source_type == "json":
    group_mode = st.selectbox(
        "View data by",
        options=["calendar", "category"],
        index=0,
        format_func=str.title,
        key="ai_view_mode",
    )
else:
    group_mode = "calendar"
group_label = group_mode.title()

# --- Preprocess ---
df = pd.DataFrame(all_events)
df["calendar"] = df["calendar_name"].apply(normalize_calendar_name)
df = normalize_time(df, tz="local")
df["group"] = df[group_mode]

start_date, end_date = select_month_range(df)

# Filter range
df = df[
    ((df["start"].dt.date >= start_date) & (df["start"].dt.date <= end_date)) |
    ((df["end"].dt.date >= start_date) & (df["end"].dt.date <= end_date)) |
    ((df["start"].dt.date <= start_date) & (df["end"].dt.date >= end_date))
]

# --- Summarize by group ---
summary = (
    df.groupby("group")["duration_hours"]
    .agg(Total_Hours="sum", Avg_Hours_Per_Event="mean", Event_Count="count")
    .reset_index()
)
summary.rename(columns={"group": group_label}, inplace=True)

# --- Optional: show the raw summary ---
with st.expander("📋 Show summary data sent to ChatGPT"):
    st.dataframe(summary)

# --- Editable system prompt ---
default_system_prompt = (
    "You are a helpful assistant that analyzes calendar data for time management. "
    f"Based on the following {group_label.lower()} usage summary, provide natural language insights: "
    "highlight which calendars take most time, when activity peaks, any imbalance or overbooking, and give simple time management tips."
)

system_prompt = st.text_area(
    "🛠️ Customize AI assistant behavior",
    value=default_system_prompt,
    height=100
)

# --- Prepare user prompt ---
summary_text = summary.to_string(index=False)

user_prompt = f"""
Here is my {group_label.lower()} usage summary from {start_date} to {end_date}:

{summary_text}

Please analyze this and provide meaningful insights.
"""

# --- Analyze ---
model="gpt-4.1-mini"
if st.button(f"🔍 Analyze with ChatGPT ({model})"):
    with st.spinner("Thinking..."):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7,
                max_tokens=800
            )
            st.markdown("### 💬 ChatGPT Insights")
            st.markdown(response.choices[0].message.content)
        except Exception as e:
            st.error(f"API error: {e}")
