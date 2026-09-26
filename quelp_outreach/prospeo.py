"""
Prospeo people search — names, titles and company domains. NO email reveals.

Cost: 1 credit per search page that returns at least one person (25 per page).
Prospeo itself doesn't re-charge the same page within 30 days, and pages are
also cached locally in cache.sqlite, so re-runs don't even make the request.

Filters live in prospeo_filters.json (see the Filters Documentation at
prospeo.io/api-docs/filters-documentation). Keys starting with "_" are notes
and are stripped before sending. Easiest way to build filters: set up a
search in the Prospeo dashboard → "..." → Search API, and copy the payload.
"""

import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from config import CACHE_DB, PROSPEO_API_KEY

_BASE = "https://api.prospeo.io"
_CACHE_DAYS = 30


def _headers() -> dict:
    if not PROSPEO_API_KEY:
        sys.exit("PROSPEO_API_KEY is not set in .env.")
    return {"X-KEY": PROSPEO_API_KEY, "Content-Type": "application/json"}


def load_filters(path: Path) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def credits() -> int | None:
    """Remaining Prospeo credits (free call)."""
    try:
        r = httpx.get(f"{_BASE}/account-information", headers=_headers(), timeout=15)
        return (r.json().get("response") or {}).get("remaining_credits")
    except Exception as e:
        print(f"    [prospeo] could not read balance: {e}")
        return None


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(CACHE_DB)
    c.execute("CREATE TABLE IF NOT EXISTS prospeo_pages "
              "(key TEXT PRIMARY KEY, body TEXT NOT NULL, fetched_at TEXT NOT NULL)")
    return c


def search_page(filters: dict, page: int) -> tuple[dict, bool]:
    """
    Return (response_json, charged). charged is False for cache hits and
    Prospeo's own free repeats. Raises SystemExit on bad filters/key.
    """
    key = hashlib.sha1(json.dumps([filters, page], sort_keys=True).encode()).hexdigest()
    conn = _conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_CACHE_DAYS)).isoformat()
    row = conn.execute("SELECT body FROM prospeo_pages WHERE key=? AND fetched_at>?",
                       (key, cutoff)).fetchone()
    if row:
        conn.close()
        return json.loads(row[0]), False

    for attempt in range(4):
        r = httpx.post(f"{_BASE}/search-person", headers=_headers(),
                       json={"page": page, "filters": filters}, timeout=30)
        if r.status_code == 429:
            time.sleep(2 + attempt * 2)
            continue
        break
    data = r.json()

    if data.get("error"):
        code = data.get("error_code", "")
        conn.close()
        if code == "NO_RESULTS":
            return {"results": [], "pagination": {"total_page": 0}}, False
        if code in ("INVALID_FILTERS", "PLAN_REQUIRED"):
            sys.exit(f"Prospeo rejected the filters ({code}): {data.get('filter_error')}")
        if code == "INVALID_API_KEY":
            sys.exit("Prospeo rejected the API key — check PROSPEO_API_KEY.")
        if code == "INSUFFICIENT_CREDITS":
            sys.exit("Prospeo: out of credits.")
        sys.exit(f"Prospeo error: {code} {data}")

    conn.execute("INSERT OR REPLACE INTO prospeo_pages VALUES (?, ?, ?)",
                 (key, json.dumps(data), datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()
    return data, not data.get("free", False)


def to_row(result: dict) -> dict:
    p = result.get("person") or {}
    c = result.get("company") or {}
    email_info = p.get("email") or {}
    return {
        "first_name": p.get("first_name") or "",
        "last_name":  p.get("last_name") or "",
        "full_name":  p.get("full_name") or "",
        "title":      p.get("current_job_title") or p.get("job_title") or "",
        "linkedin":   p.get("linkedin_url") or "",
        "company":    c.get("name") or "",
        "domain":     c.get("domain") or c.get("website") or "",
        "employees":  c.get("employee_count") or c.get("employee_range") or "",
        "industry":   c.get("industry") or "",
        # VERIFIED means Prospeo has an email we could reveal (paid) — a
        # hint for the optional fallback, never revealed here.
        "prospeo_email_status": email_info.get("status") or "",
    }


def search_people(filters: dict, pages: int, start_page: int = 1) -> tuple[list[dict], int]:
    """Return (rows, credits_charged) for pages start_page..start_page+pages-1."""
    rows, charged = [], 0
    for page in range(start_page, start_page + pages):
        data, paid = search_page(filters, page)
        results = data.get("results") or []
        charged += int(paid and bool(results))
        rows.extend(to_row(r) for r in results)
        total = (data.get("pagination") or {}).get("total_page") or 0
        print(f"  Prospeo page {page}: {len(results)} people"
              f"{'' if paid else ' (free)'}  [total pages: {total}]")
        if page >= total:
            break
        time.sleep(1.1)          # Starter plan: 1 search request / second
    return rows, charged
