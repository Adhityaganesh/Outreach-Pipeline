"""
Connect and check sending inboxes.

  python inbox.py add      # browser sign-in; saves tokens/<address>.json
  python inbox.py list     # configured inboxes, connection status, sent today / cap

After adding, list the address in INBOXES in .env, e.g.
  INBOXES=adhitya@getquelp.com:15,team@getquelp.com:10
"""

import argparse

from gmail_auth import InboxNotConnected, add_inbox, get_gmail_service
from inboxes import configured


def main() -> None:
    p = argparse.ArgumentParser(description="Manage sending inboxes.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("add", help="Sign in to a new inbox (opens a browser)")
    sub.add_parser("list", help="Show inboxes, connection and today's usage")
    args = p.parse_args()

    if args.cmd == "add":
        print("A browser window will open — sign in as the inbox you want to add.")
        address = add_inbox()
        listed = {ib.address for ib in configured()}
        print(f"\nConnected {address}.")
        if address not in listed:
            print(f"Now add it to INBOXES in .env, e.g.  INBOXES={address}:15")
        return

    for ib in configured():
        try:
            get_gmail_service(ib.address)
            state = "connected"
        except InboxNotConnected:
            state = "NOT connected — run: python inbox.py add"
        print(f"{ib.label:<32} sent today {ib.sent():>3}/{ib.cap:<3}  {state}")


if __name__ == "__main__":
    main()
