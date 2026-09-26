"""
Results per sending inbox / domain — for judging a domain trial.

  python stats.py                 # all time
  python stats.py --days 15       # first emails sent in the last 15 days

Replies and bounces are recorded by followup.py (it reads every thread),
so run `python followup.py` (dry run is fine) first for up-to-date numbers.

Rough guide: bounce rate under 2% is healthy; 0 replies after ~100 sends,
or replies asking who you are, means the sender domain or copy isn't working.
"""

import argparse
from datetime import datetime, timedelta, timezone

import pandas as pd

from sent_log import load_log


def main() -> None:
    p = argparse.ArgumentParser(description="Reply / bounce stats per inbox and domain.")
    p.add_argument("--days", type=int, default=0, help="Only first emails from the last N days")
    a = p.parse_args()

    df = load_log()
    df = df[df["status"].isin(["sent", "followed_up", "replied", "bounced"])].copy()
    if a.days:
        since = datetime.now(timezone.utc) - timedelta(days=a.days)
        df = df[pd.to_datetime(df["sent_at"], utc=True, errors="coerce") >= since]
    if df.empty:
        print("No real sends in the log yet.")
        return

    df["inbox"] = df["inbox"].replace("", "(default / old rows)")
    df["domain"] = df["inbox"].str.split("@").str[-1]

    def table(key: str) -> pd.DataFrame:
        g = df.groupby(key)
        t = pd.DataFrame({
            "sent": g.size(),
            "followed_up": g.apply(lambda x: (x["followup_sent_at"] != "").sum()),
            "replied": g.apply(lambda x: (x["status"] == "replied").sum()),
            "bounced": g.apply(lambda x: (x["status"] == "bounced").sum()),
        })
        t["reply_%"] = (100 * t["replied"] / t["sent"]).round(1)
        t["bounce_%"] = (100 * t["bounced"] / t["sent"]).round(1)
        return t

    span = f"last {a.days} days" if a.days else "all time"
    print(f"Per domain ({span})\n")
    print(table("domain").to_string())
    print(f"\nPer inbox ({span})\n")
    print(table("inbox").to_string())
    print("\n'replied' counts any reply, including \"no\" — read them to judge interest.")


if __name__ == "__main__":
    main()
