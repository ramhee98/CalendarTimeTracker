import streamlit as st
import pandas as pd
import altair as alt
from utils import (
    load_events_for_page, build_events_df, render_accuracy_settings,
    load_goals, save_goals, render_goal_progress, goal_type_label, GOAL_TYPE_LABELS,
    load_tag_occurrences, load_tag_groups, load_app_settings, slot_blocker, evaluate_goals, goal_estimate_kind,
)
from analysis import (
    PERIODS, format_goal_amount, goal_target_text, is_tag_goal, period_label, slots_to_ics, suggest_goal_slots,
)

st.set_page_config(page_title="Goals", layout="wide")
st.title("🎯 Goals")
st.caption(
    "Set daily, weekly or monthly targets and see whether you hit them: hours per calendar or "
    "category, or how often a tag or tag group (from the Tags page) occurs. Weeks run Monday–Sunday."
)

all_events, source_type = load_events_for_page("goals")
accuracy = render_accuracy_settings("goals")
with st.spinner("Processing calendar data..."):
    df = build_events_df(all_events, accuracy)

goals = load_goals()
tags = load_tag_occurrences()
tag_groups = load_tag_groups()
calendars = sorted(df["calendar"].dropna().unique().tolist())
categories = sorted(df["category"].dropna().unique().tolist()) if source_type == "json" else []
tag_names = sorted(tags["tag"].unique().tolist())
group_names = [g["name"] for g in tag_groups]
known_names = {"calendar": calendars, "category": categories, "tag": tag_names, "tag_group": group_names}


# Calendars, categories, tags and groups can share a name, so the picker
# shows the type with every entry and the type is derived from the choice.
def target_label(goal_type, name):
    return f"{goal_type_label(goal_type)} · {name}"


target_of = {}
for goal_type, names in known_names.items():
    target_of.update({target_label(goal_type, n): (goal_type, n) for n in names})
# Keep saved goals whose calendar/category no longer exists visible, so
# saving reports them instead of silently dropping them.
for g in goals:
    target_of.setdefault(target_label(g["type"], g["name"]), (g["type"], g["name"]))

# --- Edit goals ---
with st.expander("✏️ Edit goals", expanded=not goals):
    st.caption(
        "**Min** = at least, **Max** = at most: hours for 📅 calendars and 🗂️ categories, number of "
        "times for 🏷️ tags and 🔖 tag groups (e.g. ☕ Coffee · day · Max 2). Fill one or both. Add rows "
        "at the bottom, delete by selecting a row and pressing Delete. Tag groups are edited on the Tags page."
    )
    editor_df = pd.DataFrame(
        [{"target": target_label(g["type"], g["name"]), **{k: g[k] for k in ("period", "min", "max")}} for g in goals],
        columns=["target", "period", "min", "max"],
    ).astype({"target": "object", "period": "object", "min": "float", "max": "float"})
    edited = st.data_editor(
        editor_df,
        num_rows="dynamic",
        hide_index=True,
        use_container_width=True,
        key="goals_editor",
        column_config={
            "target": st.column_config.SelectboxColumn(
                "Target", options=list(target_of), required=True, width="large",
                help="📅 = a single calendar, 🗂️ = a category (all calendars with that category), "
                     "🏷️ = a tag, 🔖 = a tag group from the Tags page",
            ),
            "period": st.column_config.SelectboxColumn("Per", options=list(PERIODS), required=True, default="week"),
            "min": st.column_config.NumberColumn("Min", min_value=0.0, step=0.5,
                                                 help="Hours, or number of times for tags"),
            "max": st.column_config.NumberColumn("Max", min_value=0.0, step=0.5,
                                                 help="Hours, or number of times for tags"),
        },
    )

    if st.button("💾 Save goals"):
        new_goals, problems = [], []
        for i, row in edited.reset_index(drop=True).iterrows():
            if pd.isna(row["target"]) or row["target"] not in target_of:
                continue
            goal_type, name = target_of[row["target"]]
            goal = {
                "type": goal_type,
                "name": name,
                "period": row["period"] if isinstance(row["period"], str) else "week",
                "min": None if pd.isna(row["min"]) else float(row["min"]),
                "max": None if pd.isna(row["max"]) else float(row["max"]),
            }
            if goal["name"] not in known_names[goal["type"]]:
                problems.append(f"Row {i + 1}: no {GOAL_TYPE_LABELS[goal['type']][1].lower()} "
                                f"named '{goal['name']}'.")
            elif goal["min"] is None and goal["max"] is None:
                problems.append(f"Row {i + 1}: set a min, a max or both.")
            elif goal["min"] is not None and goal["max"] is not None and goal["min"] > goal["max"]:
                problems.append(f"Row {i + 1}: min is larger than max.")
            else:
                new_goals.append(goal)
        if problems:
            for p in problems:
                st.error(p)
        else:
            save_goals(new_goals)
            st.success(f"Saved {len(new_goals)} goal(s).")
            st.rerun()

