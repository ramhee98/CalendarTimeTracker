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

# Add loading state management
if "ai_insights_loaded" not in st.session_state:
    st.session_state.ai_insights_loaded = False

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

# --- Cache management ---
col1, col2 = st.columns([3, 1])
with col2:
    if st.button("🔄 Refresh Data", help="Clear cache and reload calendar data"):
        st.cache_data.clear()
        st.session_state.ai_insights_loaded = False
        st.rerun()

# --- Load calendar data ---
with st.spinner("Loading calendar data..."):
    all_events, source_type = load_all_events()

if not all_events:
    st.warning("No events available to analyze.")
    st.stop()

# Mark as loaded
st.session_state.ai_insights_loaded = True

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

# --- Choose time grouping ---
time_group = st.radio(
    "Time grouping",
    options=["day", "week", "month"],
    index=2,
    horizontal=True,
    help="Choose how to group the data by time periods"
)

# --- Preprocess ---
with st.spinner("Processing calendar data..."):
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

# --- Add time grouping column ---
if time_group == "day":
    df["time_period"] = df["start"].dt.date
elif time_group == "week":
    df["time_period"] = df["start"].dt.to_period("W").astype(str)
elif time_group == "month":
    df["time_period"] = df["start"].dt.to_period("M").astype(str)

# --- Summarize by group and time period ---
summary = (
    df.groupby(["group", "time_period"])["duration_hours"]
    .agg(Total_Hours="sum", Avg_Hours_Per_Event="mean", Event_Count="count")
    .reset_index()
)
summary.rename(columns={"group": group_label, "time_period": f"Time Period ({time_group.title()})"}, inplace=True)

# --- Optional: show the raw summary ---
with st.expander("📋 Show summary data sent to ChatGPT"):
    st.dataframe(summary)

# --- Editable system prompt ---
default_system_prompt = (
    "You are a helpful assistant that analyzes calendar data for time management. "
    f"Based on the following {group_label.lower()} usage summary grouped by {time_group}, provide natural language insights: "
    "highlight patterns over time, which calendars take most time in different periods, trends and changes, any imbalance or overbooking, and give simple time management tips."
)

system_prompt = st.text_area(
    "🛠️ Customize AI assistant behavior",
    value=default_system_prompt,
    height=100
)

# --- Prepare user prompt ---
summary_text = summary.to_string(index=False)

user_prompt = f"""
Here is my {group_label.lower()} usage summary grouped by {time_group} from {start_date} to {end_date}:

{summary_text}

Please analyze this and provide meaningful insights about patterns over time and time management.
"""

verbosity = st.selectbox(
    "Select verbosity level",
    options=["low", "medium", "high"],
    index=0,
    help="Controls how detailed the response will be. Low is concise, medium is balanced, high is very detailed."
)

model = st.selectbox(
    "Select model",
    options=["gpt-5-nano", "gpt-5-mini", "gpt-5"],
    index=0,
    help="Choose the model to use for analysis. Higher models may provide better insights."
)

# --- Analyze ---
if st.button(f"🔍 Analyze with ChatGPT ({model})"):
    with st.spinner("Thinking..."):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                verbosity=verbosity
            )
            st.markdown("### 💬 ChatGPT Insights")
            st.markdown(response.choices[0].message.content)
        except Exception as e:
            st.error(f"API error: {e}")
