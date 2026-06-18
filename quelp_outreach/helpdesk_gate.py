"""
Block 2 — Helpdesk Gate
Rejects companies already running a helpdesk (Zendesk, Freshdesk, Intercom,
Helpscout, Zoho Desk) via HTML fingerprinting and CNAME checks.
"""

import argparse
import asyncio
import random
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import TypedDict

import dns.resolver
import httpx
import pandas as pd

from config import CACHE_DB, DNS_TIMEOUT_SECONDS

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class HelpdeskResult(TypedDict):
    helpdesk: str | None   # "zendesk"|"freshdesk"|"intercom"|"helpscout"|"zoho"|None
    method: str | None     # "html"|"cname"|None
    confidence: str        # "high"|"low"

# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(CACHE_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS helpdesk_cache "
        "(domain TEXT PRIMARY KEY, helpdesk TEXT, method TEXT, confidence TEXT)"
    )
    conn.commit()
    return conn


def _cache_get(conn: sqlite3.Connection, domain: str) -> HelpdeskResult | None:
    row = conn.execute(
        "SELECT helpdesk, method, confidence FROM helpdesk_cache WHERE domain = ?",
        (domain,),
    ).fetchone()
    if row is None:
        return None
    return HelpdeskResult(
        helpdesk=row[0] or None,
        method=row[1] or None,
        confidence=row[2],
    )


def _cache_set(conn: sqlite3.Connection, domain: str, result: HelpdeskResult) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO helpdesk_cache (domain, helpdesk, method, confidence) "
        "VALUES (?, ?, ?, ?)",
        (domain, result["helpdesk"], result["method"], result["confidence"]),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# HTML fingerprints
# ---------------------------------------------------------------------------

# Each entry: (vendor_name, [verbatim strings to grep in raw HTML])
_HTML_SIGS: list[tuple[str, list[str]]] = [
    ("zendesk",   ["static.zdassets.com", "zendesk.com", "zdassets", "zopim", "ze-snippet"]),
    ("intercom",  ["widget.intercom.io", "js.intercomcdn.com", "intercomSettings", "intercomcdn",
                   "intercom.help", "intercom-font"]),
    ("freshdesk", ["widget.freshworks.com", "freshchat.com", "freshdesk.com",
                   "freshworks.com", "FreshworksWidget"]),
    ("helpscout", ["beacon-v2.helpscout.net", "helpscout.net", 'Beacon(']),
    ("zoho",      ["desk.zoho.com", "salesiq.zoho.com", "zohostatic.com", "ZohoDeskAsap"]),
]

# URL paths to probe per domain
_PROBE_PATHS = ["", "/support", "/help", "/contact"]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# CNAME targets
# ---------------------------------------------------------------------------

_CNAME_SIGS: list[tuple[str, list[str]]] = [
    ("zendesk",   ["zendesk.com"]),
    ("intercom",  ["intercom.help", "intercom.io"]),
    ("freshdesk", ["freshdesk.com", "freshservice.com"]),
    ("helpscout", ["helpscoutdocs.com"]),
    ("zoho",      ["zoho.com"]),
]

_CNAME_PREFIXES = ["support", "help", "docs"]

# ---------------------------------------------------------------------------
# Detection logic
# ---------------------------------------------------------------------------

def _fingerprint_html(html: str) -> str | None:
    """Return vendor name if any HTML signature matches, else None."""
    for vendor, sigs in _HTML_SIGS:
        for sig in sigs:
            if sig in html:
                return vendor
    return None


async def _fetch_html(client: httpx.AsyncClient, url: str) -> str:
    """Fetch URL and return raw text, or '' on any error."""
    try:
        r = await client.get(url, timeout=10.0, follow_redirects=True)
        return r.text
    except Exception:
        return ""


async def _html_check(domain: str) -> str | None:
    """Probe domain homepage + sub-paths; return first vendor hit or None."""
    async with httpx.AsyncClient(headers=_HEADERS, max_redirects=5) as client:
        for path in _PROBE_PATHS:
            url = f"https://{domain}{path}"
            html = await _fetch_html(client, url)
            hit = _fingerprint_html(html)
            if hit:
                return hit
    return None


def _cname_check(domain: str) -> str | None:
    """Check support./help./docs. CNAMEs; return first vendor hit or None."""
    resolver = dns.resolver.Resolver()
    resolver.lifetime = DNS_TIMEOUT_SECONDS

    for prefix in _CNAME_PREFIXES:
        fqdn = f"{prefix}.{domain}"
        try:
            answers = resolver.resolve(fqdn, "CNAME")
            for rdata in answers:
                target = str(rdata.target).rstrip(".").lower()
                for vendor, suffixes in _CNAME_SIGS:
                    if any(target.endswith(s) for s in suffixes):
                        return vendor
        except Exception:
            continue
    return None


