"""
Find Emails — cheapest route from "people at companies" to verified emails.

  python find_emails.py --prospeo --pages 2           # plan only (default): free checks, no credits
  python find_emails.py --prospeo --pages 2 --live    # search Prospeo + verify with Clearout
  python find_emails.py --in people.csv --live        # your own CSV of names + domains
  python find_emails.py --test                        # free: Clearout test addresses only

Per person:
  1. Free checks: no name/domain, free-mail domain, suppressed domain,
     no mail server (MX, cached), catch-all domain (cached from earlier runs)
  2. Guess up to 3 addresses (GUESS_PATTERNS), known-good pattern for the
     domain first, and verify each with Clearout — stop at the first valid
  3. First catch_all result marks the whole domain catch-all: 1 credit, then
     everyone else at that company is skipped for free
  4. Optional --finder: Clearout Email Finder (4 credits, personal addresses only)

Output (default data/leads_found.csv) is merged across runs and feeds straight into:
  python list_send.py --in data/leads_found.csv
"""

import argparse
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from clearout import FINDER_COST, VERIFY_COST, Clearout, StopRun
from config import (
    BASE_DIR,
    CACHE_DB,
    CLEAROUT_CREDIT_CAP,
    DATA_DIR,
    GUESS_PATTERNS,
)
from mx_gate import detect_provider
from sent_log import contacted_emails, is_suppressed, load_suppressed

_OUT = DATA_DIR / "leads_found.csv"
_PEOPLE = DATA_DIR / "prospeo_people.csv"
_FILTERS = BASE_DIR / "prospeo_filters.json"

_FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com",
    "hotmail.com", "live.com", "icloud.com", "me.com", "aol.com", "proton.me",
    "protonmail.com", "rediffmail.com", "zoho.com", "yandex.com", "gmx.com",
}

# Pattern names → local-part builders. f/l = initials.
_PATTERNS = {
    "first.last": lambda f, l: f"{f}.{l}" if l else "",
    "first":      lambda f, l: f,
    "firstlast":  lambda f, l: f"{f}{l}" if l else "",
    "flast":      lambda f, l: f"{f[0]}{l}" if l else "",
    "f.last":     lambda f, l: f"{f[0]}.{l}" if l else "",
    "first_last": lambda f, l: f"{f}_{l}" if l else "",
    "firstl":     lambda f, l: f"{f}{l[0]}" if l else "",
    "first.l":    lambda f, l: f"{f}.{l[0]}" if l else "",
    "last":       lambda f, l: l,
}

# contact_enrich.py's Hunter cache uses different names for the same patterns
_HUNTER_TO_OURS = {
    "firstname@": "first", "firstname.lastname@": "first.last",
    "f.lastname@": "f.last", "firstnamelastname@": "firstlast",
    "firstname_lastname@": "first_last", "firstname.l@": "first.l",
}


# ---------------------------------------------------------------------------
# Name / domain helpers
# ---------------------------------------------------------------------------

def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _name_parts(first: str, last: str, full: str) -> tuple[str, str]:
    first, last = str(first or "").strip(), str(last or "").strip()
    if not first and full:
        bits = re.sub(r"^(dr|mr|mrs|ms|prof)\.?\s+", "", str(full).strip(), flags=re.I).split()
        first = bits[0] if bits else ""
        last = bits[-1] if len(bits) > 1 else ""
    # "Kumar R." / "Singh-Rao" → use the last real word, drop suffix initials
    last_words = [w for w in re.split(r"[\s]+", last) if len(_slug(w)) > 1]
    return _slug(first.split()[0] if first.split() else ""), _slug(last_words[-1] if last_words else "")


