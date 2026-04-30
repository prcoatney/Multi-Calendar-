"""Flask app for Multi-Org Calendar Scheduler.

Each organization gets its own slug-based namespace with independent
members, calendars, availability search, and scheduling.
"""

import json
import os
from datetime import date, datetime, timedelta
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
    list_events,
    delete_event,
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
        "api_list_events",
        "planner_pdf",
        "hyperpaper_pdf",
        "planner_dashboard",
        "planner_create_token",
        "planner_seed_token",
        "api_chat",
        "api_notes_ai",
        "planner_setup_script",
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


# ── Events API (for external consumers like reMarkable planner) ──

@app.route("/api/<org_slug>/members/<int:member_id>/events")
def api_list_events(org_slug, member_id):
    """List calendar events for a member. Query params: start, end, max.

    Example: /api/dominion/members/3/events?start=2026-04-26&end=2026-05-03
    Returns JSON array of events.
    """
    # API key auth — simple bearer token
    api_key = os.environ.get("CALENDAR_API_KEY", "")
    auth_header = request.headers.get("Authorization", "")
    api_key_param = request.args.get("key", "")

    if api_key and not (
        auth_header == f"Bearer {api_key}" or api_key_param == api_key
    ):
        return jsonify({"error": "Unauthorized"}), 401

    org = get_org_by_slug(org_slug)
    if not org:
        return jsonify({"error": "Org not found"}), 404

    if not member_belongs_to_org(member_id, org_slug):
        return jsonify({"error": "Member not in org"}), 404

    if not is_member_authorized(org_slug, member_id):
        return jsonify({"error": "Member not authorized for Google Calendar"}), 403

    # Parse date params → RFC3339
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    max_results = int(request.args.get("max", 50))

    time_min = None
    time_max = None
    if start:
        if "T" not in start:
            start += "T00:00:00Z"
        time_min = start
    if end:
        if "T" not in end:
            end += "T23:59:59Z"
        time_max = end

    try:
        events = list_events(org_slug, member_id, time_min, time_max, max_results)
        return jsonify(events)
    except ValueError as e:
        return jsonify({"error": str(e)}), 403
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Planner PDF endpoint (for reMarkable living planner) ──

from planner_gen import generate_planner, events_hash as planner_events_hash
from planner_gen import parse_hour  # reuse
from hyperpaper_gen import generate_hyperpaper, events_hash as hp_events_hash

# Cache: {device_token: {"hash": str, "pdf": bytes}}
_planner_cache = {}
_hyperpaper_cache = {}
# Hyperpaper page UUID → page index mapping, uploaded once by device
# Survives PDF regen. Shape: {token: {"uuid_to_idx": {<uuid>: <int>}, "uploaded": <ts>}}
_hyperpaper_content_cache = {}
# De-dup: track stroke count last seen per (token, page_uuid) so we only
# evaluate freshly-added strokes. Shape: {(token, page_uuid): int}
_hyperpaper_strike_state = {}
# Last-activity log per token, for the /health endpoint.
# Shape: {token: {"last_heartbeat", "last_strike", "last_pdf_pull",
#                 "last_content_upload", "last_action"}}
_hyperpaper_activity = {}


def _record_activity(token, **kwargs):
    """Update activity log for token. Fields: last_heartbeat, last_strike, etc."""
    a = _hyperpaper_activity.setdefault(token, {})
    now = datetime.utcnow().isoformat() + "Z"
    for k, v in kwargs.items():
        a[k] = v if v is not True else now
    if not kwargs:
        a["last_heartbeat"] = now


