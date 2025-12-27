import streamlit as st
import pandas as pd
from openai import OpenAI
import os
import json
from datetime import date, datetime
from app import load_all_events, normalize_calendar_name, normalize_time, select_month_range
from dotenv import load_dotenv
from ai_config_manager import load_ai_config, get_system_prompt, get_available_prompt_types, format_prompt_template

st.set_page_config(page_title="AI Insights", layout="wide")
st.title("Calendar AI Insights (ChatGPT-Powered)")
st.caption("Understand how your time is distributed with AI-generated insights.")

# --- Persistent cache management ---
CACHE_FILE = "ai_responses_cache.json"

def load_ai_responses_cache():
    """Load AI responses from disk cache"""
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        st.warning(f"Could not load AI cache: {e}")
    return {}

def save_ai_responses_cache(cache_data):
    """Save AI responses to disk cache"""
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        st.warning(f"Could not save AI cache: {e}")
        return False

def clear_ai_responses_cache():
    """Clear AI responses cache file"""
    try:
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
        return True
    except Exception as e:
        st.warning(f"Could not clear AI cache file: {e}")
        return False

# Add loading state management
if "ai_insights_loaded" not in st.session_state:
    st.session_state.ai_insights_loaded = False

# Initialize AI response cache from persistent storage
if "ai_responses" not in st.session_state:
    st.session_state.ai_responses = load_ai_responses_cache()
if "current_analysis_key" not in st.session_state:
    st.session_state.current_analysis_key = None

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
col1, col2, col3 = st.columns([2, 1, 1])
with col2:
    if st.button("🔄 Refresh Data", help="Clear cache and reload calendar data"):
        st.cache_data.clear()
        st.session_state.ai_insights_loaded = False
        # Clear AI response cache when refreshing data
        st.session_state.ai_responses = {}
        clear_ai_responses_cache()
        st.session_state.current_analysis_key = None
        st.rerun()

with col3:
    if st.button("🗑️ Clear AI Cache", help="Clear only AI response cache"):
        st.session_state.ai_responses = {}
        clear_ai_responses_cache()
        st.session_state.current_analysis_key = None
        st.success("AI response cache cleared!")
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

# --- Create analysis key for caching ---
def create_analysis_key(start_date, end_date, group_mode, time_group, selected_prompt_type, system_prompt, model, verbosity):
    """Create a unique key for caching analysis results"""
    import hashlib
    key_string = f"{start_date}_{end_date}_{group_mode}_{time_group}_{selected_prompt_type}_{hash(system_prompt)}_{model}_{verbosity}"
    return hashlib.md5(key_string.encode()).hexdigest()

analysis_key = create_analysis_key(start_date, end_date, group_mode, time_group, selected_prompt_type, system_prompt, model, verbosity)

# Check if we have a cached response for current parameters
if analysis_key in st.session_state.ai_responses:
    cached_response = st.session_state.ai_responses[analysis_key]
    st.markdown("### 💬 ChatGPT Insights (Cached)")
    st.info("📋 Showing cached response. Click 'Analyze' to get a fresh response.")
    st.markdown(cached_response["content"])
    st.caption(f"*Generated on {cached_response['timestamp']} using {cached_response['model']}*")

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
            
            # Cache the response
            response_content = response.choices[0].message.content
            st.session_state.ai_responses[analysis_key] = {
                "content": response_content,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "model": model,
                "verbosity": verbosity,
                "prompt_type": selected_prompt_type,
                "is_customized": customize_prompt if enable_prompt_customization else False,
                "custom_prompt": system_prompt if (customize_prompt and enable_prompt_customization) else None
            }
            st.session_state.current_analysis_key = analysis_key
            
            # Save to persistent storage
            save_ai_responses_cache(st.session_state.ai_responses)
            
            st.markdown("### 💬 ChatGPT Insights")
            st.markdown(response_content)
            st.caption(f"*Generated using {model} with {verbosity} verbosity*")
        except Exception as e:
            st.error(f"API error: {e}")

# --- Show cached responses summary ---
if st.session_state.ai_responses:
    with st.expander(f"📚 Cached Responses ({len(st.session_state.ai_responses)} total)", expanded=False):
        st.caption("Previous analysis results are cached and will be shown when you return.")
        st.caption("Responses are saved to disk and will persist across page refreshes.")
        
        # Initialize session state for showing cached responses
        if "show_cached_response" not in st.session_state:
            st.session_state.show_cached_response = {}
        
        for i, (cache_key, cached_data) in enumerate(reversed(list(st.session_state.ai_responses.items()))):
            with st.container():
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.write(f"**Analysis #{len(st.session_state.ai_responses) - i}**")
                    
                    # Display prompt information
                    prompt_display = cached_data['prompt_type'].replace('_', ' ').title()
                    if cached_data.get('is_customized', False):
                        prompt_display += " (Customized)"
                    
                    st.caption(f"Generated: {cached_data['timestamp']} | Model: {cached_data['model']} | Prompt: {prompt_display}")
                with col2:
                    # Toggle button for showing/hiding cached response
                    is_shown = st.session_state.show_cached_response.get(cache_key, False)
                    button_text = "Hide" if is_shown else "Show"
                    if st.button(button_text, key=f"toggle_cached_{cache_key}"):
                        st.session_state.show_cached_response[cache_key] = not is_shown
                        st.rerun()
                
                # Show cached response below if toggled on
                if st.session_state.show_cached_response.get(cache_key, False):
                    st.markdown("#### 💬 Cached Response")
                    
                    # Show custom prompt if it was customized
                    if cached_data.get('is_customized', False) and cached_data.get('custom_prompt'):
                        st.markdown("**🛠️ Custom Prompt Used:**")
                        st.code(cached_data['custom_prompt'], language="text")
                        st.markdown("**Response:**")
                    
                    st.markdown(cached_data["content"])
                    st.caption(f"*Generated on {cached_data['timestamp']} using {cached_data['model']} with {cached_data['verbosity']} verbosity*")
                
                st.divider()
