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
Follow-ups go out from the inbox that sent the first email, and count toward
that inbox's daily cap together with first emails.
"""

import argparse
import random
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from googleapiclient.errors import HttpError

import pitch
from config import DAILY_SEND_CAP, FOLLOWUP_DAYS, SEND_DELAY_MAX, SEND_DELAY_MIN
from gmail_auth import InboxNotConnected, get_gmail_service, get_rfc_message_id, send_email
from inboxes import Inbox, configured
from inboxes import summary as inbox_summary
from sent_log import (
    add_suppressed,
    is_suppressed,
    load_log,
    load_suppressed,
    save_log,
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


def _parse_utc(value) -> datetime | None:
    """
    Parse an ISO timestamp from the log.

    Deliberately stdlib, not pd.to_datetime: on some pandas builds (2.2.2 on
    CPython 3.13) that call SEGFAULTS the interpreter, which killed the whole
    daily run before it printed anything. These values are ISO strings this
    pipeline wrote itself, so fromisoformat is enough — and cannot crash.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _first_name(name: str) -> str:
    n = str(name).strip()
    return n.split()[0] if n else "there"


class _Services:
    """
    Gmail service per sending inbox. A follow-up must come from the inbox
    that sent the first email — the thread only exists in that mailbox.
    Rows logged before multi-inbox support have a blank inbox → default account.
    """
    def __init__(self):
        self._cache: dict[str, object] = {}

    def get(self, inbox: str):
        key = str(inbox or "").strip().lower()
        if key not in self._cache:
            try:
                self._cache[key] = get_gmail_service(key)
            except InboxNotConnected:
                self._cache[key] = None
        return self._cache[key]


class _Budget:
    """Remaining sends today per inbox (single-inbox mode: one shared budget)."""
    def __init__(self, inboxes: list[Inbox]):
        self.inboxes = inboxes
        self.single = len(inboxes) == 1 and not inboxes[0].address
        self._left: dict[str, int] = {}

    def _key(self, inbox: str) -> str:
        return "" if self.single else str(inbox or "").lower()

    def left(self, inbox: str) -> int:
        k = self._key(inbox)
        if k not in self._left:
            match = [ib for ib in self.inboxes if ib.address == k]
            ib = match[0] if match else Inbox(k, DAILY_SEND_CAP)
            self._left[k] = ib.remaining()
        return self._left[k]

    def use(self, inbox: str) -> None:
        self._left[self._key(inbox)] = self.left(inbox) - 1


def classify(df: pd.DataFrame, services: _Services, now: datetime):
    cutoff = now - timedelta(days=FOLLOWUP_DAYS)
    suppressed = load_suppressed()
    due, replied, bounced, skip = [], [], [], []

    for idx, row in df.iterrows():
        # followed_up rows are still checked so a reply to the follow-up is recorded
        if row["status"] not in ("sent", "followed_up") or not row["thread_id"]:
            skip.append((idx, f"status={row['status'] or 'blank'}"))
            continue
        sent_at = _parse_utc(row["sent_at"])
        if sent_at is None:
            skip.append((idx, "no sent_at"))
            continue

        service = services.get(row["inbox"])
        if service is None:
            skip.append((idx, f"inbox {row['inbox']} not connected — python inbox.py add"))
            continue
        state, subject = _thread_state(service, row["thread_id"])
        if subject and not row["subject"]:
            df.at[idx, "subject"] = subject          # backfill old logs
        if state == "replied":
            replied.append(idx)
        elif state == "bounced":
            bounced.append(idx)
        elif row["status"] == "followed_up":
            skip.append((idx, "already followed up"))
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

def run(live: bool, cap: int | None, assume_yes: bool = False) -> None:
    df = load_log()
    if df.empty:
        print("sent_log.csv is empty — send some first emails first.")
        return

    inboxes = configured(cap)
    services = _Services()
    now = datetime.now(timezone.utc)
    due, replied, bounced, skip = classify(df, services, now)

    for idx in replied:
        print(f"[REPLIED]  {df.at[idx, 'email']}  ({df.at[idx, 'name']}) — won't email again")
    for idx in bounced:
        print(f"[BOUNCED]  {df.at[idx, 'email']} — added to suppression list")
    for idx, why in skip:
        if df.at[idx, "status"] in ("sent",):
            print(f"[SKIP]     {df.at[idx, 'email']} — {why}")
    for idx in due:
        print(f"[DUE]      {df.at[idx, 'email']}  ({df.at[idx, 'name']}, {df.at[idx, 'company']})"
              f"  ← {df.at[idx, 'inbox'] or 'default inbox'}")

    # Status updates are safe in dry run too — they only record what Gmail shows.
    _apply_states(df, replied, bounced)
    save_log(df)

    # Each follow-up counts against the cap of the inbox that sends it
    budget = _Budget(inboxes)
    batch = []
    for idx in due:
        if budget.left(df.at[idx, "inbox"]) > 0:
            batch.append(idx)
            budget.use(df.at[idx, "inbox"])
    print(f"\nDue: {len(due)}  |  Replied: {len(replied)}  |  Bounced: {len(bounced)}  |  "
          f"can send {len(batch)} now")
    print(f"Today: {inbox_summary(inboxes)}")

    if not live or not batch:
        if not live:
            print("\nDry run. Preview of the follow-up:\n")
            print(pitch.render_followup("Priya"))
        return

    if assume_yes:
        print("--yes: sending without confirmation (unattended run).")
    elif input("\nType SEND to confirm, anything else to abort: ").strip() != "SEND":
        print("Aborted.")
        return

    sent = 0
    blocked: set[str] = set()
    for n, idx in enumerate(batch):
        row = df.loc[idx]
        if row["inbox"] in blocked:
            continue
        service = services.get(row["inbox"])
        subject = row["subject"] or pitch.SUBJECT
        subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        rfc = row["rfc_message_id"] or get_rfc_message_id(service, row["gmail_message_id"])
        print(f"[{n+1}/{len(batch)}] {row['inbox'] or 'default inbox'} → {row['email']}")
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
                print(f"  Gmail is rate-limiting {row['inbox'] or 'this account'}. "
                      "No more follow-ups from it this run.")
                blocked.add(row["inbox"])
        if n < len(batch) - 1:
            d = random.uniform(SEND_DELAY_MIN, SEND_DELAY_MAX)
            print(f"  waiting {d:.0f}s…")
            time.sleep(d)

    print(f"\nDone. {sent} follow-up(s) sent. {len(due) - sent} still due.")


def main() -> None:
    p = argparse.ArgumentParser(description="Follow up on non-replied leads. Default: dry run.")
    p.add_argument("--cap", type=int, default=None,
                   help="Override every inbox's daily cap (shared with first emails; "
                        f"default: INBOXES caps, else {DAILY_SEND_CAP})")
    m = p.add_mutually_exclusive_group()
    m.add_argument("--dry-run", action="store_true", help="(default) Preview only")
    m.add_argument("--live", action="store_true", help="Send for real (asks for SEND)")
    p.add_argument("--yes", action="store_true",
                   help="Skip the typed SEND confirmation. For scheduled runs only.")
    a = p.parse_args()
    run(a.live, a.cap, a.yes)


if __name__ == "__main__":
    main()