@app.route("/api/notes-ai", methods=["POST"])
def api_notes_ai():
    """Receive .rm file, OCR the notes area, send to Claude for analysis."""
    api_key = os.environ.get("CALENDAR_API_KEY", "")
    if api_key and request.args.get("key") != api_key:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data or not data.get("rm_data"):
        return jsonify({"error": "rm_data required"}), 400

    import base64
    import io as io_mod

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        return jsonify({"error": "No API key"}), 500

    try:
        # Decode .rm file
        rm_bytes = base64.b64decode(data["rm_data"])

        # Read strokes with rmscene
        from rmscene import read_blocks
        blocks = list(read_blocks(io_mod.BytesIO(rm_bytes)))

        strokes = []
        all_ys = []
        for b in blocks:
            if type(b).__name__ == "SceneLineItemBlock":
                val = b.item.value
                if val and hasattr(val, "points") and val.points:
                    pts = [(p.x, p.y) for p in val.points]
                    strokes.append(pts)
                    all_ys.extend([p.y for p in val.points])

        if not strokes:
            return jsonify({"response": "No handwriting found", "last_y": 0})

        # Filter to notes area only (left panel in .rm coords)
        # Notes panel: x=-591 to 269, y=535 to 1863
        notes_strokes = []
        for pts in strokes:
            avg_x = sum(x for x, y in pts) / len(pts)
            avg_y = sum(y for x, y in pts) / len(pts)
            if avg_x < 269 and 535 < avg_y < 1863:
                notes_strokes.append(pts)

        if not notes_strokes:
            notes_strokes = strokes  # fallback to all strokes

        # Render to image — large, high contrast, preserve aspect ratio
        from PIL import Image, ImageDraw

        all_x = [x for pts in notes_strokes for x, y in pts]
        all_y = [y for pts in notes_strokes for x, y in pts]
        min_x, max_x = min(all_x), max(all_x)
        min_y, max_y = min(all_y), max(all_y)
        range_x = max(max_x - min_x, 1)
        range_y = max(max_y - min_y, 1)

        # Scale to fill a large image with proper aspect ratio
        scale = min(2400 / range_x, 3200 / range_y, 8.0)
        img_w = int(range_x * scale) + 200
        img_h = int(range_y * scale) + 200
        img_w = max(img_w, 800)
        img_h = max(img_h, 400)

        img = Image.new("L", (img_w, img_h), 255)
        draw = ImageDraw.Draw(img)

        for pts in notes_strokes:
            scaled = []
            for x, y in pts:
                sx = int((x - min_x) * scale + 100)
                sy = int((y - min_y) * scale + 100)
                scaled.append((sx, sy))
            if len(scaled) >= 2:
                draw.line(scaled, fill=0, width=max(3, int(scale * 0.5)))

        # Save to buffer
        img_buf = io_mod.BytesIO()
        img.save(img_buf, format="PNG")
        img_b64 = base64.b64encode(img_buf.getvalue()).decode()

        # OCR + Analysis with Claude
        import urllib.request as ur
        import json as json_mod

        body = json_mod.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1000,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                    {"type": "text", "text": "Read this handwriting and respond directly to what it says. Do NOT describe what you see or mention handwriting. Respond with substance and depth as a knowledgeable thought partner. If it is a theological or academic topic, engage at a seminary level. 4-6 sentences. Use only basic ASCII characters."}
                ]
            }]
        }).encode()

        req = ur.Request("https://api.anthropic.com/v1/messages",
            data=body, headers={
                "Content-Type": "application/json",
                "x-api-key": anthropic_key,
                "anthropic-version": "2023-06-01",
            })

        with ur.urlopen(req, timeout=45) as r:
            resp = json_mod.loads(r.read())

        content = resp.get("content", [])
        if isinstance(content, list):
            text = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
        else:
            text = str(content)

        # Calculate last_y from notes strokes only (not all strokes)
        notes_ys = [y for pts in notes_strokes for x, y in pts]
        last_y = int(max(notes_ys)) if notes_ys else 0

        return jsonify({"response": text, "last_y": last_y})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Proxy chat to Anthropic Claude."""
    api_key = os.environ.get("CALENDAR_API_KEY", "")
    if api_key and request.args.get("key") != api_key:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data or not data.get("prompt"):
        return jsonify({"error": "prompt required"}), 400

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        return jsonify({"error": "No API key configured"}), 500

    import urllib.request as ur
    import json as json_mod
    body = json_mod.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 300,
        "system": "You are a helpful assistant writing on a reMarkable tablet. Keep responses under 3 sentences. Use only basic ASCII characters - no special symbols, no markdown, no bullet points.",
        "messages": [{"role": "user", "content": data["prompt"]}],
    }).encode()
    req = ur.Request("https://api.anthropic.com/v1/messages",
        data=body, headers={
            "Content-Type": "application/json",
            "x-api-key": anthropic_key,
            "anthropic-version": "2023-06-01",
        })
    try:
        with ur.urlopen(req, timeout=30) as r:
            resp = json_mod.loads(r.read())
        content = resp.get("content", [])
        if isinstance(content, list):
            text = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
        else:
            text = str(content)
        return jsonify({"response": text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _resolve_device(token):
    """Resolve a device token to (org_slug, member_id) or return None."""
    from db import get_device_token
    device = get_device_token(token)
    if device:
        return device["org_slug"], device["member_id"]
    if token == "rmpp-coat-001":
        return "cross-formed-kids", 2
    return None


def _fetch_hyperpaper_events(org_slug, member_id, year=2026):
    """Fetch + filter calendar events for a year. Returns (events_dict, hash).

    Reusable by /pdf and /strike (when /strike needs to rebuild manifest cache).
    Includes the OAuth-failure sentinel injection.
    """
    events = {}
    auth_failed = False
    for m in range(1, 13):
        start = date(year, m, 1)
        end = date(year, m + 1, 1) if m < 12 else date(year + 1, 1, 1)
        try:
            raw = list_events(org_slug, member_id,
                             start.isoformat() + "T00:00:00Z",
                             end.isoformat() + "T00:00:00Z", 500)
        except Exception as e:
            if "invalid_grant" in str(e) or "RefreshError" in type(e).__name__:
                auth_failed = True
                break
            continue
        for ev in raw:
            s = ev.get("start", "")
            if not s or "T" not in s:
                continue
            try:
                d = datetime.fromisoformat(s.replace("Z", "+00:00"))
                hr, mn = d.hour, d.minute
                ampm = "a" if hr < 12 else "p"
                h12 = hr if hr <= 12 else hr - 12
                if h12 == 0:
                    h12 = 12
                t = "%d:%02d%s" % (h12, mn, ampm) if mn else "%d%s" % (h12, ampm)
                sort_key = hr * 60 + mn
                title = ev.get("summary", "")
                title = title.replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
                title = ''.join(c if ord(c) < 256 else '?' for c in title)
                ev_id = ev.get("id", "")
                events.setdefault((d.year, d.month, d.day), []).append((sort_key, t, title, ev_id))
            except Exception:
                continue

    from hyperpaper_gen import SKIP_WORDS, SKIP_CONTAINS
    for k in events:
        events[k].sort()
        filtered = []
        for _, t, title, ev_id in events[k]:
            tl = title.strip().lower()
            if tl in SKIP_WORDS:
                continue
            if any(s in tl for s in SKIP_CONTAINS):
                continue
            filtered.append((t, title, ev_id))
        events[k] = filtered
    events = {k: v for k, v in events.items() if v}

    if auth_failed:
        today = datetime.utcnow().date()
        events[(today.year, today.month, today.day)] = [("9a", "[!] Check google token", "")]

    return events, hp_events_hash(events)


def _ensure_hyperpaper_manifest(token):
    """Return the manifest for this token, rebuilding cache if missing.
    Returns (manifest_dict, hash) or (None, None) if device unknown.
    """
    cached = _hyperpaper_cache.get(token)
    if cached and "manifest" in cached:
        return cached["manifest"], cached["hash"]
    resolved = _resolve_device(token)
    if not resolved:
        return None, None
    org_slug, member_id = resolved
    events, h = _fetch_hyperpaper_events(org_slug, member_id)
    pdf_bytes, manifest = generate_hyperpaper(events)
    manifest["hash"] = h
    _hyperpaper_cache[token] = {"hash": h, "pdf": pdf_bytes, "manifest": manifest}
    return manifest, h


@app.route("/api/hyperpaper/pdf")
def hyperpaper_pdf():
    """Device pulls its Hyperpaper PDF with calendar overlay."""
    token = request.args.get("token", "")
    if not token:
        return jsonify({"error": "token required"}), 400

    resolved = _resolve_device(token)
    if not resolved:
        return jsonify({"error": "unknown device"}), 404
    org_slug, member_id = resolved

    events, h = _fetch_hyperpaper_events(org_slug, member_id)

    if request.args.get("check"):
        return jsonify({"hash": h})

    cached = _hyperpaper_cache.get(token)
    if cached and cached["hash"] == h:
        from flask import Response
        return Response(cached["pdf"], mimetype="application/pdf",
                       headers={"X-Planner-Hash": h})

    pdf_bytes, manifest = generate_hyperpaper(events)
    manifest["hash"] = h
    _hyperpaper_cache[token] = {"hash": h, "pdf": pdf_bytes, "manifest": manifest}
    _record_activity(token, last_pdf_pull=True, last_action="pdf regenerated")

    from flask import Response
    return Response(pdf_bytes, mimetype="application/pdf",
                   headers={"X-Planner-Hash": h})


@app.route("/api/hyperpaper/content", methods=["POST"])
def hyperpaper_content_upload():
    """Device uploads its Hyperpaper .content file once (and on subsequent changes)
    so the server can resolve page UUIDs to PDF page indices without re-shipping
    .content with every strike upload.

    POST body = raw .content JSON bytes
    Query: ?token=xxx
    """
    token = request.args.get("token", "")
    if not token:
        return jsonify({"error": "token required"}), 400
    try:
        content = json.loads(request.get_data())
    except Exception as e:
        return jsonify({"error": f"invalid JSON: {e}"}), 400
    pages = content.get("cPages", {}).get("pages") or content.get("pages") or []
    uuid_to_idx = {}
    for i, p in enumerate(pages):
        pid = p.get("id") if isinstance(p, dict) else p
        if pid:
            uuid_to_idx[pid] = i
    _hyperpaper_content_cache[token] = {
        "uuid_to_idx": uuid_to_idx,
        "uploaded": datetime.utcnow().isoformat(),
    }
    _record_activity(token, last_content_upload=True,
                     last_action=f"content uploaded ({len(uuid_to_idx)} pages)")
    return jsonify({"ok": True, "pages": len(uuid_to_idx)})


@app.route("/api/hyperpaper/strike", methods=["POST"])
def hyperpaper_strike():
    """Device uploads a modified .rm file. Server detects snap-strike strokes,
    matches against current manifest bboxes, fires deletes.

    Query: ?token=xxx&page_uuid=<uuid>
    Body: raw .rm bytes
    Returns: {"checked": N, "matched": [...], "deleted": [...], "errors": [...]}
    """
    token = request.args.get("token", "")
    page_uuid = request.args.get("page_uuid", "")
    if not token or not page_uuid:
        return jsonify({"error": "token and page_uuid required"}), 400

    rm_bytes = request.get_data()
    if not rm_bytes:
        return jsonify({"error": "empty body"}), 400

    # If .content cache missing, signal device to re-upload before retrying.
    # 409 Conflict + needs_content marker — watcher knows to upload .content then retry.
    content_cache = _hyperpaper_content_cache.get(token)
    if not content_cache:
        return jsonify({"error": "no .content cached", "needs_content": True}), 409
    page_idx = content_cache["uuid_to_idx"].get(page_uuid)
    if page_idx is None:
        # page UUID unknown — device's .content may be stale relative to server's copy.
        # Tell device to re-upload .content.
        return jsonify({"error": f"unknown page_uuid", "needs_content": True}), 409

    # Self-heal manifest: if cache empty (Railway redeploy), rebuild inline.
    manifest, _h = _ensure_hyperpaper_manifest(token)
    if not manifest:
        return jsonify({"error": "unknown device"}), 404
    page_events = manifest.get("pages", {}).get(str(page_idx), [])

    # Parse strokes
    from rmscene import read_blocks
    from rmscene.scene_stream import SceneLineItemBlock
    import io as _io
    strokes = []
    try:
        for blk in read_blocks(_io.BytesIO(rm_bytes)):
            if isinstance(blk, SceneLineItemBlock) and blk.item.value is not None:
                pts = blk.item.value.points
                if pts:
                    strokes.append(pts)
    except Exception as e:
        return jsonify({"error": f"rmscene parse failed: {e}"}), 500

    # Only check strokes added since last upload
    state_key = f"{token}::{page_uuid}"
    last_seen = _hyperpaper_strike_state.get(state_key, 0)
    new_strokes = strokes[last_seen:]
    _hyperpaper_strike_state[state_key] = len(strokes)

    # Snap-strike detection: residual ≤ 0.5, length > 50
    SCALE = 0.322
    PDF_W_HALF = 226.0
    matched = []
    deleted = []
    errors = []
    for pts in new_strokes:
        if len(pts) < 2:
            continue
        xs = [p.x for p in pts]; ys = [p.y for p in pts]; n = len(pts)
        mx, my = sum(xs)/n, sum(ys)/n
        sxx = sum((x-mx)**2 for x in xs); syy = sum((y-my)**2 for y in ys)
        sxy = sum((xs[i]-mx)*(ys[i]-my) for i in range(n))
        if sxx >= syy and sxx:
            a = sxy/sxx; b = my - a*mx
            r = [a*xs[i]+b-ys[i] for i in range(n)]
        elif syy:
            a = sxy/syy; b = mx - a*my
            r = [a*ys[i]+b-xs[i] for i in range(n)]
        else:
            continue
        rms = (sum(x*x for x in r)/n) ** 0.5
        length = ((xs[-1]-xs[0])**2 + (ys[-1]-ys[0])**2) ** 0.5
        if rms > 0.5 or length <= 50:
            continue
        # Convert midpoint to PDF coords
        mid_rm_x = (pts[0].x + pts[-1].x) / 2
        mid_rm_y = (pts[0].y + pts[-1].y) / 2
        pdf_x = mid_rm_x * SCALE + PDF_W_HALF
        pdf_y = mid_rm_y * SCALE
        for ev in page_events:
            x, y, w, h = ev["bbox"]
            if x <= pdf_x <= x+w and y <= pdf_y <= y+h:
                matched.append({"id": ev["id"], "title": ev["title"]})
                # Resolve org/member from token, fire delete
                from db import get_device_token
                device = get_device_token(token)
                if not device and token == "rmpp-coat-001":
                    device = {"org_slug": "cross-formed-kids", "member_id": 2}
                if not device:
                    errors.append(f"unknown token {token}")
                    break
                try:
                    delete_event(device["org_slug"], device["member_id"], ev["id"])
                    deleted.append(ev["id"])
                    _hyperpaper_cache.pop(token, None)  # invalidate so next /pdf regens
                except Exception as e:
                    errors.append(f"{ev['id']}: {e}")
                break

    _record_activity(token, last_strike=True,
                     last_action=f"strike: matched={len(matched)} deleted={len(deleted)}")
    return jsonify({
        "page_idx": page_idx,
        "total_strokes": len(strokes),
        "new_strokes_checked": len(new_strokes),
        "matched": matched,
        "deleted": deleted,
        "errors": errors,
    })


@app.route("/api/hyperpaper/heartbeat", methods=["POST"])
def hyperpaper_heartbeat():
    """Device pings here on every watcher cycle. /health surfaces last seen.

    POST /api/hyperpaper/heartbeat?token=xxx
    Optional JSON body: {"note": "..."} appended to last_action.
    """
    token = request.args.get("token", "")
    if not token:
        return jsonify({"error": "token required"}), 400
    try:
        note = (json.loads(request.get_data() or b"{}") or {}).get("note", "")
    except Exception:
        note = ""
    _record_activity(token, last_heartbeat=True)
    if note:
        _record_activity(token, last_action=f"heartbeat: {note}")
    return jsonify({"ok": True})


def _utc_to_central(iso_str):
    """Convert ISO UTC timestamp (with Z suffix) to friendly Central-time string."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.rstrip("Z")).replace(tzinfo=pytz.UTC)
        ct = dt.astimezone(pytz.timezone("US/Central"))
        return ct.strftime("%-I:%M:%S %p %b %-d (%Z)")
    except Exception:
        return iso_str


