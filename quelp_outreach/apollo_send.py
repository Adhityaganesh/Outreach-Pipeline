"""
Apollo Send — reads an Apollo contacts export CSV and sends Quelp pitch emails.
Skips rows with no verified email. Logs to sent_log.csv same as Block 5.

Modes (safest is default):
  --dry-run   (DEFAULT) Print what would be sent. No emails go out.
  --test-to   Redirect all emails to one address for inbox preview.
  --live      Send to real recipients. Requires typing "SEND".
"""

import argparse
import base64
import csv
import random
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import pandas as pd
from groq import Groq
from googleapiclient.errors import HttpError

# Reuse Gmail auth and config from Block 5
from config import (
    DATA_DIR,
    DAILY_SEND_CAP,
    GMAIL_TOKEN_PATH,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GROQ_API_KEY,
    SEND_DELAY_MAX,
    SEND_DELAY_MIN,
    SENDER_NAME,
)
from send import _get_gmail_service, _log_send

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SENT_LOG = DATA_DIR / "sent_log.csv"

# ---------------------------------------------------------------------------
# Email template — explains Quelp's use-case
# ---------------------------------------------------------------------------

_SUBJECT = "your support inbox — a thought"

_BODY = """\
Hi {first_name},

{opener}

Quick question — when a customer emails "how do I...", does your team have to dig through Notion, your docs, and old tickets to piece together an answer?

Quelp is an AI copilot that handles this automatically. When a support email lands, it searches your connected knowledge bases live — Notion, Confluence, Freshdesk — drafts an accurate reply grounded in your actual docs, and surfaces everything in one clean UI.

Your team stops tab-switching. Replies go out in seconds, and they're accurate because Quelp pulls from your real documentation — not guesses.

Built for teams like yours where a founder or a small team is handling repetitive "how do I..." tickets that your own docs could already answer — if someone could find them fast enough.

Happy to set Quelp up on your inbox for a free two-week trial. Takes about 15 minutes to connect your tools.

Worth a quick look?

{sender}\
"""

# ---------------------------------------------------------------------------
# Groq opener
# ---------------------------------------------------------------------------

_groq_client = None

def _groq() -> Groq:
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq(api_key=GROQ_API_KEY)
    return _groq_client


def _make_opener(first_name: str, company: str, title: str, industry: str) -> str:
    """One personalised sentence referencing their company/role."""
    if not GROQ_API_KEY:
        return f"Saw {company} and thought this might be relevant."

    prompt = (
        f"Write ONE short sentence (max 20 words) that opens a cold email to {first_name}, "
        f"the {title} at {company} (industry: {industry or 'software/IT'}). "
        "The sentence should acknowledge something plausible about running a small software company "
        "and naturally set up a question about their customer support workflow. "
        "Do NOT mention Quelp. Do NOT use generic phrases like 'I came across your company'. "
        "Be specific and conversational. Return only the sentence, no quotes."
    )
    try:
        resp = _groq().chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=60,
        )
        return resp.choices[0].message.content.strip().strip('"')
    except Exception:
        return f"Running a {company}-sized team means wearing a lot of hats — including support."


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------

_INVALID_STATUSES = {"unavailable", "invalid", "bounced"}


def _load_apollo(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)

    # Normalise column names
    df.columns = [c.strip() for c in df.columns]

    # Filter: must have a valid email
    df = df[df["Email"].notna() & (df["Email"].str.strip() != "")]
    df = df[~df["Email Status"].str.lower().isin(_INVALID_STATUSES)]

    # Non-person first names — company words, colors, tech terms
    _FAKE_FIRST_NAMES = {
        "orange", "blue", "red", "green", "black", "white", "silver",
        "tech", "digital", "software", "cloud", "info", "data", "smart",
        "global", "solution", "solutions", "services", "systems", "group",
    }

    def _clean_first(first: str, company: str) -> str:
        f = first.strip()
        # Looks like a company word or is the start of the company name
        if f.lower() in _FAKE_FIRST_NAMES:
            return "there"
        # First name is same as company's first word
        if f.lower() == company.strip().split()[0].lower():
            return "there"
        return f or "there"

    df["first_name"] = df.apply(
        lambda r: _clean_first(str(r.get("First Name", "")), str(r.get("Company Name", ""))),
        axis=1,
    )
    df["last_name"]  = df["Last Name"].fillna("").str.strip()
    df["email"]      = df["Email"].str.strip().str.lower()
    df["company"]    = df["Company Name"].fillna("").str.strip()
    df["title"]      = df["Title"].fillna("").str.strip()
    df["industry"]   = df["Industry"].fillna("").str.strip()

    return df.reset_index(drop=True)


def _already_sent() -> set[str]:
    if not _SENT_LOG.exists():
        return set()
    df = pd.read_csv(_SENT_LOG)
    return set(df["email"].astype(str).str.lower().tolist())


# ---------------------------------------------------------------------------
# Email build
# ---------------------------------------------------------------------------

