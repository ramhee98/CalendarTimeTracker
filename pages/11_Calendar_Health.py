from datetime import datetime, timezone

import altair as alt
import pandas as pd
import streamlit as st
import tzlocal

from calendar_health import ERROR_LABELS, MAX_CHECKS, clear_health, load_health
from calendar_store import hash_url
from utils import calendar_health_rows, fetch_and_parse_ics, load_app_settings, run_parallel

st.set_page_config(page_title="Calendar Health", layout="wide")
st.title("🩺 Calendar Health")
st.caption(
    "Whether your calendar links still work. Every sync records a check: the 🔄 buttons on the pages and in "
    "Settings, and the automatic daily sync. A calendar that can't be fetched keeps its cached events."
)

for msg in st.session_state.pop("health_flash", []):
    st.success(msg)

LEVELS = {
    "ok": ("🟢", "OK"),
    "warning": ("🟡", "Warning"),
    "failing": ("🔴", "Failing"),
    "unknown": ("⚪", "Never checked"),
}
LOCAL_TZ = tzlocal.get_localzone()


def ago(ts, now):
    if ts is None:
        return "—"
    seconds = (now - ts).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min ago"
    if seconds < 172800:
        return f"{seconds / 3600:.0f} h ago"
    return f"{seconds / 86400:.0f} days ago"


def local(ts):
    return ts.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M") if ts else "—"


def check_calendars(calendars):
    """Fetch (and sync) `calendars`, recording a check for each."""
    progress = st.progress(0.0, text=f"Checking {len(calendars)} calendar(s)...")
    results = run_parallel(
        lambda c: fetch_and_parse_ics(c["url"], c["custom_name"], source="check"), calendars,
        on_done=lambda n: progress.progress(n / len(calendars), text=f"Checked {n} of {len(calendars)}..."),
    )
    # Failures are recorded in the health file with the reason
    failed = [c["custom_name"] for c, r in zip(calendars, results) if isinstance(r, Exception)]
    progress.empty()
    st.cache_data.clear()
    st.session_state.setdefault("health_flash", []).append(
        f"Checked {len(calendars)} calendar(s)." + (f" Failed: {', '.join(failed)}." if failed else " All answered.")
    )
    st.rerun()


settings = load_app_settings()
rows = calendar_health_rows(settings)
if not rows:
    st.info("No calendars configured yet. Add them on the Settings page.")
    st.stop()

now = datetime.now(timezone.utc)
calendars = [c for c, _ in rows]

# --- Summary ---
counts = {level: sum(1 for _, s in rows if s["level"] == level) for level in LEVELS}
cols = st.columns(4)
for col, (level, (icon, label)) in zip(cols, LEVELS.items()):
    col.metric(f"{icon} {label}", counts[level])

c1, c2, c3 = st.columns([1, 2, 1])
with c1:
    if st.button("🩺 Check all now", type="primary", use_container_width=True,
                 help="Fetches every calendar (this also syncs its events)"):
        check_calendars(calendars)
with c2:
    choice = st.selectbox("Calendar", range(len(rows)), format_func=lambda i: rows[i][0]["custom_name"],
                          label_visibility="collapsed")
with c3:
    if st.button("🩺 Check this calendar", use_container_width=True):
        check_calendars([calendars[choice]])

# --- Overview table ---
table = []
for c, s in rows:
    icon, label = LEVELS[s["level"]]
    last = s["last"] or {}
    table.append({
        "Status": f"{icon} {label}",
        "Calendar": c["custom_name"],
        "Type": c.get("type", "time"),
        "Last check": ago(s["last_check"], now),
        "Last good fetch": ago(s["last_good"], now),
        "Uptime": round(s["uptime"] * 100) if s["uptime"] is not None else None,
        "Fetch time (s)": round(last["ms"] / 1000, 2) if last.get("ms") is not None else None,
        "Events": last.get("events") if last.get("ok") else None,
        "Problems": " · ".join(s["problems"]) or "—",
    })
