import os
import hashlib
import pandas as pd

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

def update_event_store(url, new_events_df):
    cached_df = load_cached_events(url)
    if "uid" in new_events_df.columns:
        # Remove old rows with same UID
        if not cached_df.empty and "uid" in cached_df.columns:
            cached_df = cached_df[~cached_df["uid"].isin(new_events_df["uid"])]
        combined_df = pd.concat([cached_df, new_events_df], ignore_index=True)
    else:
        # Fallback deduplication if no UID
        if not cached_df.empty:
            combined_df = pd.concat([cached_df, new_events_df]).drop_duplicates(subset=["start", "end"])
        else:
            combined_df = new_events_df
    save_events_to_cache(url, combined_df)
    return combined_df