if not goals:
    st.info("No goals yet. Add one above, e.g. Sport · week · Min 4 or ☕ Coffee · day · Max 2.")
    st.stop()

# --- Overview ---
now = pd.Timestamp.now(tz=df["start"].dt.tz)
app_settings = load_app_settings()
history_weeks = st.slider(
    "History to evaluate (weeks)", 4, 104, int(app_settings.get("goals_history_weeks", 12) or 12),
    help="The same span for every goal: 12 weeks = 84 days for daily goals, 12 weeks for weekly goals and "
         "3 months for monthly goals. Only complete days, weeks and months count. The default is set in "
         "Settings → 📊 Data & display.",
)

with st.spinner("Evaluating goals..."):
    results = evaluate_goals(df, goals, now, tags=tags, groups=tag_groups, history_weeks=history_weeks)

st.subheader("This Period")
for goal, r in results:
    render_goal_progress(goal, r)
for goal, r in results:
    if r.get("estimate_note"):
        st.caption(r["estimate_note"])

overview = pd.DataFrame([{
    "Goal": target_label(g["type"], g["name"]),
    "Target": f"{goal_target_text(g)} / {g['period']}",
    "Done": format_goal_amount(g, r["done"]),
    "Booked": (f"~{format_goal_amount(g, r['expected'])} expected" if "expected" in r
               else format_goal_amount(g, r["booked"])),
    "Status": r["status_label"],
    "Streak": f"{r['streak']} {g['period']}{'s' if r['streak'] != 1 else ''}",
    "Hit rate": f"{r['met_count']}/{r['history_count']}" if r["history_count"] else "—",
} for g, r in results])
st.dataframe(overview, use_container_width=True, hide_index=True)
st.caption("Done = hours (or tags) already passed this period, Booked = done plus events still scheduled; "
           "for sleep and Oura activity goals, plus an estimate from the last 4 weeks of what isn't recorded yet. "
           "Streak and hit rate count complete periods only. Tags are read from the local cache; "
           "🔄 Sync Calendars updates them too unless Sync is limited to the page's calendars.")

# --- Free-slot suggestions ---
st.subheader("💡 Suggested slots")
# Sleep and Oura activity are recorded after the fact, not planned in free slots.
time_min_goals = [g for g, r in results if not is_tag_goal(g) and g["min"] is not None and "expected" not in r
                  and goal_estimate_kind(df, g, app_settings) is None]
if not time_min_goals:
    st.caption("Suggestions are made for calendar and category goals with a Min "
               "(not for sleep or Oura activity, which are recorded after the fact).")
