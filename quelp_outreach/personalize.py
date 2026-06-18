"""
Block 4 — Personalize
Fetches company homepage, generates a one-line opener via Groq, and
assembles a ready-to-send subject + body per contact row.
"""

import argparse
import asyncio
import re
import sqlite3
import sys
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from groq import Groq
import pandas as pd

from config import CACHE_DB, GROQ_API_KEY, SENDER_NAME

# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(CACHE_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS opener_cache "
        "(domain TEXT PRIMARY KEY, opener TEXT NOT NULL)"
    )
    conn.commit()
    return conn


def _cache_get(conn: sqlite3.Connection, domain: str) -> str | None:
    row = conn.execute(
        "SELECT opener FROM opener_cache WHERE domain = ?", (domain,)
    ).fetchone()
    return row[0] if row else None


def _cache_set(conn: sqlite3.Connection, domain: str, opener: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO opener_cache (domain, opener) VALUES (?, ?)",
        (domain, opener),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Homepage text extraction
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_FALLBACK_OPENER = "You're building something interesting in your space."


def _extract_page_text(html: str) -> str:
    """
    Pull the most signal-dense text from a homepage in priority order:
    meta description → og:description → <h1> + <h2> hero text → first 600
    chars of visible body text.  Returns a single string under ~800 chars.
    """
    soup = BeautifulSoup(html, "html.parser")

    chunks: list[str] = []

    # Meta description
    for attr in ("name", "property"):
        tag = soup.find("meta", attrs={attr: re.compile(r"description", re.I)})
        if tag and tag.get("content"):
            chunks.append(tag["content"].strip())
            break

    # og:description
    og = soup.find("meta", property="og:description")
    if og and og.get("content"):
        chunks.append(og["content"].strip())

    # Hero headings
    for tag in soup.find_all(["h1", "h2"])[:4]:
        text = tag.get_text(" ", strip=True)
        if text:
            chunks.append(text)

    # First visible paragraph text as a fallback filler
    for p in soup.find_all("p")[:6]:
        text = p.get_text(" ", strip=True)
        if len(text) > 40:
            chunks.append(text)
            break

    combined = " | ".join(dict.fromkeys(chunks))  # deduplicate order-preserving
    return combined[:800]


async def _fetch_homepage(domain: str) -> str:
    """Return extracted page text for domain homepage, or '' on failure."""
    url = f"https://{domain}"
    try:
        async with httpx.AsyncClient(headers=_HEADERS, max_redirects=5) as client:
            r = await client.get(url, timeout=10.0, follow_redirects=True)
            if r.status_code >= 400:
                return ""
            return _extract_page_text(r.text)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Groq call
# ---------------------------------------------------------------------------

_GROQ_MODEL   = "llama-3.3-70b-versatile"
_GROQ_DELAY   = 1.5   # seconds between calls — stays well under free-tier limits
_groq_client: Groq | None = None


def _get_groq() -> Groq:
    global _groq_client
    if _groq_client is None:
        if not GROQ_API_KEY:
            sys.exit("GROQ_API_KEY is not set. Add it to .env.")
        _groq_client = Groq(api_key=GROQ_API_KEY)
    return _groq_client


def _call_groq(company: str, page_text: str) -> str:
    prompt = (
        f"In ONE sentence (max 25 words), write a cold-email opener that references "
        f"what {company} does AND naturally connects it to the idea that their "
        f"support/email volume grows with their business. "
        f"Conversational, not a description. "
        f"Start with 'Saw' or 'Noticed' — not '{company} offers' or '{company} is'.\n\n"
        f"Homepage text:\n{page_text}"
    )
    try:
        resp = _get_groq().chat.completions.create(
            model=_GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=60,
            temperature=0.3,
        )
        result = resp.choices[0].message.content.strip().strip('"')
        # Trim to one sentence just in case
        sentences = re.split(r"(?<=[.!?])\s+", result)
        return sentences[0] if sentences else result
    except Exception:
        return _FALLBACK_OPENER


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def make_opener(domain: str, company_name: str) -> str:
    """
    Fetch homepage, extract text, call Groq for a one-line opener.
    Cached in cache.sqlite. Returns fallback string on any failure.
    """
    domain = domain.strip().lower()
    conn = _get_conn()
    cached = _cache_get(conn, domain)
    if cached is not None:
        conn.close()
        return cached

    page_text = asyncio.run(_fetch_homepage(domain))
    if not page_text:
        opener = _FALLBACK_OPENER
    else:
        opener = _call_groq(company_name or domain, page_text)

    _cache_set(conn, domain, opener)
    conn.close()
    return opener


# ---------------------------------------------------------------------------
# Email assembly
# ---------------------------------------------------------------------------

def _first_name(email: str) -> str:
    """
    Derive a first name from the email local-part, or return 'there'.
    Handles: firstname@, firstname.lastname@, firstname_lastname@.
    Rejects role addresses (all-lowercase common words) and returns 'there'.
    """
    _ROLE_PREFIXES = {
        "founder", "hello", "support", "contact", "team", "info",
        "care", "sales", "contactus", "jobs", "careers", "hr",
        "billing", "noreply", "no-reply", "admin", "help",
    }
    local = email.split("@")[0].lower()
    if local in _ROLE_PREFIXES:
        return "there"
    # Take first segment before . or _
    part = re.split(r"[._\-]", local)[0]
    if len(part) < 2 or not part.isalpha():
        return "there"
    return part.capitalize()


_EMAIL_TEMPLATE = """\
Hi {first_name},

{opener}

I'm a solo founder building Quelp — it drafts support replies grounded in your own help docs, right inside Gmail. You review and send; nothing goes out on its own.

At a small SaaS, support@ usually lands on the founder or one teammate, answered by hand in Gmail. That's exactly where Quelp fits.

I'm looking for a few design partners to use it free for two weeks — I set it up on your inbox myself, 15 minutes, no work on your side.

Worth a quick look?

{sender}"""

_SUBJECT_TEMPLATE = "support@ at {company} — quick question"


def assemble_email(row: dict, opener: str) -> tuple[str, str]:
    """
    Returns (subject, body) for a contacts.csv row.
    row keys used: best_email, company (optional).
    """
    best_email   = str(row.get("best_email", ""))
    company      = str(row.get("company", row.get("domain", "your company")))
    first_name   = _first_name(best_email)

    subject = _SUBJECT_TEMPLATE.format(company=company)
    body    = _EMAIL_TEMPLATE.format(
        first_name=first_name,
        opener=opener,
        sender=SENDER_NAME,
    )
    return subject, body


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_TEST_ROWS = [
    {"domain": "hifives.in",    "company": "HiFives",    "best_email": "sales@hifives.in"},
    {"domain": "antengage.com", "company": "AntEngage",  "best_email": "founder@antengage.com"},
]


def cmd_test() -> None:
    for row in _TEST_ROWS:
        domain  = row["domain"]
        company = row["company"]
        print(f"\n{'='*60}")
        print(f"Domain : {domain}")
        print(f"Company: {company}")
        print(f"{'='*60}")

        opener = make_opener(domain, company)
        subject, body = assemble_email(row, opener)

        print(f"\nOpener : {opener}")
        print(f"\nSubject: {subject}")
        print(f"\nBody:\n{body}")

        time.sleep(_GROQ_DELAY)


def cmd_personalize(in_path: Path, out_path: Path) -> None:
    df = pd.read_csv(in_path)
    required = {"domain", "best_email"}
    missing = required - set(df.columns)
    if missing:
        sys.exit(f"Input CSV missing columns: {missing}")

    openers  = []
    subjects = []
    bodies   = []

    total = len(df)
    for i, row in enumerate(df.to_dict("records"), 1):
        domain  = str(row.get("domain", "")).strip().lower()
        company = str(row.get("company", domain))
        print(f"[{i}/{total}] {domain}", end="  ", flush=True)

        opener = make_opener(domain, company)
        subject, body = assemble_email(row, opener)

        openers.append(opener)
        subjects.append(subject)
        bodies.append(body)

        print(f"→ {opener[:60]}{'…' if len(opener) > 60 else ''}")
        time.sleep(_GROQ_DELAY)

    df["opener"]  = openers
    df["subject"] = subjects
    df["body"]    = bodies
    df.to_csv(out_path, index=False)
    print(f"\nDone. Written to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Personalize emails via Groq")
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
        cmd_personalize(Path(args.infile), Path(args.outfile))


if __name__ == "__main__":
    main()
