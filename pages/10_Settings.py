import glob
import json
import os
import re
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

from analysis import is_all_day, merge_tag_variants, tag_occurrences
from calendar_store import decode_ics_response, get_cache_file_path, hash_url
from utils import (
    CALENDARS_JSON, ensure_ics_content, fetch_and_parse_ics, load_all_events_from_cache, load_app_settings,
    load_calendar_urls, load_tag_groups, normalize_calendar_name, normalize_time, read_calendars_config,
    save_app_settings, write_calendars_config, write_file_with_backup,
)

st.set_page_config(page_title="Settings", layout="wide")
st.title("⚙️ Settings")
st.caption("Manage your calendars, how time is counted and displayed, the Tags page, the local event cache, "
           "and edit the config files directly.")

# Messages that should survive the rerun after a save. They go into a
# container that always exists: a message added above the tabs would shift
# them, and Streamlit then re-creates them on the first tab.
with st.container():
    for msg in st.session_state.pop("settings_flash", []):
        st.success(msg)


def flash(msg):
    st.session_state.setdefault("settings_flash", []).append(msg)


COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
EDITED_KEYS = ("url", "custom_name", "type", "category", "color")
CALENDAR_TYPES = ("time", "tags")


def is_sharing_link(url):
    """Google Calendar sharing links serve a web page, not the .ics feed."""
    return "calendar.google.com/calendar/u/" in url or "?cid=" in url


def format_age(ts):
    seconds = (datetime.now() - ts).total_seconds()
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{seconds / 3600:.0f} h ago"
    return f"{seconds / 86400:.0f} days ago"


def looks_like_tags(path, threshold=0.9, min_events=5):
    """True if almost all cached events are zero-length or all-day, as in a
    tag calendar (e.g. Oura tags) rather than a calendar of time blocks."""
    try:
        df = pd.read_csv(path, usecols=["start", "end"])
    except Exception:
        return False
    start = pd.to_datetime(df["start"], errors="coerce", utc=True)
    end = pd.to_datetime(df["end"], errors="coerce", utc=True)
    valid = start.notna() & end.notna()
    if valid.sum() < min_events:
        return False
    start, end = start[valid], end[valid]
    marker = (start == end) | is_all_day(start, end)
    return marker.mean() >= threshold


def cache_info(path):
    """Events, first/last start and last update of one cache CSV."""
    info = {"Events": 0, "First event": "—", "Last event": "—", "Updated": "never", "Size (KB)": 0.0}
    if not os.path.exists(path):
        return info
    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    info["Updated"] = f"{mtime:%Y-%m-%d %H:%M} ({format_age(mtime)})"
    info["Size (KB)"] = round(os.path.getsize(path) / 1024, 1)
    try:
        df = pd.read_csv(path, usecols=["start"])
    except Exception:
        return info
    starts = pd.to_datetime(df["start"], errors="coerce", utc=True).dropna()
    info["Events"] = len(df)
    if not starts.empty:
        info["First event"] = f"{starts.min():%Y-%m-%d}"
        info["Last event"] = f"{starts.max():%Y-%m-%d}"
    return info


tab_calendars, tab_display, tab_tags, tab_cache, tab_raw = st.tabs(
    ["📅 Calendars", "📊 Data & display", "🏷️ Tags", "💾 Cache & sync", "📝 Raw files"]
)

