"""
Block 1 — MX Gate
Classifies a domain's email provider via MX records (with SPF fallback).
Caches results in cache.sqlite so re-runs skip DNS lookups.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

import dns.resolver
import httpx
import pandas as pd

from config import APOLLO_API_KEY, CACHE_DB, DATA_DIR, DNS_TIMEOUT_SECONDS, MAX_EMPLOYEES

# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(CACHE_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mx_cache "
        "(domain TEXT PRIMARY KEY, provider TEXT NOT NULL)"
    )
    conn.commit()
    return conn


def _cache_get(conn: sqlite3.Connection, domain: str) -> str | None:
    row = conn.execute(
        "SELECT provider FROM mx_cache WHERE domain = ?", (domain,)
    ).fetchone()
    return row[0] if row else None


def _cache_set(conn: sqlite3.Connection, domain: str, provider: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO mx_cache (domain, provider) VALUES (?, ?)",
        (domain, provider),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# DNS helpers
# ---------------------------------------------------------------------------

def _resolve_txt(domain: str) -> list[str]:
    """Return all TXT record strings for domain, or [] on any error."""
    try:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = DNS_TIMEOUT_SECONDS
        answers = resolver.resolve(domain, "TXT")
        records = []
        for rdata in answers:
            for txt in rdata.strings:
                records.append(txt.decode("utf-8", errors="ignore"))
        return records
    except Exception:
        return []


def detect_via_spf(domain: str) -> str | None:
    """
    Read TXT records and look for SPF includes.
    Returns "google", "microsoft", or None if inconclusive.
    """
    for txt in _resolve_txt(domain):
        lower = txt.lower()
        if "include:_spf.google.com" in lower:
            return "google"
        if "include:spf.protection.outlook.com" in lower:
            return "microsoft"
    return None


# Primary Google Workspace MX hosts (exact suffix match — most reliable signal).
# Real Workspace records: aspmx.l.google.com, alt[1-4].aspmx.l.google.com,
# smtp.google.com.  All end with "aspmx.l.google.com" or "smtp.google.com".
_GOOGLE_MX_PRIMARY = ("aspmx.l.google.com", "smtp.google.com")

# Secondary Google signals — broader suffixes, checked only if primary misses.
# "googlemail.com" covers legacy Gmail MX; bare ".google.com" catches any
# future *.google.com MX. endswith() prevents "notgoogle.com.evil.net" false hits.
_GOOGLE_MX_SECONDARY = ("googlemail.com", ".google.com")

_MICROSOFT_MX = ("mail.protection.outlook.com", "outlook.com")

# Known third-party gateway MX suffixes that hide the real provider
_GATEWAY_MX = (
    "mimecast.com",
    "pphosted.com",
    "ppe-hosted.com",
    "barracudanetworks.com",
)


def detect_provider(domain: str) -> str:
    """
    Classify domain's email provider.
    Returns one of: "google" | "microsoft" | "other" | "unknown"
    Results are cached in cache.sqlite.
    """
    domain = domain.strip().lower()
    conn = _get_conn()

    cached = _cache_get(conn, domain)
    if cached is not None:
        return cached

    provider = _detect_uncached(domain)
    _cache_set(conn, domain, provider)
    conn.close()
    return provider


def _detect_uncached(domain: str) -> str:
    # --- MX lookup ---
    mx_hosts: list[str] = []
    hit_gateway = False

    try:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = DNS_TIMEOUT_SECONDS
        answers = resolver.resolve(domain, "MX")
        for rdata in answers:
            host = str(rdata.exchange).rstrip(".").lower()
            mx_hosts.append(host)
    except (
        dns.resolver.NoAnswer,
        dns.resolver.NXDOMAIN,
        dns.resolver.NoNameservers,
        dns.exception.Timeout,
    ):
        # Can't resolve MX — try SPF before giving up
        spf = detect_via_spf(domain)
        return spf if spf else "unknown"
    except Exception:
        spf = detect_via_spf(domain)
        return spf if spf else "unknown"

    # --- Classify from MX hosts ---
    for host in mx_hosts:
        # Primary Google check: exact suffix on the two canonical Workspace hosts.
        if any(host == g or host.endswith("." + g) for g in _GOOGLE_MX_PRIMARY):
            return "google"
        # Microsoft check.
        if any(host.endswith(m) for m in _MICROSOFT_MX):
            return "microsoft"
        if any(gw in host for gw in _GATEWAY_MX):
            hit_gateway = True

    # Secondary Google check (googlemail.com / *.google.com) — runs after the
    # full loop so a gateway MX on the same domain doesn't short-circuit it.
    for host in mx_hosts:
        if any(host.endswith(g) for g in _GOOGLE_MX_SECONDARY):
            return "google"

    # --- SPF fallback (gateway or unmatched MX) ---
    if hit_gateway or mx_hosts:
        spf = detect_via_spf(domain)
        if spf:
            return spf

    return "other" if mx_hosts else "unknown"


# ---------------------------------------------------------------------------
# Employee size gate — Apollo enrichment
# ---------------------------------------------------------------------------

def _init_size_cache(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS size_cache (
            domain TEXT PRIMARY KEY,
            employee_count INTEGER,
            cached_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def _apollo_employee_count(domain: str) -> int | None:
    """
    Return employee count for domain via Apollo's organizations/enrich endpoint.
    Caches result in SQLite for 30 days — each domain costs 1 Apollo credit,
    but only on first lookup. Re-runs are free.
    Returns None if key missing, domain not found, or request fails.
    """
    if not APOLLO_API_KEY:
        return None

    conn = _get_conn()
    _init_size_cache(conn)

    # Check cache first (valid for 30 days)
    row = conn.execute("""
        SELECT employee_count FROM size_cache
        WHERE domain = ?
        AND cached_at > datetime('now', '-30 days')
    """, (domain,)).fetchone()

    if row is not None:
        conn.close()
        return row[0]  # may be None if we cached a "not found"

    # Cache miss — hit Apollo (costs 1 credit)
    count = None
    try:
        resp = httpx.post(
            "https://api.apollo.io/v1/organizations/enrich",
            json={"domain": domain},
            headers={
                "x-api-key": APOLLO_API_KEY,
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            org = resp.json().get("organization") or {}
            raw = org.get("estimated_num_employees") or org.get("num_employees")
            count = int(raw) if raw is not None else None
    except Exception:
        pass

    # Cache the result (including None = not found, so we don't retry)
    conn.execute(
        "INSERT OR REPLACE INTO size_cache (domain, employee_count) VALUES (?, ?)",
        (domain, count),
    )
    conn.commit()
    conn.close()
    return count


def passes_size_gate(domain: str, max_employees: int) -> tuple[bool, int | None]:
    """
    Returns (passes, employee_count).
    Passes if: Apollo key not set (skip gate), count unknown, or count <= max.
    """
    if not APOLLO_API_KEY:
        return True, None
    count = _apollo_employee_count(domain)
    if count is None:
        return True, None   # can't determine size — let it through
    return count <= max_employees, count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# (domain, expected_provider_or_None)  — None means "unknown, just show result"
_TEST_DOMAINS: list[tuple[str, str | None]] = [
    ("google.com",       "google"),
    ("microsoft.com",    "microsoft"),
    ("github.com",       "microsoft"),
    ("zoho.com",         "other"),
    # --- prospect domains ---
    ("hifives.in",       "google"),
    ("nexaei.com",       "microsoft"),
    ("antengage.com",    "google"),
    ("vaultstackai.com", None),          # unknown — show result
    ("tryprospect.com",  "google"),      # has Helpscout gateway; SPF should save it
]


def cmd_test() -> None:
    header = f"{'Domain':<35} {'Expected':<12} {'Actual':<12} {'Match'}"
    print(header)
    print("-" * len(header))
    all_ok = True
    for domain, expected in _TEST_DOMAINS:
        actual = detect_provider(domain)
        if expected is None:
            match_str = "—"
        elif actual == expected:
            match_str = "OK"
        else:
            match_str = "MISMATCH <<<"
            all_ok = False
        exp_display = expected if expected else "?"
        print(f"{domain:<35} {exp_display:<12} {actual:<12} {match_str}")
    print()
    if all_ok:
        print("All expected results matched.")
    else:
        print("One or more mismatches — review above.")


def cmd_filter(in_path: Path, out_path: Path, max_employees: int) -> None:
    df = pd.read_csv(in_path)
    if "domain" not in df.columns:
        sys.exit("Input CSV must have a 'domain' column.")

    size_gate_active = bool(APOLLO_API_KEY)
    if size_gate_active:
        print(f"Employee size gate active (max {max_employees}). Apollo key found.")
    else:
        print("No APOLLO_API_KEY set — employee size gate skipped.")

    providers    = []
    emp_counts   = []
    size_results = []

    for domain in df["domain"].astype(str):
        provider = detect_provider(domain)
        providers.append(provider)

        if provider == "google":
            passes, count = passes_size_gate(domain, max_employees)
            emp_counts.append(count)
            size_results.append(passes)
            status = f"google ✓  employees={count if count is not None else '?'}"
            if not passes:
                status += f" > {max_employees} — DROPPED"
        else:
            emp_counts.append(None)
            size_results.append(False)
            status = provider

        print(f"  {domain:<40} {status}")

    df["provider"]       = providers
    df["employee_count"] = emp_counts

    passed = df[
        (df["provider"] == "google") &
        pd.Series([bool(r) for r in size_results], index=df.index)
    ]
    passed.to_csv(out_path, index=False)

    mx_passed   = sum(1 for p in providers if p == "google")
    size_dropped = mx_passed - len(passed)
    print(
        f"\nProcessed {len(df)} rows. "
        f"{mx_passed} passed MX gate. "
        f"{size_dropped} dropped by size gate. "
        f"{len(passed)} written to {out_path}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="MX Gate — email provider classifier")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--test", action="store_true", help="Run built-in domain test")
    group.add_argument("--in", dest="infile", metavar="CSV", help="Input CSV path")
    parser.add_argument("--out", dest="outfile", metavar="CSV", help="Output CSV path")
    args = parser.parse_args()

    if args.test:
        cmd_test()
    else:
        if not args.outfile:
            sys.exit("--out is required when using --in")
        cmd_filter(Path(args.infile), Path(args.outfile), MAX_EMPLOYEES)


if __name__ == "__main__":
    main()