@app.route("/api/hyperpaper/health")
def hyperpaper_health():
    """Read-only status. Hit this to see if the watcher is alive without SSH.

    GET /api/hyperpaper/health?token=xxx
    """
    token = request.args.get("token", "")
    if not token:
        return jsonify({"error": "token required"}), 400
    activity = _hyperpaper_activity.get(token, {})
    has_content = token in _hyperpaper_content_cache
    has_manifest = token in _hyperpaper_cache and "manifest" in _hyperpaper_cache[token]
    fresh_seconds = None
    if activity.get("last_heartbeat"):
        try:
            last = datetime.fromisoformat(activity["last_heartbeat"].rstrip("Z"))
            fresh_seconds = int((datetime.utcnow() - last).total_seconds())
        except Exception:
            pass
    activity_local = {
        k: _utc_to_central(v) if k.startswith("last_") and k != "last_action" else v
        for k, v in activity.items()
    }
    return jsonify({
        "token": token,
        "activity": activity,
        "activity_local": activity_local,
        "heartbeat_age_seconds": fresh_seconds,
        "content_cached": has_content,
        "manifest_cached": has_manifest,
    })


@app.route("/hyperpaper/status")
def hyperpaper_status_dashboard():
    """CFK-branded dashboard for the strike-watcher pipeline.

    GET /hyperpaper/status?token=rmpp-coat-001
    """
    token = request.args.get("token", "rmpp-coat-001")
    return render_template("hyperpaper_status.html", token=token)


