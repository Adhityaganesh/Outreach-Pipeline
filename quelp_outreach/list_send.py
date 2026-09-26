"""
List Send — give it a lead list, it sends the Quelp pitch.

  python list_send.py --in leads.csv                     # dry run (default)
  python list_send.py --in leads.csv --test-to me@x.com  # 3 samples to your inbox
  python list_send.py --in leads.csv --live              # real send, asks for SEND

Any CSV works as long as it has an email column (see leads.py for accepted
headers). For each lead it:
  1. skips anyone already contacted, suppressed, or outside the size range
  2. writes a one-line opener (Groq, cached — the preview IS what gets sent)
  3. sends the first email from pitch.py via Gmail, rotating across INBOXES
     and respecting each inbox's daily cap
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
    GROQ_MODEL,
    MAX_EMPLOYEES,
    MIN_EMPLOYEES,
    SEND_DELAY_MAX,
    SEND_DELAY_MIN,
)
from gmail_auth import (InboxNotConnected, get_gmail_service, send_email,
                        sender_address, token_path)
from inboxes import Inbox, assign, configured
from inboxes import summary as inbox_summary
from leads import load_leads
from sent_log import (
    contacted_emails,
    is_suppressed,
    load_suppressed,
    log_send,
)

_PREVIEW = DATA_DIR / "preview.csv"
_GROQ_MODEL = GROQ_MODEL

# Cache key includes a hash of the prompt so editing pitch.py regenerates openers.
_PROMPT_VERSION = hashlib.sha1(
    (pitch.OPENER_SYSTEM + pitch.opener_prompt("x", "x", "x") + _GROQ_MODEL).encode()
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
        messages = [
            {"role": "system", "content": pitch.OPENER_SYSTEM},
            {"role": "user", "content": pitch.opener_prompt(
                row["first_name"], row["title"], row["company"], row["industry"])},
        ]
        # Reasoning models (gpt-oss, qwen3) spend tokens thinking before they
        # emit anything, so a small budget returns EMPTY content with
        # finish_reason 'length'. Give room, and ask for minimal reasoning
        # where the model supports it.
        kwargs = dict(model=_GROQ_MODEL, messages=messages,
                      temperature=0.4, max_tokens=400)
        try:
            resp = _groq.chat.completions.create(reasoning_effort="low", **kwargs)
        except Exception:
            resp = _groq.chat.completions.create(**kwargs)
        text = (resp.choices[0].message.content or "").strip().strip('"').strip()
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
    opener = _llm_opener(row)
    if not opener:
        # Template line — NOT cached, so a later run with a working key or a
        # valid model id writes a real opener instead of reusing this one.
        return pitch.fallback_opener(row["title"], row["company"])
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

def run_dry(df: pd.DataFrame, inboxes: list[Inbox], limit: int) -> None:
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

    left = sum(ib.remaining() for ib in inboxes)
    print(f"\nToday: {inbox_summary(inboxes)}")
    print(f"A --live run would send {min(left, len(df))}.")


def _connect(inboxes: list[Inbox]) -> dict[str, object]:
    """Gmail service per inbox; exits listing any inbox that isn't connected."""
    services, missing = {}, []
    for ib in inboxes:
        try:
            services[ib.address] = get_gmail_service(ib.address)
        except InboxNotConnected:
            missing.append(ib.address)
    if missing:
        sys.exit("Not connected: " + ", ".join(missing) +
                 "\nRun `python inbox.py add` for each (or remove it from INBOXES).")
    return services


def run_test(df: pd.DataFrame, test_to: str, n: int, inboxes: list[Inbox]) -> None:
    """Samples rotate across inboxes, so every sending address gets checked."""
    df = render(df.head(max(n, len(inboxes))))
    services = _connect(inboxes)
    print(f"TEST SEND — {len(df)} sample(s) to {test_to} from {len(inboxes)} inbox(es)\n")
    for i, r in df.iterrows():
        ib = inboxes[i % len(inboxes)]
        service = services[ib.address]
        frm = sender_address(service)
        # Send the real subject and body verbatim: a "[TEST …]" prefix with
        # email addresses in it is itself a spam signal, so a modified subject
        # would test something you never actually send.
        subject = r["subject"]
        try:
            gid, tid, rfc = send_email(service, test_to, subject, r["body"],
                                       extra_headers={"X-Outreach-Test-To": r["email"]})
            log_send(test_to, r["company"], f"{r['first_name']} {r['last_name']}".strip(),
                     "high", subject, gid, rfc, tid, "test", inbox=frm)
            print(f"  OK  from {frm}: sample for {r['email']} → {test_to}")
        except HttpError as e:
            print(f"  ERROR from {frm}, {r['email']}: {e}")
        if i < len(df) - 1:
            time.sleep(3)
    print("\nCheck the inbox AND the spam folder before going live — one sample per sending address.")
    print("Each sample is the real subject and body; the intended recipient is in the")
    print("X-Outreach-Test-To header (Gmail: ⋮ → Show original).")


