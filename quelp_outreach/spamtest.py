"""
Score every connected inbox on mail-tester, one sample each.

  python spamtest.py --list
  python spamtest.py test-aaa@srv1.mail-tester.com test-bbb@srv1.mail-tester.com ...

Open mail-tester.com once per inbox you want to score, copy each address, and
pass them in order. Addresses are paired with inboxes alphabetically and the
pairing is printed, so you know which result URL belongs to which sender.

Every sample is byte-identical — same subject, same body, same opener — so the
only variable is the sending address. Sends are logged as status 'test', which
keeps them out of the daily cap and out of follow-ups.

Expect results to cluster by DOMAIN, not by mailbox: SpamAssassin's TLD and
content rules see the domain. Scores can still wobble a point or two between
runs because Google rotates outbound relay IPs and some are blocklisted.
"""

import argparse
import sys
import time

import pitch
from config import TOKENS_DIR
from gmail_auth import InboxNotConnected, get_gmail_service, send_email, sender_address
from sent_log import log_send

# Fixed sample lead — identical for every inbox so the test isolates the sender.
_SAMPLE = {"first_name": "Priya", "title": "Head of Sales", "company": "Northwind Cloud"}


def connected() -> list[str]:
    return sorted(f.stem for f in TOKENS_DIR.glob("*.json"))


def main() -> None:
    p = argparse.ArgumentParser(description="Send one identical sample from each connected inbox.")
    p.add_argument("addresses", nargs="*", metavar="MAIL_TESTER_ADDRESS",
                   help="One address per inbox, in the order shown by --list")
    p.add_argument("--list", action="store_true", help="Show connected inboxes and exit")
    p.add_argument("--only", metavar="ADDRESS", action="append",
                   help="Restrict to this inbox (repeatable)")
    a = p.parse_args()

    inboxes = connected()
    if a.only:
        want = {x.strip().lower() for x in a.only}
        missing = want - set(inboxes)
        if missing:
            sys.exit(f"Not connected: {', '.join(sorted(missing))}")
        inboxes = [i for i in inboxes if i in want]

    if a.list or not a.addresses:
        print(f"{len(inboxes)} connected inbox(es), in pairing order:\n")
        for i, address in enumerate(inboxes, 1):
            print(f"  {i}. {address}")
        print(f"\nGet {len(inboxes)} address(es) from mail-tester.com, then:")
        print("  python spamtest.py " + " ".join(f"ADDR{i+1}" for i in range(len(inboxes))))
        print("\nFewer addresses than inboxes is fine — the extra inboxes are skipped.")
        return

    if len(a.addresses) > len(inboxes):
        sys.exit(f"{len(a.addresses)} addresses but only {len(inboxes)} inboxes connected.")

    pairs = list(zip(inboxes, a.addresses))
    opener = pitch.fallback_opener(_SAMPLE["title"], _SAMPLE["company"])
    subject, body = pitch.render_first_email(_SAMPLE["first_name"], opener)

    print("Pairing:\n")
    for inbox, address in pairs:
        print(f"  {inbox:<32} → {address}")
    print(f"\nSubject: {subject}")
    print(f"Body: {len(body)} chars, identical for every inbox\n")

    for n, (inbox, address) in enumerate(pairs, 1):
        try:
            service = get_gmail_service(inbox)
        except InboxNotConnected as e:
            print(f"[{n}/{len(pairs)}] {inbox}: {e}")
            continue
        frm = sender_address(service)
        try:
            gid, tid, rfc = send_email(service, address, subject, body)
            log_send(address, _SAMPLE["company"], _SAMPLE["first_name"], "high",
                     subject, gid, rfc, tid, "test", inbox=frm)
            print(f"[{n}/{len(pairs)}] OK  {frm} → {address}")
        except Exception as e:
            print(f"[{n}/{len(pairs)}] ERROR {frm}: {e}")
        if n < len(pairs):
            time.sleep(4)

    print("\nWait ~30s, then open each mail-tester result page in the order above.")
    print("Domains will cluster. If two inboxes on the SAME domain differ by more")
    print("than a point, it is almost certainly relay-IP luck, not the mailbox.")


if __name__ == "__main__":
    main()
