from datetime import date

import altair as alt
import pandas as pd
import streamlit as st

from analysis import (
    apply_tag_groups, period_label, resolve_tag_groups, suggest_tag_groups, suggest_tag_groups_by_name,
    tag_counts_by_period,
    tag_group_members, tag_occurrences, tag_summary,
)
from utils import (
    load_calendar_urls, load_events_for_page, load_tag_groups, normalize_calendar_name, normalize_time,
    save_tag_groups, select_month_range,
)

st.set_page_config(page_title="Tags", layout="wide")
st.title("🏷️ Tags")
st.caption("Count markers from tag calendars, such as Oura tags (☕ Coffee, 🍺 Beer). "
           "Tag calendars are never counted as time.")

for msg in st.session_state.pop("tags_flash", []):
    st.success(msg)

calendars, _ = load_calendar_urls()
tag_calendars = [c for c in (calendars or []) if c.get("type") == "tags"]
if not tag_calendars:
    st.info(
        "No tag calendars configured yet. Add a calendar of markers, for example from "
        "[oura-tags-ical](https://github.com/ramhee98/oura-tags-ical), and set its **Type** to `tags` "
        "on the Settings page (or add `\"type\": \"tags\"` to it in `calendars.json`)."
    )
    st.stop()

all_events, _ = load_events_for_page("tags", kind="tags", stop_if_empty=False)
if not all_events:
    st.warning("No tag events cached yet. Press 🔄 Sync Calendars to fetch them.")
    st.stop()

with st.spinner("Processing tags..."):
    df = pd.DataFrame(all_events)
    df["calendar"] = df["calendar_name"].apply(normalize_calendar_name)
    df = normalize_time(df, tz="local")
    occ = tag_occurrences(df)

if occ.empty:
    st.warning("The tag calendars contain no tag events.")
    st.stop()

if len(tag_calendars) > 1:
    names = sorted(occ["calendar"].unique())
    chosen = st.multiselect("Tag calendars", names, default=names)
    occ = occ[occ["calendar"].isin(chosen)]
    if occ.empty:
        st.info("Select at least one tag calendar.")
        st.stop()

# --- Tag groups (editor works on a draft until saved) ---
groups = load_tag_groups()
all_tags = sorted(occ["tag"].unique())
if "tag_groups_draft" not in st.session_state:
    st.session_state.tag_groups_draft = [dict(g, _id=i) for i, g in enumerate(groups)]
    st.session_state.tag_groups_next_id = len(groups)
draft = st.session_state.tag_groups_draft


def _add_to_draft(group):
    draft.append(dict(group, _id=st.session_state.tag_groups_next_id))
    st.session_state.tag_groups_next_id += 1


