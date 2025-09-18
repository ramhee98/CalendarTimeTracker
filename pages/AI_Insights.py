import streamlit as st
import pandas as pd
from openai import OpenAI
import os
from datetime import date
from app import load_all_events, normalize_calendar_name, normalize_time, select_month_range
from dotenv import load_dotenv
from ai_config_manager import load_ai_config, get_system_prompt, get_available_prompt_types, format_prompt_template

st.set_page_config(page_title="AI Insights", layout="wide")
st.title("Calendar AI Insights (ChatGPT-Powered)")
st.caption("Understand how your time is distributed with AI-generated insights.")

# Add loading state management
if "ai_insights_loaded" not in st.session_state:
    st.session_state.ai_insights_loaded = False

# --- Load AI configuration ---
ai_config = load_ai_config()
available_models = ai_config.get("model_settings", {}).get("available_models")
default_model = ai_config.get("model_settings", {}).get("default_model")
default_verbosity = ai_config.get("model_settings", {}).get("default_verbosity", "low")
enable_prompt_customization = ai_config.get("ui_settings", {}).get("enable_prompt_customization", True)
max_prompt_length = ai_config.get("ui_settings", {}).get("max_prompt_length", 2000)

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
show_raw_data_default = ai_config.get("ui_settings", {}).get("show_raw_data_by_default", False)
with st.expander("📋 Show summary data sent to ChatGPT", expanded=show_raw_data_default):
    st.dataframe(summary)

# --- Prompt selection and customization ---
available_prompt_types = get_available_prompt_types(ai_config)

# Create columns for prompt selection and customization
prompt_col1, prompt_col2 = st.columns([2, 1])

with prompt_col1:
    if len(available_prompt_types) > 1:
        selected_prompt_type = st.selectbox(
            "Select prompt template",
            options=available_prompt_types,
            index=0,
            format_func=lambda x: x.replace("_", " ").title(),
            help="Choose from predefined prompt templates or use 'Default'"
        )
    else:
        selected_prompt_type = "default"

with prompt_col2:
    if enable_prompt_customization:
        customize_prompt = st.checkbox("Customize prompt", value=False)
    else:
        customize_prompt = False

# Get the base prompt from configuration
base_system_prompt = get_system_prompt(ai_config, selected_prompt_type)
formatted_system_prompt = format_prompt_template(base_system_prompt, group_label, time_group)

# --- Editable system prompt ---
if customize_prompt and enable_prompt_customization:
    system_prompt = st.text_area(
        "Customize AI assistant behavior",
        value=formatted_system_prompt,
        height=120,
        max_chars=max_prompt_length,
        help=f"Customize the system prompt (max {max_prompt_length} characters). Available placeholders: {{group_label}}, {{time_group}}"
    )
else:
    system_prompt = formatted_system_prompt
    if len(available_prompt_types) > 1 or not enable_prompt_customization:
        with st.expander("👁️ View current prompt"):
            st.code(system_prompt, language="text")

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
    index=["low", "medium", "high"].index(default_verbosity) if default_verbosity in ["low", "medium", "high"] else 1,
    help="Controls how detailed the response will be. Low is concise, medium is balanced, high is very detailed."
)

model = st.selectbox(
    "Select model",
    options=available_models,
    index=available_models.index(default_model) if default_model in available_models else 0,
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
