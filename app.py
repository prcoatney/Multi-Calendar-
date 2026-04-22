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
    get_member,
    get_member_ids,
    get_all_ical_urls,
    get_member_calendar_map,
    get_bookable_member,
    add_calendar,
    remove_calendar,
    add_member,
    remove_member,
    save_member_name,
    set_booking_config,
    member_belongs_to_org,
    create_org,
)
from google_calendar import (
    get_auth_url,
    handle_oauth_callback,
    decode_state,
    is_member_authorized,
    create_event,
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
    open_endpoints = (
        "home",
        "org_login",
        "auth_callback",
        "static",
        "public_booking_page",
        "public_booking_availability",
        "public_booking_create",
    )
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
    """Remove a member from the org. Requires the member's exact name as confirmation."""
    _get_org_or_404(org_slug)
    if not member_belongs_to_org(member_id, org_slug):
        return jsonify({"error": "Member not found in this organization"}), 404
    data = request.get_json(silent=True) or {}
    confirm_name = (data.get("confirm_name") or "").strip()
    if not confirm_name:
        return jsonify({"error": "Type the member's exact name to confirm deletion."}), 400
    ok = remove_member(member_id, org_slug, confirm_name)
    if not ok:
        return jsonify({"error": "Confirmation name did not match. Nothing was deleted."}), 400
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


@app.route("/<org_slug>/api/members/<int:member_id>/booking", methods=["PUT"])
def api_set_member_booking(org_slug, member_id):
    """Update a member's public booking configuration (slug + enabled flag)."""
    _get_org_or_404(org_slug)
    if not member_belongs_to_org(member_id, org_slug):
        return jsonify({"error": "Member not found in this organization"}), 404
    data = request.get_json() or {}
    booking_slug = data.get("booking_slug")
    booking_enabled = bool(data.get("booking_enabled"))
    if booking_enabled and not (booking_slug or "").strip():
        return jsonify({"error": "A booking slug is required to enable public booking."}), 400
    try:
        final_slug = set_booking_config(member_id, org_slug, booking_slug, booking_enabled)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify({"success": True, "booking_slug": final_slug, "booking_enabled": booking_enabled})


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
    """Remove a calendar by ID. Requires the calendar's exact label as confirmation."""
    _get_org_or_404(org_slug)
    data = request.get_json() or {}
    member_id = data.get("member_id")
    if not member_id or not member_belongs_to_org(member_id, org_slug):
        return jsonify({"error": "Invalid member ID"}), 400

    confirm_label = (data.get("confirm_label") or "").strip()
    if not confirm_label:
        return jsonify({"error": "Type the calendar's label to confirm deletion."}), 400

    ok = remove_calendar(cal_id, member_id, confirm_label)
    if not ok:
        return jsonify({"error": "Confirmation label did not match. Nothing was deleted."}), 400
    return jsonify({"success": True})


# ── Availability API ──

@app.route("/<org_slug>/api/find-availability", methods=["POST"])
def find_availability(org_slug):
    """Find available meeting slots across selected members in the org."""
    _get_org_or_404(org_slug)
    data = request.get_json() or {}

    cal_map = get_member_calendar_map(org_slug)

    # Optional filter: only include these member names in the search.
    # None/missing/empty means "all members" (back-compat).
    selected_names = data.get("member_names")
    if selected_names:
        name_set = {n for n in selected_names if isinstance(n, str)}
        cal_map = [c for c in cal_map if c["member_name"] in name_set]

    ical_urls = [c["ical_url"] for c in cal_map]

    if len(ical_urls) < 1:
        return jsonify({"error": "No calendars for the selected members. Pick at least one member with a calendar, or add calendars in Settings."}), 400

    duration = data.get("duration_minutes", 60)
    days_ahead = data.get("days_ahead", 90)
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

    # If the client passed the same member_names filter used to find availability,
    # only create events on *those* members' calendars.
    all_member_ids = get_member_ids(org_slug)
    selected_names = data.get("member_names")
    if selected_names:
        name_set = {n for n in selected_names if isinstance(n, str)}
        all_members = {m["id"]: m["name"] for m in get_members(org_slug)}
        member_ids = [mid for mid in all_member_ids if all_members.get(mid) in name_set]
        if not member_ids:
            return jsonify({"error": "No members selected"}), 400
    else:
        member_ids = all_member_ids

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


# ── Public booking (Calendly-style) ──

@app.route("/<org_slug>/book/<booking_slug>")
def public_booking_page(org_slug, booking_slug):
    """Public landing page for booking time with a single member.

    Unauthenticated: anyone with the link can see availability and request a meeting.
    """
    org = _get_org_or_404(org_slug)
    member = get_bookable_member(org_slug, booking_slug)
    if not member:
        abort(404)
    return render_template("booking.html", org=org, member=member)


@app.route("/<org_slug>/api/public/availability/<booking_slug>", methods=["POST"])
def public_booking_availability(org_slug, booking_slug):
    """Return available slots for a single bookable member. No auth required."""
    _get_org_or_404(org_slug)
    member = get_bookable_member(org_slug, booking_slug)
    if not member:
        return jsonify({"error": "This booking link is not active."}), 404

    data = request.get_json() or {}
    duration = int(data.get("duration_minutes", 30))
    days_ahead = int(data.get("days_ahead", 14))
    tz_str = data.get("timezone", "America/New_York")

    # Public booking for Coat runs on a tighter window than the internal
    # scheduler: Mon-Thu only, 8:30 AM to 3:00 PM. Host window is fixed.
    BOOKING_WEEKDAYS = {0, 1, 2, 3}  # Mon, Tue, Wed, Thu
    BOOKING_START_HOUR = 8.5          # 8:30 AM
    BOOKING_END_HOUR = 15.0           # 3:00 PM

    # Fetch only this member's calendars.
    cal_map = [c for c in get_member_calendar_map(org_slug) if c["member_name"] == member["name"]]
    ical_urls = [c["ical_url"] for c in cal_map]

    if not ical_urls:
        return jsonify({"error": "No calendars configured for this booking link yet."}), 400

    try:
        tz = pytz.timezone(tz_str)
    except pytz.exceptions.UnknownTimeZoneError:
        return jsonify({"error": f"Unknown timezone: {tz_str}"}), 400

    now = datetime.now(tz)
    search_start = now + timedelta(hours=1)  # no "in 5 minutes" bookings
    search_end = now + timedelta(days=days_ahead)

    try:
        slots, _report = find_available_slots(
            ical_urls=ical_urls,
            search_start=search_start,
            search_end=search_end,
            meeting_duration_minutes=duration,
            work_hours_start=BOOKING_START_HOUR,
            work_hours_end=BOOKING_END_HOUR,
            timezone_str=tz_str,
            allowed_weekdays=BOOKING_WEEKDAYS,
        )
    except Exception as e:
        return jsonify({"error": f"Failed to process calendars: {str(e)}"}), 500

    return jsonify({"slots": slots, "timezone": tz_str, "member_name": member["name"]})


@app.route("/<org_slug>/api/public/book/<booking_slug>", methods=["POST"])
def public_booking_create(org_slug, booking_slug):
    """Book a meeting on the target member's calendar, with Google Meet + guest invite."""
    _get_org_or_404(org_slug)
    member = get_bookable_member(org_slug, booking_slug)
    if not member:
        return jsonify({"error": "This booking link is not active."}), 404

    if not is_member_authorized(org_slug, member["id"]):
        return jsonify({
            "error": "This booking link isn't ready yet — the host hasn't connected their calendar."
        }), 503

    data = request.get_json() or {}
    guest_name = (data.get("guest_name") or "").strip()
    guest_email = (data.get("guest_email") or "").strip()
    start_time = data.get("start")
    end_time = data.get("end")
    notes = (data.get("notes") or "").strip()

    if not guest_name:
        return jsonify({"error": "Your name is required."}), 400
    if not guest_email or "@" not in guest_email:
        return jsonify({"error": "A valid email is required."}), 400
    if not start_time or not end_time:
        return jsonify({"error": "Pick a time slot first."}), 400

    summary = f"{member['name']} ↔ {guest_name}"
    description_lines = [f"Booked via public link by {guest_name} <{guest_email}>."]
    if notes:
        description_lines += ["", notes]
    description = "\n".join(description_lines)

    try:
        event = create_event(
            org_slug=org_slug,
            member_id=member["id"],
            summary=summary,
            start_time=start_time,
            end_time=end_time,
            description=description,
            attendee_emails=[guest_email],
            add_meet_link=True,
            send_updates="all",
        )
    except Exception as e:
        return jsonify({"error": f"Failed to create the meeting: {str(e)}"}), 500

    meet_link = None
    conf = event.get("conferenceData") or {}
    for entry in conf.get("entryPoints") or []:
        if entry.get("entryPointType") == "video":
            meet_link = entry.get("uri")
            break
    if not meet_link:
        meet_link = event.get("hangoutLink")

    return jsonify({
        "success": True,
        "event_link": event.get("htmlLink"),
        "meet_link": meet_link,
    })


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
