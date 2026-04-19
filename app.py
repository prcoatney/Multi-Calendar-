"""Flask app for Multi-Org Calendar Scheduler.

Each organization gets its own slug-based namespace with independent
members, calendars, availability search, and scheduling.
"""

import os
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, redirect, render_template, session, url_for, abort
import pytz

from calendar_utils import find_available_slots
from db import (
    get_all_orgs,
    get_org_by_slug,
    get_members,
    get_member_ids,
    get_all_ical_urls,
    get_member_calendar_map,
    add_calendar,
    remove_calendar,
    add_member,
    remove_member,
    save_member_name,
    member_belongs_to_org,
    create_org,
)
from google_calendar import (
    get_auth_url,
    handle_oauth_callback,
    decode_state,
    is_member_authorized,
    create_event_all_members,
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-in-production")
app.config["PREFERRED_URL_SCHEME"] = "https"

from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)


# ── Helpers ──

def _get_org_or_404(slug):
    """Look up an org by slug, abort 404 if not found."""
    org = get_org_by_slug(slug)
    if not org:
        abort(404)
    return org


def _is_authenticated(org_slug):
    """Check if the session is authenticated for this org."""
    return session.get(f"auth_{org_slug}") is True


# ── Auth middleware ──

@app.before_request
def require_login():
    """Gate org routes behind per-org password (if one is set)."""
    # Public routes that never need auth
    open_endpoints = ("home", "org_login", "auth_callback", "static")
    if request.endpoint in open_endpoints:
        return

    # Extract org slug from view args
    slug = (request.view_args or {}).get("org_slug")
    if not slug:
        return

    org = get_org_by_slug(slug)
    if not org:
        return  # Will 404 in the view

    # If org has no password, skip auth
    if not org["password"]:
        return

    # Check session
    if not _is_authenticated(slug):
        return redirect(url_for("org_login", org_slug=slug))


# ── Landing page ──

@app.route("/")
def home():
    """Landing page: pick your organization."""
    orgs = get_all_orgs()
    return render_template("home.html", orgs=orgs)


# ── Per-org login ──

@app.route("/<org_slug>/login", methods=["GET", "POST"])
def org_login(org_slug):
    """Per-org password gate."""
    org = _get_org_or_404(org_slug)

    # If no password required, just redirect in
    if not org["password"]:
        return redirect(url_for("org_index", org_slug=org_slug))

    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == org["password"]:
            session[f"auth_{org_slug}"] = True
            return redirect(url_for("org_index", org_slug=org_slug))
        error = "Wrong password."
    return render_template("login.html", org=org, error=error)


# ── Main scheduler page ──

@app.route("/<org_slug>/")
def org_index(org_slug):
    """Render the main scheduler page for an org."""
    org = _get_org_or_404(org_slug)
    members = get_members(org_slug)
    total_cals = sum(len(m["calendars"]) for m in members)
    return render_template("index.html", org=org, members=members, total_cals=total_cals)


# ── Settings page ──

@app.route("/<org_slug>/settings")
def org_settings(org_slug):
    """Render the settings page for managing members and calendars."""
    org = _get_org_or_404(org_slug)
    members = get_members(org_slug)
    return render_template("settings.html", org=org, members=members)


# ── Member API ──

@app.route("/<org_slug>/api/members", methods=["GET"])
def api_get_members(org_slug):
    """Return all members with their saved iCal URLs."""
    _get_org_or_404(org_slug)
    return jsonify(get_members(org_slug))


@app.route("/<org_slug>/api/members", methods=["POST"])
def api_add_member(org_slug):
    """Add a new member to the org."""
    _get_org_or_404(org_slug)
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400

    member_id = add_member(org_slug, name)
    return jsonify({"success": True, "member_id": member_id})


@app.route("/<org_slug>/api/members/<int:member_id>", methods=["DELETE"])
def api_remove_member(org_slug, member_id):
    """Remove a member from the org."""
    _get_org_or_404(org_slug)
    if not member_belongs_to_org(member_id, org_slug):
        return jsonify({"error": "Member not found in this organization"}), 404
    remove_member(member_id, org_slug)
    return jsonify({"success": True})


@app.route("/<org_slug>/api/members/<int:member_id>/name", methods=["PUT"])
def api_rename_member(org_slug, member_id):
    """Rename a member."""
    _get_org_or_404(org_slug)
    if not member_belongs_to_org(member_id, org_slug):
        return jsonify({"error": "Member not found in this organization"}), 404
    data = request.get_json()
    name = (data.get("name") or "").strip() if data else ""
    if not name:
        return jsonify({"error": "Name is required"}), 400
    save_member_name(member_id, org_slug, name)
    return jsonify({"success": True})


# ── Calendar API ──

@app.route("/<org_slug>/api/calendars", methods=["POST"])
def api_add_calendar(org_slug):
    """Add a calendar URL for a member."""
    _get_org_or_404(org_slug)
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    member_id = data.get("member_id")
    label = data.get("label", "Main")
    ical_url = data.get("ical_url", "")

    if not member_id or not member_belongs_to_org(member_id, org_slug):
        return jsonify({"error": "Invalid member ID"}), 400
    if not ical_url or not ical_url.startswith("https://"):
        return jsonify({"error": "Valid HTTPS URL required"}), 400
    if "calendar.google.com" in ical_url and "/ical/" not in ical_url:
        return jsonify({"error": "That looks like a regular Google Calendar link. You need the 'Secret address in iCal format' — go to Google Calendar Settings, click your calendar, and scroll down to find it."}), 400

    add_calendar(member_id, label, ical_url)
    return jsonify({"success": True})


