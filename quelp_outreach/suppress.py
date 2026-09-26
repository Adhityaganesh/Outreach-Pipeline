"""
Manage the do-not-email list (data/suppress.txt).

  python suppress.py add jane@acme.com @competitor.com
  python suppress.py list

Anyone who replies "no" / "unsubscribe" goes here. Emails and whole domains
(@domain.com) are both supported. list_send.py and followup.py skip them.
followup.py adds bounced addresses automatically.
"""

import argparse

from sent_log import add_suppressed, load_suppressed


def main() -> None:
    p = argparse.ArgumentParser(description="Manage the suppression list.")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="Add emails or @domains")
    a.add_argument("entries", nargs="+")
    sub.add_parser("list", help="Show the list")
    args = p.parse_args()

    if args.cmd == "add":
        n = add_suppressed(args.entries)
        print(f"Added {n} new entr{'y' if n == 1 else 'ies'}.")
    else:
        for e in sorted(load_suppressed()):
            print(e)


if __name__ == "__main__":
    main()
