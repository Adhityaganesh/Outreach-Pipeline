"""
List Send — give it a lead list, it sends the Quelp pitch.

  python list_send.py --in leads.csv                     # dry run (default)
  python list_send.py --in leads.csv --test-to me@x.com  # 3 samples to your inbox
  python list_send.py --in leads.csv --live              # real send, asks for SEND

Any CSV works as long as it has an email column (see leads.py for accepted
headers). For each lead it:
  1. skips anyone already contacted, suppressed, or outside the size range
  2. writes a one-line opener (Groq, cached — the preview IS what gets sent)
  3. sends the first email from pitch.py via Gmail, respecting the daily cap
  4. logs subject + threading ids so followup.py can reply in-thread

Review data/preview.csv after a dry run — it has every full email body.
"""

import argparse
import hashlib
import random
import re
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd
from googleapiclient.errors import HttpError

import pitch
from config import (
    CACHE_DB,
    DAILY_SEND_CAP,
    DATA_DIR,
    GROQ_API_KEY,
    MAX_EMPLOYEES,
    MIN_EMPLOYEES,
    SEND_DELAY_MAX,
    SEND_DELAY_MIN,
)
from gmail_auth import get_gmail_service, send_email
from leads import load_leads
from sent_log import (
    contacted_emails,
    is_suppressed,
    load_suppressed,
    log_send,
    remaining_today,
    sent_today,
)

_PREVIEW = DATA_DIR / "preview.csv"
_GROQ_MODEL = "llama-3.3-70b-versatile"

# Cache key includes a hash of the prompt so editing pitch.py regenerates openers.
_PROMPT_VERSION = hashlib.sha1(
    (pitch.OPENER_SYSTEM + pitch.opener_prompt("x", "x", "x")).encode()
).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Size gate
# ---------------------------------------------------------------------------

def _size_ok(employees: str, lo: int, hi: int) -> bool:
    """Keep if the headcount (or range like '51-200') overlaps [lo, hi]. Unknown → keep."""
    nums = [int(n) for n in re.findall(r"\d+", str(employees).replace(",", ""))]
    if not nums:
        return True
    return nums[0] <= hi and nums[-1] >= lo


# ---------------------------------------------------------------------------
# Openers (cached in cache.sqlite)
# ---------------------------------------------------------------------------

_groq = None


def _conn():
    c = sqlite3.connect(CACHE_DB)
    c.execute("CREATE TABLE IF NOT EXISTS list_openers "
              "(key TEXT PRIMARY KEY, opener TEXT NOT NULL)")
    return c


def _llm_opener(row) -> str:
    global _groq
    if not GROQ_API_KEY:
        return ""
    try:
        if _groq is None:
            from groq import Groq
            _groq = Groq(api_key=GROQ_API_KEY)
        resp = _groq.chat.completions.create(
            model=_GROQ_MODEL,
            messages=[
                {"role": "system", "content": pitch.OPENER_SYSTEM},
                {"role": "user", "content": pitch.opener_prompt(
                    row["first_name"], row["title"], row["company"], row["industry"])},
            ],
            temperature=0.4,
            max_tokens=60,
        )
        text = resp.choices[0].message.content.strip().strip('"').strip()
        text = re.split(r"(?<=[.!?])\s+", text)[0]
        # Reject anything that slipped past the rules
        if not text or len(text.split()) > 30 or "quelp" in text.lower() or "!" in text:
            return ""
        return text
    except Exception as e:
        print(f"  [opener] LLM failed ({e.__class__.__name__}) — using fallback.")
        return ""


def get_opener(row, conn) -> str:
    key = f"{_PROMPT_VERSION}:{row['email']}"
    hit = conn.execute("SELECT opener FROM list_openers WHERE key=?", (key,)).fetchone()
    if hit:
        return hit[0]
    opener = _llm_opener(row) or pitch.fallback_opener(row["title"], row["company"])
    conn.execute("INSERT OR REPLACE INTO list_openers VALUES (?, ?)", (key, opener))
    conn.commit()
    return opener


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

def build_queue(path: Path, size_gate: bool) -> pd.DataFrame:
    df = load_leads(path)
    contacted = contacted_emails()
    suppressed = load_suppressed()

    before = len(df)
    df = df[~df["email"].isin(contacted)]
    n_contacted = before - len(df)

    before = len(df)
    df = df[[not is_suppressed(e, suppressed) for e in df["email"]]]
    n_suppressed = before - len(df)

    n_size = 0
    if size_gate:
        before = len(df)
        df = df[[_size_ok(e, MIN_EMPLOYEES, MAX_EMPLOYEES) for e in df["employees"]]]
        n_size = before - len(df)

    if n_contacted:
        print(f"  Skipped {n_contacted}: already contacted")
    if n_suppressed:
        print(f"  Skipped {n_suppressed}: on suppression list")
    if n_size:
        print(f"  Skipped {n_size}: outside {MIN_EMPLOYEES}–{MAX_EMPLOYEES} employees "
              f"(use --no-size-gate to include)")
    print(f"  Queue: {len(df)} leads\n")
    return df.reset_index(drop=True)


