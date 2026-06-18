"""
Block 0 — Source
Scrapes Indian B2B SaaS company domains from two sources and outputs
raw_companies.csv (columns: company, domain).

Sources:
  A. Google Maps (volume) — via Apify compass/crawler-google-places
  B. Product Hunt (fresh daily) — via Apify maximeheckel/product-hunt-scraper

Modes:
  --maps          Run Maps source only
  --producthunt   Run Product Hunt source only
  --all           Both sources (default output: data/raw_companies.csv)
  --test          Minimal credits dry run (1 city, 5 Maps results + 5 PH results)

Options:
  --cities N      How many cities to rotate through for Maps (default: 3)
  --out PATH      Output CSV path (default: data/raw_companies.csv)
"""

import argparse
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
import xml.etree.ElementTree as ET
import pandas as pd
from apify_client import ApifyClient
from dotenv import load_dotenv
import os

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR   = Path(__file__).parent
DATA_DIR   = BASE_DIR / "data"
CACHE_DB   = BASE_DIR / "cache.sqlite"
APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")

INDIAN_CITIES = [
    "Bengaluru", "Chennai", "Hyderabad", "Pune", "Mumbai",
    "Delhi", "Gurugram", "Noida", "Ahmedabad",
]

MAPS_QUERIES = [
    "B2B SaaS company {city}",
    "software company {city}",
    "SaaS startup {city}",
]

# Domains to always drop
BLOCKLIST = {
    "facebook.com", "linkedin.com", "instagram.com", "twitter.com",
    "youtube.com", "medium.com", "play.google.com", "apps.apple.com",
    "google.com", "google.co.in", "maps.google.com",
    "github.com", "wikipedia.org", "indiamart.com", "justdial.com",
    "sulekha.com", "glassdoor.com", "naukri.com", "indeed.com",
    "quora.com", "reddit.com", "pinterest.com", "snapchat.com",
    "t.me", "wa.me", "bit.ly", "linktr.ee", "beacons.ai",
    "saasboomi.org", "saaslegal.org",  # communities/associations, not companies
}

# ---------------------------------------------------------------------------
# Cache — tracks domains already seen across runs
# ---------------------------------------------------------------------------

