"""
Block 3 — Contact Enrichment (v2)
Named-founder discovery + email construction, then role-email fallback.

Flow per domain:
  1. Scrape /about /team /contact /founders for HTML
  2. Groq parses names+roles from HTML
  3. Infer email pattern from any real email found on-page, else Hunter.io,
     else assume "firstname@" (default for <50-person Indian SaaS)
  4. Construct emails: name + pattern
  5. Waterfall API fallback if construction fails (GetProspect → Tomba → Hunter)
  6. Tag confidence: high / medium / low
"""

import argparse
import asyncio
import json
import os
import random
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import TypedDict

import httpx
from bs4 import BeautifulSoup
import pandas as pd
from groq import Groq

from config import CACHE_DB, DNS_TIMEOUT_SECONDS, GROQ_API_KEY
from mx_gate import detect_provider

# ---------------------------------------------------------------------------
# Env / keys
# ---------------------------------------------------------------------------

_HUNTER_KEY      = os.getenv("HUNTER_KEY", "")
_GETPROSPECT_KEY = os.getenv("GETPROSPECT_KEY", "")
_TOMBA_KEY       = os.getenv("TOMBA_KEY", "")
_TOMBA_SECRET    = os.getenv("TOMBA_SECRET", "")

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class Contact(TypedDict):
    domain: str
    company: str
    name: str          # "" if unknown
    role: str          # "" if unknown
    best_email: str
    all_candidates: str
    email_source: str
    confidence: str    # high | medium | low


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(CACHE_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS contacts_v2 "
        "(domain TEXT PRIMARY KEY, raw_json TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS hunter_pattern "
        "(domain TEXT PRIMARY KEY, pattern TEXT NOT NULL)"
    )
    conn.commit()
    return conn


