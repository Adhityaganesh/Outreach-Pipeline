"""
Block 6 — Follow-up
Sends one follow-up to leads who were emailed but haven't replied after N days.

Modes (safest is default):
  --dry-run   (DEFAULT) Print who would get a follow-up and who is skipped. Sends nothing.
  --live      Send actual follow-ups. Requires typing "SEND".

Requires gmail.send + gmail.readonly scopes. On first run after Block 5 it will
re-consent because the readonly scope is new.
"""

import argparse
import base64
import csv
import os
import random
import sys
import time
import tempfile
import shutil
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import pandas as pd
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import (
    DATA_DIR,
    DAILY_SEND_CAP,
    GMAIL_TOKEN_PATH,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    SEND_DELAY_MIN,
    SEND_DELAY_MAX,
    SENDER_NAME,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

_SENT_LOG = DATA_DIR / "sent_log.csv"

FOLLOWUP_DAYS = int(os.getenv("FOLLOWUP_DAYS", "3"))

_CLIENT_CONFIG = {
    "installed": {
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
        "token_uri":     "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost:8080"],
    }
}

_FOLLOWUP_BODY = """\
Hi {first_name},

Quick nudge on this — I know inboxes get buried. Still happy to set Quelp up on your inbox for a free two-week trial whenever you have 15 minutes. No worries if it's not a fit.

{sender_name}\
"""

# ---------------------------------------------------------------------------
# Gmail auth — same token.json as Block 5, but now needs readonly too.
# If token.json has only gmail.send, it will re-consent automatically.
# ---------------------------------------------------------------------------

def _get_gmail_service():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        sys.exit(
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in .env."
        )

    creds: Credentials | None = None

    if GMAIL_TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_PATH), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None

        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_config(_CLIENT_CONFIG, _SCOPES)
            creds = flow.run_local_server(port=8080, access_type="offline", prompt="consent")

        GMAIL_TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Reply detection
# ---------------------------------------------------------------------------

def _has_reply(service, thread_id: str, sent_to: str) -> bool:
    """
    Return True if the thread contains any inbound message from sent_to.
    We look for messages whose From header contains the recipient's address.
    """
    try:
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="metadata",
            metadataHeaders=["From", "To"],
        ).execute()
    except HttpError as e:
        print(f"    [warn] Could not fetch thread {thread_id}: {e}")
        return False

    messages = thread.get("messages", [])
    if len(messages) <= 1:
        return False

    # Skip the first message (our outbound). Check the rest.
    for msg in messages[1:]:
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        from_header = headers.get("From", "").lower()
        if sent_to.lower() in from_header:
            return True

    return False


# ---------------------------------------------------------------------------
# Follow-up email building
# ---------------------------------------------------------------------------

def _build_followup_mime(
    to: str,
    original_subject: str,
    original_message_id: str,
    first_name: str,
) -> str:
    """Return a base64url-encoded follow-up threaded as a reply."""
    body = _FOLLOWUP_BODY.format(
        first_name=first_name or "there",
        sender_name=SENDER_NAME,
    )
    subject = f"Re: {original_subject}" if not original_subject.startswith("Re:") else original_subject

    msg = MIMEText(body, "plain", "utf-8")
    msg["To"]         = to
    msg["From"]       = f"{SENDER_NAME} <me>"
    msg["Subject"]    = subject
    msg["In-Reply-To"] = original_message_id
    msg["References"]  = original_message_id

    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


# ---------------------------------------------------------------------------
# Sent-log helpers
# ---------------------------------------------------------------------------

_LOG_FIELDS = [
    "email", "company", "name", "confidence",
    "sent_at", "gmail_message_id", "thread_id", "status",
]

# Optional columns added by this block
_EXTENDED_FIELDS = _LOG_FIELDS + ["subject", "followup_sent_at", "followup_message_id"]


def _load_log() -> pd.DataFrame:
    if not _SENT_LOG.exists():
        sys.exit(f"sent_log.csv not found at {_SENT_LOG}. Run Block 5 first.")
    df = pd.read_csv(_SENT_LOG)
    # Ensure optional columns exist
    for col in ["subject", "followup_sent_at", "followup_message_id"]:
        if col not in df.columns:
            df[col] = ""
    df["sent_at"] = pd.to_datetime(df["sent_at"], utc=True, errors="coerce")
    return df


def _save_log(df: pd.DataFrame) -> None:
    """Write df back to sent_log.csv atomically."""
    cols = [c for c in _EXTENDED_FIELDS if c in df.columns]
    tmp = tempfile.NamedTemporaryFile(
        mode="w", delete=False, suffix=".csv",
        dir=str(DATA_DIR), newline="", encoding="utf-8",
    )
    try:
        df[cols].to_csv(tmp, index=False)
        tmp.close()
        shutil.move(tmp.name, str(_SENT_LOG))
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _first_name(name: str) -> str:
    return str(name).split()[0] if name and str(name).strip() else "there"


def _classify_rows(df: pd.DataFrame, service, now: datetime):
    """
    Return three lists of index values:
      due       — status='sent', old enough, no reply detected
      replied   — status='sent' but reply found in thread
      skip      — everything else (already followed_up, test, error, too recent)
    """
    due, replied, skip = [], [], []

    cutoff = now - timedelta(days=FOLLOWUP_DAYS)

    for idx, row in df.iterrows():
        status = str(row.get("status", "")).strip()

        if status != "sent":
            skip.append(idx)
            continue

        sent_at = row["sent_at"]
        if pd.isna(sent_at) or sent_at > cutoff:
            skip.append(idx)
            continue

        thread_id = str(row.get("thread_id", "")).strip()
        email     = str(row.get("email", "")).strip()

        if not thread_id:
            skip.append(idx)
            continue

        if _has_reply(service, thread_id, email):
            replied.append(idx)
        else:
            due.append(idx)

    return due, replied, skip


