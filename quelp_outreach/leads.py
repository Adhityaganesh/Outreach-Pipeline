"""
Load a lead list from ANY CSV — Apollo, Hunter, Snov, Prospeo, LinkedIn Sales
Navigator exports, or a sheet you made by hand.

Only an email column is required. Everything else is optional and matched by
common header names (case/spacing/underscores ignored):

  email       Email, Work Email, Email Address, best_email, Business Email
  first_name  First Name, Firstname, First, Given Name
  last_name   Last Name, Lastname, Surname
  name        Name, Full Name, Contact Name      (split if no first_name)
  company     Company, Company Name, Organization, Account Name
  title       Title, Job Title, Position, Designation, Role
  industry    Industry
  domain      Website, Domain, Company Domain, Company Website
  employees   # Employees, Employees, Company Size, Headcount
  email_status  Email Status, Email Verification, Verification Status

Rows are dropped when: no/invalid email, email status says invalid/bounced,
role address (info@, sales@ …), or duplicate email.
"""

import re
from pathlib import Path

import pandas as pd

_ALIASES = {
    "email":        ["email", "work email", "email address", "best email",
                     "business email", "work email address", "primary email"],
    "first_name":   ["first name", "firstname", "first", "given name"],
    "last_name":    ["last name", "lastname", "surname", "family name"],
    "name":         ["name", "full name", "contact name", "person name"],
    "company":      ["company", "company name", "organization", "organisation",
                     "account name", "company name for emails"],
    "title":        ["title", "job title", "position", "designation", "role"],
    "industry":     ["industry"],
    "domain":       ["domain", "website", "company domain", "company website",
                     "company url", "website url"],
    "employees":    ["# employees", "employees", "company size", "headcount",
                     "employee count", "number of employees"],
    "email_status": ["email status", "email verification", "verification status",
                     "email verification status"],
}

_BAD_STATUSES = {"invalid", "bounced", "unavailable", "undeliverable",
                 "unverifiable", "do not email", "spamtrap"}

_ROLE_LOCALS = {
    "info", "hello", "hi", "contact", "contactus", "sales", "support", "help",
    "team", "admin", "office", "hr", "jobs", "careers", "billing", "accounts",
    "noreply", "no-reply", "marketing", "press", "media", "partners",
    "founders", "care", "enquiry", "enquiries", "inquiries", "legal",
}

# Non-person "first names" that scrapers sometimes emit
_FAKE_FIRST = {
    "orange", "blue", "red", "green", "black", "white", "silver", "tech",
    "digital", "software", "cloud", "info", "data", "smart", "global",
    "solution", "solutions", "services", "systems", "group", "team", "sales",
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
_SALUTATION = re.compile(r"^(dr|mr|mrs|ms|prof)\.?\s+", re.I)


def _norm(h: str) -> str:
    return re.sub(r"[\s_\-]+", " ", str(h)).strip().lower()


def _map_columns(cols) -> dict:
    normed = {_norm(c): c for c in cols}
    mapping = {}
    for field, aliases in _ALIASES.items():
        for a in aliases:
            if a in normed:
                mapping[field] = normed[a]
                break
    return mapping


def _clean_first(first: str, company: str) -> str:
    f = _SALUTATION.sub("", str(first)).strip()
    if not f or not re.match(r"^[A-Za-z][A-Za-z'\-]+$", f):
        return "there"
    if f.lower() in _FAKE_FIRST:
        return "there"
    if company and f.lower() == company.strip().split()[0].lower():
        return "there"
    return f[:1].upper() + f[1:]


def load_leads(path: Path, verbose: bool = True) -> pd.DataFrame:
    """
    Return a clean frame with columns:
      email, first_name, last_name, company, title, industry, domain, employees
    """
    raw = pd.read_csv(path, dtype=str).fillna("")
    m = _map_columns(raw.columns)

    if "email" not in m:
        raise SystemExit(
            f"No email column found in {path}.\n"
            f"Headers seen: {list(raw.columns)}\n"
            "Rename your email column to 'email' and retry."
        )

    def col(field):
        return raw[m[field]].astype(str).str.strip() if field in m else pd.Series([""] * len(raw))

    df = pd.DataFrame({
        "email":        col("email").str.lower(),
        "first_name":   col("first_name"),
        "last_name":    col("last_name"),
        "name":         col("name"),
        "company":      col("company"),
        "title":        col("title"),
        "industry":     col("industry"),
        "domain":       col("domain"),
        "employees":    col("employees"),
        "email_status": col("email_status").str.lower(),
    })

    # Split full name if there's no first-name column
    need_split = (df["first_name"] == "") & (df["name"] != "")
    parts = df.loc[need_split, "name"].str.replace(_SALUTATION, "", regex=True).str.split()
    df.loc[need_split, "first_name"] = parts.str[0].fillna("")
    df.loc[need_split, "last_name"] = parts.str[1:].str.join(" ").fillna("")

    # Fill company from email domain if missing
    no_co = df["company"] == ""
    df.loc[no_co, "company"] = df.loc[no_co, "email"].str.split("@").str[-1].str.split(".").str[0].str.capitalize()

    df["first_name"] = [_clean_first(f, c) for f, c in zip(df["first_name"], df["company"])]

    total = len(df)
    reasons = {}

    def drop(mask, why):
        nonlocal df
        n = int(mask.sum())
        if n:
            reasons[why] = reasons.get(why, 0) + n
            df = df[~mask]

    drop(~df["email"].str.match(_EMAIL_RE), "missing/invalid email")
    drop(df["email_status"].isin(_BAD_STATUSES), "email status invalid/bounced")
    drop(df["email"].str.split("@").str[0].isin(_ROLE_LOCALS), "role address (info@, sales@…)")
    drop(df["email"].duplicated(), "duplicate email")

    df = df.drop(columns=["name", "email_status"]).reset_index(drop=True)

    if verbose:
        print(f"Loaded {path.name}: {total} rows → {len(df)} usable leads.")
        print(f"  Columns matched: " + ", ".join(f"{k}←'{v}'" for k, v in m.items()))
        for why, n in reasons.items():
            print(f"  Dropped {n}: {why}")
    return df