def _build_raw(to: str, subject: str, body: str) -> str:
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"]      = to
    msg["From"]    = f"{SENDER_NAME} <me>"
    msg["Subject"] = subject
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _send_one(service, to: str, subject: str, body: str) -> tuple[str, str]:
    raw = _build_raw(to, subject, body)
    result = service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()
    return result.get("id", ""), result.get("threadId", "")


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def run_dry_run(df: pd.DataFrame) -> None:
    already = _already_sent()

    print(f"\n{'='*65}")
    print("DRY RUN — no emails will be sent")
    print(f"{'='*65}\n")

    for _, row in df.iterrows():
        email   = row["email"]
        skipped = email in already
        tag     = "[SKIP — already sent]" if skipped else "[WOULD SEND]"

        opener = _make_opener(
            row["first_name"], row["company"], row["title"], row["industry"]
        )
        body = _BODY.format(
            first_name=row["first_name"] or "there",
            opener=opener,
            sender=SENDER_NAME,
        )

        print(f"{tag}")
        print(f"  To      : {email}  ({row['first_name']} {row['last_name']})")
        print(f"  Company : {row['company']}  ({row.get('# Employees', '?')} employees)")
        print(f"  Subject : {_SUBJECT}")
        print(f"  Opener  : {opener}")
        print()

    would_send = len(df[~df["email"].isin(already)])
    print(f"{'='*65}")
    print(f"Would send: {would_send}  |  Already sent: {len(df) - would_send}")


def run_test_send(df: pd.DataFrame, test_to: str) -> None:
    service = _get_gmail_service()
    sent = 0

    print(f"\n{'='*65}")
    print(f"TEST SEND — all emails redirected to: {test_to}")
    print(f"{'='*65}\n")

    for _, row in df.iterrows():
        first_name = row["first_name"] or "there"
        opener = _make_opener(
            first_name, row["company"], row["title"], row["industry"]
        )
        body = _BODY.format(
            first_name=first_name,
            opener=opener,
            sender=SENDER_NAME,
        )

        print(f"[{sent+1}] orig={row['email']}  ({row['company']}) → {test_to}")
        try:
            msg_id, thread_id = _send_one(service, test_to, _SUBJECT, body)
            _log_send(test_to, row["company"], f"{row['first_name']} {row['last_name']}",
                      "high", msg_id, thread_id, "test")
            print(f"  OK  message_id={msg_id}")
            sent += 1
        except HttpError as e:
            print(f"  ERROR — {e}")

        if sent < len(df):
            delay = random.uniform(SEND_DELAY_MIN, SEND_DELAY_MAX)
            print(f"  Waiting {delay:.0f}s…")
            time.sleep(delay)

    print(f"\nTest send done. {sent} sent to {test_to}.")


def run_live(df: pd.DataFrame, cap: int) -> None:
    already = _already_sent()
    queue   = df[~df["email"].isin(already)].reset_index(drop=True)
    to_send = min(len(queue), cap)

    print(f"\n{'='*65}")
    print(f"LIVE SEND")
    print(f"  Queue: {len(queue)}  |  Cap: {cap}  |  Will send: {to_send}")
    print(f"  Delay: {SEND_DELAY_MIN:.0f}–{SEND_DELAY_MAX:.0f}s")
    print(f"{'='*65}")

    if to_send == 0:
        print("Nothing to send.")
        return

    print("\nRecipients:")
    for i, row in queue.head(to_send).iterrows():
        print(f"  {i+1}. {row['email']}  ({row['first_name']} {row['last_name']}, {row['company']})")

    print()
    confirm = input("Type SEND to confirm, anything else to abort: ").strip()
    if confirm != "SEND":
        print("Aborted.")
        return

    service = _get_gmail_service()
    sent = 0

    for i, row in queue.head(to_send).iterrows():
        first_name = row["first_name"] or "there"
        opener = _make_opener(
            first_name, row["company"], row["title"], row["industry"]
        )
        body = _BODY.format(
            first_name=first_name,
            opener=opener,
            sender=SENDER_NAME,
        )

        print(f"[{sent+1}/{to_send}] → {row['email']}  ({row['company']})")
        print(f"  Opener: {opener}")
        try:
            msg_id, thread_id = _send_one(service, row["email"], _SUBJECT, body)
            _log_send(
                row["email"], row["company"],
                f"{row['first_name']} {row['last_name']}",
                "high", msg_id, thread_id, "sent",
            )
            print(f"  OK  message_id={msg_id}")
            sent += 1
        except HttpError as e:
            print(f"  ERROR — {e}")
            _log_send(row["email"], row["company"],
                      f"{row['first_name']} {row['last_name']}",
                      "high", "", "", f"error: {e}")

        if sent < to_send:
            delay = random.uniform(SEND_DELAY_MIN, SEND_DELAY_MAX)
            print(f"  Waiting {delay:.0f}s…")
            time.sleep(delay)

    print(f"\nDone. {sent}/{to_send} sent. Log: {_SENT_LOG}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send Quelp pitch emails from an Apollo contacts CSV."
    )
    parser.add_argument(
        "--in", dest="infile",
        default="/Users/adhityaganesh/Downloads/apollo-contacts-export.csv",
        metavar="CSV", help="Apollo export CSV",
    )
    parser.add_argument(
        "--cap", type=int, default=DAILY_SEND_CAP, metavar="N",
        help=f"Max emails per run (default: {DAILY_SEND_CAP})",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true",
                      help="(DEFAULT) Preview emails, send nothing.")
    mode.add_argument("--test-to", dest="test_to", metavar="EMAIL",
                      help="Send all emails to this address for inbox preview.")
    mode.add_argument("--live", action="store_true",
                      help="Send to real recipients. Requires typed SEND.")

    args = parser.parse_args()
    df = _load_apollo(Path(args.infile))

    print(f"Loaded {len(df)} contacts with valid emails from Apollo export.")

    if args.test_to:
        run_test_send(df, args.test_to)
    elif args.live:
        run_live(df, args.cap)
    else:
        run_dry_run(df)


if __name__ == "__main__":
    main()