def _init_cache(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sourced_domains (
            domain TEXT PRIMARY KEY,
            first_seen TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def _seen_domains(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT domain FROM sourced_domains").fetchall()}


def _mark_seen(conn: sqlite3.Connection, domains: list[str]) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO sourced_domains (domain) VALUES (?)",
        [(d,) for d in domains],
    )
    conn.commit()


# ---------------------------------------------------------------------------
# City rotation — deterministic rotation so each run hits different cities
# ---------------------------------------------------------------------------

def _rotate_cities(n: int) -> list[str]:
    """
    Pick n cities. Uses a counter stored in cache.sqlite so consecutive
    runs cycle through all cities without repeating.
    """
    conn = sqlite3.connect(str(CACHE_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS city_rotation (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            offset INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    row = conn.execute("SELECT offset FROM city_rotation WHERE id=1").fetchone()
    offset = row[0] if row else 0
    selected = [INDIAN_CITIES[(offset + i) % len(INDIAN_CITIES)] for i in range(n)]
    new_offset = (offset + n) % len(INDIAN_CITIES)
    conn.execute("""
        INSERT INTO city_rotation (id, offset) VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET offset=excluded.offset
    """, (new_offset,))
    conn.commit()
    conn.close()
    return selected


# ---------------------------------------------------------------------------
# Domain normalisation
# ---------------------------------------------------------------------------

def _clean_domain(raw: str) -> str | None:
    """Strip https://, www., paths. Return bare domain or None if invalid."""
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw.startswith("http"):
        raw = "https://" + raw
    try:
        host = urlparse(raw).netloc.lower()
    except Exception:
        return None
    host = re.sub(r"^www\.", "", host)
    host = host.split(":")[0]   # drop port
    if not host or "." not in host:
        return None
    # Must look like a real domain
    if not re.match(r"^[a-z0-9][a-z0-9\-\.]+\.[a-z]{2,}$", host):
        return None
    return host


def _is_blocked(domain: str) -> bool:
    if domain in BLOCKLIST:
        return True
    for b in BLOCKLIST:
        if domain.endswith("." + b):
            return True
    return False


def _normalise(rows: list[dict]) -> pd.DataFrame:
    """rows: list of {'company': str, 'domain_raw': str}"""
    out = []
    for r in rows:
        d = _clean_domain(r.get("domain_raw", ""))
        if d and not _is_blocked(d):
            out.append({"company": str(r.get("company", "")).strip(), "domain": d})
    df = pd.DataFrame(out, columns=["company", "domain"])
    return df.drop_duplicates(subset="domain").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Source A — Google Maps
# ---------------------------------------------------------------------------

def _run_maps(cities: list[str], max_items_per_query: int = 20) -> pd.DataFrame:
    client = ApifyClient(APIFY_TOKEN)
    rows = []

    for city in cities:
        for query_tpl in MAPS_QUERIES:
            query = query_tpl.format(city=city)
            print(f"  [Maps] {query} (max {max_items_per_query})")
            try:
                run = client.actor("compass/crawler-google-places").call(
                    run_input={
                        "searchStringsArray": [query],
                        "maxCrawledPlacesPerSearch": max_items_per_query,
                        "language": "en",
                        "includeWebResults": False,
                    }
                )
                for item in client.dataset(run.default_dataset_id).iterate_items():
                    website = item.get("website") or item.get("url") or ""
                    name    = item.get("title") or item.get("name") or ""
                    if website:
                        rows.append({"company": name, "domain_raw": website})
            except Exception as e:
                print(f"    [warn] Maps query failed: {e}")

    return _normalise(rows)


# ---------------------------------------------------------------------------
# Source B — Product Hunt (public RSS feed, no auth needed)
# ---------------------------------------------------------------------------

_PH_RSS = "https://www.producthunt.com/feed"

def _run_producthunt(max_items: int = 50) -> pd.DataFrame:
    rows = []
    print(f"  [ProductHunt] Fetching recent launches via RSS…")
    try:
        resp = httpx.get(
            _PH_RSS,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
            follow_redirects=True,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = root.findall(".//item")[:max_items]
        for item in items:
            title = item.findtext("title") or ""
            link  = item.findtext("link") or ""
            # PH RSS links go to producthunt.com/posts/slug — extract the
            # product website from the description if present, else skip.
            desc  = item.findtext("description") or ""
            # Look for a website URL in the description (PH embeds it as href)
            urls  = re.findall(r'href=["\']([^"\']+)["\']', desc)
            website = ""
            for u in urls:
                d = _clean_domain(u)
                if d and "producthunt.com" not in d:
                    website = u
                    break
            # Fallback: use the post link itself to derive domain (less ideal)
            if not website and link:
                website = link
            name = title.split(" - ")[0].strip()
            if website:
                rows.append({"company": name, "domain_raw": website})
    except Exception as e:
        print(f"    [warn] ProductHunt fetch failed: {e}")

    return _normalise(rows)


# ---------------------------------------------------------------------------
# Dedup against cache + merge
# ---------------------------------------------------------------------------

def _filter_new(df: pd.DataFrame, conn: sqlite3.Connection) -> pd.DataFrame:
    seen = _seen_domains(conn)
    return df[~df["domain"].isin(seen)].reset_index(drop=True)


def _write_output(df: pd.DataFrame, out_path: Path) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    print(f"\nWrote {len(df)} companies → {out_path}")


# ---------------------------------------------------------------------------
# CLI modes
# ---------------------------------------------------------------------------

def mode_maps(cities_n: int, out_path: Path) -> None:
    cities = _rotate_cities(cities_n)
    print(f"Running Maps for cities: {cities}")
    df = _run_maps(cities)
    conn = sqlite3.connect(str(CACHE_DB))
    _init_cache(conn)
    df = _filter_new(df, conn)
    _mark_seen(conn, df["domain"].tolist())
    conn.close()
    _write_output(df, out_path)


def mode_producthunt(out_path: Path) -> None:
    df = _run_producthunt()
    conn = sqlite3.connect(str(CACHE_DB))
    _init_cache(conn)
    df = _filter_new(df, conn)
    _mark_seen(conn, df["domain"].tolist())
    conn.close()
    _write_output(df, out_path)


def mode_all(cities_n: int, out_path: Path) -> None:
    cities = _rotate_cities(cities_n)
    print(f"Running Maps for cities: {cities}")
    df_maps = _run_maps(cities)

    df_ph = _run_producthunt()

    combined = pd.concat([df_maps, df_ph]).drop_duplicates(subset="domain").reset_index(drop=True)

    conn = sqlite3.connect(str(CACHE_DB))
    _init_cache(conn)
    combined = _filter_new(combined, conn)
    _mark_seen(conn, combined["domain"].tolist())
    conn.close()

    _write_output(combined, out_path)


def mode_test() -> None:
    """Minimal credit run: 1 city, 5 Maps results + 5 PH results. No cache write."""
    print("\n=== TEST MODE (minimal credits, no cache write) ===\n")

    cities = [INDIAN_CITIES[0]]  # just Bengaluru
    print(f"Maps: 1 city ({cities[0]}), 1 query, max 5 results")
    df_maps = _run_maps(cities, max_items_per_query=5)

    print()
    df_ph = _run_producthunt(max_items=5)

    combined = pd.concat([df_maps, df_ph]).drop_duplicates(subset="domain").reset_index(drop=True)

    print(f"\n--- Results ({len(combined)} companies) ---")
    print(combined.to_string(index=False))
    print("\n(Test mode: cache NOT updated, nothing written to CSV)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    if not APIFY_TOKEN:
        sys.exit("APIFY_TOKEN is not set in .env. Add it and retry.")

    default_out = DATA_DIR / "raw_companies.csv"

    parser = argparse.ArgumentParser(
        description="Block 0: Scrape Indian B2B SaaS domains via Apify."
    )
    parser.add_argument("--cities", type=int, default=3, metavar="N",
                        help="Number of cities to rotate through for Maps (default: 3)")
    parser.add_argument("--out", default=str(default_out), metavar="PATH",
                        help=f"Output CSV path (default: {default_out})")

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--maps",        action="store_true", help="Run Google Maps source only")
    mode.add_argument("--producthunt", action="store_true", help="Run Product Hunt source only")
    mode.add_argument("--all",         action="store_true", help="Run both sources")
    mode.add_argument("--test",        action="store_true", help="Minimal credit test run")

    args = parser.parse_args()
    out  = Path(args.out)

    if args.test:
        mode_test()
    elif args.maps:
        mode_maps(args.cities, out)
    elif args.producthunt:
        mode_producthunt(out)
    else:
        mode_all(args.cities, out)


if __name__ == "__main__":
    main()
