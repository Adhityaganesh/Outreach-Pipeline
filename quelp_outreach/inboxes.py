"""
Sending inboxes — which ones exist, their daily caps, and how a batch is
spread across them.

Configured with INBOXES in .env:
  INBOXES=adhitya@getquelp.com:15,team@getquelp.com:10
Without INBOXES, there's one inbox: the default token.json account ("").

Deliverability note: spam reputation is per DOMAIN as well as per inbox.
More inboxes on one domain spreads per-mailbox load, not domain risk —
keep the total modest while a domain is new.
"""

from dataclasses import dataclass

from config import DAILY_SEND_CAP, INBOXES
from sent_log import remaining_today, sent_today


@dataclass
class Inbox:
    address: str      # "" = default token.json account
    cap: int

    @property
    def label(self) -> str:
        return self.address or "default inbox"

    def remaining(self) -> int:
        return remaining_today(self.cap, self.address or None)

    def sent(self) -> int:
        return sent_today(self.address or None)


def configured(cap_override: int | None = None) -> list[Inbox]:
    """Inboxes from .env. cap_override (e.g. --cap) replaces every inbox's cap."""
    out = []
    for part in INBOXES.split(","):
        part = part.strip()
        if not part:
            continue
        addr, _, cap = part.partition(":")
        c = int(cap) if cap.strip().isdigit() else DAILY_SEND_CAP
        out.append(Inbox(addr.strip().lower(), cap_override if cap_override is not None else c))
    if not out:
        out = [Inbox("", cap_override if cap_override is not None else DAILY_SEND_CAP)]
    return out


def cap_for(address: str, inboxes: list[Inbox]) -> int:
    for ib in inboxes:
        if ib.address == (address or "").lower():
            return ib.cap
    return inboxes[0].cap if len(inboxes) == 1 else DAILY_SEND_CAP


def assign(n: int, inboxes: list[Inbox]) -> list[Inbox]:
    """
    Round-robin n sends across inboxes that still have capacity today.
    Returns up to n Inbox entries (fewer if total capacity runs out).
    """
    left = {ib.address: ib.remaining() for ib in inboxes}
    out: list[Inbox] = []
    while len(out) < n and any(v > 0 for v in left.values()):
        for ib in inboxes:
            if len(out) >= n:
                break
            if left[ib.address] > 0:
                out.append(ib)
                left[ib.address] -= 1
    return out


def summary(inboxes: list[Inbox]) -> str:
    return "  |  ".join(f"{ib.label}: {ib.sent()}/{ib.cap}" for ib in inboxes)