else:
    plan = st.radio("Plan for", ["current", "next"], horizontal=True, key="slots_plan",
                    format_func={"current": "The rest of this day / week / month",
                                 "next": "The next day / week / month"}.get)
    slot_settings = app_settings
    # Blocking uses every event (long and free ones too): the free-slot
    # settings decide what blocks, not the data-accuracy rules.
    raw_df = build_events_df(all_events, {"long_events": "include", "ignore_free_events": False})
    with st.spinner("Looking for free time..."):
        blocked_for, nights_for = slot_blocker(slot_settings, now, time_events=raw_df)
        plans = suggest_goal_slots(
            df, time_min_goals, now, blocked_for, plan=plan,
            min_minutes=slot_settings["slots_min_minutes"],
            buffer_minutes=slot_settings["slots_buffer_minutes"],
            max_slots=slot_settings["slots_max_per_goal"],
        )
    if slot_settings["slots_sleep_mode"] != "none" and not nights_for(now, now + pd.Timedelta(days=7)):
        st.warning("No sleep found for the coming nights, so night-time counts as free. Check the sleep "
                   "settings in Settings → 💡 Free slots (calendar, event names, usual sleep times) and sync.")
    if not plans:
        st.success("Every goal with a Min is reached or covered by booked events, nothing to plan.")
    all_items = []
    for p in plans:
        goal = p["goal"]
        name = goal["name"]
        st.markdown(f"**{target_label(goal['type'], name)}** · {p['need_hours']:.1f}h to go "
                    f"({p['booked']:.1f}h of {goal_target_text(goal)} booked "
                    f"{'for ' + period_label(goal['period'], p['period_start']) if plan == 'next' else 'so far'})")
        if p["slots"]:
            st.dataframe(pd.DataFrame([{
                "Day": f"{s['start']:%a %d.%m.}",
                "Time": f"{s['start']:%H:%M}–{s['end']:%H:%M}",
                "Length": f"{s['minutes'] / 60:g}h",
                "Why": s["reason"],
            } for s in p["slots"]]), hide_index=True, use_container_width=True)
            items = [(name, s) for s in p["slots"]]
            all_items += items
            st.download_button(f"📥 Add to calendar (.ics) · {name}", slots_to_ics(items, now).encode("utf-8"),
                               f"slots_{name}.ics".replace(" ", "_"), "text/calendar", key=f"ics_{goal['type']}_{name}")
        if p["missing_hours"] > 1e-6:
            st.warning(f"Not enough free time found for {p['missing_hours']:.1f}h"
                       + (f" (only {p['free_hours']:.1f}h free in total)." if p["free_hours"] < p["need_hours"]
                          else f" within {slot_settings['slots_max_per_goal']} suggestion(s): raise "
                               "Suggestions per goal in Settings → 💡 Free slots for more."))
    if len(plans) > 1 and all_items:
        st.download_button("📥 Add all suggestions to calendar (.ics)", slots_to_ics(all_items, now).encode("utf-8"),
                           "slots_all.ics", "text/calendar", key="ics_all")
    st.caption("Free = not blocked by events, the weekly blocked times or sleep, with a buffer around events. "
               "Slots prefer the days and hours you usually work on a goal and are spread over different days. "
               "Change what blocks time in Settings → 💡 Free slots. The .ics files import into most calendar apps.")

# --- History per goal ---
st.subheader("History")
for goal, r in results:
    period = r["period"]
    st.markdown(f"#### {target_label(goal['type'], goal['name'])} · {goal_target_text(goal)} per {period}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Current streak", f"{r['streak']} {period}{'s' if r['streak'] != 1 else ''}")
    c2.metric("Hit rate", f"{r['met_count'] / r['history_count'] * 100:.0f}%" if r["history_count"] else "—",
              help=f"Complete {period}s that met the goal")
    unit_suffix = "×" if is_tag_goal(goal) else "h"
    values = r["history"]["value"]
    c3.metric(f"Average per {period}", f"{values.mean():.1f}{unit_suffix}" if r["history_count"] else "—",
              help=f"Mean of the complete {period}s evaluated")
    c4.metric(f"Median per {period}", f"{values.median():.1f}{unit_suffix}" if r["history_count"] else "—",
              help=f"The middle {period}: half were higher, half lower. Unlike the average, one unusual "
                   f"{period} barely moves it.")

    unit = "Times" if is_tag_goal(goal) else "Hours"
    hist = r["history"].reset_index()
    hist["state"] = hist["met"].map({True: "Met", False: "Missed"})
    current = pd.DataFrame([{"period_start": r["period_start"], "value": r["booked"], "state": "Current (booked)"}])
    chart_df = pd.concat([hist[["period_start", "value", "state"]], current], ignore_index=True)
    chart_df["label"] = [period_label(period, d) for d in chart_df["period_start"]]

    bars = alt.Chart(chart_df).mark_bar().encode(
        x=alt.X("label:N", title=period.title(), sort=None, axis=alt.Axis(labelAngle=-45)),
        y=alt.Y("value:Q", title=unit),
        color=alt.Color(
            "state:N",
            title="",
            scale=alt.Scale(domain=["Met", "Missed", "Current (booked)"], range=["#2e7d32", "#c62828", "#9e9e9e"]),
        ),
        tooltip=[alt.Tooltip("label:N", title=period.title()), alt.Tooltip("value:Q", title=unit, format=".1f"),
                 "state:N"],
    )
    layers = [bars]
    for key, name in (("min", "Min"), ("max", "Max")):
        if goal[key] is not None:
            layers.append(
                alt.Chart(pd.DataFrame({"y": [goal[key]], "target": [name]}))
                .mark_rule(strokeDash=[4, 4], color="#555")
                .encode(y="y:Q", tooltip=["target:N", alt.Tooltip("y:Q", title=unit)])
            )
    st.altair_chart(alt.layer(*layers).properties(height=260), use_container_width=True)