async def _detect_uncached(domain: str) -> HelpdeskResult:
    # Run HTML probe (async) and CNAME check (sync, in executor) concurrently.
    loop = asyncio.get_event_loop()
    html_task = asyncio.create_task(_html_check(domain))
    cname_future = loop.run_in_executor(None, _cname_check, domain)

    html_hit, cname_hit = await asyncio.gather(html_task, cname_future)

    if html_hit:
        return HelpdeskResult(helpdesk=html_hit, method="html", confidence="high")
    if cname_hit:
        return HelpdeskResult(helpdesk=cname_hit, method="cname", confidence="high")
    return HelpdeskResult(helpdesk=None, method=None, confidence="high")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_helpdesk(domain: str) -> HelpdeskResult:
    """
    Detect whether domain runs a helpdesk.
    Cached in cache.sqlite; re-runs skip all network calls.
    """
    domain = domain.strip().lower()
    conn = _get_conn()
    cached = _cache_get(conn, domain)
    if cached is not None:
        conn.close()
        return cached

    result = asyncio.run(_detect_uncached(domain))
    _cache_set(conn, domain, result)
    conn.close()
    return result


# ---------------------------------------------------------------------------
# Batch helpers (concurrency cap + jitter)
# ---------------------------------------------------------------------------

_CONCURRENCY = 5
_JITTER_MIN = 1.0
_JITTER_MAX = 2.0


async def _batch_detect(domains: list[str]) -> list[HelpdeskResult]:
    """Detect helpdesks for a list of domains with concurrency cap and jitter."""
    semaphore = asyncio.Semaphore(_CONCURRENCY)
    conn = _get_conn()

    async def _run_one(domain: str) -> HelpdeskResult:
        domain = domain.strip().lower()
        cached = _cache_get(conn, domain)
        if cached is not None:
            return cached

        async with semaphore:
            await asyncio.sleep(random.uniform(_JITTER_MIN, _JITTER_MAX))
            result = await _detect_uncached(domain)
            _cache_set(conn, domain, result)
            return result

    results = await asyncio.gather(*[_run_one(d) for d in domains])
    conn.close()
    return list(results)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# (domain, expected_helpdesk_or_None)  — None means "just show, no assertion"
_TEST_DOMAINS: list[tuple[str, str | None]] = [
    ("tryprospect.com",   "intercom"),    # known positive — uses Intercom (intercom.help)
    ("hifives.in",        None),          # known clean — expect None
    ("antengage.com",     None),          # known clean — expect None
]


def cmd_test() -> None:
    header = f"{'Domain':<35} {'Expected':<12} {'Helpdesk':<12} {'Method':<8} {'Match'}"
    print(header)
    print("-" * len(header))
    all_ok = True
    for domain, expected in _TEST_DOMAINS:
        r = detect_helpdesk(domain)
        actual = r["helpdesk"] or "none"
        exp_display = expected or "none"
        if expected is None:
            match_str = "—"
        elif actual == exp_display:
            match_str = "OK"
        else:
            match_str = "MISMATCH <<<"
            all_ok = False
        method = r["method"] or "—"
        print(f"{domain:<35} {exp_display:<12} {actual:<12} {method:<8} {match_str}")
    print()
    if all_ok:
        print("All expected results matched.")
    else:
        print("One or more mismatches — review above.")


def cmd_filter(in_path: Path, out_path: Path) -> None:
    df = pd.read_csv(in_path)
    if "domain" not in df.columns:
        sys.exit("Input CSV must have a 'domain' column.")

    domains = df["domain"].astype(str).tolist()
    print(f"Scanning {len(domains)} domains (concurrency={_CONCURRENCY}, jitter={_JITTER_MIN}-{_JITTER_MAX}s)…")
    t0 = time.time()
    results = asyncio.run(_batch_detect(domains))

    helpdesks = [r["helpdesk"] for r in results]
    methods   = [r["method"]   for r in results]

    df["helpdesk"] = helpdesks
    df["method"]   = methods

    passed = df[df["helpdesk"].isna()]
    passed = passed.drop(columns=["method"])   # method only meaningful for rejects
    passed.to_csv(out_path, index=False)

    rejected = len(df) - len(passed)
    elapsed = time.time() - t0
    print(
        f"Done in {elapsed:.1f}s. "
        f"{len(passed)} passed, {rejected} rejected. "
        f"Written to {out_path}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Helpdesk Gate")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--test", action="store_true", help="Run built-in domain test")
    group.add_argument("--in", dest="infile", metavar="CSV")
    parser.add_argument("--out", dest="outfile", metavar="CSV")
    args = parser.parse_args()

    if args.test:
        cmd_test()
    else:
        if not args.outfile:
            sys.exit("--out is required when using --in")
        cmd_filter(Path(args.infile), Path(args.outfile))


if __name__ == "__main__":
    main()
