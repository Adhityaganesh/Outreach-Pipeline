import os
from pathlib import Path
from dotenv import load_dotenv

# Absolute path, not CWD: a scheduled run (launchd/cron) starts with no shell
# environment and often a different working directory, and load_dotenv() would
# silently find nothing — every setting would fall back to its default.
load_dotenv(Path(__file__).parent / ".env")


def _env(key: str, default: str = "") -> str:
    """
    os.getenv with one guard: python-dotenv strips an inline comment only when a
    value precedes it, so `KEY=   # note` in .env yields the COMMENT as the value.
    Treat a value that is only a comment as unset.
    """
    v = os.getenv(key)
    if v is None:
        return default
    v = v.strip()
    return default if v.startswith("#") else v

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
CACHE_DB = BASE_DIR / "cache.sqlite"

GROQ_API_KEY       = _env("GROQ_API_KEY", "")
# Groq chat model for email openers. Availability varies per account — list
# yours at https://api.groq.com/openai/v1/models. A stale id fails with 404
# and openers silently fall back to the template line.
GROQ_MODEL         = _env("GROQ_MODEL", "openai/gpt-oss-120b")
SENDER_NAME        = _env("SENDER_NAME", "Adhitya")
# Name shown in the From line, e.g. "Adhitya from Quelp" (defaults to SENDER_NAME)
SENDER_DISPLAY_NAME = _env("SENDER_DISPLAY_NAME", "") or SENDER_NAME
HUNTER_KEY         = _env("HUNTER_KEY", "")
GETPROSPECT_KEY    = _env("GETPROSPECT_KEY", "")
TOMBA_KEY          = _env("TOMBA_KEY", "")
TOMBA_SECRET       = _env("TOMBA_SECRET", "")
GOOGLE_CLIENT_ID     = _env("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = _env("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = _env("GOOGLE_REFRESH_TOKEN", "")
APOLLO_API_KEY       = _env("APOLLO_API_KEY", "")
MIN_EMPLOYEES        = int(_env("MIN_EMPLOYEES", "20"))
MAX_EMPLOYEES        = int(_env("MAX_EMPLOYEES", "200"))

# Sender identity (signature + opt-out line)
SENDER_TITLE         = _env("SENDER_TITLE", "Founder, Quelp")
SENDER_SITE          = _env("SENDER_SITE", "quelp.co.in")

# List-Unsubscribe header. Required of bulk senders, but on a 1:1 cold email it
# tells the receiver this is bulk mail, which can cost inbox placement. The P.S.
# opt-out line in pitch.py stays either way.
LIST_UNSUBSCRIBE     = _env("LIST_UNSUBSCRIBE", "true").lower() not in ("0", "false", "no")

# Token file for Gmail OAuth (gitignored) — the default inbox
GMAIL_TOKEN_PATH = BASE_DIR / "token.json"
# One token per extra inbox: tokens/<address>.json (gitignored), via `python inbox.py add`
TOKENS_DIR       = BASE_DIR / "tokens"
# Sending inboxes, comma-separated, optional per-inbox daily cap after a colon:
#   INBOXES=adhitya@getquelp.com:15,team@getquelp.com:10
# Empty → single-inbox mode using token.json. Without a :cap, DAILY_SEND_CAP applies.
INBOXES              = _env("INBOXES", "")

# Throttle limits
DNS_TIMEOUT_SECONDS  = 5
MAX_DOMAINS_PER_RUN  = 5_000
# New domains: keep this low (10–15) for the first 2–3 weeks, then raise
# slowly. The cap is PER INBOX, per calendar day (UTC) across ALL runs,
# first emails + follow-ups combined (see sent_log.sent_today()).
DAILY_SEND_CAP       = int(_env("DAILY_SEND_CAP", "15"))
SEND_DELAY_MIN       = float(_env("SEND_DELAY_MIN", "40"))
SEND_DELAY_MAX       = float(_env("SEND_DELAY_MAX", "90"))
FOLLOWUP_DAYS        = int(_env("FOLLOWUP_DAYS", "3"))

# Suppression list: one email or @domain per line. Never emailed again.
SUPPRESS_PATH        = DATA_DIR / "suppress.txt"

# Lead finding (find_emails.py): Prospeo search → guess → Clearout verify
PROSPEO_API_KEY      = _env("PROSPEO_API_KEY", "")
CLEAROUT_API_KEY     = _env("CLEAROUT_API_KEY", "")
# Base URL can differ by account region — see Clearout → Developer → Reference
CLEAROUT_BASE_URL    = _env("CLEAROUT_BASE_URL", "https://api.clearout.io/v2").rstrip("/")
# Hard ceiling on Clearout credits spent per find_emails.py run
CLEAROUT_CREDIT_CAP  = int(_env("CLEAROUT_CREDIT_CAP", "50"))
# Guess order, tried until one verifies (max 3 per person by default)
GUESS_PATTERNS       = _env("GUESS_PATTERNS", "first.last,first,firstlast")
