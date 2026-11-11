import streamlit as st
import pandas as pd
import altair as alt
from utils import (
    load_events_for_page, build_events_df, render_accuracy_settings,
    load_goals, save_goals, render_goal_progress,
)
from analysis import evaluate_goal, goal_target_text, period_label

st.set_page_config(page_title="Goals", layout="wide")
st.title("🎯 Goals")
st.caption(
    "Set weekly or monthly time targets per calendar or category and see whether you hit them. "
    "Weeks run Monday–Sunday."
)

all_events, source_type = load_events_for_page("goals")
accuracy = render_accuracy_settings("goals")
with st.spinner("Processing calendar data..."):
    df = build_events_df(all_events, accuracy)

goals = load_goals()
calendars = sorted(df["calendar"].dropna().unique().tolist())
categories = sorted(df["category"].dropna().unique().tolist()) if source_type == "json" else []
type_options = ["calendar", "category"] if source_type == "json" else ["calendar"]

# --- Edit goals ---
with st.expander("✏️ Edit goals", expanded=not goals):
    st.caption(
        "**Min** = spend at least this many hours, **Max** = spend at most. "
        "Fill one or both. Add rows at the bottom, delete by selecting a row and pressing Delete."
    )
    editor_df = pd.DataFrame(goals, columns=["type", "name", "period", "min", "max"]).astype(
        {"type": "object", "name": "object", "period": "object", "min": "float", "max": "float"}
    )
    edited = st.data_editor(
        editor_df,
        num_rows="dynamic",
        hide_index=True,
        use_container_width=True,
        key="goals_editor",
        column_config={
            "type": st.column_config.SelectboxColumn("Type", options=type_options, required=True, default="calendar"),
            "name": st.column_config.SelectboxColumn(
                "Calendar / Category", options=sorted(set(calendars) | set(categories)), required=True
            ),
            "period": st.column_config.SelectboxColumn("Per", options=["week", "month"], required=True, default="week"),
            "min": st.column_config.NumberColumn("Min (h)", min_value=0.0, step=0.5),
            "max": st.column_config.NumberColumn("Max (h)", min_value=0.0, step=0.5),
        },
    )

    if st.button("💾 Save goals"):
        new_goals, problems = [], []
        for i, row in edited.reset_index(drop=True).iterrows():
            if pd.isna(row["name"]) or not str(row["name"]).strip():
                continue
            goal = {
                "type": row["type"] if isinstance(row["type"], str) else "calendar",
                "name": str(row["name"]).strip(),
                "period": row["period"] if isinstance(row["period"], str) else "week",
                "min": None if pd.isna(row["min"]) else float(row["min"]),
                "max": None if pd.isna(row["max"]) else float(row["max"]),
            }
            known = calendars if goal["type"] == "calendar" else categories
            if goal["name"] not in known:
                problems.append(f"Row {i + 1}: no {goal['type']} named '{goal['name']}'.")
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
    st.info("No goals yet. Add one above, e.g. Sport · week · Min 4.")
    st.stop()

# --- Overview ---
now = pd.Timestamp.now(tz=df["start"].dt.tz)
history = st.slider("History to evaluate (complete weeks/months)", 4, 52, 12)

with st.spinner("Evaluating goals..."):
    results = [(g, evaluate_goal(df, g, now, history)) for g in goals]

st.subheader("This Period")
for goal, r in results:
    render_goal_progress(goal, r)

overview = pd.DataFrame([{
    "Goal": f"{g['name']} ({g['type']})",
    "Target": f"{goal_target_text(g)} / {g['period']}",
    "Done": round(r["done"], 1),
    "Booked": round(r["booked"], 1),
    "Status": r["status_label"],
    "Streak": f"{r['streak']} {g['period']}{'s' if r['streak'] != 1 else ''}",
    "Hit rate": f"{r['met_count']}/{r['history_count']}" if r["history_count"] else "—",
} for g, r in results])
st.dataframe(overview, use_container_width=True, hide_index=True)
st.caption("Done = hours already passed this period, Booked = done plus events still scheduled. "
           "Streak and hit rate count complete periods only.")

# --- History per goal ---
st.subheader("History")
for goal, r in results:
    period = r["period"]
    st.markdown(f"#### {goal['name']} · {goal_target_text(goal)} per {period}")
    c1, c2, c3 = st.columns(3)
    c1.metric("Current streak", f"{r['streak']} {period}{'s' if r['streak'] != 1 else ''}")
    c2.metric("Hit rate", f"{r['met_count'] / r['history_count'] * 100:.0f}%" if r["history_count"] else "—",
              help=f"Complete {period}s that met the goal")
    c3.metric(f"Average per {period}",
              f"{r['history']['hours'].mean():.1f}h" if r["history_count"] else "—")

    hist = r["history"].reset_index()
    hist["state"] = hist["met"].map({True: "Met", False: "Missed"})
    current = pd.DataFrame([{"period_start": r["period_start"], "hours": r["booked"], "state": "Current (booked)"}])
    chart_df = pd.concat([hist[["period_start", "hours", "state"]], current], ignore_index=True)
    chart_df["label"] = [period_label(period, d) for d in chart_df["period_start"]]

    bars = alt.Chart(chart_df).mark_bar().encode(
        x=alt.X("label:N", title=period.title(), sort=None, axis=alt.Axis(labelAngle=-45)),
        y=alt.Y("hours:Q", title="Hours"),
        color=alt.Color(
            "state:N",
            title="",
            scale=alt.Scale(domain=["Met", "Missed", "Current (booked)"], range=["#2e7d32", "#c62828", "#9e9e9e"]),
        ),
        tooltip=[alt.Tooltip("label:N", title=period.title()), alt.Tooltip("hours:Q", format=".1f"), "state:N"],
    )
    layers = [bars]
    for key, name in (("min", "Min"), ("max", "Max")):
        if goal[key] is not None:
            layers.append(
                alt.Chart(pd.DataFrame({"y": [goal[key]], "target": [name]}))
                .mark_rule(strokeDash=[4, 4], color="#555")
                .encode(y="y:Q", tooltip=["target:N", alt.Tooltip("y:Q", title="Hours")])
            )
    st.altair_chart(alt.layer(*layers).properties(height=260), use_container_width=True)
