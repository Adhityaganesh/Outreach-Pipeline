"""
Shared sent_log.csv + suppression list.

One schema for every script. Older logs (8 columns, no subject) are migrated
in place the first time any script touches them — no data is lost.
"""

import csv
import os
import shutil
import tempfile
from datetime import datetime, timezone

import pandas as pd

from config import DATA_DIR, SUPPRESS_PATH

SENT_LOG = DATA_DIR / "sent_log.csv"

LOG_FIELDS = [
    "email", "company", "name", "confidence", "subject",
    "sent_at", "gmail_message_id", "rfc_message_id", "thread_id", "status",
    "followup_sent_at", "followup_message_id", "inbox",
]

# Statuses:
#   sent         first email delivered to Gmail
#   followed_up  follow-up sent
#   replied      a human replied in the thread → never emailed again
#   bounced      delivery failure in the thread → never emailed again
#   test         --test-to preview send (ignored by follow-ups + dedupe)
#   error: ...   API error, nothing delivered


# ---------------------------------------------------------------------------
# Log I/O
# ---------------------------------------------------------------------------

def _ensure_schema() -> None:
    """Add any missing columns to an existing log (old 8-column format)."""
    if not SENT_LOG.exists():
        return
    with open(SENT_LOG, newline="", encoding="utf-8") as f:
        header = next(csv.reader(f), [])
    if header == LOG_FIELDS:
        return
    df = pd.read_csv(SENT_LOG, dtype=str).fillna("")
    for col in LOG_FIELDS:
        if col not in df.columns:
            df[col] = ""
    save_log(df)


def load_log() -> pd.DataFrame:
    """Return the full log as strings (empty frame with schema if none)."""
    _ensure_schema()
    if not SENT_LOG.exists():
        return pd.DataFrame(columns=LOG_FIELDS)
    return pd.read_csv(SENT_LOG, dtype=str).fillna("")


def save_log(df: pd.DataFrame) -> None:
    """Atomic rewrite of the log."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for col in LOG_FIELDS:
        if col not in df.columns:
            df[col] = ""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", delete=False, suffix=".csv",
        dir=str(DATA_DIR), newline="", encoding="utf-8",
    )
    try:
        df[LOG_FIELDS].to_csv(tmp, index=False)
        tmp.close()
        shutil.move(tmp.name, str(SENT_LOG))
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise


def log_send(email: str, company: str, name: str, confidence: str,
             subject: str, gmail_message_id: str, rfc_message_id: str,
             thread_id: str, status: str, inbox: str = "") -> None:
    """Append one row. inbox = the address it was sent from."""
    _ensure_schema()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_header = not SENT_LOG.exists()
    with open(SENT_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if write_header:
            w.writeheader()
        w.writerow({
            "email": email, "company": company, "name": name,
            "confidence": confidence, "subject": subject,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "gmail_message_id": gmail_message_id,
            "rfc_message_id": rfc_message_id,
            "thread_id": thread_id, "status": status,
            "followup_sent_at": "", "followup_message_id": "",
            "inbox": inbox.lower(),
        })


def contacted_emails() -> set[str]:
    """Every address that was really emailed (excludes test + error rows)."""
    df = load_log()
    if df.empty:
        return set()
    real = df[~df["status"].str.startswith(("test", "error"))]
    return set(real["email"].str.strip().str.lower())


def sent_today(inbox: str | None = None) -> int:
    """
    Emails that went out today (UTC), first emails + follow-ups combined.
    inbox=None counts every inbox; an address counts only that inbox.
    """
    df = load_log()
    if df.empty:
        return 0
    today = datetime.now(timezone.utc).date().isoformat()
    real = df[~df["status"].str.startswith(("test", "error"))]
    if inbox:
        real = real[real["inbox"].str.lower() == inbox.lower()]
    first = real["sent_at"].str.startswith(today).sum()
    fups = real["followup_sent_at"].str.startswith(today).sum()
    return int(first + fups)


def remaining_today(cap: int, inbox: str | None = None) -> int:
    return max(0, cap - sent_today(inbox))


# ---------------------------------------------------------------------------
# Suppression list — data/suppress.txt, one email or @domain per line
# ---------------------------------------------------------------------------

def load_suppressed() -> set[str]:
    if not SUPPRESS_PATH.exists():
        return set()
    out = set()
    for line in SUPPRESS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            out.add(line)
    return out


def is_suppressed(email: str, suppressed: set[str]) -> bool:
    email = email.strip().lower()
    domain = "@" + email.split("@")[-1]
    return email in suppressed or domain in suppressed


def add_suppressed(entries: list[str]) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = load_suppressed()
    new = [e.strip().lower() for e in entries if e.strip().lower() not in existing]
    if new:
        with open(SUPPRESS_PATH, "a", encoding="utf-8") as f:
            for e in new:
                f.write(e + "\n")
    return len(new)
