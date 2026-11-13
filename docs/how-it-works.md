# 🔧 How it works

[← README](../README.md) · [Pages](pages/) · [Configuration](configuration.md)

## Loading and sync

- **Cache first**: every page loads instantly from the local cache in `data/` (one CSV per calendar URL), shared by all pages.
- **⚡ Quick Load / 🔄 Sync Calendars**: each page has both buttons. Quick Load reads the cache; Sync fetches the feeds. By default Sync refreshes **all** calendars, so every page is up to date afterwards. Settings → 💾 Cache & sync can limit it to the calendars the page shows (tag calendars on Tags, time calendars elsewhere); Home always syncs all.
- **Background refresh**: while the app runs, all calendars sync automatically every 24 hours (5 minutes before the cache expires).
- **What a sync changes**: events from the past 30 days and all future events are replaced with what the feed contains now, so events removed at the source disappear from the cache too. Events **older than 30 days** are kept, even if the source no longer has them, so your history survives calendars that only publish recent events.
- **Failed fetches**: a calendar that can't be fetched keeps its cached events. Every fetch is recorded on the [🩺 Calendar Health](pages/calendar-health.md) page, and failing calendars are shown in a banner on every page.
- **Duplicates**: events are identified by their `UID`; an event that appears again replaces the cached one.

## Recurring events and time zones

- **Recurring events** are expanded from their `RRULE` into individual occurrences up to about 400 days ahead, honouring `EXDATE` exclusions and moved or cancelled single occurrences (`RECURRENCE-ID`). A weekly meeting therefore counts every week, not once.
- **Time zones**: series are expanded in their own time zone, so a 09:00 meeting stays at 09:00 across daylight saving changes. Everything is shown in your local time.
- **All-day events** are anchored at midday UTC, so they stay on the right day in every time zone.
- **Encoding**: feeds served without a declared charset are read as UTF-8 (the iCalendar default), so emojis in event names show correctly.

## Data accuracy

A 3-day trip stored as one event counts as 72 hours, and the same meeting in two calendars counts twice. Both quietly inflate every chart.

- **⚖️ Data accuracy** (sidebar on Home, Compare Periods, Trends and AI Insights, and Settings → 📊 Data & display): count all-day and multi-day events fully, or exclude them. Events longer than a configurable number of hours (default 16) also count as long. The choice applies to every page.
- **Ignore events marked as free**: leaves out events whose availability is "Free" (iCal `TRANSP:TRANSPARENT`) on every page. Sync once after turning it on, so the cache knows which events are free.
- **🩺 Data Quality** on Home lists long events in the selected range, overlapping events grouped by calendar pair, and the total double-counted hours.
- **Oura summaries**: the all-day monthly summaries of [oura-sleep-ical](https://github.com/ramhee98/oura-sleep-ical) and the daily and monthly overviews of [oura-activity-ical](https://github.com/ramhee98/oura-activity-ical) are left out on every page (they stay in the cache), so they don't count as 24 hours of sleep or activity.
- **Range edges**: an event that starts before or ends after the selected date range only counts with its in-range part, on every page.

## Free time

[Goals](pages/goals.md) (suggested slots) and [Focus](pages/focus.md) need to know when you're free. That comes from Settings → 💡 Free slots ([details](pages/settings.md#-free-slots)):

- **Busy**: events of blocking calendars; per calendar, only events marked *busy* or also those marked *free*.
- **Off**: fixed blocks every week (e.g. a night 22:00–07:00 or Mon 08:00–10:00) and sleep, from a sleep calendar (optionally with a go_to_bed bedtime plan for the nights not recorded yet) or from "going to bed" / "waking up" events, with optional wind-down time before bed. Other nights without data use your usual sleep times of the last 4 weeks.
- **Free**: awake time that isn't busy.

## Privacy

- All calendar data, settings and caches stay in the project folder on your machine.
- The only external services are your calendar feeds, the GitHub version check (and `git pull` when you press Update in the sidebar), and OpenAI if you use AI Insights. AI Insights sends aggregated hours per calendar or category and period (including their names), not event titles.
- Calendar URLs are stored and shown shortened in the health data and the background-sync log, without their secret part. Error messages of a failed sync on a page still show the full URL.

## Updates

The sidebar shows the installed version (from `version.txt`) and the latest version on GitHub, and can pull the latest version with `git pull`.
