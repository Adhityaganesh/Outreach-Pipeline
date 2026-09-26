"""
The whole pipeline, once a day, unattended.

  python daily.py --dry-run     # show what today's run would do, send nothing
  python daily.py               # the real run (what the scheduler calls)
  python daily.py --pause       # stop all scheduled runs
  python daily.py --resume
  python daily.py --status      # queue depth, caps, credits, last run

Each run, in this order:
  1. FOLLOW-UPS   reads every open thread, records replies, suppresses bounces,
                  sends day-N follow-ups. First, so a bounced address cannot
                  receive anything else today.
  2. TOP UP       only when the unsent queue is below --min-queue. Fetches the
                  NEXT Prospeo page (never page 1 again — see state below) and
                  verifies addresses with Clearout, inside a credit cap.
  3. SEND         first emails, up to each inbox's daily cap.

Stops before step 3 and exits non-zero if anything in step 1 fails, so a
broken run never silently turns into a send.

State lives in data/daily_state.json — mainly next_prospeo_page. Prospeo
caches a page for 30 days, so re-fetching page 1 daily would return the same
people, who are already contacted, and the queue would never refill.

SAFETY: this sends real email with no human confirmation. What limits it:
  - the per-inbox daily cap in INBOXES (currently the only send limit)
  - --max-credits per run for Clearout
  - --max-pages per run for Prospeo
  - data/PAUSED stops everything
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from config import BASE_DIR, CLEAROUT_CREDIT_CAP, DATA_DIR
from inboxes import configured
from inboxes import summary as inbox_summary
from sent_log import contacted_emails, load_suppressed, is_suppressed

_STATE = DATA_DIR / "daily_state.json"
_PAUSED = DATA_DIR / "PAUSED"
_LEADS = DATA_DIR / "leads_found.csv"
_LOGS = DATA_DIR / "logs"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state() -> dict:
    if _STATE.exists():
        try:
            return json.loads(_STATE.read_text())
        except ValueError:
            print("[warn] daily_state.json unreadable — starting fresh")
    return {"next_prospeo_page": 1, "runs": []}


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state["runs"] = state.get("runs", [])[-30:]
    _STATE.write_text(json.dumps(state, indent=2))


def queue_depth() -> int:
    """Leads in leads_found.csv not yet contacted and not suppressed."""
    if not _LEADS.exists():
        return 0
    df = pd.read_csv(_LEADS, dtype=str).fillna("")
    done = contacted_emails()
    sup = load_suppressed()
    return sum(1 for e in df["email"].str.lower()
               if e not in done and not is_suppressed(e, sup))


def run_step(name: str, cmd: list[str], dry_run: bool) -> tuple[bool, str]:
    print(f"\n{'=' * 64}\n{name}\n{'=' * 64}")
    printable = " ".join(cmd[1:])
    if dry_run:
        print(f"[dry run] would run: python {printable}")
        return True, ""
    print(f"$ python {printable}\n")
    proc = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    print(out.rstrip())
    if proc.returncode != 0:
        print(f"[{name}] exited {proc.returncode}")
    return proc.returncode == 0, out


def main() -> None:
    p = argparse.ArgumentParser(description="Run the whole pipeline once, unattended.")
    p.add_argument("--dry-run", action="store_true", help="Show the plan, send nothing")
    p.add_argument("--min-queue", type=int, default=10,
                   help="Top up leads when fewer than this remain unsent (default 10)")
    p.add_argument("--max-pages", type=int, default=1,
                   help="Prospeo pages per top-up, 1 credit each (default 1)")
    p.add_argument("--max-credits", type=int, default=CLEAROUT_CREDIT_CAP,
                   help=f"Clearout credit cap per top-up (default {CLEAROUT_CREDIT_CAP})")
    p.add_argument("--no-topup", action="store_true", help="Never fetch new leads")
    p.add_argument("--pause", action="store_true", help="Stop scheduled runs")
    p.add_argument("--resume", action="store_true", help="Undo --pause")
    p.add_argument("--status", action="store_true", help="Show state and exit")
    a = p.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()

    if a.pause:
        _PAUSED.write_text(f"paused {_now()}\n")
        print(f"Paused. Scheduled runs will exit until: python daily.py --resume")
        return
    if a.resume:
        _PAUSED.unlink(missing_ok=True)
        print("Resumed.")
        return

    inboxes = configured()
    if a.status:
        print(f"Paused        : {'YES' if _PAUSED.exists() else 'no'}")
        print(f"Queue         : {queue_depth()} leads unsent")
        print(f"Inboxes       : {inbox_summary(inboxes)}")
        print(f"Next page     : {state.get('next_prospeo_page', 1)}")
        for r in state.get("runs", [])[-5:]:
            print(f"  {r['at']}  sent={r.get('sent', '?')}  topped_up={r.get('topped_up')}"
                  f"  ok={r.get('ok')}")
        return

    if _PAUSED.exists():
        print(f"PAUSED ({_PAUSED.read_text().strip()}) — nothing to do.")
        return

    print(f"Daily run {_now()}")
    print(f"Queue: {queue_depth()} unsent  |  {inbox_summary(inboxes)}")

    # 1. Follow-ups first: records replies, suppresses bounces.
    ok, _ = run_step("1/3  FOLLOW-UPS", [sys.executable, "followup.py", "--live", "--yes"], a.dry_run)
    if not ok and not a.dry_run:
        print("\nFollow-up step failed — stopping before sending anything.")
        state.setdefault("runs", []).append({"at": _now(), "ok": False, "stage": "followup"})
        save_state(state)
        sys.exit(1)

    # 2. Top up only when the queue is short.
    topped_up = False
    depth = queue_depth()
    if a.no_topup:
        print(f"\n2/3  TOP UP — skipped (--no-topup). Queue: {depth}")
    elif depth >= a.min_queue:
        print(f"\n2/3  TOP UP — not needed. Queue {depth} >= {a.min_queue}")
    else:
        page = int(state.get("next_prospeo_page", 1))
        ok, out = run_step(
            f"2/3  TOP UP (queue {depth} < {a.min_queue}, Prospeo page {page})",
            [sys.executable, "find_emails.py", "--prospeo", "--live",
             "--pages", str(a.max_pages), "--start-page", str(page),
             "--max-credits", str(a.max_credits)],
            a.dry_run)
        if not a.dry_run:
            topped_up = ok
            # Advance regardless of yield: a page whose people were all
            # unusable would otherwise be refetched forever.
            state["next_prospeo_page"] = page + a.max_pages

    # 3. Send.
    if not _LEADS.exists():
        print(f"\n3/3  SEND — no {_LEADS.name} yet. Nothing to send.")
        sent_ok = True
    else:
        sent_ok, _ = run_step("3/3  SEND",
                              [sys.executable, "list_send.py", "--in", str(_LEADS),
                               "--live", "--yes"], a.dry_run)

    if not a.dry_run:
        state.setdefault("runs", []).append(
            {"at": _now(), "ok": bool(sent_ok), "topped_up": topped_up,
             "queue_after": queue_depth()})
        save_state(state)

    print(f"\n{'=' * 64}")
    print(f"Done. Queue now: {queue_depth()}  |  {inbox_summary(configured())}")
    if not a.dry_run and not sent_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
