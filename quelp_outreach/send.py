"""
Block 5 — Send
Sends emails from ready_to_send.csv via the authenticated user's Gmail account.

Three modes (safest is default):
  --dry-run   (DEFAULT) Print what would be sent; write dry_run_preview.csv. No sends.
  --test-to   Send ALL selected emails to ONE address to verify in a real inbox.
  --live      Send to real recipients. Requires typed "SEND" confirmation.

Confidence gate (default: medium — skips low-confidence role-email guesses):
  --min-confidence {high,medium,low}
"""

import argparse
import base64
import csv
import os
import random
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import pandas as pd
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import (
    BASE_DIR,
    DATA_DIR,
    DAILY_SEND_CAP,
    GMAIL_TOKEN_PATH,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    SEND_DELAY_MAX,
    SEND_DELAY_MIN,
    SENDER_NAME,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCOPES       = ["https://www.googleapis.com/auth/gmail.send"]
_SENT_LOG     = DATA_DIR / "sent_log.csv"
_DRY_RUN_FILE = DATA_DIR / "dry_run_preview.csv"

_CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}

# Client-secrets dict built from env vars — no secrets file needed on disk.
_CLIENT_CONFIG = {
    "installed": {
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
        "token_uri":     "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

# ---------------------------------------------------------------------------
# Gmail auth
# ---------------------------------------------------------------------------

def _get_gmail_service():
    """
    Return an authenticated Gmail API service object.
    On first run, opens the OAuth consent flow and caches token.json.
    On subsequent runs, refreshes the token silently.
    """
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        sys.exit(
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in .env.\n"
            "Create OAuth credentials at console.cloud.google.com → APIs & Services → Credentials."
        )

    creds: Credentials | None = None

    if GMAIL_TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_PATH), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_config(_CLIENT_CONFIG, _SCOPES)
            creds = flow.run_local_server(port=0)
        GMAIL_TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Email building
# ---------------------------------------------------------------------------

def _build_mime(to: str, subject: str, body: str) -> str:
    """Return a base64url-encoded RFC 2822 message string."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["to"]      = to
    msg["from"]    = f"{SENDER_NAME} <me>"   # Gmail replaces <me> with authed address
    msg["subject"] = subject
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


# ---------------------------------------------------------------------------
# Sent-log helpers
# ---------------------------------------------------------------------------

_LOG_FIELDS = [
    "email", "company", "name", "confidence",
    "sent_at", "gmail_message_id", "thread_id", "status",
]


def _load_sent_emails() -> set[str]:
    """Return the set of email addresses already in sent_log."""
    if not _SENT_LOG.exists():
        return set()
    df = pd.read_csv(_SENT_LOG)
    return set(df["email"].astype(str).str.lower().tolist())


def _log_send(
    email: str,
    company: str,
    name: str,
    confidence: str,
    gmail_message_id: str,
    thread_id: str,
    status: str,
) -> None:
    """Append one row to sent_log.csv."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_header = not _SENT_LOG.exists()
    with open(_SENT_LOG, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_LOG_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({
            "email":            email,
            "company":          company,
            "name":             name,
            "confidence":       confidence,
            "sent_at":          datetime.now(timezone.utc).isoformat(),
            "gmail_message_id": gmail_message_id,
            "thread_id":        thread_id,
            "status":           status,
        })


# ---------------------------------------------------------------------------
# Queue loading + confidence gate
# ---------------------------------------------------------------------------

def _load_queue(in_path: Path, min_confidence: str) -> pd.DataFrame:
    """
    Load ready_to_send.csv, validate columns, apply confidence gate.
    If the CSV has no 'confidence' column (pre-Block-3 file), treats all
    rows as 'low' so --min-confidence medium/high correctly skips them.
    """
    required = {"best_email", "subject", "body"}
    df = pd.read_csv(in_path)
    missing = required - set(df.columns)
    if missing:
        sys.exit(f"Input CSV missing columns: {missing}")

    df = df.dropna(subset=["best_email", "subject", "body"])
    df = df[df["best_email"].str.strip() != ""]

    # Normalise confidence column — default to "low" if absent
    if "confidence" not in df.columns:
        df["confidence"] = "low"
    df["confidence"] = df["confidence"].fillna("low").str.strip().str.lower()
    df["confidence"] = df["confidence"].apply(
        lambda v: v if v in _CONFIDENCE_RANK else "low"
    )

    # Normalise name column
    if "name" not in df.columns:
        df["name"] = ""
    df["name"] = df["name"].fillna("")

    # Apply confidence gate
    threshold = _CONFIDENCE_RANK[min_confidence]
    before = len(df)
    df = df[df["confidence"].map(_CONFIDENCE_RANK) >= threshold]
    filtered = before - len(df)
    if filtered:
        print(
            f"  Confidence gate ({min_confidence}+): {filtered} row(s) skipped "
            f"(below threshold), {len(df)} remain."
        )

    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Core send
# ---------------------------------------------------------------------------

def _send_one(service, to: str, subject: str, body: str) -> tuple[str, str]:
    """Send a single email. Returns (message_id, thread_id)."""
    raw = _build_mime(to, subject, body)
    result = service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()
    return result.get("id", ""), result.get("threadId", "")


# ---------------------------------------------------------------------------
# Mode: dry-run
# ---------------------------------------------------------------------------

def run_dry_run(in_path: Path, min_confidence: str, cap: int) -> None:
    df = _load_queue(in_path, min_confidence)
    already_sent = _load_sent_emails()

    preview_rows = []
    print(f"\n{'='*65}")
    print(f"DRY RUN — nothing will be sent")
    print(f"Min confidence: {min_confidence}  |  Cap: {cap}")
    print(f"{'='*65}\n")

    for _, row in df.iterrows():
        to         = str(row["best_email"]).strip().lower()
        subject    = str(row["subject"])
        body       = str(row["body"])
        company    = str(row.get("company", row.get("domain", "")))
        name       = str(row.get("name", ""))
        confidence = str(row.get("confidence", "low"))
        body_preview = "\n".join(body.splitlines()[:3])

        skipped = to in already_sent
        tag = "[SKIP — already sent]" if skipped else "[WOULD SEND]"

        print(f"{tag}  confidence={confidence}")
        print(f"  To      : {to}" + (f"  ({name})" if name else ""))
        print(f"  Company : {company}")
        print(f"  Subject : {subject}")
        print(f"  Body    :\n    " + body_preview.replace("\n", "\n    "))
        print()

        preview_rows.append({
            "to":           to,
            "company":      company,
            "name":         name,
            "confidence":   confidence,
            "subject":      subject,
            "body_preview": body_preview,
            "would_skip":   skipped,
        })

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(preview_rows).to_csv(_DRY_RUN_FILE, index=False)

    send_count = sum(1 for r in preview_rows if not r["would_skip"])
    skip_count = len(preview_rows) - send_count
    capped     = max(0, send_count - cap)

    print(f"{'='*65}")
    print(f"Summary: {send_count} would send, {skip_count} already sent, "
          f"{capped} would be held by daily cap ({cap}/run).")
    print(f"Preview written to {_DRY_RUN_FILE}")


# ---------------------------------------------------------------------------
# Mode: test-to
# ---------------------------------------------------------------------------

def run_test_send(in_path: Path, test_to: str, min_confidence: str, cap: int) -> None:
    df = _load_queue(in_path, min_confidence)
    service = _get_gmail_service()
    sent = 0

    print(f"\n{'='*65}")
    print(f"TEST SEND — all emails redirected to: {test_to}")
    print(f"Min confidence: {min_confidence}  |  Cap: {cap}  |  Delay: {SEND_DELAY_MIN:.0f}–{SEND_DELAY_MAX:.0f}s")
    print(f"{'='*65}\n")

    for idx, row in df.iterrows():
        if sent >= cap:
            remaining = len(df) - sent
            print(f"\nCap of {cap} reached. {remaining} email(s) not sent this run.")
            break

        subject    = str(row["subject"])
        body       = str(row["body"])
        company    = str(row.get("company", row.get("domain", "")))
        name       = str(row.get("name", ""))
        confidence = str(row.get("confidence", "low"))
        orig_to    = str(row["best_email"]).strip().lower()

        print(f"[{sent+1}/{cap}] orig={orig_to}  conf={confidence} → {test_to}")
        try:
            msg_id, thread_id = _send_one(service, test_to, subject, body)
            _log_send(test_to, company, name, confidence, msg_id, thread_id, "test")
            print(f"  OK  message_id={msg_id}")
            sent += 1
        except HttpError as e:
            print(f"  ERROR — {e}")
            _log_send(test_to, company, name, confidence, "", "", f"error: {e}")

        is_last = (idx == df.index[-1]) or (sent >= cap)
        if not is_last:
            delay = random.uniform(SEND_DELAY_MIN, SEND_DELAY_MAX)
            print(f"  Waiting {delay:.0f}s…")
            time.sleep(delay)

    print(f"\nTest send done. {sent} sent to {test_to}. Log: {_SENT_LOG}")


# ---------------------------------------------------------------------------
# Mode: live
# ---------------------------------------------------------------------------

def run_live(in_path: Path, min_confidence: str, cap: int) -> None:
    df = _load_queue(in_path, min_confidence)
    already_sent = _load_sent_emails()

    queue = [
        row for _, row in df.iterrows()
        if str(row["best_email"]).strip().lower() not in already_sent
    ]
    to_send = min(len(queue), cap)

    print(f"\n{'='*65}")
    print(f"LIVE SEND")
    print(f"  Queue  : {len(queue)} unsent  |  Cap: {cap}  |  Will send: {to_send}")
    print(f"  Conf   : {min_confidence}+")
    print(f"  Delay  : {SEND_DELAY_MIN:.0f}–{SEND_DELAY_MAX:.0f}s between emails")
    print(f"{'='*65}")

    if to_send == 0:
        print("Nothing to send (queue empty or all already sent).")
        return

    print("\nEmails that WILL be sent to real recipients:")
    for i, row in enumerate(queue[:to_send], 1):
        conf = str(row.get("confidence", "low"))
        name = str(row.get("name", ""))
        label = f"  ({name})" if name else ""
        print(f"  {i}. {row['best_email']}{label}  [{conf}]  —  {row['subject']}")

    print()
    confirm = input("Type SEND to confirm, anything else to abort: ").strip()
    if confirm != "SEND":
        print("Aborted.")
        return

    service = _get_gmail_service()
    sent = 0

    for i, row in enumerate(queue[:to_send]):
        to         = str(row["best_email"]).strip().lower()
        subject    = str(row["subject"])
        body       = str(row["body"])
        company    = str(row.get("company", row.get("domain", "")))
        name       = str(row.get("name", ""))
        confidence = str(row.get("confidence", "low"))

        print(f"[{sent+1}/{to_send}] → {to}  conf={confidence}")
        try:
            msg_id, thread_id = _send_one(service, to, subject, body)
            _log_send(to, company, name, confidence, msg_id, thread_id, "sent")
            print(f"  OK  message_id={msg_id}")
            sent += 1
        except HttpError as e:
            print(f"  ERROR — {e}")
            _log_send(to, company, name, confidence, "", "", f"error: {e}")

        is_last = (i == to_send - 1)
        if not is_last:
            delay = random.uniform(SEND_DELAY_MIN, SEND_DELAY_MAX)
            print(f"  Waiting {delay:.0f}s…")
            time.sleep(delay)

    remaining = len(queue) - sent
    print(f"\nLive send done. {sent} sent. "
          f"{remaining} remain (run again to continue).")
    print(f"Log: {_SENT_LOG}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    default_in = DATA_DIR / "ready_to_send.csv"

    parser = argparse.ArgumentParser(
        description="Send cold emails from Gmail. Default mode: --dry-run."
    )
    parser.add_argument(
        "--in", dest="infile", default=str(default_in), metavar="CSV",
        help=f"Input CSV (default: {default_in})",
    )
    parser.add_argument(
        "--cap", type=int, default=DAILY_SEND_CAP, metavar="N",
        help=f"Max emails to send this run (default: {DAILY_SEND_CAP})",
    )
    parser.add_argument(
        "--min-confidence", dest="min_confidence",
        choices=["high", "medium", "low"], default="medium",
        help="Minimum confidence level to include (default: medium).",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="(DEFAULT) Print what would be sent; no emails go out.",
    )
    mode.add_argument(
        "--test-to", dest="test_to", metavar="EMAIL",
        help="Send all selected emails to this address for inbox preview.",
    )
    mode.add_argument(
        "--live", action="store_true",
        help="Send to real recipients. Requires typed SEND confirmation.",
    )

    args = parser.parse_args()
    in_path = Path(args.infile)

    if not in_path.exists():
        sys.exit(f"Input file not found: {in_path}")

    if args.test_to:
        run_test_send(in_path, args.test_to, args.min_confidence, args.cap)
    elif args.live:
        run_live(in_path, args.min_confidence, args.cap)
    else:
        # Default: dry-run (covers both --dry-run flag and no flag at all)
        run_dry_run(in_path, args.min_confidence, args.cap)


if __name__ == "__main__":
    main()