@app.route("/api/hyperpaper/event/delete", methods=["POST"])
def hyperpaper_event_delete():
    """Device-initiated delete of a calendar event. Triggered by strikethrough gesture.

    POST /api/hyperpaper/event/delete?token=xxx&id=<google_event_id>
    """
    token = request.args.get("token", "")
    event_id = request.args.get("id", "")
    if not token or not event_id:
        return jsonify({"error": "token and id required"}), 400

    from db import get_device_token
    device = get_device_token(token)
    if not device:
        if token == "rmpp-coat-001":
            device = {"org_slug": "cross-formed-kids", "member_id": 2}
        else:
            return jsonify({"error": "unknown device"}), 404

    try:
        delete_event(device["org_slug"], device["member_id"], event_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Invalidate manifest cache so next pull regenerates
    _hyperpaper_cache.pop(token, None)
    return jsonify({"ok": True, "deleted": event_id})


@app.route("/api/hyperpaper/manifest")
def hyperpaper_manifest():
    """Device pulls the event-bbox manifest matching the current PDF.

    Returns {"page_w", "page_h", "hash", "pages": {"<page>": [{id, title, time, bbox}, ...]}}
    Listener uses this to resolve a strikethrough gesture's coordinates to a calendar event ID.
    """
    token = request.args.get("token", "")
    if not token:
        return jsonify({"error": "token required"}), 400
    cached = _hyperpaper_cache.get(token)
    if not cached or "manifest" not in cached:
        return jsonify({"error": "no manifest yet — fetch /api/hyperpaper/pdf first"}), 404
    return jsonify(cached["manifest"])


@app.route("/api/planner/pdf")
def planner_pdf():
    """Device pulls its planner PDF from here.

    GET /api/planner/pdf?token=xxx          — download PDF
    GET /api/planner/pdf?token=xxx&check=1  — just check hash (lightweight)
    """
    api_key = os.environ.get("CALENDAR_API_KEY", "")
    token = request.args.get("token", "")
    if not token:
        return jsonify({"error": "token required"}), 400

    # Look up token in DB
    from db import get_device_token
    device = get_device_token(token)
    if not device:
        # Legacy fallback for existing device
        if token == "rmpp-coat-001":
            device = {"org_slug": "cross-formed-kids", "member_id": 2}
        else:
            return jsonify({"error": "unknown device"}), 404
    org_slug, member_id = device["org_slug"], device["member_id"]

    # Get current week (Sunday start)
    today = datetime.utcnow().date()
    week_start = today - timedelta(days=(today.weekday() + 1) % 7)
    week_end = week_start + timedelta(days=8)

    # Fetch events
    auth_failed = False
    raw_events = []
    try:
        raw_events = list_events(org_slug, member_id,
                                 week_start.isoformat() + "T00:00:00Z",
                                 week_end.isoformat() + "T00:00:00Z", 100)
    except Exception as e:
        if "invalid_grant" in str(e) or "RefreshError" in type(e).__name__:
            auth_failed = True
        else:
            return jsonify({"error": str(e)}), 500

    # Convert to planner format
    events = {}
    for ev in raw_events:
        s = ev.get("start", "")
        if not s or "T" not in s:
            continue
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            hr, mn = d.hour, d.minute
            ampm = "a" if hr < 12 else "p"
            h12 = hr if hr <= 12 else hr - 12
            if h12 == 0: h12 = 12
            t = "%d:%02d%s" % (h12, mn, ampm) if mn else "%d%s" % (h12, ampm)
            sort_key = d.hour * 60 + d.minute
            events.setdefault((d.year, d.month, d.day), []).append((sort_key, t, ev.get("summary", "")))
        except:
            continue
    # Sort by actual time, then strip sort key
    for k in events:
        events[k].sort()
        events[k] = [(t, title) for _, t, title in events[k]]

    if auth_failed:
        today = datetime.utcnow().date()
        events[(today.year, today.month, today.day)] = [("9a", "[!] Check google token")]

    h = planner_events_hash(events)

    # Check-only mode
    if request.args.get("check"):
        return jsonify({"hash": h})

    # Return cached PDF if hash matches
    cached = _planner_cache.get(token)
    if cached and cached["hash"] == h:
        from flask import Response
        return Response(cached["pdf"], mimetype="application/pdf",
                       headers={"X-Planner-Hash": h})

    # Generate fresh PDF
    pdf_bytes = generate_planner(events, week_start)
    _planner_cache[token] = {"hash": h, "pdf": pdf_bytes}

    from flask import Response
    return Response(pdf_bytes, mimetype="application/pdf",
                   headers={"X-Planner-Hash": h})


# ── Planner Dashboard ──

@app.route("/planner")
def planner_dashboard():
    """User dashboard — see token, connect calendar, get setup script."""
    from db import get_device_token, create_device_token, get_member_tokens

    # For now, hardcode to CFK org member 2 (patron).
    # In production: auth → lookup user → show their dashboard.
    org_slug = "cross-formed-kids"
    member_id = 2

    tokens = get_member_tokens(org_slug, member_id)
    cal_connected = is_member_authorized(org_slug, member_id)

    html = """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CFK Living Planner</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',sans-serif;background:#f8f9fa;color:#323741;min-height:100vh}
.bar{width:100%%;height:6px;background:linear-gradient(90deg,#233255,#3764B4,#009B9C,#78B450,#EBD24B,#E19B37,#D76E69,#8CCDD7)}
.container{max-width:600px;margin:0 auto;padding:40px 24px}
h1{font-size:28px;font-weight:700;color:#233255;margin-bottom:4px}
.sub{font-size:11px;letter-spacing:3px;color:#233255;text-transform:uppercase;margin-bottom:32px}
.card{background:white;border-radius:12px;padding:24px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,0.1)}
.card h2{font-size:16px;color:#233255;margin-bottom:12px}
.status{display:inline-block;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:600}
.green{background:#e8f8ea;color:#1a6b2a}
.red{background:#fde8e8;color:#991b1b}
.token{font-family:monospace;font-size:18px;background:#f0f2f5;padding:12px 16px;border-radius:8px;margin:12px 0;word-break:break-all}
code{background:#1a1a2e;color:#0f0;padding:16px;border-radius:8px;display:block;font-size:13px;margin:12px 0;white-space:pre-wrap;word-break:break-all}
.btn{display:inline-block;padding:12px 24px;background:linear-gradient(135deg,#233255,#009B9C);color:white;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;text-decoration:none;margin:8px 4px 8px 0}
.btn:hover{opacity:0.9}
.btn.secondary{background:#eee;color:#333}
</style>
</head><body>
<div class="bar"></div>
<div class="container">
<h1>Living Planner</h1>
<p class="sub">Cross Formed Kids</p>

<div class="card">
<h2>Google Calendar</h2>
<span class="status """+("green" if cal_connected else "red")+"""">"""+("Connected" if cal_connected else "Not Connected")+"""</span>
"""+("" if cal_connected else '<br><a class="btn" href="/cross-formed-kids/google-auth/2" style="margin-top:12px">Connect Calendar</a>')+"""
</div>

<div class="card">
<h2>Your Device Token</h2>
"""
    if tokens:
        html += '<div class="token">%s</div>' % tokens[0]["token"]
    else:
        html += '<p>No device registered yet.</p><a class="btn" href="/planner/new-token">Create Token</a>'

    html += """
</div>

<div class="card">
<h2>Device Setup</h2>
<p style="margin-bottom:12px">Plug your reMarkable into USB and paste this into Terminal:</p>
<code>curl -sL https://"""+request.host+"""/planner/setup?token="""+((tokens[0]["token"]) if tokens else "YOUR_TOKEN")+""" | bash</code>
</div>

<div class="card">
<h2>How It Works</h2>
<ol style="padding-left:20px;line-height:2">
<li>Connect your Google Calendar above</li>
<li>Run the setup command on your reMarkable (one time)</li>
<li>Your planner updates automatically every 5 minutes</li>
<li>Close and reopen the planner to see new events</li>
</ol>
</div>

</div></body></html>"""
    return html


@app.route("/planner/seed-token", methods=["POST"])
def planner_seed_token():
    """Seed a specific token (admin use)."""
    api_key = os.environ.get("CALENDAR_API_KEY", "")
    if api_key and request.args.get("key") != api_key:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    if not data or not data.get("token"):
        return jsonify({"error": "token required"}), 400
    from db import get_db
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO device_tokens (token, org_slug, member_id, device_name) VALUES (?, ?, ?, ?)",
                 (data["token"], data.get("org_slug", ""), data.get("member_id", 0), data.get("device_name", "")))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "token": data["token"]})


