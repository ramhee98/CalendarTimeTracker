# CalendarTimeTracker

**CalendarTimeTracker** is a Streamlit (Python) app for analyzing how time is spent across multiple public `.ics` calendar URLs. It focuses on **event duration** rather than content and supports both **calendar-based** and **category-based** visualizations.

## 🚀 Features

- 📅 Load multiple calendars from `calendars.txt` or `calendars.json`
- 📊 Visualize total and relative time spent per month
- 🔥 Activity heatmap (weekday × hour)
- 🍰 Pie chart for calendar/category time distribution
- 📦 Summary tables with CSV export
- 🔄 Dynamically switch between viewing by **Calendar** or **Category**
- 🎨 Custom calendar colors
- 🔍 Calendar Insights (GPT-powered) Analyze page

## 🖼️ Screenshots

### 📋 Summary Table
Aggregate total hours, averages, and event counts per calendar or category.

![Summary Table](img/summary.png)

---

### 📆 Total Time per Month (Stacked)
View absolute duration stacked per calendar each month.

![Total Time per Month](img/total-time-per-month-stacked.png)

---

### 📊 Relative Time per Month (100% Stacked)
Visualize how time is distributed each month, normalized to 100%.

![Relative Time per Month](img/relative-time-per-month.png)

---

### 🥧 Time Distribution by Calendar
See which calendar contributes most to your time allocation.

![Time Distribution Pie Chart](img/time-distribution-per-calendar.png)

---

### 📥 Importing `.ics` into a Calendar
Upload `.ics` files and assign them to specific calendars configured via `calendars.json` or `calendars.txt`.

![Import ICS File](img/import-ics.png)

## 📦 Requirements
Install dependencies:

```bash
pip install -r requirements.txt
```
Optionally an OpenAI API key with access to GPT models for further insights.

## 🛠 Installation

Use the install script to get the project up and running on your local machine:

```bash
bash install.sh
```

## ⚙️ Configuration

You can configure calendars in two ways with samples provided:

### Option 1: `calendars.txt`

A plain text file where each line contains a calendar URL and an optional name.

### Option 2: `calendars.json` (Preferred)

Supports rich metadata like category assignment and custom calendar colors.

If `calendars.json` is present, it will be used automatically and enables category-based grouping.

## ▶️ Running the App

```bash
streamlit run app.py
```

The app will:

1. Load events from `.ics` calendar sources
2. Ask you to select a view mode (Calendar or Category) if using `calendars.json`
3. Let you choose a month range to analyze
4. Display multiple interactive charts and tables

## 📂 Output

- All data stays local
- Summary tables can be downloaded as CSV
- Visuals include tooltips and interactive features

## 🧠 Notes

- Time zone normalization is done as **naive** by default.
- Duplicate events are filtered using the event `UID` field.
- Caching reduces repeated loads for unchanged calendars.
- Events from the **past 30 days and all future dates** are automatically re-synced with the source `.ics` file.
  - If an event is **removed** from the source calendar, it will also be **removed from the local cache**.
  - Events **older than 30 days** are preserved for historical reference, even if they no longer exist in the source.

## 🙌 Contribution

Contributions are welcome! Please fork the repository, create a new branch for your changes, and submit a pull request.

## 📄 License

This project is licensed under the [MIT License](LICENSE).

## ⚠️ Disclaimer

Use this tool at your own risk. Ensure you have proper backups and permissions before running the script in a production environment.

## 👤 Author

Developed by [ramhee98](https://github.com/ramhee98). For questions or suggestions, feel free to open an issue in the repository.