@app.route("/<org_slug>/api/calendars/<int:cal_id>", methods=["DELETE"])
def api_remove_calendar(org_slug, cal_id):
    """Remove a calendar by ID."""
    _get_org_or_404(org_slug)
    data = request.get_json() or {}
    member_id = data.get("member_id")
    if not member_id or not member_belongs_to_org(member_id, org_slug):
        return jsonify({"error": "Invalid member ID"}), 400

    remove_calendar(cal_id, member_id)
    return jsonify({"success": True})


# ── Availability API ──

@app.route("/<org_slug>/api/find-availability", methods=["POST"])
def find_availability(org_slug):
    """Find available meeting slots across all members in the org."""
    _get_org_or_404(org_slug)
    data = request.get_json() or {}

    cal_map = get_member_calendar_map(org_slug)
    ical_urls = [c["ical_url"] for c in cal_map]

    if len(ical_urls) < 1:
        return jsonify({"error": "No calendars configured. Go to Settings to add them."}), 400

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
        slots, fetch_report = find_available_slots(
            ical_urls=ical_urls,
            search_start=search_start,
            search_end=search_end,
            meeting_duration_minutes=duration,
            work_hours_start=work_start,
            work_hours_end=work_end,
            timezone_str=tz_str,
        )
    except Exception as e:
        return jsonify({"error": f"Failed to process calendars: {str(e)}"}), 500

    # Map report to member names (don't expose URLs)
    member_report = []
    for i, entry in enumerate(fetch_report):
        info = cal_map[i]
        member_report.append({
            "member": info["member_name"],
            "label": info["label"],
            "events": entry["events"],
            "ok": entry["ok"],
            "error": entry["error"],
        })

    return jsonify({"slots": slots, "timezone": tz_str, "report": member_report})


# ── OAuth / Auth API ──

@app.route("/<org_slug>/api/auth/status")
def auth_status(org_slug):
    """Check which members have authorized Google Calendar access."""
    _get_org_or_404(org_slug)
    member_ids = get_member_ids(org_slug)
    statuses = {}
    for mid in member_ids:
        statuses[str(mid)] = is_member_authorized(org_slug, mid)
    return jsonify(statuses)


@app.route("/<org_slug>/api/auth/start/<int:member_id>")
def auth_start(org_slug, member_id):
    """Start OAuth flow for a member."""
    _get_org_or_404(org_slug)
    if not member_belongs_to_org(member_id, org_slug):
        return jsonify({"error": "Invalid member ID"}), 400

    redirect_uri = url_for("auth_callback", _external=True)
    auth_url = get_auth_url(org_slug, member_id, redirect_uri)
    return redirect(auth_url)


@app.route("/api/auth/callback")
def auth_callback():
    """Handle OAuth callback from Google (shared across all orgs)."""
    code = request.args.get("code")
    state = request.args.get("state")

    if not code or not state:
        return "Missing authorization code or state", 400

    try:
        org_slug, member_id = decode_state(state)
    except (ValueError, IndexError):
        return "Invalid state parameter", 400

    org = get_org_by_slug(org_slug)
    if not org:
        return "Unknown organization", 400

    if not member_belongs_to_org(member_id, org_slug):
        return "Invalid member for organization", 400

    redirect_uri = url_for("auth_callback", _external=True)
    try:
        handle_oauth_callback(code, org_slug, member_id, redirect_uri)
    except Exception as e:
        return f"OAuth error: {str(e)}", 500

    return redirect(url_for("org_index", org_slug=org_slug) + f"?auth_success={member_id}")


# ── Schedule API ──

@app.route("/<org_slug>/api/schedule-meeting", methods=["POST"])
def schedule_meeting(org_slug):
    """Schedule a meeting on all members' calendars."""
    _get_org_or_404(org_slug)
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    summary = data.get("summary", "Team Meeting")
    start_time = data.get("start")
    end_time = data.get("end")
    description = data.get("description", "")
    # Google Meet link generation is gated to the CFK org for now.
    add_meet_link = bool(data.get("add_meet_link")) and org_slug == "cross-formed-kids"

    if not start_time or not end_time:
        return jsonify({"error": "start and end times are required"}), 400

    member_ids = get_member_ids(org_slug)
    unauthorized = [mid for mid in member_ids if not is_member_authorized(org_slug, mid)]
    if unauthorized:
        return jsonify({
            "error": "Not all members have authorized calendar access",
            "unauthorized": unauthorized,
        }), 403

    try:
        results = create_event_all_members(
            org_slug=org_slug,
            member_ids=member_ids,
            summary=summary,
            start_time=start_time,
            end_time=end_time,
            description=description,
            add_meet_link=add_meet_link,
        )
    except Exception as e:
        return jsonify({"error": f"Failed to create events: {str(e)}"}), 500

    return jsonify({"success": True, "events": results})


# ── Admin API ──

@app.route("/admin/orgs", methods=["POST"])
def admin_create_org():
    """Create a new organization."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400

    password = (data.get("password") or "").strip()
    slug = (data.get("slug") or "").strip() or None

    try:
        slug = create_org(name, slug=slug, password=password)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409

    return jsonify({"success": True, "slug": slug})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
