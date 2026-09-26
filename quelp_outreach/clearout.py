"""
Clearout API client — email verify, email finder, credit balance.

Credit rules (docs.clearout.io):
  verify   1 credit for valid / invalid / catch_all, 0 for unknown
  finder   4 credits for a personal address, 2 for a role address, 0 if not found
  test addresses (valid@example.com, catch_all@example.com, …) are free

Every result is cached in cache.sqlite, so re-running a list never pays twice.
'unknown' verify results are NOT cached — they're free, so the next run retries.
A per-run credit cap is enforced BEFORE each paid call.
"""

import sqlite3
import sys
import time
from datetime import datetime, timezone

import httpx

from config import CACHE_DB, CLEAROUT_API_KEY, CLEAROUT_BASE_URL

VERIFY_COST = 1
FINDER_COST = 4          # worst case (personal address); role costs 2

# Clearout error codes that mean "stop, no more credits / daily limit"
_OUT_OF_CREDITS = {1002, 1028, 1031}
_DAILY_LIMIT = {1017, 1032}
_NOT_FOUND = 1027        # finder: "Email address not found" — free, not an error


class StopRun(Exception):
    """Credits exhausted, daily limit hit, or the run's credit cap reached."""


def _norm_status(s: str) -> str:
    s = str(s or "").strip().lower().replace("-", "_").replace(" ", "_")
    return "catch_all" if s in ("catchall", "accept_all") else s


def _is_test_address(email: str) -> bool:
    return email.lower().endswith("@example.com")


class Clearout:
    def __init__(self, credit_cap: int, api_key: str = CLEAROUT_API_KEY,
                 base_url: str = CLEAROUT_BASE_URL):
        if not api_key:
            sys.exit("CLEAROUT_API_KEY is not set in .env "
                     "(Clearout → Developer → API → Create API Token).")
        self.cap = credit_cap
        self.spent = 0
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            timeout=httpx.Timeout(75.0, connect=10.0),
        )
        self._db = sqlite3.connect(CACHE_DB)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS clearout_verify ("
            "email TEXT PRIMARY KEY, status TEXT, safe_to_send TEXT, "
            "role TEXT, checked_at TEXT)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS clearout_find ("
            "key TEXT PRIMARY KEY, email TEXT, role TEXT, checked_at TEXT)"
        )
        self._db.commit()

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        for attempt in range(4):
            try:
                r = self._http.request(method, path, json=body)
            except httpx.TransportError as e:
                if attempt == 3:
                    raise
                print(f"    [clearout] network error ({e.__class__.__name__}) — retrying")
                time.sleep(3)
                continue

            if r.status_code == 429:
                wait = int(r.headers.get("x-ratelimit-reset", "20") or 20) + 1
                print(f"    [clearout] rate limit — waiting {wait}s")
                time.sleep(wait)
                continue
            if r.status_code == 401:
                sys.exit("Clearout rejected the API token (401). Check CLEAROUT_API_KEY, "
                         "and CLEAROUT_BASE_URL under Developer → Reference.")
            if r.status_code == 402:
                raise StopRun("Clearout: out of credits (402).")
            if r.status_code in (503, 524) and attempt < 3:
                time.sleep(5)
                continue

            try:
                data = r.json()
            except ValueError:
                r.raise_for_status()
                raise
            if data.get("status") == "failed" or "error" in data:
                err = data.get("error") or {}
                code = err.get("code")
                if code in _OUT_OF_CREDITS:
                    raise StopRun(f"Clearout: out of credits ({code}).")
                if code in _DAILY_LIMIT:
                    raise StopRun(f"Clearout: daily verify limit reached ({code}).")
                if code == _NOT_FOUND:
                    return {"status": "success", "data": {}}
                raise RuntimeError(f"Clearout error {r.status_code}: {err.get('message') or data}")
            return data
        raise RuntimeError(f"Clearout: gave up on {path} after retries")

    def _charge(self, n: int) -> None:
        if self.spent + n > self.cap:
            raise StopRun(f"Credit cap reached ({self.spent}/{self.cap} this run). "
                          "Raise --max-credits or CLEAROUT_CREDIT_CAP to continue.")

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def credits(self) -> int | None:
        """Remaining credits (free call)."""
        try:
            data = self._request("GET", "/email_verify/getcredits")
            d = data.get("data") or {}
            return d.get("available_credits", (d.get("credits") or {}).get("available"))
        except StopRun:
            return 0
        except Exception as e:
            print(f"    [clearout] could not read balance: {e}")
            return None

    def verify(self, email: str) -> dict:
        """
        Return {status, safe_to_send, role, cached, cost}.
        status: valid | invalid | catch_all | unknown
        """
        email = email.strip().lower()
        row = self._db.execute(
            "SELECT status, safe_to_send, role FROM clearout_verify WHERE email=?", (email,)
        ).fetchone()
        if row:
            return {"status": row[0], "safe_to_send": row[1], "role": row[2],
                    "cached": True, "cost": 0}

        free = _is_test_address(email)
        if not free:
            self._charge(VERIFY_COST)
        data = self._request("POST", "/email_verify/instant",
                             {"email": email, "timeout": 60000}).get("data") or {}
        status = _norm_status(data.get("status"))
        res = {
            "status": status,
            "safe_to_send": str(data.get("safe_to_send") or "").lower(),
            "role": str(data.get("role") or "").lower(),
            "cached": False,
            "cost": 0,
        }
        if status in ("valid", "invalid", "catch_all") and not free:
            res["cost"] = VERIFY_COST
            self.spent += VERIFY_COST
        if status != "unknown":
            self._db.execute(
                "INSERT OR REPLACE INTO clearout_verify VALUES (?, ?, ?, ?, ?)",
                (email, status, res["safe_to_send"], res["role"],
                 datetime.now(timezone.utc).isoformat()),
            )
            self._db.commit()
        return res

    def find(self, full_name: str, domain: str) -> dict:
        """
        Clearout Email Finder. Return {email, role, cached, cost}; email '' if none.
        Found addresses are already verified by Clearout.
        """
        key = f"{full_name.strip().lower()}|{domain.strip().lower()}"
        row = self._db.execute(
            "SELECT email, role FROM clearout_find WHERE key=?", (key,)
        ).fetchone()
        if row:
            return {"email": row[0], "role": row[1], "cached": True, "cost": 0}

        self._charge(FINDER_COST)
        data = self._request("POST", "/email_finder/instant",
                             {"name": full_name, "domain": domain,
                              "timeout": 30000, "queue": False}).get("data") or {}
        emails = data.get("emails") or []
        email, role, cost = "", "", 0
        if emails:
            first = emails[0]
            email = str(first.get("email_address") or "").lower()
            role = str(first.get("role") or "").lower()
            cost = 2 if role == "yes" else 4
            self.spent += cost
        self._db.execute(
            "INSERT OR REPLACE INTO clearout_find VALUES (?, ?, ?, ?)",
            (key, email, role, datetime.now(timezone.utc).isoformat()),
        )
        self._db.commit()
        return {"email": email, "role": role, "cached": False, "cost": cost}
