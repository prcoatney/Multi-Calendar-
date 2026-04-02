"""Google Calendar API integration for creating events on founder calendars."""

import os
import json
from datetime import datetime
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TOKEN_DIR = os.path.join(os.path.dirname(__file__), "tokens")

# Ensure token directory exists
os.makedirs(TOKEN_DIR, exist_ok=True)


def get_flow(redirect_uri: str) -> Flow:
    """Create an OAuth flow from the client credentials file."""
    credentials_path = os.path.join(os.path.dirname(__file__), "credentials.json")
    if not os.path.exists(credentials_path):
        raise FileNotFoundError(
            "credentials.json not found. Download it from Google Cloud Console "
            "(APIs & Services > Credentials > OAuth 2.0 Client ID > Download JSON) "
            "and place it in the project root."
        )
    flow = Flow.from_client_secrets_file(
        credentials_path,
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    return flow


def get_auth_url(founder_id: str, redirect_uri: str) -> str:
    """Generate an OAuth authorization URL for a founder."""
    flow = get_flow(redirect_uri)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=founder_id,
    )
    return auth_url


def handle_oauth_callback(code: str, founder_id: str, redirect_uri: str) -> None:
    """Exchange the auth code for tokens and save them."""
    flow = get_flow(redirect_uri)
    flow.fetch_token(code=code)
    creds = flow.credentials
    token_path = os.path.join(TOKEN_DIR, f"token_{founder_id}.json")
    with open(token_path, "w") as f:
        f.write(creds.to_json())


def get_credentials(founder_id: str) -> Credentials | None:
    """Load saved credentials for a founder, refreshing if needed."""
    token_path = os.path.join(TOKEN_DIR, f"token_{founder_id}.json")
    if not os.path.exists(token_path):
        return None

    creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return creds


def is_founder_authorized(founder_id: str) -> bool:
    """Check if a founder has valid OAuth credentials."""
    creds = get_credentials(founder_id)
    return creds is not None and creds.valid


def create_event(
    founder_id: str,
    summary: str,
    start_time: str,
    end_time: str,
    description: str = "",
    attendee_emails: list[str] | None = None,
) -> dict:
    """Create a calendar event for a single founder.

    Args:
        founder_id: The founder identifier (e.g., "founder1").
        summary: Event title.
        start_time: ISO 8601 datetime string.
        end_time: ISO 8601 datetime string.
        description: Optional event description.
        attendee_emails: Optional list of attendee email addresses.

    Returns:
        The created event resource from the Google Calendar API.
    """
    creds = get_credentials(founder_id)
    if not creds:
        raise ValueError(f"Founder '{founder_id}' has not authorized Google Calendar access.")

    service = build("calendar", "v3", credentials=creds)

    event_body = {
        "summary": summary,
        "description": description,
        "start": {
            "dateTime": start_time,
        },
        "end": {
            "dateTime": end_time,
        },
    }

    if attendee_emails:
        event_body["attendees"] = [{"email": e} for e in attendee_emails]

    event = service.events().insert(calendarId="primary", body=event_body).execute()
    return event


def create_event_all_founders(
    founder_ids: list[str],
    summary: str,
    start_time: str,
    end_time: str,
    description: str = "",
) -> list[dict]:
    """Create the same event on all founders' calendars.

    Returns a list of created event resources.
    """
    results = []
    for fid in founder_ids:
        result = create_event(fid, summary, start_time, end_time, description)
        results.append({"founder_id": fid, "event": result})
    return results