def run_live(df: pd.DataFrame, inboxes: list[Inbox], assume_yes: bool = False) -> None:
    plan = assign(len(df), inboxes)
    if not plan:
        print(f"Daily cap reached on every inbox ({inbox_summary(inboxes)}). Run again tomorrow.")
        return
    batch = render(df.head(len(plan)))

    print("=" * 65)
    print(f"LIVE SEND — {len(batch)} email(s) across {len({ib.address for ib in plan})} inbox(es)")
    print(f"Today so far: {inbox_summary(inboxes)}")
    print(f"Delay {SEND_DELAY_MIN:.0f}–{SEND_DELAY_MAX:.0f}s between sends")
    print("=" * 65)
    for i, r in batch.iterrows():
        print(f"  {i+1}. {r['email']}  ({r['first_name']}, {r['title']} @ {r['company']})"
              f"  ← {plan[i].label}")
        print(f"      opener: {r['opener']}")
    print()
    if assume_yes:
        print("--yes: sending without confirmation (unattended run).")
    elif input("Type SEND to confirm, anything else to abort: ").strip() != "SEND":
        print("Aborted.")
        return

    services = _connect(inboxes)
    blocked: set[str] = set()          # inboxes Gmail rate-limited this run
    sent = 0
    for i, r in batch.iterrows():
        ib = plan[i]
        if ib.address in blocked:
            continue
        service = services[ib.address]
        frm = sender_address(service)
        name = f"{r['first_name']} {r['last_name']}".strip()
        print(f"[{i+1}/{len(batch)}] {frm} → {r['email']}")
        try:
            gid, tid, rfc = send_email(service, r["email"], r["subject"], r["body"])
            log_send(r["email"], r["company"], name, "high", r["subject"], gid, rfc, tid, "sent",
                     inbox=frm)
            print(f"  OK  {gid}")
            sent += 1
        except HttpError as e:
            print(f"  ERROR — {e}")
            log_send(r["email"], r["company"], name, "high", r["subject"], "", "", "",
                     f"error: {e.status_code}", inbox=frm)
            if e.status_code in (403, 429):
                print(f"  Gmail is rate-limiting {frm}. No more sends from it this run.")
                blocked.add(ib.address)
                if len(blocked) == len({x.address for x in plan}):
                    break
        if i < len(batch) - 1:
            d = random.uniform(SEND_DELAY_MIN, SEND_DELAY_MAX)
            print(f"  waiting {d:.0f}s…")
            time.sleep(d)

    print(f"\nDone. {sent} sent. {len(df) - sent} still queued — run again tomorrow.")
    print(f"Today: {inbox_summary(inboxes)}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Send the Quelp pitch to a lead list. Default: dry run.")
    p.add_argument("--in", dest="infile", required=True, metavar="CSV", help="Lead list CSV")
    p.add_argument("--cap", type=int, default=None,
                   help=f"Override every inbox's daily cap (default: INBOXES caps, else {DAILY_SEND_CAP})")
    p.add_argument("--limit", type=int, default=0, help="Dry run: only render the first N")
    p.add_argument("--no-size-gate", dest="size_gate", action="store_false",
                   help=f"Include companies outside {MIN_EMPLOYEES}–{MAX_EMPLOYEES} employees")
    m = p.add_mutually_exclusive_group()
    m.add_argument("--dry-run", action="store_true", help="(default) Preview only")
    m.add_argument("--test-to", metavar="EMAIL", help="Send a few samples to this address")
    m.add_argument("--live", action="store_true", help="Send for real (asks for SEND)")
    p.add_argument("--samples", type=int, default=3, help="--test-to: how many (default 3, min 1 per inbox)")
    p.add_argument("--yes", action="store_true",
                   help="Skip the typed SEND confirmation. For scheduled runs only — "
                        "the daily cap is then the ONLY thing limiting a live send.")
    p.add_argument("--from-inbox", metavar="ADDRESS",
                   help="Use only this inbox. Needed for per-domain spam tests: "
                        "mail-tester issues a new address per test, so send one sample per domain.")
    a = p.parse_args()

    path = Path(a.infile).expanduser()
    if not path.exists():
        sys.exit(f"File not found: {path}")

    df = build_queue(path, a.size_gate)
    if df.empty:
        print("Nothing to send.")
        return

    inboxes = configured(a.cap)
    if a.from_inbox:
        want = a.from_inbox.strip().lower()
        match = [ib for ib in inboxes if ib.address == want]
        if not match:
            # Not in INBOXES — allowed if it has a token, so you can test an
            # inbox that is still only warming up.
            if not token_path(want).exists():
                sys.exit(f"{want} is not connected. Run: python inbox.py add")
            match = [Inbox(want, a.cap or DAILY_SEND_CAP)]
        inboxes = match
    if a.test_to:
        run_test(df, a.test_to, a.samples, inboxes)
    elif a.live:
        run_live(df, inboxes, a.yes)
    else:
        run_dry(df, inboxes, a.limit)


if __name__ == "__main__":
    main()