def render(df: pd.DataFrame) -> pd.DataFrame:
    """Add opener, subject, body columns."""
    conn = _conn()
    openers, subjects, bodies = [], [], []
    for i, row in df.iterrows():
        opener = get_opener(row, conn)
        subject, body = pitch.render_first_email(row["first_name"], opener)
        openers.append(opener)
        subjects.append(subject)
        bodies.append(body)
    conn.close()
    df = df.copy()
    df["opener"], df["subject"], df["body"] = openers, subjects, bodies
    return df


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def run_dry(df: pd.DataFrame, cap: int, limit: int) -> None:
    df = render(df.head(limit) if limit else df)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(_PREVIEW, index=False)

    print("=" * 65)
    print("DRY RUN — nothing sent")
    print("=" * 65)
    for _, r in df.head(5).iterrows():
        print(f"\nTo: {r['email']}  ({r['first_name']} {r['last_name']}, {r['title']} @ {r['company']})")
        print(f"Subject: {r['subject']}\n")
        print(r["body"])
        print("-" * 65)
    if len(df) > 5:
        print(f"\n… {len(df) - 5} more. Full bodies in {_PREVIEW}")

    left = remaining_today(cap)
    print(f"\nToday: {sent_today()} sent, cap {cap} → a --live run would send {min(left, len(df))}.")


def run_test(df: pd.DataFrame, test_to: str, n: int) -> None:
    df = render(df.head(n))
    service = get_gmail_service()
    print(f"TEST SEND — {len(df)} sample(s) to {test_to}\n")
    for i, r in df.iterrows():
        subject = f"[TEST → {r['email']}] {r['subject']}"
        try:
            gid, tid, rfc = send_email(service, test_to, subject, r["body"])
            log_send(test_to, r["company"], f"{r['first_name']} {r['last_name']}".strip(),
                     "high", subject, gid, rfc, tid, "test")
            print(f"  OK  {r['email']} → {test_to}")
        except HttpError as e:
            print(f"  ERROR {r['email']}: {e}")
        if i < len(df) - 1:
            time.sleep(3)
    print("\nCheck the inbox AND the spam folder before going live.")


def run_live(df: pd.DataFrame, cap: int) -> None:
    left = remaining_today(cap)
    if left == 0:
        print(f"Daily cap reached ({sent_today()}/{cap} today). Run again tomorrow.")
        return
    batch = render(df.head(left))

    print("=" * 65)
    print(f"LIVE SEND — {len(batch)} email(s)  |  today so far: {sent_today()}/{cap}")
    print(f"Delay {SEND_DELAY_MIN:.0f}–{SEND_DELAY_MAX:.0f}s between sends")
    print("=" * 65)
    for i, r in batch.iterrows():
        print(f"  {i+1}. {r['email']}  ({r['first_name']}, {r['title']} @ {r['company']})")
        print(f"      opener: {r['opener']}")
    print()
    if input("Type SEND to confirm, anything else to abort: ").strip() != "SEND":
        print("Aborted.")
        return

    service = get_gmail_service()
    sent = 0
    for i, r in batch.iterrows():
        name = f"{r['first_name']} {r['last_name']}".strip()
        print(f"[{i+1}/{len(batch)}] → {r['email']}")
        try:
            gid, tid, rfc = send_email(service, r["email"], r["subject"], r["body"])
            log_send(r["email"], r["company"], name, "high", r["subject"], gid, rfc, tid, "sent")
            print(f"  OK  {gid}")
            sent += 1
        except HttpError as e:
            print(f"  ERROR — {e}")
            log_send(r["email"], r["company"], name, "high", r["subject"], "", "", "", f"error: {e.status_code}")
            if e.status_code in (403, 429):
                print("  Gmail is rate-limiting this account. Stopping the run.")
                break
        if i < len(batch) - 1:
            d = random.uniform(SEND_DELAY_MIN, SEND_DELAY_MAX)
            print(f"  waiting {d:.0f}s…")
            time.sleep(d)

    print(f"\nDone. {sent} sent. {len(df) - sent} still queued — run again tomorrow.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Send the Quelp pitch to a lead list. Default: dry run.")
    p.add_argument("--in", dest="infile", required=True, metavar="CSV", help="Lead list CSV")
    p.add_argument("--cap", type=int, default=DAILY_SEND_CAP,
                   help=f"Max emails per day across all runs (default {DAILY_SEND_CAP})")
    p.add_argument("--limit", type=int, default=0, help="Dry run: only render the first N")
    p.add_argument("--no-size-gate", dest="size_gate", action="store_false",
                   help=f"Include companies outside {MIN_EMPLOYEES}–{MAX_EMPLOYEES} employees")
    m = p.add_mutually_exclusive_group()
    m.add_argument("--dry-run", action="store_true", help="(default) Preview only")
    m.add_argument("--test-to", metavar="EMAIL", help="Send a few samples to this address")
    m.add_argument("--live", action="store_true", help="Send for real (asks for SEND)")
    p.add_argument("--samples", type=int, default=3, help="--test-to: how many (default 3)")
    a = p.parse_args()

    path = Path(a.infile).expanduser()
    if not path.exists():
        sys.exit(f"File not found: {path}")

    df = build_queue(path, a.size_gate)
    if df.empty:
        print("Nothing to send.")
        return

    if a.test_to:
        run_test(df, a.test_to, a.samples)
    elif a.live:
        run_live(df, a.cap)
    else:
        run_dry(df, a.cap, a.limit)


if __name__ == "__main__":
    main()