# =====================================================================
# Calendars
# =====================================================================
def render_calendars_tab():
    has_json = os.path.exists(CALENDARS_JSON)
    if not has_json and os.path.exists("calendars.txt"):
        st.info(
            "Your calendars are configured in `calendars.txt`. Editing here needs `calendars.json`, "
            "which also supports categories and colors. Converting keeps `calendars.txt` untouched; "
            "`calendars.json` is used from then on."
        )
        if st.button("Convert calendars.txt to calendars.json"):
            txt_calendars, _ = load_calendar_urls(calendars_json_file="__none__.json")
            write_calendars_config({"calendars": [
                {"url": c["url"], **({"custom_name": c["custom_name"]} if c["custom_name"] != "Unnamed" else {})}
                for c in (txt_calendars or [])
            ]})
            st.cache_data.clear()
            flash(f"Converted {len(txt_calendars or [])} calendar(s) to calendars.json.")
            st.rerun()
        return

    try:
        config = read_calendars_config()
    except Exception as e:
        st.error(f"Could not read {CALENDARS_JSON}: {e}. Fix the file by hand, then reload.")
        return

    st.markdown(
        "One row per calendar. **URL** must be the iCal feed itself (in Google Calendar: "
        "*Secret address in iCal format*), or `file:///path/to/calendar.ics`. "
        "Leave **Category** empty to use the name, and **Color** empty for an automatic color. "
        "Set **Type** to `tags` for calendars of markers rather than time blocks, such as "
        "[Oura tags](https://github.com/ramhee98/oura-tags-ical) (☕ Coffee, 🍺 Beer): they're counted "
        "on the Tags page and left out of all hour totals."
    )
    editor_df = pd.DataFrame(
        [{k: str(c.get(k, "") or "") for k in EDITED_KEYS} for c in config["calendars"]],
        columns=list(EDITED_KEYS),
    ).astype("object")
    editor_df["type"] = ["tags" if str(t).lower() == "tags" else "time" for t in editor_df["type"]]
    edited = st.data_editor(
        editor_df,
        num_rows="dynamic",
        hide_index=True,
        use_container_width=True,
        key="calendars_editor",
        column_config={
            "url": st.column_config.TextColumn("URL", width="large", required=True),
            "custom_name": st.column_config.TextColumn("Name"),
            "type": st.column_config.SelectboxColumn(
                "Type", options=list(CALENDAR_TYPES), default="time", required=True,
                help="time = events with durations, tags = markers counted on the Tags page "
                     "(category and color are ignored)",
            ),
            "category": st.column_config.TextColumn("Category"),
            "color": st.column_config.TextColumn("Color", help="Hex color like #33FF57", validate=r"^(#[0-9a-fA-F]{6})?$"),
        },
    )

    # Color preview of the saved configuration (including automatic colors)
    configured, _ = load_calendar_urls()
    if configured:
        swatches = " ".join(
            f"<span style='white-space:nowrap;margin-right:12px'>"
            f"<span style='display:inline-block;width:12px;height:12px;border-radius:2px;"
            f"background:{c['color']};vertical-align:middle'></span> {c['custom_name']}</span>"
            for c in configured
            if c.get("type") != "tags"
        )
        st.markdown(swatches, unsafe_allow_html=True)

    if st.button("💾 Save calendars", type="primary"):
        originals = {c.get("url"): c for c in config["calendars"]}
        new_calendars, problems, warnings = [], [], []
        seen_urls, seen_names = set(), {}
        for i, row in edited.reset_index(drop=True).iterrows():
            values = {k: ("" if pd.isna(row[k]) else str(row[k]).strip()) for k in EDITED_KEYS}
            if not any(v for k, v in values.items() if k != "type"):
                continue  # empty row
            url, name, color = values["url"], values["custom_name"], values["color"]
            label = f"Row {i + 1}" + (f" ({name})" if name else "")
            if not url:
                problems.append(f"{label}: URL is missing.")
                continue
            if not url.startswith(("https://", "http://", "file://")):
                problems.append(f"{label}: URL must start with https://, http:// or file://.")
            elif is_sharing_link(url):
                problems.append(f"{label}: this is a Google Calendar sharing link, not the iCal feed. "
                                "Use the \"Secret address in iCal format\".")
            if url in seen_urls:
                problems.append(f"{label}: this URL is already used by another row.")
            seen_urls.add(url)
            if color and not COLOR_RE.match(color):
                problems.append(f"{label}: color must look like #33FF57.")
            if name:
                if name in seen_names:
                    warnings.append(f"'{name}' is used by rows {seen_names[name]} and {i + 1}; "
                                    "their events are shown together.")
                seen_names.setdefault(name, i + 1)

            # Keep keys this editor doesn't know about (e.g. added by hand)
            entry = {k: v for k, v in originals.get(url, {}).items() if k not in EDITED_KEYS}
            entry = {"url": url, **{k: values[k] for k in ("custom_name", "category", "color") if values[k]}, **entry}
            if values["type"] == "tags":
                entry["type"] = "tags"  # omitted for normal time calendars
            new_calendars.append(entry)

        if problems:
            for p in problems:
                st.error(p)
        else:
            new_config = dict(config)
            new_config["calendars"] = new_calendars
            write_calendars_config(new_config)
            st.cache_data.clear()
            added = len(seen_urls - set(originals))
            removed = len(set(originals) - seen_urls)
            flash(
                f"Saved {len(new_calendars)} calendar(s) to {CALENDARS_JSON} "
                f"(previous version kept as {CALENDARS_JSON}.bak)."
                + (f" {added} new: sync them on the Cache & sync tab." if added else "")
                + (f" {removed} removed: their cached events stay until you clean up unused cache files." if removed else "")
            )
            for w in warnings:
                flash("Note: " + w)
            st.rerun()

    with st.expander("🔌 Test a calendar URL"):
        test_url = st.text_input("URL to test", placeholder="https://calendar.google.com/calendar/ical/.../basic.ics")
        if st.button("Test URL") and test_url:
            try:
                if test_url.startswith("file://"):
                    with open(test_url[len("file://"):], "r", encoding="utf-8") as f:
                        content = f.read()
                else:
                    response = requests.get(test_url, timeout=15)
                    response.raise_for_status()
                    content = decode_ics_response(response)
                ensure_ics_content(content)
                name = next((line.split(":", 1)[1].strip() for line in content.splitlines()
                             if line.startswith("X-WR-CALNAME:")), None)
                st.success(f"Valid iCal feed with {content.count('BEGIN:VEVENT')} event(s)"
                           + (f", calendar name: {name}" if name else "") + ".")
            except Exception as e:
                st.error(f"Not usable: {e}")


