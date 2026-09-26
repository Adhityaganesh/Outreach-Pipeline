"""
Block 6 — Follow-up
Sends ONE follow-up, in the same thread, to people who got the first email
FOLLOWUP_DAYS+ days ago and haven't replied.

  python followup.py            # dry run (default)
  python followup.py --live     # send, asks for SEND

Before sending, every thread is checked:
  - any message from someone other than you → status 'replied', never emailed again
  - a delivery-failure notice              → status 'bounced', address suppressed
Only emails sent with the CURRENT pitch.SUBJECT get a follow-up, so leads
from an older campaign never receive a mismatched nudge.
Follow-ups count toward the same daily cap as first emails.
"""

import argparse
import random
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from googleapiclient.errors import HttpError

import pitch
from config import DAILY_SEND_CAP, FOLLOWUP_DAYS, SEND_DELAY_MAX, SEND_DELAY_MIN
from gmail_auth import get_gmail_service, get_rfc_message_id, send_email
from sent_log import (
    add_suppressed,
    is_suppressed,
    load_log,
    load_suppressed,
    remaining_today,
    save_log,
    sent_today,
)

_BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail delivery")


# ---------------------------------------------------------------------------
# Thread inspection
# ---------------------------------------------------------------------------

def _thread_state(service, thread_id: str) -> tuple[str, str]:
    """
    Return (state, subject) where state is 'none' | 'replied' | 'bounced' | 'error'.
    Any message in the thread that you didn't send counts: replies from a
    colleague or a different alias are still replies.
    """
    try:
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
    except HttpError as e:
        print(f"    [warn] could not read thread {thread_id}: {e.status_code}")
        return "error", ""

    msgs = thread.get("messages", [])
    subject = ""
    state = "none"
    for i, m in enumerate(msgs):
        headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        if i == 0:
            subject = headers.get("subject", "")
        if "SENT" in m.get("labelIds", []):
            continue
        frm = headers.get("from", "").lower()
        if any(b in frm for b in _BOUNCE_SENDERS):
            state = "bounced"
        else:
            return "replied", subject      # a human reply wins over everything
    return state, subject


def _norm_subject(s: str) -> str:
    s = str(s).strip()
    while s.lower().startswith("re:"):
        s = s[3:].strip()
    return s.lower()


def _first_name(name: str) -> str:
    n = str(name).strip()
    return n.split()[0] if n else "there"


def classify(df: pd.DataFrame, service, now: datetime):
    cutoff = now - timedelta(days=FOLLOWUP_DAYS)
    suppressed = load_suppressed()
    due, replied, bounced, skip = [], [], [], []

    for idx, row in df.iterrows():
        if row["status"] != "sent" or not row["thread_id"]:
            skip.append((idx, f"status={row['status'] or 'blank'}"))
            continue
        sent_at = pd.to_datetime(row["sent_at"], utc=True, errors="coerce")
        if pd.isna(sent_at):
            skip.append((idx, "no sent_at"))
            continue

        state, subject = _thread_state(service, row["thread_id"])
        if subject and not row["subject"]:
            df.at[idx, "subject"] = subject          # backfill old logs
        if state == "replied":
            replied.append(idx)
        elif state == "bounced":
            bounced.append(idx)
        elif _norm_subject(df.at[idx, "subject"]) != _norm_subject(pitch.SUBJECT):
            # Sent under an older pitch (e.g. the support-inbox campaign) —
            # a sales-call follow-up in that thread would make no sense.
            skip.append((idx, "earlier campaign — different pitch"))
        elif is_suppressed(row["email"], suppressed):
            skip.append((idx, "suppressed"))
        elif sent_at > cutoff:
            age = (now - sent_at).total_seconds() / 86400
            skip.append((idx, f"too recent ({age:.1f}d < {FOLLOWUP_DAYS}d)"))
        elif state == "error":
            skip.append((idx, "thread unreadable"))
        else:
            due.append(idx)
    return due, replied, bounced, skip


def _apply_states(df, replied, bounced) -> None:
    if replied:
        df.loc[replied, "status"] = "replied"
    if bounced:
        df.loc[bounced, "status"] = "bounced"
        add_suppressed(df.loc[bounced, "email"].tolist())


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def run(live: bool, cap: int) -> None:
    df = load_log()
    if df.empty:
        print("sent_log.csv is empty — send some first emails first.")
        return

    service = get_gmail_service()
    now = datetime.now(timezone.utc)
    due, replied, bounced, skip = classify(df, service, now)

    for idx in replied:
        print(f"[REPLIED]  {df.at[idx, 'email']}  ({df.at[idx, 'name']}) — won't email again")
    for idx in bounced:
        print(f"[BOUNCED]  {df.at[idx, 'email']} — added to suppression list")
    for idx, why in skip:
        if df.at[idx, "status"] in ("sent",):
            print(f"[SKIP]     {df.at[idx, 'email']} — {why}")
    for idx in due:
        print(f"[DUE]      {df.at[idx, 'email']}  ({df.at[idx, 'name']}, {df.at[idx, 'company']})")

    # Status updates are safe in dry run too — they only record what Gmail shows.
    _apply_states(df, replied, bounced)
    save_log(df)

    left = remaining_today(cap)
    batch = due[:left]
    print(f"\nDue: {len(due)}  |  Replied: {len(replied)}  |  Bounced: {len(bounced)}  |  "
          f"Today: {sent_today()}/{cap} → can send {len(batch)} now")

    if not live or not batch:
        if not live:
            print("\nDry run. Preview of the follow-up:\n")
            print(pitch.render_followup("Priya"))
        return

    if input("\nType SEND to confirm, anything else to abort: ").strip() != "SEND":
        print("Aborted.")
        return

    sent = 0
    for n, idx in enumerate(batch):
        row = df.loc[idx]
        subject = row["subject"] or pitch.SUBJECT
        subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        rfc = row["rfc_message_id"] or get_rfc_message_id(service, row["gmail_message_id"])
        print(f"[{n+1}/{len(batch)}] → {row['email']}")
        try:
            gid, _, _ = send_email(
                service, row["email"], subject, pitch.render_followup(_first_name(row["name"])),
                thread_id=row["thread_id"], in_reply_to=rfc,
            )
            df.at[idx, "status"] = "followed_up"
            df.at[idx, "followup_sent_at"] = datetime.now(timezone.utc).isoformat()
            df.at[idx, "followup_message_id"] = gid
            save_log(df)                      # save as we go — safe on Ctrl-C
            print(f"  OK  {gid}")
            sent += 1
        except HttpError as e:
            print(f"  ERROR — {e}")
            if e.status_code in (403, 429):
                print("  Gmail is rate-limiting this account. Stopping.")
                break
        if n < len(batch) - 1:
            d = random.uniform(SEND_DELAY_MIN, SEND_DELAY_MAX)
            print(f"  waiting {d:.0f}s…")
            time.sleep(d)

    print(f"\nDone. {sent} follow-up(s) sent. {len(due) - sent} still due.")


def main() -> None:
    p = argparse.ArgumentParser(description="Follow up on non-replied leads. Default: dry run.")
    p.add_argument("--cap", type=int, default=DAILY_SEND_CAP,
                   help=f"Daily cap shared with first emails (default {DAILY_SEND_CAP})")
    m = p.add_mutually_exclusive_group()
    m.add_argument("--dry-run", action="store_true", help="(default) Preview only")
    m.add_argument("--live", action="store_true", help="Send for real (asks for SEND)")
    a = p.parse_args()
    run(a.live, a.cap)


if __name__ == "__main__":
    main()
