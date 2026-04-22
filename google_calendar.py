"""Google Calendar API integration for creating events on member calendars."""

import os
import json
from datetime import datetime
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

# Store tokens next to the database so they live on the same persistent volume.
# On Railway the working dir is ephemeral — anything under __file__ gets wiped
# on redeploy, forcing every member to re-authorize. DATABASE_PATH points at
# the mounted volume, so tokens kept alongside it survive.
_DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "dominion.db"))
TOKEN_DIR = os.path.join(os.path.dirname(_DB_PATH) or ".", "tokens")
os.makedirs(TOKEN_DIR, exist_ok=True)

# One-time migration: copy tokens from the old in-repo location onto the volume
# if the volume has none yet. Safe to run every boot — it only copies missing files.
_LEGACY_TOKEN_DIR = os.path.join(os.path.dirname(__file__), "tokens")
if _LEGACY_TOKEN_DIR != TOKEN_DIR and os.path.isdir(_LEGACY_TOKEN_DIR):
    for _fname in os.listdir(_LEGACY_TOKEN_DIR):
        if not _fname.startswith("token_") or not _fname.endswith(".json"):
            continue
        _src = os.path.join(_LEGACY_TOKEN_DIR, _fname)
        _dst = os.path.join(TOKEN_DIR, _fname)
        if os.path.exists(_dst):
            continue
        try:
            with open(_src, "r") as _f:
                _data = _f.read()
            with open(_dst, "w") as _f:
                _f.write(_data)
            print(f"[tokens] Migrated {_fname} from legacy dir to volume")
        except Exception as _e:
            print(f"[tokens] Migration failed for {_fname}: {_e}")


def _get_credentials_path() -> str:
    """Get or create credentials.json from env var or local file."""
    local_path = os.path.join(os.path.dirname(__file__), "credentials.json")
    if os.path.exists(local_path):
        return local_path

    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        tmp = os.path.join(os.path.dirname(__file__), ".credentials_tmp.json")
        with open(tmp, "w") as f:
            f.write(creds_json)
        return tmp

    raise FileNotFoundError(
        "credentials.json not found and GOOGLE_CREDENTIALS_JSON env var not set. "
        "Download credentials from Google Cloud Console or set the env var."
    )


def _token_path(org_slug: str, member_id: int) -> str:
    """Return the token file path for an org member."""
    return os.path.join(TOKEN_DIR, f"token_{org_slug}_{member_id}.json")


def _migrate_old_tokens(org_slug: str, member_id: int, old_founder_id: str) -> None:
    """Migrate old-style token files (token_founder1.json) to new naming."""
    old_path = os.path.join(TOKEN_DIR, f"token_{old_founder_id}.json")
    new_path = _token_path(org_slug, member_id)
    if os.path.exists(old_path) and not os.path.exists(new_path):
        os.rename(old_path, new_path)


def get_flow(redirect_uri: str) -> Flow:
    """Create an OAuth flow from the client credentials file."""
    credentials_path = _get_credentials_path()
    flow = Flow.from_client_secrets_file(
        credentials_path,
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    return flow


def encode_state(org_slug: str, member_id: int) -> str:
    """Encode org slug and member ID into an OAuth state string."""
    return f"{org_slug}:{member_id}"


def decode_state(state: str) -> tuple[str, int]:
    """Decode an OAuth state string back to org slug and member ID."""
    parts = state.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid OAuth state: {state}")
    return parts[0], int(parts[1])


def get_auth_url(org_slug: str, member_id: int, redirect_uri: str) -> str:
    """Generate an OAuth authorization URL for a member."""
    flow = get_flow(redirect_uri)
    state = encode_state(org_slug, member_id)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
    )
    return auth_url


def handle_oauth_callback(code: str, org_slug: str, member_id: int, redirect_uri: str) -> None:
    """Exchange the auth code for tokens and save them."""
    flow = get_flow(redirect_uri)
    flow.fetch_token(code=code)
    creds = flow.credentials
    path = _token_path(org_slug, member_id)
    with open(path, "w") as f:
        f.write(creds.to_json())


def get_credentials(org_slug: str, member_id: int) -> Credentials | None:
    """Load saved credentials for a member, refreshing if needed."""
    path = _token_path(org_slug, member_id)
    if not os.path.exists(path):
        return None

    creds = Credentials.from_authorized_user_file(path, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(path, "w") as f:
            f.write(creds.to_json())
    return creds


def is_member_authorized(org_slug: str, member_id: int) -> bool:
    """Check if a member has valid OAuth credentials."""
    creds = get_credentials(org_slug, member_id)
    return creds is not None and creds.valid


def create_event(
    org_slug: str,
    member_id: int,
    summary: str,
    start_time: str,
    end_time: str,
    description: str = "",
    attendee_emails: list[str] | None = None,
    add_meet_link: bool = False,
    conference_request_id: str | None = None,
    send_updates: str = "none",
) -> dict:
    """Create a calendar event for a single member.

    send_updates: "all" emails attendees the invite, "none" is silent (default).
    """
    creds = get_credentials(org_slug, member_id)
    if not creds:
        raise ValueError(f"Member {member_id} in '{org_slug}' has not authorized Google Calendar access.")

    service = build("calendar", "v3", credentials=creds)

    event_body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_time},
        "end": {"dateTime": end_time},
    }

    if attendee_emails:
        event_body["attendees"] = [{"email": e} for e in attendee_emails]

    insert_kwargs = {"calendarId": "primary", "body": event_body, "sendUpdates": send_updates}

    if add_meet_link:
        # Reuse the same requestId across members so they all land on one Meet room.
        req_id = conference_request_id or f"meet-{org_slug}-{int(datetime.utcnow().timestamp())}"
        event_body["conferenceData"] = {
            "createRequest": {
                "requestId": req_id,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        insert_kwargs["conferenceDataVersion"] = 1

    event = service.events().insert(**insert_kwargs).execute()
    return event


def create_event_all_members(
    org_slug: str,
    member_ids: list[int],
    summary: str,
    start_time: str,
    end_time: str,
    description: str = "",
    add_meet_link: bool = False,
) -> list[dict]:
    """Create the same event on all members' calendars."""
    results = []
    # Shared request ID so every member's event resolves to the same Meet room.
    shared_req_id = f"meet-{org_slug}-{int(datetime.utcnow().timestamp())}" if add_meet_link else None
    for mid in member_ids:
        result = create_event(
            org_slug,
            mid,
            summary,
            start_time,
            end_time,
            description,
            add_meet_link=add_meet_link,
            conference_request_id=shared_req_id,
        )
        results.append({"member_id": mid, "event": result})
    return results