with tab_calendars:
    render_calendars_tab()

# =====================================================================
# Data & display
# =====================================================================
with tab_display:
    settings = load_app_settings()
    with st.form("display_settings"):
        st.markdown("**Counting**")
        long_events = st.radio(
            "All-day & multi-day events",
            ["include", "exclude"],
            index=1 if settings["long_events"] == "exclude" else 0,
            format_func={"include": "Count fully", "exclude": "Exclude"}.get,
            horizontal=True,
            help="A 3-day trip counts as 72 hours when counted fully. "
                 "Also available as ⚖️ Data accuracy in the sidebar of each page.",
        )
        long_hours = st.number_input(
            "Treat events longer than (hours) as long", min_value=1, max_value=168,
            value=int(settings["long_event_hours"]), step=1,
        )
        ignore_free = st.checkbox(
            "Ignore events marked as free",
            value=bool(settings.get("ignore_free_events")),
            help="Leave out events whose availability is \"Free\" instead of \"Busy\" "
                 "(iCal TRANSP:TRANSPARENT). Sync once after updating so the cache "
                 "knows which events are free.",
        )

        st.markdown("**Defaults when a page opens**")
        has_categories = load_calendar_urls()[1] == "json"
        default_view = st.radio(
            "View data by",
            ["calendar", "category"],
            index=1 if settings.get("default_view") == "category" else 0,
            format_func=str.title,
            horizontal=True,
            disabled=not has_categories,
            help=None if has_categories else "Categories need calendars.json.",
        )
        range_options = [1, 3, 6, 12, 24, 0]
        current_range = int(settings.get("default_range_months", 12) or 0)
        default_range = st.selectbox(
            "Date range",
            range_options,
            index=range_options.index(current_range) if current_range in range_options else 3,
            format_func=lambda m: "All data" if m == 0 else f"Last {m} month{'s' if m != 1 else ''}",
        )

        if st.form_submit_button("💾 Save", type="primary"):
            settings.update({
                "long_events": long_events,
                "long_event_hours": int(long_hours),
                "ignore_free_events": bool(ignore_free),
                "default_view": default_view,
                "default_range_months": int(default_range),
            })
            save_app_settings(settings)
            flash("Saved. Pages use the new defaults the next time they open.")
            st.rerun()

