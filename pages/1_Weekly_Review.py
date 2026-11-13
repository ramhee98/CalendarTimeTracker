import streamlit as st
import pandas as pd
import altair as alt
from datetime import date, timedelta
from utils import (
    load_events_for_page, build_events_df, render_accuracy_settings,
    select_group_mode, load_goals, load_tag_occurrences, load_tag_groups,
    load_app_settings, goal_estimate_kind, goal_frame, goal_estimate,
)
from analysis import (
    clip_between, local_midnight, period_start, period_label, typical_week,
    hours_by_period, goal_events, goal_status, goal_target_text, long_event_mask,
    goal_amount, format_goal_amount, is_tag_goal, estimated_goal_status,
)

st.set_page_config(page_title="Weekly Review", layout="wide")
st.title("🗓️ Weekly Review")
st.caption("One screen to review a week: how it compares to your usual weeks, your goals, and what's booked next.")

all_events, source_type = load_events_for_page("review")
accuracy = render_accuracy_settings("review")
group_mode = select_group_mode(source_type, "review_view_mode")
group_label = group_mode.title()

with st.spinner("Processing calendar data..."):
    df = build_events_df(all_events, accuracy)
    df["group"] = df[group_mode]

tz = df["start"].dt.tz
now = pd.Timestamp.now(tz=tz)
this_week = period_start("week", now.date())


def week_title(ws):
    we = ws + timedelta(days=6)
    return f"{period_label('week', ws)} ({ws:%d %b} – {we:%d %b %Y})"


# --- Week picker (default: last complete week) ---
first_week = period_start("week", df["start"].min().date())
weeks = []
ws = this_week
while ws >= first_week:
    weeks.append(ws)
    ws -= timedelta(days=7)

c1, c2 = st.columns([2, 1])
with c1:
    week = st.selectbox(
        "Week to review",
        weeks,
        index=1 if len(weeks) > 1 else 0,
        format_func=lambda w: week_title(w) + (" — in progress" if w == this_week else ""),
    )
with c2:
    baseline_weeks = st.selectbox("Compare with the average of the previous", [4, 8, 12], index=0,
                                  format_func=lambda n: f"{n} weeks")

lo, hi = local_midnight(week, tz), local_midnight(week + timedelta(days=7), tz)
week_df = clip_between(df, lo, hi)
per_group = hours_by_period(df, "week", week, week, "group")
per_group = per_group.iloc[0] if not per_group.empty else pd.Series(dtype=float)
typical, typical_total, n_weeks = typical_week(df, "group", week, baseline_weeks)
total = float(per_group.sum())

if week == this_week:
    st.info("This week isn't over yet: totals include events still scheduled for the rest of the week.")

# --- Headline metrics ---
timed = week_df[~long_event_mask(week_df, accuracy.get("long_event_hours", 16))]
daily = pd.Series(
    {week + timedelta(days=i): clip_between(df, local_midnight(week + timedelta(days=i), tz),
                                            local_midnight(week + timedelta(days=i + 1), tz))["duration_hours"].sum()
     for i in range(7)}
)
busiest_day = daily.idxmax() if daily.max() > 0 else None

m1, m2, m3, m4 = st.columns(4)
m1.metric(
    "Total hours", f"{total:.1f}h",
    delta=f"{total - typical_total:+.1f}h vs {n_weeks}-week avg" if n_weeks else None,
    delta_color="off",
)
m2.metric("Events", len(week_df))
m3.metric("Busiest day", f"{busiest_day:%a %d %b}" if busiest_day else "—",
          delta=f"{daily.max():.1f}h" if busiest_day else None, delta_color="off")
if not timed.empty:
    ends_tod = timed["end"].dt.hour * 60 + timed["end"].dt.minute
    # An event ending after midnight belongs to the evening before
    ends_tod = ends_tod.where(timed["end"].dt.date == timed["start"].dt.date, ends_tod + 24 * 60)
    latest_idx = ends_tod.idxmax()
    latest_event = timed.loc[latest_idx]
    late_days = timed[ends_tod >= 21 * 60]["start"].dt.date.nunique()
    m4.metric("Latest finish", f"{latest_event['start']:%a} {latest_event['end']:%H:%M}",
              delta=f"{late_days} evening(s) past 21:00", delta_color="off")
