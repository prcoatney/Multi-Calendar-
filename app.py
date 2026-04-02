"""Flask app for Founder Calendar Scheduler.

Reads 3 founders' Google Calendar iCal feeds, finds mutual availability,
and books meetings on all calendars via Google Calendar API.
"""

import os
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, redirect, render_template, session, url_for
import pytz

from calendar_utils import find_available_slots
from db import get_founders, get_all_ical_urls, add_calendar, remove_calendar, save_founder_name
from google_calendar import (
    get_auth_url,
    handle_oauth_callback,
    is_founder_authorized,
    create_event_all_founders,
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-in-production")
app.config["PREFERRED_URL_SCHEME"] = "https"

from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

APP_PASSWORD = os.environ.get("APP_PASSWORD", "domcal")
FOUNDER_IDS = ["founder1", "founder2", "founder3"]


@app.before_request
def require_login():
    """Gate every route behind a simple password."""
    open_routes = ("login", "static")
    if request.endpoint in open_routes:
        return
    if not session.get("authenticated"):
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    """Simple password gate."""
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == APP_PASSWORD:
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = "Wrong password."
    return render_template("login.html", error=error)


@app.route("/")
def index():
    """Render the main page."""
    founders = get_founders()
    return render_template("index.html", founders=founders)


@app.route("/settings")
def settings():
    """Render the settings page for managing founder iCal URLs."""
    founders = get_founders()
    return render_template("settings.html", founders=founders)


@app.route("/api/founders", methods=["GET"])
def api_get_founders():
    """Return all founders with their saved iCal URLs."""
    return jsonify(get_founders())


@app.route("/api/calendars", methods=["POST"])
def api_add_calendar():
    """Add a calendar URL for a founder."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    fid = data.get("founder_id")
    label = data.get("label", "Main")
    ical_url = data.get("ical_url", "")

    if fid not in FOUNDER_IDS:
        return jsonify({"error": "Invalid founder ID"}), 400
    if not ical_url or not ical_url.startswith("https://"):
        return jsonify({"error": "Valid HTTPS URL required"}), 400

    add_calendar(fid, label, ical_url)
    return jsonify({"success": True})


@app.route("/api/calendars/<int:cal_id>", methods=["DELETE"])
def api_remove_calendar(cal_id):
    """Remove a calendar by ID."""
    data = request.get_json() or {}
    fid = data.get("founder_id", "")
    if fid not in FOUNDER_IDS:
        return jsonify({"error": "Invalid founder ID"}), 400

    remove_calendar(cal_id, fid)
    return jsonify({"success": True})


@app.route("/api/find-availability", methods=["POST"])
def find_availability():
    """Find available meeting slots across all founders.

    Uses saved iCal URLs from the database. Optional overrides via JSON body.
    """
    data = request.get_json() or {}

    # Load all calendar URLs from database
    ical_urls = get_all_ical_urls()

    if len(ical_urls) < 2:
        return jsonify({"error": "Not enough calendars configured. Go to Settings to add them."}), 400

    # Validate URLs
    for url in ical_urls:
        if not url.startswith("https://"):
            return jsonify({"error": f"Invalid URL (must be HTTPS): {url}"}), 400

    duration = data.get("duration_minutes", 60)
    days_ahead = data.get("days_ahead", 5)
    work_start = data.get("work_hours_start", 9)
    work_end = data.get("work_hours_end", 17)
    tz_str = data.get("timezone", "America/New_York")

    try:
        tz = pytz.timezone(tz_str)
    except pytz.exceptions.UnknownTimeZoneError:
        return jsonify({"error": f"Unknown timezone: {tz_str}"}), 400

    now = datetime.now(tz)
    search_start = now
    search_end = now + timedelta(days=days_ahead)

    try:
        slots = find_available_slots(
            ical_urls=ical_urls,
            search_start=search_start,
            search_end=search_end,
            meeting_duration_minutes=duration,
            work_hours_start=work_start,
            work_hours_end=work_end,
            timezone_str=tz_str,
        )
    except Exception as e:
        return jsonify({"error": f"Failed to fetch calendars: {str(e)}"}), 500

    return jsonify({"slots": slots, "timezone": tz_str})


@app.route("/api/auth/status")
def auth_status():
    """Check which founders have authorized Google Calendar access."""
    statuses = {}
    for fid in FOUNDER_IDS:
        statuses[fid] = is_founder_authorized(fid)
    return jsonify(statuses)


@app.route("/api/auth/start/<founder_id>")
def auth_start(founder_id):
    """Start OAuth flow for a founder."""
    if founder_id not in FOUNDER_IDS:
        return jsonify({"error": "Invalid founder ID"}), 400

    redirect_uri = url_for("auth_callback", _external=True)
    auth_url = get_auth_url(founder_id, redirect_uri)
    return redirect(auth_url)


@app.route("/api/auth/callback")
def auth_callback():
    """Handle OAuth callback from Google."""
    code = request.args.get("code")
    state = request.args.get("state")  # founder_id

    if not code or not state:
        return "Missing authorization code or state", 400

    if state not in FOUNDER_IDS:
        return "Invalid founder ID in state", 400

    redirect_uri = url_for("auth_callback", _external=True)
    try:
        handle_oauth_callback(code, state, redirect_uri)
    except Exception as e:
        return f"OAuth error: {str(e)}", 500

    return redirect(url_for("index") + f"?auth_success={state}")


@app.route("/api/schedule-meeting", methods=["POST"])
def schedule_meeting():
    """Schedule a meeting on all founders' calendars.

    Expects JSON body:
    {
        "summary": "Founders Sync",
        "start": "2024-01-15T10:00:00-05:00",
        "end": "2024-01-15T11:00:00-05:00",
        "description": "Weekly sync meeting"
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    summary = data.get("summary", "Founders Meeting")
    start_time = data.get("start")
    end_time = data.get("end")
    description = data.get("description", "")

    if not start_time or not end_time:
        return jsonify({"error": "start and end times are required"}), 400

    # Check all founders are authorized
    unauthorized = [fid for fid in FOUNDER_IDS if not is_founder_authorized(fid)]
    if unauthorized:
        return jsonify({
            "error": "Not all founders have authorized calendar access",
            "unauthorized": unauthorized,
        }), 403

    try:
        results = create_event_all_founders(
            founder_ids=FOUNDER_IDS,
            summary=summary,
            start_time=start_time,
            end_time=end_time,
            description=description,
        )
    except Exception as e:
        return jsonify({"error": f"Failed to create events: {str(e)}"}), 500

    return jsonify({"success": True, "events": results})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