def _cache_get(conn: sqlite3.Connection, domain: str) -> list | None:
    row = conn.execute(
        "SELECT raw_json FROM contacts_v2 WHERE domain = ?", (domain,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def _cache_set(conn: sqlite3.Connection, domain: str, data: list) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO contacts_v2 (domain, raw_json) VALUES (?, ?)",
        (domain, json.dumps(data)),
    )
    conn.commit()


def _hunter_pattern_get(conn: sqlite3.Connection, domain: str) -> str | None:
    row = conn.execute(
        "SELECT pattern FROM hunter_pattern WHERE domain = ?", (domain,)
    ).fetchone()
    return row[0] if row else None


def _hunter_pattern_set(conn: sqlite3.Connection, domain: str, pattern: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO hunter_pattern (domain, pattern) VALUES (?, ?)",
        (domain, pattern),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.ASCII)

_JUNK_DOMAINS = {
    "sentry.io", "example.com", "yourdomain.com", "company.com",
    "wixpress.com", "squarespace.com", "amazonaws.com",
    "cloudfront.net", "googletagmanager.com", "google.com",
    "facebook.com", "twitter.com", "linkedin.com",
}

_SCRAPE_PATHS = [
    "", "/about", "/about-us", "/team", "/our-team", "/leadership",
    "/leadership-team", "/contact", "/contact-us", "/founders", "/people",
]


async def _fetch_pages(domain: str) -> list[tuple[str, str]]:
    """Return list of (url, html) for each path that succeeds."""
    pages: list[tuple[str, str]] = []
    async with httpx.AsyncClient(headers=_HEADERS, max_redirects=5) as client:
        for path in _SCRAPE_PATHS:
            url = f"https://{domain}{path}"
            try:
                r = await client.get(url, timeout=10.0, follow_redirects=True)
                if r.status_code < 400:
                    pages.append((url, r.text))
            except Exception:
                continue
    return pages


def _extract_real_emails(html: str, domain: str) -> list[str]:
    """Extract emails that belong to the target domain from HTML."""
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    seen: set[str] = set()

    for tag in soup.find_all("a", href=re.compile(r"^mailto:", re.I)):
        raw = tag["href"][7:].split("?")[0].strip().lower()
        if _EMAIL_RE.match(raw) and raw not in seen:
            seen.add(raw)
            found.append(raw)

    for email in _EMAIL_RE.findall(soup.get_text()):
        email = email.lower()
        if email not in seen:
            seen.add(email)
            found.append(email)

    def _keep(e: str) -> bool:
        local, _, d = e.partition("@")
        if d in _JUNK_DOMAINS:
            return False
        if local in _JUNK_LOCALS:
            return False
        return d == domain or d.endswith("." + domain)

    return [e for e in found if _keep(e)]


# ---------------------------------------------------------------------------
# Step 1 — Name discovery via Groq
# ---------------------------------------------------------------------------

_groq_client: Groq | None = None

def _groq() -> Groq:
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq(api_key=GROQ_API_KEY)
    return _groq_client


_FOUNDER_ROLES = {"founder", "co-founder", "cofounder", "ceo", "cto", "coo",
                  "head", "director", "vp", "president", "chief", "owner",
                  "managing director", "md"}

# Large well-known companies that slip through MX/helpdesk gate — skip them
_COMPANY_BLOCKLIST = {
    "databricks.com", "sysdig.com", "builtin.com", "seekout.com",
    "startree.ai", "obviously.ai", "rafay.co", "ushur.com",
    "uplandsoftware.com", "saasinsider.com",
}

# Email local-parts that are service pages, not people
_JUNK_LOCALS = {
    "alexa", "google", "iot", "big", "data", "generative", "business",
    "reinventing", "cumulations", "android", "ios", "web", "mobile",
    "blockchain", "cloud", "ar", "vr", "ml", "ai", "react", "flutter",
    "wordpress", "shopify", "magento", "sap", "oracle", "microsoft",
    "salesforce", "hubspot", "zoho", "freshdesk", "zendesk",
}


def _groq_parse_people(html_snippets: list[str], domain: str) -> list[dict]:
    """
    Send up to 6000 chars of plain text (stripped HTML) to Groq; return [{name, role}].
    Only returns founder/C-suite/head-of roles.
    Prioritises pages that likely have team content by sorting snippets with
    team/about/leadership/founder keywords to the front.
    """
    if not GROQ_API_KEY:
        return []

    def _priority(html: str) -> int:
        t = html.lower()
        for kw in ("leadership", "our-team", "our team", "founder", "co-founder", "ceo", "cto"):
            if kw in t:
                return 0
        return 1

    sorted_snippets = sorted(html_snippets, key=_priority)

    # Convert to plain text to strip JS/CSS noise
    texts = []
    for html in sorted_snippets:
        text = BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True)
        texts.append(text)

    combined = "\n\n---\n\n".join(texts)[:6000]

    prompt = (
        "You are extracting people's names and job roles from company website HTML.\n"
        "Return ONLY a JSON array of objects with keys 'name' and 'role'.\n"
        "Include only real people with founder/CEO/CTO/COO/Head/Director/VP titles.\n"
        "Ignore generic role emails like support@, info@.\n"
        "If no such people are found, return [].\n\n"
        f"HTML:\n{combined}"
    )

    try:
        resp = _groq().chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=512,
        )
        raw = resp.choices[0].message.content.strip()
        # Strip markdown code fences if present (model may wrap JSON in ```json...```)
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```\s*$", "", raw, flags=re.MULTILINE)
        # Extract just the JSON array in case there's surrounding text
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        raw = m.group(0) if m else raw
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        _FOUNDER_KEYWORDS = {
            "founder", "co-founder", "cofounder", "ceo", "cto", "coo", "cpo",
            "chief", "owner", "president", "managing director", "md", "vp",
            "vice president", "head of", "director",
        }
        result = []
        for item in data:
            name = str(item.get("name", "")).strip()
            role = str(item.get("role", "")).strip().lower()
            # Skip names that look like initials only (e.g. "Neha V.")
            name_parts = name.split()
            if not name or len(name_parts) < 2:
                continue
            # Last part is single initial → likely a customer, not team member
            if len(name_parts[-1].rstrip(".")) <= 1:
                continue
            # Role must match founder/C-suite keywords
            if not any(kw in role for kw in _FOUNDER_KEYWORDS):
                continue
            result.append({"name": name, "role": item.get("role", "").strip()})
        return result
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Step 2 — Email pattern detection
# ---------------------------------------------------------------------------

def _infer_pattern_from_email(email: str) -> str | None:
    """
    Given a real personal email, infer the pattern.
    e.g. sourav@domain.com   -> "firstname@"
         s.kumar@domain.com  -> "f.lastname@"
         sourav.k@domain.com -> "firstname.l@"
    """
    local, _, _ = email.partition("@")
    parts = local.split(".")
    if len(parts) == 1:
        # Could be firstname or firstnamelastname — treat as firstname@
        return "firstname@"
    if len(parts) == 2:
        a, b = parts
        if len(a) == 1:
            return "f.lastname@"
        if len(b) == 1:
            return "firstname.l@"
        return "firstname.lastname@"
    return "firstname@"


def _hunter_domain_pattern(domain: str, conn: sqlite3.Connection) -> str | None:
    """Query Hunter.io Domain Search API for email pattern. Cached."""
    cached = _hunter_pattern_get(conn, domain)
    if cached is not None:
        return cached if cached != "__none__" else None

    if not _HUNTER_KEY:
        return None

    try:
        r = httpx.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": _HUNTER_KEY, "limit": 1},
            timeout=8.0,
        )
        data = r.json()
        pattern = data.get("data", {}).get("pattern")
        # Hunter returns patterns like "{first}", "{first}.{last}", etc.
        # Normalise to our internal format
        normalised = _normalise_hunter_pattern(pattern) if pattern else None
        _hunter_pattern_set(conn, domain, normalised or "__none__")
        return normalised
    except Exception:
        _hunter_pattern_set(conn, domain, "__none__")
        return None


def _normalise_hunter_pattern(pattern: str) -> str:
    """Convert Hunter pattern string to our format."""
    p = pattern.lower()
    if p in ("{first}", "{firstname}"):
        return "firstname@"
    if p in ("{first}.{last}", "{firstname}.{lastname}"):
        return "firstname.lastname@"
    if p in ("{f}.{last}", "{f}.{lastname}"):
        return "f.lastname@"
    if p in ("{first}{last}", "{firstname}{lastname}"):
        return "firstnamelastname@"
    if p in ("{first}_{last}", "{firstname}_{lastname}"):
        return "firstname_lastname@"
    return "firstname@"


def _construct_email(name: str, pattern: str, domain: str) -> str | None:
    """Apply pattern to a full name to produce an email address."""
    parts = name.strip().split()
    if not parts:
        return None
    first = parts[0].lower()
    last  = parts[-1].lower() if len(parts) > 1 else ""

    def _slug(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s)

    first = _slug(first)
    last  = _slug(last)

    if not first:
        return None

    if pattern == "firstname@":
        local = first
    elif pattern == "firstname.lastname@":
        local = f"{first}.{last}" if last else first
    elif pattern == "f.lastname@":
        local = f"{first[0]}.{last}" if last else first
    elif pattern == "firstnamelastname@":
        local = f"{first}{last}" if last else first
    elif pattern == "firstname_lastname@":
        local = f"{first}_{last}" if last else first
    else:
        local = first

    if not local:
        return None
    return f"{local}@{domain}"


# ---------------------------------------------------------------------------
# Step 3 — Waterfall API fallback
# ---------------------------------------------------------------------------

def _getprospect_find(domain: str) -> dict | None:
    """GetProspect find email by domain (50 free/mo)."""
    if not _GETPROSPECT_KEY:
        return None
    try:
        r = httpx.get(
            "https://api.getprospect.com/public/v1/email/find",
            params={"apiKey": _GETPROSPECT_KEY, "domain": domain},
            timeout=8.0,
        )
        data = r.json()
        email = data.get("email")
        if email and data.get("status") != "not_found":
            return {"email": email, "name": data.get("firstName", ""), "source": "getprospect"}
    except Exception:
        pass
    return None


def _tomba_find(domain: str) -> dict | None:
    """Tomba domain search (25 free/mo)."""
    if not _TOMBA_KEY:
        return None
    try:
        r = httpx.get(
            f"https://api.tomba.io/v1/domain-search/{domain}",
            headers={"X-Tonga-Key": _TOMBA_KEY, "X-Tonga-Secret": _TOMBA_SECRET},
            timeout=8.0,
        )
        data = r.json()
        emails = data.get("data", {}).get("emails", [])
        if emails:
            top = emails[0]
            return {
                "email": top.get("value", ""),
                "name": f"{top.get('first_name','')} {top.get('last_name','')}".strip(),
                "source": "tomba",
            }
    except Exception:
        pass
    return None


def _hunter_find(domain: str) -> dict | None:
    """Hunter Domain Search for a single email (25 free/mo)."""
    if not _HUNTER_KEY:
        return None
    try:
        r = httpx.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": _HUNTER_KEY, "limit": 1},
            timeout=8.0,
        )
        data = r.json()
        emails = data.get("data", {}).get("emails", [])
        if emails:
            top = emails[0]
            return {
                "email": top.get("value", ""),
                "name": f"{top.get('first_name','')} {top.get('last_name','')}".strip(),
                "source": "hunter",
            }
    except Exception:
        pass
    return None


def _waterfall_fallback(domain: str) -> dict | None:
    """Try GetProspect -> Tomba -> Hunter; return first hit."""
    for fn in (_getprospect_find, _tomba_find, _hunter_find):
        result = fn(domain)
        if result and result.get("email"):
            return result
    return None


# ---------------------------------------------------------------------------
# Role-email fallback (tier ranking from v1)
# ---------------------------------------------------------------------------

_ROLE_TIERS: list[tuple[int, list[str]]] = [
    (1, ["sales"]),
    (2, ["hello", "contact", "team"]),
    (3, ["contactus", "info", "care"]),
    (4, ["founder", "support"]),
]

_BAD_PREFIXES = {
    "jobs", "careers", "hr", "billing", "noreply", "no-reply",
    "abuse", "unsubscribe", "legal", "privacy", "press", "media",
}


def _role_email_candidates(domain: str) -> list[str]:
    prefixes = ["sales", "hello", "contact", "team", "founder", "support", "info", "care"]
    return [f"{p}@{domain}" for p in prefixes]


def _best_role_email(domain: str, scraped: list[str]) -> str | None:
    """Pick the best role email from scraped + generated, using tier ranking."""
    candidates = scraped + _role_email_candidates(domain)
    tier_map = {}
    for tier, prefixes in _ROLE_TIERS:
        for p in prefixes:
            tier_map[p] = tier

    def _score(email: str) -> int:
        local = email.split("@")[0].lower()
        if local in _BAD_PREFIXES:
            return 99
        return tier_map.get(local, 50)

    ranked = sorted(set(candidates), key=_score)
    return ranked[0] if ranked else None


# ---------------------------------------------------------------------------
# MX check
# ---------------------------------------------------------------------------

def _mx_ok(domain: str) -> bool:
    return detect_provider(domain) == "google"


# ---------------------------------------------------------------------------
# Core: enrich one domain
# ---------------------------------------------------------------------------

async def _enrich_domain(domain: str, company: str, conn: sqlite3.Connection) -> list[dict]:
    """
    Returns a list of contact dicts (may be multiple named people).
    Each dict: domain, company, name, role, best_email, all_candidates,
               email_source, confidence
    """
    domain = domain.strip().lower()

    cached = _cache_get(conn, domain)
    if cached is not None:
        return cached

    # --- Scrape pages ---
    pages = await _fetch_pages(domain)
    all_html = [html for _, html in pages]

    # Collect real emails from pages
    real_emails: list[str] = []
    for _, html in pages:
        real_emails.extend(_extract_real_emails(html, domain))
    real_emails = list(dict.fromkeys(real_emails))  # dedupe, preserve order

    # --- Name discovery ---
    people = _groq_parse_people(all_html, domain)

    # --- Pattern detection ---
    pattern: str | None = None
    pattern_source = ""

    # (a) from scraped real emails — prefer personal-looking ones
    for email in real_emails:
        local = email.split("@")[0]
        if re.match(r"^[a-z]{2,20}$", local) and local not in _BAD_PREFIXES:
            pattern = _infer_pattern_from_email(email)
            pattern_source = "scraped_email"
            break

    # (b) Hunter.io (cached)
    if not pattern:
        pattern = _hunter_domain_pattern(domain, conn)
        if pattern:
            pattern_source = "hunter"

    # (c) default
    if not pattern:
        pattern = "firstname@"
        pattern_source = "assumed"

    results: list[dict] = []

    # --- Build contacts for each discovered person ---
    for person in people:
        name = person["name"]
        role = person["role"]
        constructed = _construct_email(name, pattern, domain)

        if constructed:
            if pattern_source == "scraped_email":
                confidence = "high"
            else:
                confidence = "medium"
            email_source = f"constructed:{pattern_source}"
            all_cands = constructed
            best = constructed
        else:
            # construction failed — waterfall
            fallback = _waterfall_fallback(domain)
            if fallback and fallback.get("email"):
                best = fallback["email"]
                confidence = "medium"
                email_source = fallback["source"]
                all_cands = best
            else:
                role_email = _best_role_email(domain, real_emails)
                best = role_email or ""
                confidence = "low"
                email_source = "role_fallback"
                all_cands = best

        results.append({
            "domain": domain,
            "company": company,
            "name": name,
            "role": role,
            "best_email": best,
            "all_candidates": all_cands,
            "email_source": email_source,
            "confidence": confidence,
        })

    # --- No people found — pure fallback ---
    if not results:
        fallback = _waterfall_fallback(domain)
        if fallback and fallback.get("email"):
            name = fallback.get("name", "")
            results.append({
                "domain": domain,
                "company": company,
                "name": name,
                "role": "",
                "best_email": fallback["email"],
                "all_candidates": fallback["email"],
                "email_source": fallback["source"],
                "confidence": "medium" if name else "low",
            })
        else:
            # pure role-email fallback
            role_email = _best_role_email(domain, real_emails)
            results.append({
                "domain": domain,
                "company": company,
                "name": "",
                "role": "",
                "best_email": role_email or "",
                "all_candidates": ";".join(real_emails) if real_emails else "",
                "email_source": "role_fallback",
                "confidence": "low",
            })

    # Drop contacts for domains with no Google MX
    if not _mx_ok(domain):
        for r in results:
            r["best_email"] = ""
            r["confidence"] = "low"

    _cache_set(conn, domain, results)
    return results


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------

_CONCURRENCY = 5
_JITTER_MIN  = 1.0
_JITTER_MAX  = 2.0


async def _batch_enrich(rows: list[dict]) -> list[list[dict]]:
    semaphore = asyncio.Semaphore(_CONCURRENCY)
    conn = _get_conn()

    async def _run_one(row: dict) -> list[dict]:
        domain  = str(row.get("domain", "")).strip().lower()
        company = str(row.get("company", domain))
        cached = _cache_get(conn, domain)
        if cached is not None:
            return cached
        async with semaphore:
            await asyncio.sleep(random.uniform(_JITTER_MIN, _JITTER_MAX))
            return await _enrich_domain(domain, company, conn)

    results = await asyncio.gather(*[_run_one(r) for r in rows])
    conn.close()
    return list(results)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_TEST_DOMAINS = [
    {"domain": "hifives.in",    "company": "HiFives"},
    {"domain": "antengage.com", "company": "AntEngage"},
]


def cmd_test() -> None:
    conn = _get_conn()
    for row in _TEST_DOMAINS:
        domain  = row["domain"]
        company = row["company"]
        print(f"\n{'='*60}")
        print(f"Domain: {domain}  ({company})")
        print(f"{'='*60}")
        contacts = asyncio.run(_enrich_domain(domain, company, conn))
        print(f"  {'Name':<22} {'Role':<20} {'Email':<38} {'Source':<22} {'Conf'}")
        print(f"  {'-'*22} {'-'*20} {'-'*38} {'-'*22} {'-'*6}")
        for c in contacts:
            name = c["name"] or "—"
            role = c["role"] or "—"
            print(
                f"  {name:<22} {role:<20} {c['best_email']:<38} "
                f"{c['email_source']:<22} {c['confidence']}"
            )
    conn.close()


def cmd_enrich(in_path: Path, out_path: Path) -> None:
    df = pd.read_csv(in_path)
    if "domain" not in df.columns:
        sys.exit("Input CSV must have a 'domain' column.")

    rows = df.to_dict("records")
    print(f"Enriching {len(rows)} domains…")
    t0 = time.time()
    all_results = asyncio.run(_batch_enrich(rows))

    # Flatten: ONE contact per domain (best ranked person)
    out_rows: list[dict] = []
    domain_to_input = {str(r.get("domain","")).strip().lower(): r for r in rows}

    _ROLE_PRIORITY = ["founder", "co-founder", "cofounder", "ceo", "cto",
                      "coo", "owner", "president", "managing director", "md",
                      "director", "vp", "head"]

    def _role_rank(contact: dict) -> int:
        role = contact.get("role", "").lower()
        for i, kw in enumerate(_ROLE_PRIORITY):
            if kw in role:
                return i
        return 99

    for contacts in all_results:
        if not contacts:
            continue
        domain = contacts[0].get("domain", "")

        # Skip known large/irrelevant companies
        if domain in _COMPANY_BLOCKLIST:
            print(f"  [skip] {domain} — in company blocklist")
            continue

        # Filter out contacts with junk email locals
        valid = [
            c for c in contacts
            if c.get("best_email", "").split("@")[0] not in _JUNK_LOCALS
        ]
        if not valid:
            continue

        # Pick the single best contact — highest role rank, then highest confidence
        conf_rank = {"high": 0, "medium": 1, "low": 2}
        best = sorted(valid, key=lambda c: (_role_rank(c), conf_rank.get(c.get("confidence","low"), 2)))[0]

        base = dict(domain_to_input.get(best["domain"], {}))
        base.update(best)
        out_rows.append(base)

    out_df = pd.DataFrame(out_rows)
    # Ensure output columns are ordered nicely
    priority = ["domain", "company", "name", "role", "best_email",
                "all_candidates", "email_source", "confidence"]
    rest = [col for col in out_df.columns if col not in priority]
    out_df = out_df[priority + rest]

    out_df.to_csv(out_path, index=False)
    elapsed = time.time() - t0
    filled = sum(1 for r in out_rows if r.get("best_email"))
    high   = sum(1 for r in out_rows if r.get("confidence") == "high")
    medium = sum(1 for r in out_rows if r.get("confidence") == "medium")
    low    = sum(1 for r in out_rows if r.get("confidence") == "low")
    print(
        f"Done in {elapsed:.1f}s. {filled}/{len(out_rows)} contacts have email. "
        f"high={high} medium={medium} low={low}. Written to {out_path}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Contact Enrichment v2")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--test", action="store_true")
    group.add_argument("--in", dest="infile", metavar="CSV")
    parser.add_argument("--out", dest="outfile", metavar="CSV")
    args = parser.parse_args()

    if args.test:
        cmd_test()
    else:
        if not args.outfile:
            sys.exit("--out is required when using --in")
        cmd_enrich(Path(args.infile), Path(args.outfile))


if __name__ == "__main__":
    main()