else:
    late_days = 0
    latest_event = None
    m4.metric("Latest finish", "—")

# --- Hours by group vs typical ---
st.subheader(f"Hours by {group_label}")
groups = sorted(set(per_group.index) | set(typical.index))
compare = pd.DataFrame({
    group_label: groups,
    "This week": [float(per_group.get(g, 0.0)) for g in groups],
    f"Avg ({n_weeks} wk)": [float(typical.get(g, 0.0)) for g in groups],
})
avg_col = f"Avg ({n_weeks} wk)"
compare["Δ h"] = compare["This week"] - compare[avg_col]
compare["Δ %"] = [
    f"{d / a * 100:+.0f}%" if a > 0 else ("new" if t > 0 else "—")
    for d, a, t in zip(compare["Δ h"], compare[avg_col], compare["This week"])
]
compare = compare[(compare["This week"] > 0) | (compare[avg_col] > 0)]
compare = compare.sort_values("This week", ascending=False)

if compare.empty:
    st.info("No events in this week or the weeks before it.")
    st.stop()

color_mapping = df.groupby("group")["color"].first().to_dict()
chart_df = compare.melt(id_vars=group_label, value_vars=["This week", avg_col], var_name="series", value_name="hours")
st.altair_chart(
    alt.Chart(chart_df).mark_bar().encode(
        x=alt.X(f"{group_label}:N", sort=compare[group_label].tolist(), axis=alt.Axis(labelAngle=-45), title=""),
        y=alt.Y("hours:Q", title="Hours"),
        xOffset=alt.XOffset("series:N", sort=["This week", avg_col]),
        color=alt.Color("series:N", title="", scale=alt.Scale(domain=["This week", avg_col], range=["#1f77b4", "#bbbbbb"])),
        tooltip=[f"{group_label}:N", "series:N", alt.Tooltip("hours:Q", format=".1f")],
    ).properties(height=320),
    use_container_width=True,
)
shown = compare.copy()
for col in ["This week", avg_col, "Δ h"]:
    shown[col] = shown[col].round(1)
st.dataframe(shown, use_container_width=True, hide_index=True)

# --- Biggest changes ---
changes = compare[compare["Δ h"].abs() >= 1].sort_values("Δ h", key=abs, ascending=False).head(4)
change_lines = [
    f"{'📈' if row['Δ h'] > 0 else '📉'} **{row[group_label]}** {row['Δ h']:+.1f}h "
    f"({row['This week']:.1f}h vs {row[avg_col]:.1f}h usually)"
    for _, row in changes.iterrows()
]
if change_lines and n_weeks:
    st.subheader("Biggest Changes")
    for line in change_lines:
        st.markdown(f"- {line}")

# --- Goals ---
weekly_goals = [g for g in load_goals() if g["period"] == "week"]
goal_lines = []
if weekly_goals:
    st.subheader("🎯 Weekly Goals")
    has_tag_goals = any(is_tag_goal(g) for g in weekly_goals)
    tags = load_tag_occurrences() if has_tag_goals else None
    tag_groups = load_tag_groups() if has_tag_goals else []
    rows = []
    week_end = week + timedelta(days=7)
    settings = load_app_settings()
    estimate_notes = []
    for goal in weekly_goals:
        # Same as Home and the Goals page: sleep and Oura activity goals add
        # an estimate of what isn't recorded yet.
        kind = goal_estimate_kind(df, goal, settings)
        frame = goal_frame(df, goal, kind, settings)
        sub = goal_events(frame, goal, tags, tag_groups)
        booked = goal_amount(goal, sub, week, week_end, tz=tz)
        done = goal_amount(goal, sub, week, week_end, now=now, tz=tz)
        _, label = goal_status(goal, done, booked)
        amount = format_goal_amount(goal, booked)
        est = goal_estimate(frame, goal, kind, week, week_end, now, settings) if kind else None
        if est is not None and est["hours"] > 0:
            _, label = estimated_goal_status(goal, done, booked + est["hours"])
            amount = f"~{format_goal_amount(goal, booked + est['hours'])} expected"
            estimate_notes.append(est["note"])
        measure = ", actual sleep" if kind == "sleep" and settings.get("goals_sleep_measure") == "asleep" else ""
        rows.append({"Goal": f"{goal['name']} ({goal['type'].replace('_', ' ')}{measure})", "Target": goal_target_text(goal),
                     "Amount": amount, "Status": label})
        goal_lines.append(f"{goal['name']} {goal_target_text(goal)}: {amount} — {label}")
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    for note in estimate_notes:
        if note:
            st.caption(note)
    st.caption("Daily and monthly goals and goal history are on the Goals page.")