# =====================================================================
# Tags page
# =====================================================================
RANGE_OPTIONS = [1, 3, 6, 12, 24, 0]


def range_label(months):
    return "All data" if months == 0 else f"Last {months} month{'s' if months != 1 else ''}"


def known_tags(merge):
    """Tag names in the cached tag calendars (without monthly summaries)."""
    events, _ = load_all_events_from_cache("tags")
    if not events:
        return []
    df = pd.DataFrame(events)
    df["calendar"] = df["calendar_name"].apply(normalize_calendar_name)
    occ = tag_occurrences(normalize_time(df, tz="local"))
    if merge:
        occ = merge_tag_variants(occ)
    return sorted(occ["tag"].unique(), key=str.casefold)


def render_tags_tab():
    calendars, _ = load_calendar_urls()
    tag_calendar_names = sorted({normalize_calendar_name(c["custom_name"])
                                 for c in (calendars or []) if c.get("type") == "tags"})
    if not tag_calendar_names:
        st.info("No tag calendars configured. Set a calendar's Type to `tags` on the Calendars tab first.")
        return

    settings = load_app_settings()
    tags = known_tags(settings["tags_merge_variants"])
    group_names = [g["name"] for g in load_tag_groups()]
    if not tags:
        st.caption("No tag events cached yet: sync the tag calendars to pick hidden or fixed tags here.")

    def keep_known(values, options):
        return [v for v in values if v in options]

    with st.form("tag_settings"):
        st.markdown("**Charts**")
        chart_mode = st.radio(
            "Preselected in the charts",
            ["top", "fixed"],
            index=1 if settings["tags_chart_mode"] == "fixed" else 0,
            format_func={"top": "The most frequent", "fixed": "A fixed list"}.get,
            horizontal=True,
        )
        top_n = st.number_input("How many of the most frequent", min_value=1, max_value=20,
                                value=int(settings["tags_chart_top_n"]), step=1)
        fixed_options = group_names + [t for t in tags if t not in group_names]
        fixed = st.multiselect(
            "Fixed list (groups and tags, in this order)",
            fixed_options,
            default=keep_known(settings["tags_chart_fixed"], fixed_options),
            help="Groups only appear in the group views and single tags only where they aren't grouped, "
                 "so pick both if you switch views.",
        )
        fill = st.checkbox(
            "Replace fixed entries without data in the date range by the most frequent ones",
            value=bool(settings["tags_chart_fill"]),
        )
        keep = st.checkbox(
            "Keep my chart selection when I switch the view or date range",
            value=bool(settings["tags_keep_selection"]),
            help="Otherwise the selection resets to the preselection above whenever the list of tags changes. "
                 "Kept until the browser tab is reloaded.",
        )

        st.markdown("**Defaults when the page opens**")
        view_labels = {"grouped": "Groups + ungrouped tags", "groups": "Groups only", "tags": "Single tags"}
        views = list(view_labels)
        default_view = st.radio("Show", views, format_func=view_labels.get, horizontal=True,
                                index=views.index(settings["tags_default_view"])
                                if settings["tags_default_view"] in views else 0,
                                help="Only used once tag groups exist.")
        granularities = ["Day", "Week", "Month"]
        granularity = st.radio("Count per", granularities, horizontal=True,
                               index=granularities.index(settings["tags_default_granularity"])
                               if settings["tags_default_granularity"] in granularities else 1)
        range_choices = [None] + RANGE_OPTIONS
        current = settings["tags_range_months"]
        tags_range = st.selectbox(
            "Date range",
            range_choices,
            index=range_choices.index(current) if current in range_choices else 0,
            format_func=lambda m: (f"Same as the other pages ({range_label(int(settings['default_range_months'] or 0))})"
                                   if m is None else range_label(m)),
        )
        if len(tag_calendar_names) > 1:
            default_calendars = st.multiselect(
                "Tag calendars", tag_calendar_names,
                default=keep_known(settings["tags_default_calendars"], tag_calendar_names),
                help="Leave empty to select all.",
            )
        else:
            default_calendars = settings["tags_default_calendars"]

        st.markdown("**Tag names**")
        hidden_options = sorted(set(tags) | set(settings["tags_hidden"]), key=str.casefold)
        hidden = st.multiselect(
            "Hidden tags", hidden_options, default=settings["tags_hidden"],
            help="Left out of the Tags page entirely, including groups and keywords.",
        )
        merge = st.checkbox(
            "Merge tags that differ only in the emoji variant or spacing (☕ Coffee and ☕️ Coffee)",
            value=bool(settings["tags_merge_variants"]),
        )

        if st.form_submit_button("💾 Save", type="primary"):
            if chart_mode == "fixed" and not fixed:
                st.error("Pick at least one group or tag for the fixed list, or use the most frequent.")
            else:
                settings.update({
                    "tags_chart_mode": chart_mode,
                    "tags_chart_top_n": int(top_n),
                    "tags_chart_fixed": fixed,
                    "tags_chart_fill": fill,
                    "tags_keep_selection": keep,
                    "tags_default_view": default_view,
                    "tags_default_granularity": granularity,
                    "tags_range_months": tags_range,
                    "tags_default_calendars": default_calendars,
                    "tags_hidden": hidden,
                    "tags_merge_variants": merge,
                })
                save_app_settings(settings)
                # Let the Tags page start from the new preselection
                st.session_state.pop("tags_chart_selection", None)
                flash("Saved the Tags settings.")
                st.rerun()


