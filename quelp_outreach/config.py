import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
CACHE_DB = BASE_DIR / "cache.sqlite"

GROQ_API_KEY       = os.getenv("GROQ_API_KEY", "")
SENDER_NAME        = os.getenv("SENDER_NAME", "Adhitya")
HUNTER_KEY         = os.getenv("HUNTER_KEY", "")
GETPROSPECT_KEY    = os.getenv("GETPROSPECT_KEY", "")
TOMBA_KEY          = os.getenv("TOMBA_KEY", "")
TOMBA_SECRET       = os.getenv("TOMBA_SECRET", "")
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN", "")
APOLLO_API_KEY       = os.getenv("APOLLO_API_KEY", "")
MAX_EMPLOYEES        = int(os.getenv("MAX_EMPLOYEES", "15"))

# Token file for Gmail OAuth (gitignored)
GMAIL_TOKEN_PATH = BASE_DIR / "token.json"

# Throttle limits
DNS_TIMEOUT_SECONDS  = 5
MAX_DOMAINS_PER_RUN  = 5_000
DAILY_SEND_CAP       = int(os.getenv("DAILY_SEND_CAP", "30"))
SEND_DELAY_MIN       = float(os.getenv("SEND_DELAY_MIN", "40"))
SEND_DELAY_MAX       = float(os.getenv("SEND_DELAY_MAX", "90"))