@app.route("/planner/new-token")
def planner_create_token():
    from db import create_device_token
    token = create_device_token("cross-formed-kids", 2, "reMarkable")
    return redirect("/planner")


@app.route("/planner/setup")
def planner_setup_script():
    """Returns a bash setup script for the device."""
    token = request.args.get("token", "")
    server = "https://" + request.host

    script = """#!/bin/bash
# CFK Living Planner — reMarkable Setup
# Run this with your reMarkable connected via USB

set -e
PASS="$1"
if [ -z "$PASS" ]; then
    echo "Usage: curl -sL URL | bash -s YOUR_DEVICE_PASSWORD"
    echo ""
    echo "Find your password: Settings > General > Help > Copyrights > scroll to bottom"
    exit 1
fi

HOST="10.11.99.1"
echo "Connecting to reMarkable..."
sshpass -p "$PASS" ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$HOST 'echo OK' || {
    echo "Cannot connect. Is your reMarkable plugged in via USB?"
    exit 1
}

echo "Installing planner service..."
sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$HOST '
mkdir -p /home/root/tutor

cat > /home/root/tutor/pull.sh << "PULLSCRIPT"
#!/bin/sh
SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
export SSL_CERT_FILE
URL="SERVER_URL/api/planner/pdf?token=DEVICE_TOKEN"
DOCDIR="/home/root/.local/share/remarkable/xochitl"
UUID="cfk-planner-live-001"
PDF="$DOCDIR/$UUID.pdf"
TMP="$PDF.tmp"
while true; do
    WIFI_IP=$(ip addr show wlan0 2>/dev/null | grep "inet " | awk "{print \\$2}" | cut -d/ -f1)
    if [ -n "$WIFI_IP" ]; then
        ps | grep "dropbear.*2222" | grep -v grep > /dev/null 2>&1 || dropbear -p ${WIFI_IP}:2222 -R 2>/dev/null
    fi
    wget -q -T 15 -O "$TMP" "$URL" 2>/dev/null
    if [ -f "$TMP" ] && [ $(wc -c < "$TMP") -gt 5000 ]; then
        if ! cmp -s "$TMP" "$PDF" 2>/dev/null; then
            mv "$TMP" "$PDF"
            rm -rf "$DOCDIR/$UUID.thumbnails" "$DOCDIR/$UUID.cache"
        else
            rm -f "$TMP"
        fi
    else
        rm -f "$TMP"
    fi
    sleep 300
done
PULLSCRIPT
chmod +x /home/root/tutor/pull.sh

cat > /etc/systemd/system/cfk-pull.service << "SVCFILE"
[Unit]
Description=CFK Planner Pull
After=network-online.target xochitl.service
Wants=network-online.target
[Service]
Type=simple
ExecStart=/home/root/tutor/pull.sh
Restart=always
RestartSec=60
[Install]
WantedBy=multi-user.target
SVCFILE

systemctl daemon-reload
systemctl enable cfk-pull.service
systemctl start cfk-pull.service
'

echo ""
echo "Done! Your planner will appear within 5 minutes."
echo "Unplug your reMarkable and enjoy."
""".replace("SERVER_URL", server).replace("DEVICE_TOKEN", token)

    return script, 200, {"Content-Type": "text/plain"}


