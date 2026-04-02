# Founder Calendar Scheduler

Find mutual availability across 3 founders' Google Calendars and schedule meetings on all calendars at once.

## How It Works

1. **Read calendars** — Each founder provides their Google Calendar's "Secret address in iCal format" (a read-only URL). The app fetches all events to determine busy times.
2. **Find availability** — The app computes overlapping free slots across all 3 calendars, filtered by work hours and weekdays.
3. **Schedule meeting** — Pick a slot, and the app creates the event on all 3 founders' Google Calendars via the Google Calendar API.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Google Cloud credentials (required for writing events)

To **add events** to calendars, you need Google OAuth credentials:

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project (or use an existing one)
3. Enable the **Google Calendar API**
4. Go to **APIs & Services > Credentials**
5. Create an **OAuth 2.0 Client ID** (type: Web application)
6. Add `http://localhost:5000/api/auth/callback` as an authorized redirect URI
7. Download the JSON file and save it as `credentials.json` in the project root

### 3. Get iCal URLs from each founder

Each founder does this in Google Calendar:
1. Go to **Settings** (gear icon)
2. Click on their calendar under "Settings for my calendars"
3. Scroll to **"Secret address in iCal format"**
4. Copy the URL

### 4. Run the app

```bash
python app.py
```

Then open http://localhost:5000

## Usage

1. Paste all 3 founders' iCal URLs
2. Set meeting duration, time window, and work hours
3. Click **Find Available Times** — the app reads all 3 calendars and shows mutual free slots
4. Select a time slot
5. Each founder clicks **Authorize** to grant calendar write access (one-time OAuth)
6. Click **Schedule Meeting** — the event is created on all 3 calendars

## Project Structure

```
app.py                 — Flask web server & API routes
calendar_utils.py      — iCal parsing & availability engine
google_calendar.py     — Google Calendar API (OAuth + event creation)
templates/index.html   — Frontend UI
static/style.css       — Styles
credentials.json       — Google OAuth credentials (you provide this)
```

## Notes

- **Reading** calendars uses the secret iCal URL (no OAuth needed)
- **Writing** events requires each founder to do a one-time Google OAuth authorization
- Tokens are stored locally in `tokens/` and auto-refresh
- The app only searches weekdays within the configured work hours
- All-day events are treated as busy time