with tab_tags:
    render_tags_tab()

# =====================================================================
# Cache & sync
# =====================================================================
with tab_cache:
    calendars, _ = load_calendar_urls()
    calendars = calendars or []
    st.caption(
        "Events are cached as CSV files in `data/`. While the app is running, all calendars sync "
        "automatically every 24 hours. Events older than 30 days stay in the cache even after they "
        "disappear from the source calendar."
    )

    def _save_sync_scope():
        settings = load_app_settings()
        settings["sync_scope"] = st.session_state.sync_scope
        save_app_settings(settings)

    scopes = {"all": "All calendars", "page": "Only the calendars the page shows"}
    current_scope = load_app_settings().get("sync_scope", "all")
    st.radio(
        "🔄 Sync Calendars on a page fetches",
        list(scopes),
        index=1 if current_scope == "page" else 0,
        format_func=scopes.get,
        horizontal=True,
        key="sync_scope",
        on_change=_save_sync_scope,
        help="Only the calendars the page shows: time calendars on Search, Compare Periods, Trends and "
             "AI Insights, tag calendars on the Tags page. Faster, but the other calendars stay as cached until "
             "you sync them, sync on Home, or the automatic daily sync runs. Home always syncs all calendars. "
             "Saved immediately.",
    )

    rows, tag_hints = [], []
    for c in calendars:
        path = get_cache_file_path(c["url"])
        info = cache_info(path)
        rows.append({"Calendar": c["custom_name"], "Type": c.get("type", "time"),
                     "Category": c["category"] or "—", **info})
        if c.get("type") != "tags" and looks_like_tags(path):
            tag_hints.append(c["custom_name"])
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        for name in tag_hints:
            st.info(
                f"**{name}** looks like a tag calendar: almost all of its events are all-day or have no "
                "duration, so they add whole days or nothing to your totals. If it holds markers like "
                "☕ Coffee rather than time blocks, set its Type to `tags` on the Calendars tab."
            )
    else:
        st.info("No calendars configured.")

    if calendars:
        names = [f"{c['custom_name']}" for c in calendars]
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1:
            choice = st.selectbox("Calendar", range(len(calendars)), format_func=lambda i: names[i])
        selected = calendars[choice]
        with c2:
            st.write("")
            if st.button("🔄 Sync this calendar", use_container_width=True):
                with st.spinner(f"Syncing {selected['custom_name']}..."):
                    try:
                        events = fetch_and_parse_ics(selected["url"], selected["custom_name"])
                        st.cache_data.clear()
                        flash(f"Synced {selected['custom_name']}: {len(events)} cached events.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Sync failed for {selected['custom_name']}: {e}")
        with c3:
            st.write("")
            if st.button("🔄 Sync all calendars", use_container_width=True):
                failed = []
                progress = st.progress(0.0)
                for i, c in enumerate(calendars):
                    progress.progress(i / len(calendars), text=f"Syncing {c['custom_name']}...")
                    try:
                        fetch_and_parse_ics(c["url"], c["custom_name"])
                    except Exception as e:
                        failed.append(f"{c['custom_name']}: {e}")
                progress.empty()
                st.cache_data.clear()
                for f in failed:
                    st.error(f)
                if not failed:
                    flash(f"Synced {len(calendars)} calendar(s).")
                    st.rerun()

        with st.expander(f"🗑️ Clear the cache of {selected['custom_name']}"):
            st.warning(
                "This deletes all cached events of this calendar. The next sync only brings back what "
                "the source calendar still contains, so older events that were removed there are lost."
            )
            confirm = st.checkbox(f"Yes, delete the cached events of {selected['custom_name']}")
            if st.button("Delete cache", disabled=not confirm):
                path = get_cache_file_path(selected["url"])
                if os.path.exists(path):
                    os.remove(path)
                st.cache_data.clear()
                flash(f"Cleared the cache of {selected['custom_name']}.")
                st.rerun()

    # --- Cache files that no configured calendar uses ---
    used = {f"{hash_url(c['url'])}.csv" for c in calendars}
    orphans = [p for p in sorted(glob.glob("data/*.csv")) if os.path.basename(p) not in used]
    st.subheader("Unused cache files")
    if not orphans:
        st.caption("Every cache file belongs to a configured calendar.")
    else:
        orphan_rows = []
        for p in orphans:
            try:
                first = pd.read_csv(p, usecols=["calendar"], nrows=1)["calendar"]
                former_name = str(first.iloc[0]) if not first.empty else "?"
            except Exception:
                former_name = "?"
            orphan_rows.append({"File": os.path.basename(p), "Former calendar": former_name, **cache_info(p)})
        st.caption(
            f"{len(orphans)} cache file(s) belong to calendars that were removed or whose URL changed. "
            "Re-adding the same URL would reuse its file, including events older than 30 days."
        )
        select_all = st.checkbox("Select all", key="orphans_select_all")
        orphan_df = pd.DataFrame(orphan_rows)
        orphan_df.insert(0, "Delete", select_all)
        edited_orphans = st.data_editor(
            orphan_df,
            use_container_width=True,
            hide_index=True,
            # Re-key on "Select all" so toggling it resets the individual ticks
            key=f"orphans_editor_{select_all}",
            disabled=[c for c in orphan_df.columns if c != "Delete"],
            column_config={"Delete": st.column_config.CheckboxColumn("Delete", help="Tick the files to delete")},
        )
        to_delete = edited_orphans.loc[edited_orphans["Delete"].fillna(False).astype(bool), "File"].tolist()
        if to_delete:
            confirm = st.checkbox(
                f"Yes, delete {len(to_delete)} selected file(s): {', '.join(to_delete)}",
                key="orphans_confirm",
            )
        else:
            confirm = False
            st.caption("Tick the files you want to delete.")
        if st.button(f"Delete selected ({len(to_delete)})", disabled=not confirm):
            for name in to_delete:
                path = os.path.join("data", name)
                if path in orphans and os.path.exists(path):
                    os.remove(path)
            flash(f"Deleted {len(to_delete)} unused cache file(s): {', '.join(to_delete)}.")
            st.rerun()


