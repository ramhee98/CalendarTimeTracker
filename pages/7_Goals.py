import streamlit as st
import pandas as pd
import altair as alt
from utils import (
    load_events_for_page, build_events_df, render_accuracy_settings,
    load_goals, save_goals, render_goal_progress, goal_type_label, GOAL_TYPE_LABELS,
    load_tag_occurrences, load_tag_groups,
)
from analysis import (
    PERIODS, evaluate_goal, format_goal_amount, goal_target_text, is_tag_goal, period_label,
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
history = st.slider("History to evaluate (complete days/weeks/months)", 4, 90, 12)

with st.spinner("Evaluating goals..."):
    results = [(g, evaluate_goal(df, g, now, history, tags, tag_groups)) for g in goals]

st.subheader("This Period")
for goal, r in results:
    render_goal_progress(goal, r)

overview = pd.DataFrame([{
    "Goal": target_label(g["type"], g["name"]),
    "Target": f"{goal_target_text(g)} / {g['period']}",
    "Done": format_goal_amount(g, r["done"]),
    "Booked": format_goal_amount(g, r["booked"]),
    "Status": r["status_label"],
    "Streak": f"{r['streak']} {g['period']}{'s' if r['streak'] != 1 else ''}",
    "Hit rate": f"{r['met_count']}/{r['history_count']}" if r["history_count"] else "—",
} for g, r in results])
st.dataframe(overview, use_container_width=True, hide_index=True)
st.caption("Done = hours (or tags) already passed this period, Booked = done plus events still scheduled. "
           "Streak and hit rate count complete periods only. Tags are read from the local cache; "
           "🔄 Sync Calendars updates them too unless Sync is limited to the page's calendars.")

# --- History per goal ---
st.subheader("History")
for goal, r in results:
    period = r["period"]
    st.markdown(f"#### {target_label(goal['type'], goal['name'])} · {goal_target_text(goal)} per {period}")
    c1, c2, c3 = st.columns(3)
    c1.metric("Current streak", f"{r['streak']} {period}{'s' if r['streak'] != 1 else ''}")
    c2.metric("Hit rate", f"{r['met_count'] / r['history_count'] * 100:.0f}%" if r["history_count"] else "—",
              help=f"Complete {period}s that met the goal")
    c3.metric(f"Average per {period}",
              f"{r['history']['value'].mean():.1f}{'×' if is_tag_goal(goal) else 'h'}" if r["history_count"] else "—")

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