# --- Day by day ---
st.subheader("Day by Day")
day_rows = []
for i in range(7):
    d = week + timedelta(days=i)
    part = clip_between(week_df, local_midnight(d, tz), local_midnight(d + timedelta(days=1), tz))
    for g, h in part.groupby("group")["duration_hours"].sum().items():
        if h > 0:
            day_rows.append({"day": f"{d:%a %d}", "group": g, "hours": h})
if day_rows:
    st.altair_chart(
        alt.Chart(pd.DataFrame(day_rows)).mark_bar().encode(
            x=alt.X("day:N", sort=[f"{week + timedelta(days=i):%a %d}" for i in range(7)], title="",
                    axis=alt.Axis(labelAngle=0)),
            y=alt.Y("hours:Q", title="Hours"),
            color=alt.Color("group:N", title=group_label,
                            scale=alt.Scale(domain=list(color_mapping.keys()), range=list(color_mapping.values()))),
            tooltip=["day:N", alt.Tooltip("group:N", title=group_label), alt.Tooltip("hours:Q", format=".1f")],
        ).properties(height=280),
        use_container_width=True,
    )

# --- Next week ---
next_week = week + timedelta(days=7)
next_hours = hours_by_period(df, "week", next_week, next_week, "group")
next_hours = next_hours.iloc[0] if not next_hours.empty else pd.Series(dtype=float)
next_total = float(next_hours.sum())
st.subheader(f"Next Week · {week_title(next_week)}")
n1, n2 = st.columns([1, 2])
n1.metric(
    "Booked so far", f"{next_total:.1f}h",
    delta=f"{next_total / typical_total * 100:.0f}% of a typical week" if typical_total else None,
    delta_color="off",
)
if next_total > 0:
    top = next_hours[next_hours > 0].sort_values(ascending=False).round(1)
    n2.dataframe(top.rename("Hours").rename_axis(group_label).reset_index(), use_container_width=True, hide_index=True)
else:
    n2.caption("Nothing booked yet.")

# --- Markdown export (aggregates only, no event titles) ---
md = [f"# Weekly Review {week_title(week)}", ""]
md.append(f"**Total:** {total:.1f}h" + (f" (avg {typical_total:.1f}h over {n_weeks} weeks, {total - typical_total:+.1f}h)" if n_weeks else ""))
md.append(f"**Events:** {len(week_df)}")
if busiest_day:
    md.append(f"**Busiest day:** {busiest_day:%A %d %b} ({daily.max():.1f}h)")
if latest_event is not None:
    md.append(f"**Latest finish:** {latest_event['start']:%A} {latest_event['end']:%H:%M} · {late_days} evening(s) past 21:00")
md += ["", f"## Hours by {group_label}", "", f"| {group_label} | This week | {avg_col} | Δ h | Δ % |", "|---|---:|---:|---:|---:|"]
for _, row in shown.iterrows():
    md.append(f"| {row[group_label]} | {row['This week']:.1f} | {row[avg_col]:.1f} | {row['Δ h']:+.1f} | {row['Δ %']} |")
if change_lines and n_weeks:
    md += ["", "## Biggest changes", ""] + [f"- {line.replace('**', '')}" for line in change_lines]
if goal_lines:
    md += ["", "## Weekly goals", ""] + [f"- {line}" for line in goal_lines]
md += ["", "## Next week", "", f"Booked so far: {next_total:.1f}h" + (f" (typical {typical_total:.1f}h)" if typical_total else "")]

st.download_button(
    "⬇️ Download review as Markdown",
    "\n".join(md) + "\n",
    file_name=f"weekly_review_{period_label('week', week)}.md",
    mime="text/markdown",
)