# =====================================================================
# Raw config files
# =====================================================================
# File -> (what it holds, required top-level list key or None, session
# state keys that hold a copy of it and must be reloaded after a save)
CONFIG_FILES = {
    "calendars.json": ("Calendars: URL, name, type, category, color", "calendars", []),
    "calendars.txt": ("Calendars, one URL per line (only used without calendars.json)", None, []),
    "app_settings.json": ("Settings from this page", None, ["tags_chart_selection"]),
    "goals.json": ("Goals", "goals", []),
    "tag_groups.json": ("Tag groups", "groups", ["tag_groups_draft", "tag_groups_next_id"]),
    "social_analysis_settings.json": ("Social Analysis settings", None, ["settings"]),
    "ai_config.json": ("AI Insights prompts and model settings", None, []),
}


def validate_config(name, text):
    """Error message for invalid file content, or None."""
    if not name.endswith(".json"):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return f"Invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}."
    if not isinstance(data, dict):
        return "The file must contain a JSON object ({ ... })."
    list_key = CONFIG_FILES[name][1]
    if list_key and not isinstance(data.get(list_key, []), list):
        return f'"{list_key}" must be a list ([ ... ]).'
    if name == "calendars.json":
        for i, c in enumerate(data.get("calendars", []), start=1):
            if not isinstance(c, dict) or not str(c.get("url", "")).strip():
                return f"Calendar {i} needs a \"url\"."
    return None