# ---------------------------------------------------------------------------
# Mode: dry-run
# ---------------------------------------------------------------------------

def run_dry_run() -> None:
    df = _load_log()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=FOLLOWUP_DAYS)

    service = _get_gmail_service()

    print(f"\n{'='*65}")
    print(f"DRY RUN — follow-up pass (nothing will be sent)")
    print(f"Follow-up window: >= {FOLLOWUP_DAYS} days since sent_at")
    print(f"Cutoff: {cutoff.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*65}\n")

    due_idxs, replied_idxs, skip_idxs = _classify_rows(df, service, now)

    for idx in due_idxs:
        row = df.loc[idx]
        age = (now - row["sent_at"]).days
        print(f"[WOULD FOLLOW UP]  {row['email']}  ({row.get('name', '')})")
        print(f"  Company : {row.get('company', '')}")
        print(f"  Sent    : {row['sent_at'].strftime('%Y-%m-%d')}  ({age}d ago)")
        print(f"  Thread  : {row.get('thread_id', '')}")
        print()

    for idx in replied_idxs:
        row = df.loc[idx]
        print(f"[REPLIED — skip]   {row['email']}  ({row.get('name', '')})")

    for idx in skip_idxs:
        row = df.loc[idx]
        status = str(row.get("status", ""))
        sent_at = row["sent_at"]
        if pd.isna(sent_at):
            reason = "no sent_at"
        elif sent_at > cutoff:
            age = (now - sent_at).total_seconds() / 86400
            reason = f"too recent ({age:.1f}d < {FOLLOWUP_DAYS}d)"
        else:
            reason = f"status={status}"
        print(f"[SKIP]             {row['email']}  — {reason}")

    print(f"\n{'='*65}")
    print(f"Due for follow-up : {len(due_idxs)}")
    print(f"Already replied   : {len(replied_idxs)}")
    print(f"Skipped           : {len(skip_idxs)}")
    print(f"{'='*65}")

    # Update replied statuses in log (safe even in dry-run — just status updates)
    if replied_idxs:
        df.loc[replied_idxs, "status"] = "replied"
        _save_log(df)
        print(f"\nMarked {len(replied_idxs)} row(s) as 'replied' in sent_log.csv.")


# ---------------------------------------------------------------------------
# Mode: live
# ---------------------------------------------------------------------------

def run_live(cap: int) -> None:
    df = _load_log()
    now = datetime.now(timezone.utc)

    service = _get_gmail_service()

    due_idxs, replied_idxs, _ = _classify_rows(df, service, now)

    # Mark replies now
    if replied_idxs:
        df.loc[replied_idxs, "status"] = "replied"

    if not due_idxs:
        _save_log(df)
        print("No follow-ups due. Log updated.")
        return

    to_send = min(len(due_idxs), cap)

    print(f"\n{'='*65}")
    print(f"LIVE FOLLOW-UP SEND")
    print(f"  Due: {len(due_idxs)}  |  Cap: {cap}  |  Will send: {to_send}")
    print(f"{'='*65}")
    print("\nRecipients:")
    for idx in due_idxs[:to_send]:
        row = df.loc[idx]
        print(f"  {row['email']}  ({row.get('name', '')})  — {row.get('company', '')}")

    print()
    confirm = input("Type SEND to confirm, anything else to abort: ").strip()
    if confirm != "SEND":
        print("Aborted.")
        return

    sent = 0
    for i, idx in enumerate(due_idxs[:to_send]):
        row   = df.loc[idx]
        email = str(row["email"]).strip()
        name  = str(row.get("name", ""))
        subj  = str(row.get("subject", ""))
        msg_id_orig = str(row.get("gmail_message_id", ""))
        thread_id   = str(row.get("thread_id", ""))

        print(f"[{sent+1}/{to_send}] → {email}  ({name})")
        try:
            raw = _build_followup_mime(email, subj, msg_id_orig, _first_name(name))
            result = service.users().messages().send(
                userId="me",
                body={"raw": raw, "threadId": thread_id},
            ).execute()
            fu_msg_id = result.get("id", "")
            df.at[idx, "status"]             = "followed_up"
            df.at[idx, "followup_sent_at"]   = now.isoformat()
            df.at[idx, "followup_message_id"] = fu_msg_id
            print(f"  OK  message_id={fu_msg_id}")
            sent += 1
        except HttpError as e:
            print(f"  ERROR — {e}")

        is_last = (i == to_send - 1)
        if not is_last:
            delay = random.uniform(SEND_DELAY_MIN, SEND_DELAY_MAX)
            print(f"  Waiting {delay:.0f}s…")
            time.sleep(delay)

    _save_log(df)
    remaining = len(due_idxs) - sent
    print(f"\nDone. {sent} follow-up(s) sent. {remaining} remaining. Log updated.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send follow-ups to non-replied leads. Default mode: --dry-run."
    )
    parser.add_argument(
        "--cap", type=int, default=DAILY_SEND_CAP, metavar="N",
        help=f"Max follow-ups to send per run (default: {DAILY_SEND_CAP})",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="(DEFAULT) Show who would get a follow-up. Sends nothing.",
    )
    mode.add_argument(
        "--live", action="store_true",
        help="Send actual follow-ups. Requires typed SEND confirmation.",
    )

    args = parser.parse_args()

    if args.live:
        run_live(args.cap)
    else:
        run_dry_run()


if __name__ == "__main__":
    main()
