# ⚙️ Configuration

[← README](../README.md) · [Pages](pages/) · [How it works](how-it-works.md)

Everything here can also be done in the app on the [⚙️ Settings](pages/settings.md) page, including a raw text editor for every file.

## Calendars

Calendars are read from `calendars.json` or, if that file doesn't exist, from `calendars.txt`. Samples of both are included (`*.sample`).

### Which URL to use

The URL must be the iCal feed itself: an address ending in `.ics`. In Google Calendar that's *Settings → your calendar → Secret address in iCal format*. A `calendar.google.com/calendar/u/0?cid=...` sharing link doesn't work, because it serves the Calendar web page instead of calendar data. Local files work too: `file:///path/to/calendar.ics`.

Secret addresses give read access to your calendar, so treat them like passwords. The app never shows them in full outside the Settings editor (see [Calendar Health](pages/calendar-health.md)).

### `calendars.json` (recommended)

```json
{
    "calendars": [
        {
            "url": "https://calendar.google.com/calendar/ical/.../basic.ics",
            "custom_name": "Work",
            "category": "Work",
            "color": "#4C78A8"
        },
        {
            "url": "https://example.com/project_b.ics",
            "custom_name": "Project B",
            "category": "Work"
        },
        {
            "url": "file:///home/user/calendar.ics"
        },
        {
            "url": "https://example.com/oura-tags.ics",
            "custom_name": "Oura Tags",
            "type": "tags"
        }
    ]
}
```

| Key | Meaning |
|---|---|
| `url` | The feed (required) |
| `custom_name` | Name shown in the app; without it the feed's own name (`X-WR-CALNAME`) or "Unnamed" is used. Calendars with the same name are shown together |
| `category` | Groups calendars for "View data by: Category"; defaults to the name |
| `color` | Hex color like `#33FF57`; without it a distinct color is picked |
| `type` | `"tags"` for a [tag calendar](#tag-calendars); leave it out for normal calendars |

Keys the app doesn't know are kept when you edit calendars on the Settings page. The previous file is saved as `calendars.json.bak`.

### `calendars.txt`

One URL per line, optionally followed by `# Name`. Lines starting with `#` are ignored.

```
https://example.com/calendar1.ics  # Work calendar
https://example.com/calendar2.ics  # Personal calendar
file:///home/user/calendar.ics
```

Categories and colors need `calendars.json`. The Settings page converts a `calendars.txt` setup to `calendars.json` in one click.

## Tag calendars

Some calendars contain markers rather than time blocks, for example [oura-tags-ical](https://github.com/ramhee98/oura-tags-ical), which turns Oura tags into events such as ☕ Coffee or 🍺 Beer. Counted as time, every all-day tag would add 24 hours. Mark such calendars with `"type": "tags"` (or set **Type** to `tags` on the Settings page).

Tag calendars are synced like any other calendar, but are left out of all hour totals, charts, time goals and Social Analysis. They're counted on the [🏷️ Tags](pages/tags.md) page and by tag goals on the [🎯 Goals](pages/goals.md) page. Category and color are ignored. The Settings page points out calendars that look like tag calendars (almost all events all-day or without duration).

## OpenAI (AI Insights only)

The [🧠 AI Insights](pages/ai-insights.md) page needs an OpenAI API key in a `.env` file in the project root:

```bash
OPENAI_API_KEY=sk-...
```

Opening AI Insights without a `.env` creates a template with a placeholder. The key isn't shown anywhere in the app.

`ai_config.json` configures the prompts (analysis styles such as detailed, quick summary or productivity focus), the available models and the UI options. If it's missing, it's created from `ai_config.json.sample`.

## Files created by the app

These are written when you save something in the app, are ignored by git, and can be edited on Settings → 📝 Raw files:

| File | Contents |
|---|---|
| `app_settings.json` | Counting, display, sync, Tags page, free-slot and health settings from the Settings page |
| `goals.json` | Goals from the Goals page |
| `tag_groups.json` | Tag groups from the Tags page |
| `social_analysis_settings.json` | People, aliases and filters of Social Analysis |
| `sync_health.json` | Recorded calendar checks of the Calendar Health page (URLs shortened, without secrets) |
| `ai_responses_cache.json` | Saved AI Insights answers |
| `data/*.csv` | The local event cache, one file per calendar URL |
| `<file>.bak` | The previous version of a file saved on the Settings page |
