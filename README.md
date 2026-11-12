# CalendarTimeTracker

**CalendarTimeTracker** is a Streamlit (Python) app that shows how you spend your time, based on your calendars' `.ics` feeds. It looks at **event durations** rather than content, by **calendar** or by **category**, and adds goals, focus analysis, habit tags from tag calendars and a health check for your calendar links.

All data stays on your machine. The only optional external service is OpenAI, used by the AI Insights page.

![Home dashboard](img/home/home-overview.png)

## 🚀 Features

**Overview**
- 🏠 [Home](docs/pages/home.md): goal progress, upcoming weeks, summary table, time per day/week/month, activity heatmap and time distribution

**Review & plan**
- 🗓️ [Weekly Review](docs/pages/weekly-review.md): a week compared with your usual weeks, with goals, next week's load and Markdown export
- 🎯 [Goals](docs/pages/goals.md): daily, weekly or monthly targets per calendar, category, tag or tag group, with streaks, hit rate and **suggested free slots**
- 🧩 [Focus](docs/pages/focus.md): focus time, fragmentation, short gaps and calendar switches per day, with a week timeline

**Explore**
- 📈 [Trends](docs/pages/trends.md): hours per calendar or category over weeks and months
- ⚖️ [Compare Periods](docs/pages/compare-periods.md): two date ranges side by side with deltas
- 🔍 [Search](docs/pages/search.md): find events by name, with totals and CSV export
- 👥 [Social Analysis](docs/pages/social-analysis.md): who you spend time with, and how often you meet
- 🏷️ [Tags](docs/pages/tags.md): counts, trends and patterns of habit tags (☕ Coffee, 🍺 Beer, …) from tag calendars such as Oura tags
- 🧠 [AI Insights](docs/pages/ai-insights.md): natural-language analysis of your time by ChatGPT

**Data & setup**
- 📂 [Import ICS](docs/pages/import-ics.md): add events from an `.ics` file to a configured calendar
- 🩺 [Calendar Health](docs/pages/calendar-health.md): whether every calendar link still works, with history and warnings
- ⚙️ [Settings](docs/pages/settings.md): calendars, counting and display, Tags defaults, free slots, cache & sync, and a raw editor for every config file

Across all pages: cache-first loading with a daily background sync, data-accuracy rules for long and free events, and time zone handling for recurring and all-day events ([how it works](docs/how-it-works.md)).

## ▶️ Quick start

Requires Python 3.9 or newer.

```bash
bash install.sh
```

The script clones or updates the repository, creates a `.venv`, installs the dependencies and starts the app. Re-running it is safe; `CTT_NO_RUN=1 bash install.sh` installs without starting. To do it by hand:

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then add your calendars on the ⚙️ Settings page, or create `calendars.json`:

```json
{
    "calendars": [
        {"url": "https://calendar.google.com/calendar/ical/.../basic.ics", "custom_name": "Work", "category": "Work"},
        {"url": "https://example.com/oura-tags.ics", "custom_name": "Oura Tags", "type": "tags"}
    ]
}
```

Use the calendar's iCal address (in Google Calendar: *Secret address in iCal format*), not a sharing link. All options, `calendars.txt`, tag calendars and the OpenAI key are described in [Configuration](docs/configuration.md).

## 📚 Documentation

- [Pages](docs/pages/): one guide per page, with screenshots
- [Configuration](docs/configuration.md): calendars, tag calendars, config files, OpenAI
- [How it works](docs/how-it-works.md): loading and sync, recurring events, time zones, data accuracy, privacy

## 🙌 Contribution

Contributions are welcome! Please fork the repository, create a new branch for your changes, and submit a pull request.

## 📄 License

This project is licensed under the [MIT License](LICENSE).

## ⚠️ Disclaimer

Use this tool at your own risk. Ensure you have proper backups and permissions before running the script in a production environment.

## 👤 Author

Developed by [ramhee98](https://github.com/ramhee98). For questions or suggestions, feel free to open an issue in the repository.
