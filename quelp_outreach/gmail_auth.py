"""
Shared Gmail auth for every sender script.

Asks for gmail.send + gmail.readonly ONCE, so send.py / list_send.py /
followup.py all share one token.json and never overwrite each other's scopes.
readonly is needed for reply/bounce detection and threading headers.

Token sources, in order:
  1. token.json (cached after first consent)
  2. GOOGLE_REFRESH_TOKEN in .env (headless machines / CI)
  3. Browser consent on http://localhost:8080
"""

import base64
import sys
from email.mime.text import MIMEText
from email.utils import formataddr

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import (
    GMAIL_TOKEN_PATH,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_REFRESH_TOKEN,
    SENDER_NAME,
)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

_TOKEN_URI = "https://oauth2.googleapis.com/token"

_CLIENT_CONFIG = {
    "installed": {
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
        "token_uri":     _TOKEN_URI,
        "redirect_uris": ["http://localhost:8080"],
    }
}

_service = None


def _load_cached() -> Credentials | None:
    if GMAIL_TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_PATH))
        # Old tokens from the send-only era lack readonly → force re-consent.
        if creds and creds.has_scopes(SCOPES):
            return creds
        print("[auth] Cached token is missing a scope — re-consenting once.")
        return None
    if GOOGLE_REFRESH_TOKEN:
        return Credentials(
            token=None,
            refresh_token=GOOGLE_REFRESH_TOKEN,
            token_uri=_TOKEN_URI,
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=SCOPES,
        )
    return None


def get_gmail_service():
    """Return an authenticated Gmail API service (cached per process)."""
    global _service
    if _service is not None:
        return _service

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        sys.exit(
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in .env.\n"
            "Create OAuth credentials at console.cloud.google.com → APIs & Services → Credentials."
        )

    creds = _load_cached()

    if creds and not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            print(f"[auth] Token refresh failed ({e}) — re-consenting.")
            creds = None

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_config(_CLIENT_CONFIG, SCOPES)
        creds = flow.run_local_server(port=8080, access_type="offline", prompt="consent")

    GMAIL_TOKEN_PATH.write_text(creds.to_json())
    _service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _service


def get_rfc_message_id(service, gmail_id: str) -> str:
    """
    Gmail API ids are NOT RFC 2822 Message-IDs. Threading headers
    (In-Reply-To / References) need the real <...@mail.gmail.com> value,
    otherwise the recipient's mail client won't thread the follow-up.
    """
    if not gmail_id:
        return ""
    try:
        msg = service.users().messages().get(
            userId="me", id=gmail_id, format="metadata",
            metadataHeaders=["Message-ID", "Message-Id"],
        ).execute()
        for h in msg.get("payload", {}).get("headers", []):
            if h.get("name", "").lower() == "message-id":
                return h.get("value", "")
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

_sender_address: str | None = None


def sender_address(service) -> str:
    """The authenticated account's address, e.g. adhitya@quelp.co.in."""
    global _sender_address
    if _sender_address is None:
        _sender_address = service.users().getProfile(userId="me").execute()["emailAddress"]
    return _sender_address


def build_raw(service, to: str, subject: str, body: str,
              in_reply_to: str = "") -> str:
    frm = sender_address(service)
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to
    msg["From"] = formataddr((SENDER_NAME, frm))
    msg["Subject"] = subject
    # One-click-style opt-out for mail clients; helps deliverability.
    msg["List-Unsubscribe"] = f"<mailto:{frm}?subject=unsubscribe>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def send_email(service, to: str, subject: str, body: str,
               thread_id: str = "", in_reply_to: str = "") -> tuple[str, str, str]:
    """
    Send one email. Returns (gmail_message_id, thread_id, rfc_message_id).
    Pass thread_id + in_reply_to to reply inside an existing thread.
    """
    payload = {"raw": build_raw(service, to, subject, body, in_reply_to)}
    if thread_id:
        payload["threadId"] = thread_id
    result = service.users().messages().send(userId="me", body=payload).execute()
    gid = result.get("id", "")
    return gid, result.get("threadId", ""), get_rfc_message_id(service, gid)