def _clean_domain(d: str) -> str:
    d = str(d or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split("?")[0]
    return d[4:] if d.startswith("www.") else d


def guesses(first: str, last: str, domain: str, order: list[str]) -> list[tuple[str, str]]:
    out, seen = [], set()
    for name in order:
        fn = _PATTERNS.get(name)
        if not fn or not first:
            continue
        local = fn(first, last)
        if local and local not in seen:
            seen.add(local)
            out.append((name, f"{local}@{domain}"))
    return out


# ---------------------------------------------------------------------------
# Per-domain memory (cache.sqlite): known pattern + catch-all flag
# ---------------------------------------------------------------------------

class DomainMemory:
    def __init__(self):
        self.db = sqlite3.connect(CACHE_DB)
        self.db.execute("CREATE TABLE IF NOT EXISTS domain_email ("
                        "domain TEXT PRIMARY KEY, pattern TEXT, catch_all INTEGER DEFAULT 0, "
                        "updated_at TEXT)")
        self.db.commit()

    def get(self, domain: str) -> tuple[str, bool]:
        row = self.db.execute("SELECT pattern, catch_all FROM domain_email WHERE domain=?",
                              (domain,)).fetchone()
        if row:
            return row[0] or "", bool(row[1])
        # Seed from contact_enrich.py's Hunter pattern cache if it has this domain
        try:
            h = self.db.execute("SELECT pattern FROM hunter_pattern WHERE domain=?",
                                (domain,)).fetchone()
            if h and h[0] in _HUNTER_TO_OURS:
                return _HUNTER_TO_OURS[h[0]], False
        except sqlite3.OperationalError:
            pass
        return "", False

    def _set(self, domain: str, **kw) -> None:
        pattern, catch_all = self.get(domain)
        pattern = kw.get("pattern", pattern)
        catch_all = kw.get("catch_all", catch_all)
        self.db.execute("INSERT OR REPLACE INTO domain_email VALUES (?, ?, ?, ?)",
                        (domain, pattern, int(catch_all), datetime.now(timezone.utc).isoformat()))
        self.db.commit()

    def set_pattern(self, domain: str, pattern: str) -> None:
        self._set(domain, pattern=pattern)

    def set_catch_all(self, domain: str) -> None:
        self._set(domain, catch_all=True)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def free_checks(row: dict, mem: DomainMemory, suppressed: set, contacted: set,
                order: list[str], max_guesses: int) -> tuple[str, list]:
    """Return (skip_reason, candidates). skip_reason '' means go ahead."""
    first, last = _name_parts(row.get("first_name"), row.get("last_name"), row.get("full_name"))
    domain = _clean_domain(row.get("domain"))
    if not first:
        return "no first name", []
    if not domain or "." not in domain:
        return "no company domain", []
    if domain in _FREE_MAIL:
        return "free-mail domain", []
    if is_suppressed(f"x@{domain}", suppressed):
        return "domain suppressed", []

    known, catch_all = mem.get(domain)
    if catch_all:
        return "catch-all domain (known)", []
    if detect_provider(domain) == "unknown":
        return "no mail server (MX)", []

    ordered = ([known] if known else []) + [p for p in order if p != known]
    cands = guesses(first, last, domain, ordered)[:max_guesses]
    if not cands:
        return "can't build an address from the name", []
    if any(e in contacted or is_suppressed(e, suppressed) for _, e in cands):
        return "already contacted / suppressed", []
    return "", cands


def find_one(row: dict, cands: list, co: Clearout, mem: DomainMemory,
             use_finder: bool) -> tuple[str, str, str]:
    """Return (email, source, reason). email '' if not found."""
    domain = _clean_domain(row.get("domain"))
    saw_unknown = False
    for pattern, email in cands:
        r = co.verify(email)
        tag = "cached" if r["cached"] else f"{r['cost']} cr"
        print(f"    {email:<40} {r['status']:<10} ({tag})")
        if r["status"] == "valid" and r["safe_to_send"] != "no" and r["role"] != "yes":
            mem.set_pattern(domain, pattern)
            return email, f"guess:{pattern}", ""
        if r["status"] == "catch_all":
            mem.set_catch_all(domain)
            return "", "", "catch-all domain"
        if r["status"] == "unknown":
            saw_unknown = True

    if use_finder:
        full = " ".join(x for x in (row.get("first_name"), row.get("last_name")) if x) \
               or row.get("full_name", "")
        f = co.find(full, domain)
        tag = "cached" if f["cached"] else f"{f['cost']} cr"
        print(f"    finder → {f['email'] or 'not found'} ({tag})")
        if f["email"] and f["role"] != "yes":
            return f["email"], "clearout_finder", ""

    return "", "", ("server said unknown — free, retry later" if saw_unknown
                    else f"no valid address in {len(cands)} guesses")


def load_people(args) -> pd.DataFrame:
    if args.prospeo:
        import prospeo
        filters = prospeo.load_filters(Path(args.filters))
        print(f"Prospeo search: {args.pages} page(s) from page {args.start_page} "
              f"(≤{args.pages} credits, 25 people/page)")
        rows, charged = prospeo.search_people(filters, args.pages, args.start_page)
        print(f"  Prospeo credits charged: {charged}\n")
        df = pd.DataFrame(rows).fillna("")
        if not df.empty:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            old = pd.read_csv(_PEOPLE, dtype=str).fillna("") if _PEOPLE.exists() else pd.DataFrame()
            pd.concat([old, df.astype(str)]).drop_duplicates(
                subset=["full_name", "domain"], keep="last").to_csv(_PEOPLE, index=False)
        return df

    path = Path(args.infile).expanduser()
    if not path.exists():
        sys.exit(f"File not found: {path}")
    from leads import _map_columns
    raw = pd.read_csv(path, dtype=str).fillna("")
    m = _map_columns(raw.columns)
    if "domain" not in m or not ({"first_name", "name"} & set(m)):
        sys.exit(f"{path.name} needs a name column (First Name / Name) and a domain column "
                 f"(Domain / Website). Headers seen: {list(raw.columns)}")
    col = lambda f: raw[m[f]].astype(str).str.strip() if f in m else ""
    return pd.DataFrame({
        "first_name": col("first_name"), "last_name": col("last_name"),
        "full_name": col("name"), "title": col("title"), "company": col("company"),
        "domain": col("domain"), "employees": col("employees"), "industry": col("industry"),
    }).fillna("")


def save_found(found: list[dict]) -> int:
    if not found:
        return 0
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new = pd.DataFrame(found)
    old = pd.read_csv(_OUT, dtype=str).fillna("") if _OUT.exists() else pd.DataFrame()
    merged = pd.concat([old, new.astype(str)]).drop_duplicates(subset=["email"], keep="first")
    merged.to_csv(_OUT, index=False)
    return len(merged) - len(old)


def _verify_batch(people, co, mem, order, args, contacted, suppressed):
    """Verify one batch of people. Returns (found rows, reasons). Raises StopRun."""
    found, reasons = [], {}
    todo = []
    for _, r in people.iterrows():
        row = r.to_dict()
        why, cands = free_checks(row, mem, suppressed, contacted, order, args.max_guesses)
        if why:
            reasons[why] = reasons.get(why, 0) + 1
        else:
            todo.append(row)

    print(f"  to verify: {len(todo)}  |  skipped free: {len(people) - len(todo)}")
    for i, row in enumerate(todo, 1):
        domain = _clean_domain(row.get("domain"))
        # Re-plan with what this run has learned: a colleague may have revealed
        # the domain's pattern, or shown that it is catch-all.
        why, cands = free_checks(row, mem, suppressed, contacted, order, args.max_guesses)
        if why:
            reasons[why] = reasons.get(why, 0) + 1
            continue
        print(f"[{i}/{len(todo)}] {row.get('first_name')} {row.get('last_name')} — "
              f"{row.get('title', '')} @ {row.get('company') or domain}")
        email, source, why = find_one(row, cands, co, mem, args.finder)
        if email:
            contacted.add(email)          # do not re-verify within this run
            found.append({
                "email": email, "first_name": row.get("first_name", ""),
                "last_name": row.get("last_name", ""), "company": row.get("company", ""),
                "title": row.get("title", ""), "industry": row.get("industry", ""),
                "domain": domain, "employees": row.get("employees", ""),
                "linkedin": row.get("linkedin", ""), "email_status": "valid",
                "source": source,
                "found_at": datetime.now(timezone.utc).isoformat(),
            })
        else:
            reasons[why] = reasons.get(why, 0) + 1
    return found, reasons


def run(args) -> None:
    order = [p.strip() for p in args.patterns.split(",") if p.strip()]
    bad = [p for p in order if p not in _PATTERNS]
    if bad:
        sys.exit(f"Unknown pattern(s) {bad}. Choose from: {', '.join(_PATTERNS)}")

    if args.prospeo and not args.live:
        _plan_prospeo(args)
        return

    mem = DomainMemory()
    suppressed = load_suppressed()
    contacted = contacted_emails()

    # --target: keep pulling Prospeo pages until N verified emails are found.
    # Yield per page varies a lot (overlap with people already contacted,
    # catch-all domains, unguessable addresses), so a fixed page count cannot
    # deliver a fixed number of leads.
    targeting = bool(args.target) and args.prospeo
    if not targeting:
        people = load_people(args)
        if people.empty:
            print("No people to process.")
            return
        if not args.live:
            _dry_preview(people, mem, suppressed, contacted, order, args)
            return
        co = Clearout(args.max_credits)
        print(f"Clearout balance: {co.credits()}\n")
        found, reasons = [], {}
        try:
            found, reasons = _verify_batch(people, co, mem, order, args,
                                           contacted, suppressed)
        except StopRun as e:
            print(f"\nStopped: {e}")
        finally:
            _report(found, reasons, co)
        return

    import prospeo
    filters = prospeo.load_filters(Path(args.filters))
    co = Clearout(args.max_credits)
    print(f"Target: {args.target} verified emails  |  Clearout balance: {co.credits()}  "
          f"|  cap {args.max_credits} credits, {args.max_pages} pages\n")

    found, reasons, page = [], {}, args.start_page
    pages_used = 0
    try:
        while len(found) < args.target and pages_used < args.max_pages:
            data, paid = prospeo.search_page(filters, page)
            results = data.get("results") or []
            total_pages = (data.get("pagination") or {}).get("total_page") or 0
            pages_used += 1
            print(f"Prospeo page {page}: {len(results)} people"
                  f"{'' if paid else ' (free)'}  [{len(found)}/{args.target} found]")
            if not results:
                print("  no more results — stopping")
                break
            batch = pd.DataFrame([prospeo.to_row(r) for r in results]).fillna("")
            _remember_people(batch)
            got, why = _verify_batch(batch, co, mem, order, args, contacted, suppressed)
            found.extend(got)
            for k, v in why.items():
                reasons[k] = reasons.get(k, 0) + v
            page += 1
            if total_pages and page > total_pages:
                print("  reached the last page of results")
                break
    except StopRun as e:
        print(f"\nStopped: {e}")
    finally:
        _report(found, reasons, co, target=args.target, next_page=page)


def _remember_people(df) -> None:
    if df.empty:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    old = pd.read_csv(_PEOPLE, dtype=str).fillna("") if _PEOPLE.exists() else pd.DataFrame()
    pd.concat([old, df.astype(str)]).drop_duplicates(
        subset=["full_name", "domain"], keep="last").to_csv(_PEOPLE, index=False)


def _dry_preview(people, mem, suppressed, contacted, order, args) -> None:
    todo = []
    for _, r in people.iterrows():
        row = r.to_dict()
        why, cands = free_checks(row, mem, suppressed, contacted, order, args.max_guesses)
        if not why:
            todo.append((row, cands))
    worst = sum(len(c) for _, c in todo) * VERIFY_COST
    print(f"People: {len(people)}  |  to verify: {len(todo)}")
    print(f"Clearout worst case: {worst} credits  |  cap: {args.max_credits}\n")
    for row, cands in todo[:10]:
        print(f"  {row.get('first_name')} {row.get('last_name')} @ "
              f"{_clean_domain(row.get('domain'))}: " + ", ".join(e for _, e in cands))
    print("\nPlan only — no credits spent. Add --live to verify.")


def _report(found, reasons, co, target=None, next_page=None) -> None:
    added = save_found(found)
    print("\n" + "=" * 65)
    hit = "" if target is None else f" / {target} target"
    print(f"Found: {len(found)}{hit}  (new in {_OUT.name}: {added})  |  "
          f"Clearout credits spent: {co.spent}"
          + (f"  |  {co.spent / len(found):.1f} per email" if found else ""))
    for why, n in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  not found {n:>3}: {why}")
    print(f"Clearout balance now: {co.credits()}")
    if next_page is not None:
        print(f"Next Prospeo page: {next_page}")
    if found:
        print(f"\nNext: python list_send.py --in data/{_OUT.name}")


def _plan_prospeo(args) -> None:
    import prospeo
    filters = prospeo.load_filters(Path(args.filters))
    print("Plan only — nothing spent.\n")
    print(f"Prospeo: {args.pages} search page(s) from page {args.start_page} "
          f"= up to {args.pages} credits for up to {args.pages * 25} people")
    print(f"  filters: {args.filters}")
    print(f"  balance: {prospeo.credits()}")
    print(f"Clearout: ~1–{args.max_guesses} credits per person verified, capped at "
          f"{args.max_credits} this run")
    print("\nAdd --live to run.")


def run_test() -> None:
    """Exercise the Clearout integration with its free test addresses."""
    co = Clearout(credit_cap=0)
    print(f"Clearout balance: {co.credits()}\n")
    expect = {
        "valid@example.com": "valid", "invalid@example.com": "invalid",
        "catch_all@example.com": "catch_all", "unknown@example.com": "unknown",
        "role@example.com": None, "safe_to_send_no@example.com": None,
    }
    ok = True
    for email, want in expect.items():
        r = co.verify(email)
        good = want is None or r["status"] == want
        ok &= good
        print(f"  {'OK ' if good else 'BAD'} {email:<32} status={r['status']:<10} "
              f"safe_to_send={r['safe_to_send']:<6} role={r['role']}  cost={r['cost']}")
    print(f"\n{'All good' if ok else 'Mismatch — check output above'}. "
          f"Credits spent: {co.spent} (should be 0).")


def main() -> None:
    p = argparse.ArgumentParser(description="Find verified emails cheaply. Default: plan only.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--prospeo", action="store_true", help="Search Prospeo with prospeo_filters.json")
    src.add_argument("--in", dest="infile", metavar="CSV", help="CSV with names + company domains")
    src.add_argument("--test", action="store_true", help="Free Clearout test-address check")
    p.add_argument("--pages", type=int, default=1, help="Prospeo pages, 1 credit each (default 1)")
    p.add_argument("--target", type=int, default=0, metavar="N",
                   help="Keep pulling Prospeo pages until N verified emails are found. "
                        "Page yield varies, so a page count cannot deliver a fixed number.")
    p.add_argument("--max-pages", type=int, default=12,
                   help="Hard stop on pages per --target run (default 12)")
    p.add_argument("--start-page", type=int, default=1, help="Prospeo page to start from")
    p.add_argument("--filters", default=str(_FILTERS), help="Prospeo filters JSON")
    p.add_argument("--live", action="store_true", help="Actually spend credits")
    p.add_argument("--max-credits", type=int, default=CLEAROUT_CREDIT_CAP,
                   help=f"Clearout credit cap for this run (default {CLEAROUT_CREDIT_CAP})")
    p.add_argument("--max-guesses", type=int, default=3, help="Guesses per person (default 3)")
    p.add_argument("--patterns", default=GUESS_PATTERNS,
                   help=f"Guess order (default {GUESS_PATTERNS}). Options: {', '.join(_PATTERNS)}")
    p.add_argument("--finder", action="store_true",
                   help="Fallback to Clearout Email Finder (4 credits per hit) when guesses fail")
    a = p.parse_args()

    if a.test:
        run_test()
    else:
        run(a)


if __name__ == "__main__":
    main()
