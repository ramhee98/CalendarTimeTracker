import os
import hashlib
import pandas as pd
from datetime import datetime, timedelta, timezone

# Ensure event store directory exists
os.makedirs("data", exist_ok=True)

def hash_url(url):
    return hashlib.md5(url.encode()).hexdigest()

def get_cache_file_path(url):
    return f"data/{hash_url(url)}.csv"

def load_cached_events(url):
    path = get_cache_file_path(url)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            return pd.read_csv(path, parse_dates=["start", "end"])
        except pd.errors.EmptyDataError:
            return pd.DataFrame()  # Recover gracefully if file is corrupt or empty
    return pd.DataFrame()

def save_events_to_cache(url, df):
    if df.empty or df.columns.empty:
        return  # Prevent saving if no usable data
    path = get_cache_file_path(url)
    df.to_csv(path, index=False)

def update_event_store(url, new_events_df, cutoff_days=30):
    cached_df = load_cached_events(url)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(cutoff_days)

    if "uid" in new_events_df.columns:
        # Only keep old events that are outside the replacement range
        if not cached_df.empty and "uid" in cached_df.columns:
            old_df = cached_df[cached_df["start"] < cutoff]
            # Replace recent and future events by dropping any matching UIDs
            uids_to_keep = set(new_events_df["uid"])
            old_df = old_df[~old_df["uid"].isin(uids_to_keep)]
        else:
            old_df = pd.DataFrame()

        combined_df = pd.concat([old_df, new_events_df], ignore_index=True)
    else:
        combined_df = new_events_df  # fallback

    save_events_to_cache(url, combined_df)
    return combined_df