with st.expander("🗂️ Tag groups", expanded=not groups):
    st.caption(
        "Combine tags into groups, e.g. all coffee types into ☕ Coffee. A tag joins a group if you pick it "
        "or if it matches one of the group's **keywords** exactly (comma-separated, any case, with or without "
        "the emoji): `Flat White` matches ☕️ Flat White but not ☕️ Decaf Flat White. A keyword that is just "
        "an emoji, like `☕`, matches every tag starting with it, including future ones. A tag may be in "
        "several groups and then counts in each."
    )
    for g in list(draft):
        gid = g["_id"]
        c_name, c_tags, c_kw, c_del = st.columns([2, 4, 2, 0.6])
        g["name"] = c_name.text_input("Name", g["name"], key=f"tg_name_{gid}", placeholder="☕ Coffee").strip()
        g["tags"] = c_tags.multiselect("Tags", sorted(set(all_tags) | set(g["tags"])), default=g["tags"],
                                       key=f"tg_tags_{gid}")
        keywords = c_kw.text_input("Keywords", ", ".join(g["keywords"]), key=f"tg_kw_{gid}", placeholder="Flat White, Cappuccino")
        g["keywords"] = [k.strip() for k in keywords.split(",") if k.strip()]
        c_del.write("")
        if c_del.button("🗑️", key=f"tg_del_{gid}", help="Remove this group"):
            draft.remove(g)
            st.rerun()
        members = sorted(tag_group_members(g, all_tags))
        st.caption(f"Matches {len(members)} tag(s): {', '.join(members)}" if members else "Matches no tags yet.")

    b_add, b_suggest, b_names, b_save = st.columns(4)
    if b_add.button("➕ Add group", use_container_width=True):
        _add_to_draft({"name": "", "tags": [], "keywords": []})
        st.rerun()
    if b_suggest.button("💡 Suggest from emojis", use_container_width=True,
                        help="One group per emoji shared by two or more tags"):
        suggestions = suggest_tag_groups(all_tags, draft)
        for suggestion in suggestions:
            _add_to_draft(suggestion)
        if suggestions:
            st.rerun()
        st.info("No new suggestions: every shared emoji is already used as a keyword.")
    if b_names.button("🔤 Suggest from names", use_container_width=True,
                      help="One group per word or phrase shared by two or more tag names, e.g. Decaf or Flat White"):
        suggestions = suggest_tag_groups_by_name(all_tags, draft)
        for suggestion in suggestions:
            _add_to_draft(suggestion)
        if suggestions:
            st.rerun()
        st.info("No new suggestions: no other word or phrase is shared by two or more tag names.")
    if b_save.button("💾 Save groups", type="primary", use_container_width=True):
        problems, names = [], set()
        for i, g in enumerate(draft, start=1):
            if not g["name"]:
                problems.append(f"Group {i}: give it a name.")
            elif g["name"] in names:
                problems.append(f"Group {i}: the name '{g['name']}' is used twice.")
            names.add(g["name"])
            if not g["tags"] and not g["keywords"]:
                problems.append(f"Group {i}: pick at least one tag or enter a keyword.")
        if problems:
            for problem in problems:
                st.error(problem)
        else:
            save_tag_groups([{k: v for k, v in g.items() if k != "_id"} for g in draft])
            empty = [g["name"] for g in draft if not tag_group_members(g, all_tags)]
            st.session_state.setdefault("tags_flash", []).append(
                f"Saved {len(draft)} group(s)."
                + (f" Not matching any tag yet: {', '.join(empty)}." if empty else "")
            )
            st.rerun()
    saved_view = [{k: v for k, v in g.items() if k != "_id"} for g in draft]
    if saved_view != groups:
        st.caption("⚠️ Unsaved changes: the charts below use the saved groups.")

start_date, end_date = select_month_range(occ)  # summary events excluded
today = date.today()
raw_in_range = occ[(occ["date"] >= start_date) & (occ["date"] <= end_date)]
if raw_in_range.empty:
    st.info("No tags in the selected date range.")
    st.stop()

# --- View: single tags or groups ---
view_modes = {"grouped": "Groups + ungrouped tags", "groups": "Groups only", "tags": "Single tags"}
resolved = resolve_tag_groups(groups, all_tags)
if resolved:
    mode = st.radio("Show", list(view_modes), format_func=view_modes.get, horizontal=True)
else:
    mode = "tags"
view = apply_tag_groups(occ, groups, mode)
in_range = view[(view["date"] >= start_date) & (view["date"] <= end_date)]
if in_range.empty:
    st.info("None of the groups has tags in the selected date range.")
    st.stop()

summary = tag_summary(view, start_date, end_date, today)
if mode != "tags":
    def _members(label):
        members = resolved.get(label)
        if not members:
            return "—"
        return ", ".join(members[:4]) + (f" … ({len(members)})" if len(members) > 4 else "")
    summary.insert(1, "Members", summary["Tag"].map(_members))
by_count = summary["Tag"].tolist()

c1, c2 = st.columns([3, 1])
with c1:
    selected = st.multiselect("Tags in the charts" if mode == "tags" else "Groups / tags in the charts",
                              by_count, default=by_count[:8])
with c2:
    granularity = st.radio("Count per", ["Day", "Week", "Month"], index=1, horizontal=True)

