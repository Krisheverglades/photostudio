"""
Google Calendar sync for the booking widget.

Flow:
  1. One-time OAuth: run `python -m app.calendar_utils` locally, which opens
     a browser, you log into the Google account whose calendar you want to
     use, and it saves a token.json (refresh token) next to this file.
  2. After that, get_busy_slots() and create_event() run silently using the
     saved token — no browser needed, including from the deployed server.

Requires GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env, from a Google
Cloud project with the Calendar API enabled
(console.cloud.google.com -> APIs & Services -> Credentials).
"""

import os
from datetime import datetime, timedelta

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TOKEN_PATH = os.path.join(os.path.dirname(__file__), "token.json")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")


def _get_credentials() -> Credentials | None:
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return creds


def run_oauth_flow() -> None:
    """One-time interactive setup — run this once from your own machine."""
    client_config = {
        "installed": {
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())
    print(f"Saved credentials to {TOKEN_PATH}")


def get_busy_slots(days_ahead: int = 30) -> list[dict]:
    """Returns busy time ranges from your calendar, for the booking widget to grey out."""
    creds = _get_credentials()
    if not creds:
        return []  # calendar not connected yet — booking widget just shows nothing blocked

    service = build("calendar", "v3", credentials=creds)
    now = datetime.utcnow()
    body = {
        "timeMin": now.isoformat() + "Z",
        "timeMax": (now + timedelta(days=days_ahead)).isoformat() + "Z",
        "items": [{"id": CALENDAR_ID}],
    }
    result = service.freebusy().query(body=body).execute()
    return result["calendars"][CALENDAR_ID]["busy"]


def create_event(summary: str, description: str, start: datetime, end: datetime) -> str | None:
    """Creates the booking on your Google Calendar. Returns the event ID, or None if not connected."""
    creds = _get_credentials()
    if not creds:
        print("[calendar_utils] Google Calendar not connected — skipping sync.")
        return None

    service = build("calendar", "v3", credentials=creds)
    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
    }
    created = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    return created.get("id")


if __name__ == "__main__":
    run_oauth_flow()