def render_raw_tab():
    st.caption(
        "Edit the config files as text. Saving checks that JSON files are valid and keeps the previous "
        "version as `<file>.bak`. Secrets such as the OpenAI key live in `.env` and aren't shown here."
    )
    names = list(CONFIG_FILES)
    name = st.selectbox(
        "File", names,
        format_func=lambda n: f"{n} — {CONFIG_FILES[n][0]}" + ("" if os.path.exists(n) else " (not created yet)"),
        key="raw_file",
    )
    exists = os.path.exists(name)
    original = ""
    if exists:
        with open(name, "r", encoding="utf-8") as f:
            original = f.read()
    # Bumping the version gives the text area a new key, i.e. reloads it from disk
    version = st.session_state.setdefault("raw_version", 0)
    text_key = f"raw_text_{name}_{version}"
    if text_key not in st.session_state:  # Format / Load backup put their text here
        st.session_state[text_key] = original
    text = st.text_area(name, height=480, key=text_key, label_visibility="collapsed")
    changed = text != original

    error = validate_config(name, text) if changed and text.strip() else None
    if error:
        st.error(error)
    elif changed:
        st.caption("⚠️ Unsaved changes.")

    c1, c2, c3, c4 = st.columns(4)
    if c1.button("💾 Save file", type="primary", use_container_width=True,
                 disabled=not changed or bool(error) or not text.strip()):
        write_file_with_backup(name, text if text.endswith("\n") else text + "\n")
        for key in CONFIG_FILES[name][2]:
            st.session_state.pop(key, None)
        st.cache_data.clear()
        st.session_state.raw_version += 1
        flash(f"Saved {name}" + (f" (previous version kept as {name}.bak)." if exists else "."))
        st.rerun()
    if c2.button("↩️ Discard changes", use_container_width=True, disabled=not changed):
        st.session_state.raw_version += 1
        st.rerun()
    if c3.button("🧹 Format JSON", use_container_width=True,
                 disabled=not name.endswith(".json") or bool(error) or not text.strip(),
                 help="Indent consistently. Doesn't save."):
        st.session_state.raw_version += 1
        st.session_state[f"raw_text_{name}_{st.session_state.raw_version}"] = (
            json.dumps(json.loads(text), indent=2 if name != "calendars.json" else 4, ensure_ascii=False) + "\n"
        )
        st.rerun()
    c4.download_button("⬇️ Download", text.encode("utf-8"), name, use_container_width=True, disabled=not text)

    if os.path.exists(name + ".bak"):
        with st.expander(f"🕘 Backup ({name}.bak)"):
            with open(name + ".bak", "r", encoding="utf-8") as f:
                backup = f.read()
            st.code(backup, language="json" if name.endswith(".json") else None)
            if st.button("Load the backup into the editor", disabled=backup == text):
                st.session_state.raw_version += 1
                st.session_state[f"raw_text_{name}_{st.session_state.raw_version}"] = backup
                st.rerun()


with tab_raw:
    render_raw_tab()