# ── Legacy injection queue (kept for compatibility) ──

# In-memory queue per device. In production, this would be a DB table.
# Format: {device_id: [{"id": str, "page": int, "x": int, "y": int, "text": str, "scale": int}, ...]}
_inject_queues = {}
_injected_ids = set()  # track what's been confirmed


@app.route("/api/planner/sync", methods=["POST"])
def planner_sync():
    """Called by Multi Calendar internally when calendar changes.
    Computes injection commands for a planner layout and queues them.

    Body: {"device_id": str, "org_slug": str, "member_id": int,
           "week_start": "2026-04-26", "planner_layout": {...}}
    """
    api_key = os.environ.get("CALENDAR_API_KEY", "")
    if api_key and request.args.get("key") != api_key:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {api_key}":
            return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    device_id = data.get("device_id", "default")
    org_slug = data.get("org_slug", "cross-formed-kids")
    member_id = data.get("member_id", 2)
    week_start = data.get("week_start", "")

    if not week_start:
        return jsonify({"error": "week_start required"}), 400

    # Fetch events for the week
    from datetime import datetime, timedelta
    ws = datetime.fromisoformat(week_start)
    we = ws + timedelta(days=7)

    try:
        events = list_events(org_slug, member_id,
                            ws.isoformat() + "Z", we.isoformat() + "Z", 100)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Build injection commands based on planner layout
    layout = data.get("planner_layout", {})
    # layout: {"pages": {day_offset: page_num}, "time_slots": {hour: y_position}, "x": inject_x}
    pages = layout.get("pages", {})
    time_slots = layout.get("time_slots", {})
    inject_x = layout.get("x", 1500)
    scale = layout.get("scale", 120)
    spacing = layout.get("spacing", 28)

    commands = []
    for ev in events:
        start = ev.get("start", "")
        if not start or "T" not in start:
            continue
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            day_offset = (dt.date() - ws.date()).days
            page = pages.get(str(day_offset))
            if page is None:
                continue
            hour = dt.hour + dt.minute / 60.0
            # Find nearest time slot
            best_y = None
            best_dist = 999
            for h_str, y in time_slots.items():
                dist = abs(float(h_str) - hour)
                if dist < best_dist:
                    best_dist = dist
                    best_y = y
            if best_y is None:
                continue

            cmd_id = "%s-%s-%s" % (device_id, ev.get("id", ""), start)
            if cmd_id in _injected_ids:
                continue

            title = ev.get("summary", "")
            # Clean for stroke font
            clean = ''.join(c for c in title if c.isalnum() or c in " .,-!?':;/()+= ")

            commands.append({
                "id": cmd_id,
                "page": page,
                "x": inject_x,
                "y": best_y,
                "text": clean,
                "scale": scale,
                "spacing": spacing,
            })
        except:
            continue

    _inject_queues[device_id] = commands
    return jsonify({"queued": len(commands)})


@app.route("/api/planner/poll")
def planner_poll():
    """Device polls this endpoint for pending injections.

    GET /api/planner/poll?device_id=xxx&key=xxx
    Returns: {"items": [...]} or {"items": []} if nothing pending.
    """
    api_key = os.environ.get("CALENDAR_API_KEY", "")
    if api_key and request.args.get("key") != api_key:
        return jsonify({"error": "Unauthorized"}), 401

    device_id = request.args.get("device_id", "default")
    items = _inject_queues.get(device_id, [])
    return jsonify({"items": items})


@app.route("/api/planner/confirm", methods=["POST"])
def planner_confirm():
    """Device confirms injection of specific items.

    POST body: {"device_id": str, "ids": [str, ...]}
    """
    api_key = os.environ.get("CALENDAR_API_KEY", "")
    if api_key and request.args.get("key") != api_key:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {api_key}":
            return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    device_id = data.get("device_id", "default")
    confirmed = data.get("ids", [])

    _injected_ids.update(confirmed)

    # Remove confirmed items from queue
    if device_id in _inject_queues:
        _inject_queues[device_id] = [
            item for item in _inject_queues[device_id]
            if item["id"] not in confirmed
        ]

    return jsonify({"confirmed": len(confirmed)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