overview = pd.DataFrame(table)
overview["Events"] = overview["Events"].astype("Int64")
st.dataframe(
    overview,
    hide_index=True,
    use_container_width=True,
    column_config={
        "Uptime": st.column_config.ProgressColumn("Uptime", help="Successful fetches among the last 30 checks",
                                                  min_value=0, max_value=100, format="%d%%"),
        "Problems": st.column_config.TextColumn("Problems", width="large"),
    },
)
st.caption(
    f"🔴 Failing = {settings.get('health_fail_threshold', 2)} or more failed fetches in a row; a single failure is "
    "only a 🟡 warning. Thresholds and the banner on the other pages are set in Settings → 💾 Cache & sync."
)

# --- Details of one calendar ---
cal, status = rows[choice]
entry = load_health()["calendars"].get(hash_url(cal["url"]), {})
checks = entry.get("checks", [])
icon, label = LEVELS[status["level"]]
st.subheader(f"{icon} {cal['custom_name']}")
st.caption(f"URL (shortened, secret parts hidden): `{entry.get('url', '—')}`")

d1, d2, d3, d4 = st.columns(4)
d1.metric("Status", label)
d2.metric("Last good fetch", ago(status["last_good"], now), help=local(status["last_good"]))
d3.metric("Failed in a row", status["failures_in_row"],
          help=f"Failing since {local(status['failing_since'])}" if status["failing_since"] else None)
d4.metric("Uptime (last 30)", f"{status['uptime'] * 100:.0f}%" if status["uptime"] is not None else "—")
for problem in status["problems"]:
    (st.error if status["level"] == "failing" else st.warning)(problem)
if status["level"] == "ok":
    st.success("The feed answers, is a calendar and its content looks plausible.")

if checks:
    hist = pd.DataFrame(checks)
    hist["time"] = pd.to_datetime(hist["time"], utc=True).dt.tz_convert(LOCAL_TZ)
    hist["result"] = hist["ok"].map({True: "OK", False: "Failed"})
    hist["seconds"] = hist["ms"] / 1000
    for col in ("events", "error", "kind", "http_status"):
        if col not in hist:
            hist[col] = None
    color = alt.Color("result:N", title="", scale=alt.Scale(domain=["OK", "Failed"], range=["#2e7d32", "#c62828"]))
    tooltip = [alt.Tooltip("time:T", format="%Y-%m-%d %H:%M"), "result:N", "source:N",
               alt.Tooltip("seconds:Q", format=".2f"), "events:Q", "error:N"]
    g1, g2 = st.columns(2)
    with g1:
        st.markdown("**Fetch time (s)**")
        st.altair_chart(alt.Chart(hist).mark_line(point=True, color="#888").encode(
            x=alt.X("time:T", title=""), y=alt.Y("seconds:Q", title="Seconds"), color=color, tooltip=tooltip,
        ).properties(height=220), use_container_width=True)
    with g2:
        st.markdown("**Events in the feed**")
        ok_hist = hist[hist["ok"]]
        st.altair_chart(alt.Chart(ok_hist).mark_line(point=True).encode(
            x=alt.X("time:T", title=""), y=alt.Y("events:Q", title="Events"), tooltip=tooltip,
        ).properties(height=220), use_container_width=True)

    recent = hist.sort_values("time", ascending=False).head(25)
    st.markdown("**Recent checks**")
    st.dataframe(pd.DataFrame({
        "Time": recent["time"].dt.strftime("%Y-%m-%d %H:%M"),
        "Result": [("✅ OK" if ok else f"❌ {ERROR_LABELS.get(k, 'Error')}") for ok, k in zip(recent["ok"], recent["kind"])],
        "Via": recent["source"].map({"sync": "Sync", "background": "Daily sync", "check": "Health check"}),
        "HTTP": recent["http_status"].map(lambda v: "—" if pd.isna(v) else int(v)),
        "Seconds": recent["seconds"].round(2),
        "Events": recent["events"].map(lambda v: "—" if pd.isna(v) else int(v)),
        "Error": recent["error"].fillna(""),
    }), hide_index=True, use_container_width=True)
    st.caption(f"The last {MAX_CHECKS} checks per calendar are kept in `sync_health.json`.")
else:
    st.info("No checks yet for this calendar. Press 🩺 Check this calendar.")

with st.expander("🗑️ Clear the health history"):
    st.caption("Deletes all recorded checks of all calendars. The cached events are not affected.")
    if st.checkbox("Yes, delete the health history") and st.button("Delete history"):
        clear_health()
        st.session_state.setdefault("health_flash", []).append("Deleted the health history.")
        st.rerun()
