"""
Shared Gmail auth for every sender script — one or many inboxes.

Asks for gmail.send + gmail.readonly ONCE per inbox, so send.py / list_send.py /
followup.py share tokens and never overwrite each other's scopes.
readonly is needed for reply/bounce detection and threading headers.

Default inbox (inbox=""), token sources in order:
  1. token.json (cached after first consent)
  2. GOOGLE_REFRESH_TOKEN in .env (headless machines / CI)
  3. Browser consent on http://localhost:8080

Named inboxes (INBOXES in .env) use tokens/<address>.json, created with:
  python inbox.py add
If a named inbox has no token file but token.json belongs to that same
address, token.json is used — so the original account keeps working.
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
    SENDER_DISPLAY_NAME,
    TOKENS_DIR,
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

_services: dict[str, object] = {}     # inbox address ("" = default) → service
_addresses: dict[int, str] = {}       # id(service) → authenticated address


class InboxNotConnected(Exception):
    """A named inbox has no token yet — run `python inbox.py add`."""


def _require_client() -> None:
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        sys.exit(
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in .env.\n"
            "Create OAuth credentials at console.cloud.google.com → APIs & Services → Credentials."
        )


def token_path(address: str):
    return TOKENS_DIR / f"{address.strip().lower()}.json"


def _load_file(path) -> Credentials | None:
    if not path.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(path))
    # Old tokens from the send-only era lack readonly → force re-consent.
    if creds and creds.has_scopes(SCOPES):
        return creds
    print(f"[auth] {path.name} is missing a scope — re-consenting once.")
    return None


def _refresh(creds: Credentials | None) -> Credentials | None:
    if creds and not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            print(f"[auth] Token refresh failed ({e}) — re-consenting.")
            return None
    return creds if creds and creds.valid else None


def _consent() -> Credentials:
    flow = InstalledAppFlow.from_client_config(_CLIENT_CONFIG, SCOPES)
    return flow.run_local_server(port=8080, access_type="offline", prompt="consent select_account")


def _build(creds: Credentials):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _default_service():
    creds = _load_file(GMAIL_TOKEN_PATH)
    if creds is None and GOOGLE_REFRESH_TOKEN and not GMAIL_TOKEN_PATH.exists():
        creds = Credentials(
            token=None, refresh_token=GOOGLE_REFRESH_TOKEN, token_uri=_TOKEN_URI,
            client_id=GOOGLE_CLIENT_ID, client_secret=GOOGLE_CLIENT_SECRET, scopes=SCOPES,
        )
    creds = _refresh(creds) or _consent()
    GMAIL_TOKEN_PATH.write_text(creds.to_json())
    return _build(creds)


def get_gmail_service(inbox: str = ""):
    """
    Return an authenticated Gmail API service (cached per process).
    inbox="" → the default account (token.json). Otherwise the named inbox;
    raises InboxNotConnected if it hasn't been added yet.
    """
    key = inbox.strip().lower()
    if key in _services:
        return _services[key]
    _require_client()

    if not key:
        service = _default_service()
    else:
        path = token_path(key)
        creds = _refresh(_load_file(path))
        if creds:
            path.write_text(creds.to_json())
            service = _build(creds)
        elif GMAIL_TOKEN_PATH.exists() and sender_address(get_gmail_service("")).lower() == key:
            service = get_gmail_service("")
        else:
            raise InboxNotConnected(
                f"Inbox {key} isn't connected. Run: python inbox.py add  (sign in as {key})"
            )
        actual = sender_address(service).lower()
        if actual != key:
            sys.exit(f"Token for {key} actually belongs to {actual}. "
                     f"Delete {path} and run: python inbox.py add")

    _services[key] = service
    return service


def add_inbox() -> str:
    """Browser consent for a new inbox; saves tokens/<address>.json. Returns the address."""
    _require_client()
    creds = _consent()
    address = _build(creds).users().getProfile(userId="me").execute()["emailAddress"].lower()
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    token_path(address).write_text(creds.to_json())
    _services.pop(address, None)
    return address


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

def sender_address(service) -> str:
    """The authenticated account's address, e.g. adhitya@quelp.co.in."""
    sid = id(service)
    if sid not in _addresses:
        _addresses[sid] = service.users().getProfile(userId="me").execute()["emailAddress"]
    return _addresses[sid]


def build_raw(service, to: str, subject: str, body: str,
              in_reply_to: str = "") -> str:
    frm = sender_address(service)
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to
    msg["From"] = formataddr((SENDER_DISPLAY_NAME, frm))
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