# --- Summary table ---
st.subheader("Summary")
shown = summary.copy()
shown["Last seen"] = [f"{d:%Y-%m-%d}" if d is not None and pd.notna(d) else "—" for d in shown["Last seen"]]
st.dataframe(
    shown,
    use_container_width=True,
    hide_index=True,
    column_config={
        "% of days": st.column_config.ProgressColumn("% of days", min_value=0, max_value=100, format="%.1f%%"),
        "Days since last": st.column_config.NumberColumn(
            "Days since last", help="Days since the tag was last logged, up to today: the current streak without it"),
        "Longest gap (days)": st.column_config.NumberColumn(
            "Longest gap (days)", help="Longest run of days without the tag inside the selected range"),
    },
)
st.download_button("Download summary as CSV", shown.to_csv(index=False).encode("utf-8"),
                   f"tags_{start_date}_{end_date}.csv", "text/csv")
st.caption(f"{len(raw_in_range)} tags from {start_date} to {end_date}. A multi-day tag counts once, on its first day."
           + (" A tag in several groups counts in each group." if mode != "tags" and resolved else ""))

if not selected:
    st.info("Select tags above to see their charts.")
    st.stop()

sel = in_range[in_range["tag"].isin(selected)]
tag_scale = alt.Scale(domain=selected)

# --- Trend ---
st.subheader(f"Count per {granularity.lower()}")
period = granularity.lower()
counts = tag_counts_by_period(sel, period, start_date, end_date)
counts = counts.reindex(columns=[t for t in selected if t in counts.columns], fill_value=0)
trend = counts.reset_index().melt(id_vars="period_start", var_name="tag", value_name="count")
if period == "day":
    trend["label"] = pd.to_datetime(trend["period_start"])
    x = alt.X("label:T", title="Day")
else:
    trend["label"] = [period_label(period, d) for d in trend["period_start"]]
    x = alt.X("label:N", title=granularity, sort=None, axis=alt.Axis(labelAngle=-45))
st.altair_chart(
    alt.Chart(trend).mark_line(point=True).encode(
        x=x,
        y=alt.Y("count:Q", title="Count"),
        color=alt.Color("tag:N", title="Tag", scale=tag_scale),
        tooltip=[alt.Tooltip("label", title=granularity), "tag:N", "count:Q"],
    ).properties(height=320).interactive(),
    use_container_width=True,
)
with st.expander("📋 Show counts table"):
    table = counts.copy()
    table.index = [d.strftime("%Y-%m-%d") if period == "day" else period_label(period, d) for d in table.index]
    st.dataframe(table, use_container_width=True)

# --- Weekday pattern ---
weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
st.subheader("Weekday pattern")
by_weekday = sel.groupby(["tag", "weekday"]).size().reset_index(name="count")
base = alt.Chart(by_weekday).encode(
    x=alt.X("weekday:N", sort=weekdays, title=""),
    y=alt.Y("tag:N", sort=selected, title=""),
)
st.altair_chart(
    (base.mark_rect().encode(color=alt.Color("count:Q", title="Count", scale=alt.Scale(scheme="oranges")),
                             tooltip=["tag:N", "weekday:N", "count:Q"])
     + base.mark_text(fontSize=11).encode(text="count:Q"))
    .properties(height=max(120, 32 * len(selected))),
    use_container_width=True,
)

# --- Time of day ---
st.subheader("Time of day")
timed = sel[~sel["all_day"]].dropna(subset=["hour"]).copy()
if timed.empty:
    st.caption("None of the selected tags has a time of day (all-day tags only).")
else:
    timed["hour"] = timed["hour"].astype(int)
    by_hour = timed.groupby(["tag", "hour"]).size().reset_index(name="count")
    base = alt.Chart(by_hour).encode(
        x=alt.X("hour:O", title="Hour of day", scale=alt.Scale(domain=list(range(24)))),
        y=alt.Y("tag:N", sort=selected, title=""),
    )
    st.altair_chart(
        (base.mark_rect().encode(color=alt.Color("count:Q", title="Count", scale=alt.Scale(scheme="blues")),
                                 tooltip=["tag:N", "hour:O", "count:Q"])
         + base.mark_text(fontSize=11).encode(text="count:Q"))
        .properties(height=max(120, 32 * timed["tag"].nunique())),
        use_container_width=True,
    )
    all_day_tags = sorted(set(sel.loc[sel["all_day"], "tag"]))
    st.caption("All-day tags have no time of day and are left out here"
               + (f": {', '.join(all_day_tags)}." if all_day_tags else "."))
