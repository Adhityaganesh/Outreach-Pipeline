"""
Score the pitch through SpamAssassin WITHOUT sending anything.

  python spamcheck.py                      # every connected inbox
  python spamcheck.py --from adhitya@intray.email
  python spamcheck.py --domain intray.email --domain thozil.xyz

Uses Postmark's free SpamCheck API (spamcheck.postmarkapp.com) — no signup,
no credits, no daily limit, and no email leaves your machine. mail-tester
allows only 3 free tests a day, so use this for iterating on copy and domains
and save mail-tester for a final confirmation.

SCORE IS POINTS AGAINST YOU: lower is better, and 5.0 is the usual spam
threshold. This checks CONTENT and FROM-DOMAIN rules, which is the part you
control. It cannot check what only exists after sending:
  - DKIM/SPF validation (verified separately: both pass)
  - the reputation of the Google relay IP used that moment
  - Gmail's own reputation model, which is not SpamAssassin at all
So treat this as "is the message itself clean", not as a delivery guarantee.
"""

import argparse
import base64
import sys
from email.utils import formatdate, make_msgid

import httpx

import pitch
from config import TOKENS_DIR
from gmail_auth import build_raw

_API = "https://spamcheck.postmarkapp.com/filter"

# Identical sample for every sender, so the address is the only variable.
_SAMPLE = {"first_name": "Priya", "title": "Head of Sales", "company": "Northwind Cloud"}


class _FakeService:
    """Stands in for the Gmail service so we can build a message offline."""

    def __init__(self, address: str):
        self._address = address

    def users(self):
        return self

    def getProfile(self, userId):
        return self

    def execute(self):
        return {"emailAddress": self._address}


def connected_inboxes() -> list[str]:
    return sorted(f.stem for f in TOKENS_DIR.glob("*.json"))


def build_message(from_address: str) -> str:
    opener = pitch.fallback_opener(_SAMPLE["title"], _SAMPLE["company"])
    subject, body = pitch.render_first_email(_SAMPLE["first_name"], opener)
    raw = build_raw(_FakeService(from_address), "priya@northwind.example", subject, body)
    msg = base64.urlsafe_b64decode(raw).decode()
    # Gmail stamps Date and Message-ID at send time. Without them SpamAssassin
    # charges ~1.5 points that a real send never incurs, so add them here.
    domain = from_address.split("@")[-1]
    headers = f"Date: {formatdate(localtime=True)}\r\nMessage-ID: {make_msgid(domain=domain)}\r\n"
    return headers + msg


def check(from_address: str) -> tuple[float, list]:
    r = httpx.post(_API, json={"email": build_message(from_address), "options": "long"}, timeout=60)
    r.raise_for_status()
    data = r.json()
    if not data.get("success", True) and "score" not in data:
        raise RuntimeError(data.get("message", str(data)))
    return float(data.get("score", 0)), data.get("rules") or []


def main() -> None:
    p = argparse.ArgumentParser(description="SpamAssassin score for the pitch, without sending.")
    p.add_argument("--from", dest="senders", action="append", metavar="ADDRESS",
                   help="Score this sending address (repeatable)")
    p.add_argument("--domain", action="append", metavar="DOMAIN",
                   help="Score adhitya@DOMAIN — handy for a domain you do not own yet")
    p.add_argument("--all-rules", action="store_true", help="Show zero-scoring rules too")
    a = p.parse_args()

    senders = list(a.senders or [])
    senders += [f"adhitya@{d.lstrip('@')}" for d in (a.domain or [])]
    if not senders:
        senders = connected_inboxes()
    if not senders:
        sys.exit("No connected inboxes. Use --from ADDRESS or run: python inbox.py add")

    print("SpamAssassin points AGAINST the message — lower is better, 5.0 = spam\n")
    results = []
    for address in senders:
        try:
            score, rules = check(address)
        except Exception as e:
            print(f"{address:<32} ERROR: {e}")
            continue
        results.append((address, score))
        verdict = "clean" if score < 2 else "borderline" if score < 5 else "WOULD BE SPAM"
        print(f"{address:<32} {score:>5.1f}  {verdict}")
        for rule in rules:
            rscore = float(rule.get("score") or 0)
            if rscore == 0 and not a.all_rules:
                continue
            desc = (rule.get("description") or rule.get("name") or "").strip()
            if desc.startswith("ADMINISTRATOR NOTICE"):
                continue
            print(f"      {rscore:+.1f}  {desc[:72]}")
        print()

    by_domain: dict[str, list[float]] = {}
    for address, score in results:
        by_domain.setdefault(address.split("@")[-1], []).append(score)
    if len(by_domain) > 1:
        print("Per domain (mean):")
        for domain, scores in sorted(by_domain.items(), key=lambda x: sum(x[1]) / len(x[1])):
            print(f"  {domain:<24} {sum(scores) / len(scores):>5.1f}")


if __name__ == "__main__":
    main()
