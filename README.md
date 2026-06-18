# Outbound Pipeline

An automated B2B outbound email pipeline for [Quelp](https://quelp.co.in) — scrapes Indian SaaS companies, qualifies them, finds founder contacts, personalises cold emails, sends via Gmail, and follows up automatically.

---

## Architecture

```
Block 0 — Source         source.py          Scrape domains (Google Maps + Product Hunt)
Block 1 — MX Gate        mx_gate.py         Keep only Google Workspace domains
Block 2 — Helpdesk Gate  helpdesk_gate.py   Drop companies already using Zendesk/Intercom/Freshdesk
Block 3 — Enrich         contact_enrich.py  Find founder name + email per domain
Block 4 — Personalise    personalize.py     Generate one-line opener via Groq LLM
Block 5 — Send           send.py            Send via Gmail API (dry-run / test / live)
Block 6 — Follow-up      followup.py        Auto follow-up threads with no reply after N days
         — Apollo Send   apollo_send.py     Send directly from Apollo contacts export
```

---

## Setup

### 1. Clone & create virtualenv

```bash
git clone https://github.com/Adhityaganesh/Outreach-Pipeline.git
cd Outbound-Pipeline/quelp_outreach
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Environment variables

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `GROQ_API_KEY` | Groq API key (LLM for openers) |
| `APIFY_TOKEN` | Apify token (Google Maps scraper) |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret |
| `SENDER_NAME` | Your name (appears in email signature) |
| `APOLLO_API_KEY` | Apollo.io API key (optional — employee size gate) |
| `HUNTER_KEY` | Hunter.io key (optional — email pattern lookup) |
| `DAILY_SEND_CAP` | Max emails per run (default: 30) |
| `SEND_DELAY_MIN` | Min delay between sends in seconds (default: 40) |
| `SEND_DELAY_MAX` | Max delay between sends in seconds (default: 90) |
| `FOLLOWUP_DAYS` | Days before follow-up is sent (default: 3) |
| `MAX_EMPLOYEES` | Employee count ceiling for size gate (default: 15) |

### 3. Gmail OAuth

On first run, a browser window will open for Google OAuth consent. Sign in with the Gmail account you want to send from. The token is cached in `token.json` (gitignored).

Your Google Cloud project needs the **Gmail API** enabled and `http://localhost:8080` registered as an authorised redirect URI.

---

## Full Pipeline Run

```bash
cd quelp_outreach

# Block 0 — scrape 3 cities
python source.py --all --cities 3

# Block 1 — keep Google Workspace only
python mx_gate.py --in data/raw_companies.csv --out data/mx_passed.csv

# Block 2 — drop companies with a helpdesk tool installed
python helpdesk_gate.py --in data/mx_passed.csv --out data/helpdesk_passed.csv

# Block 3 — find founder contact
python contact_enrich.py --in data/helpdesk_passed.csv --out data/enriched.csv

# Block 4 — generate personalised openers
python personalize.py --in data/enriched.csv --out data/ready_to_send.csv

# Block 5 — preview (default)
python send.py --dry-run --min-confidence high

# Block 5 — send live
python send.py --live --min-confidence high
```

---

## Apollo Export Send

To send directly from an Apollo.io contacts CSV export (skips Blocks 0–4):

```bash
# Preview
python apollo_send.py --dry-run

# Test to your own inbox first
python apollo_send.py --test-to you@yourdomain.com

# Send live
python apollo_send.py --live --in /path/to/apollo-contacts-export.csv
```

---

## Follow-up Pass

Run after 3+ days to follow up on threads with no reply:

```bash
# Preview who would get a follow-up
python followup.py --dry-run

# Send follow-ups
python followup.py --live
```

---

## Source Options

```bash
# Google Maps only — 5 cities
python source.py --maps --cities 5

# Product Hunt only
python source.py --producthunt

# Test run (minimal Apify credits)
python source.py --test
```

---

## Send Modes

All send scripts support three modes:

| Flag | Behaviour |
|---|---|
| *(default)* / `--dry-run` | Print what would be sent. Nothing goes out. |
| `--test-to email@x.com` | Send everything to one address for inbox preview. |
| `--live` | Send to real recipients. Requires typing `SEND`. |

---

## Data Flow

```
raw_companies.csv  →  mx_passed.csv  →  helpdesk_passed.csv
    →  enriched.csv  →  ready_to_send.csv  →  sent_log.csv
```

All intermediate files land in `quelp_outreach/data/` (gitignored — may contain PII).

---

## Stack

- **Gmail API** — sending + reply detection
- **Apify** — Google Maps scraper (`compass/crawler-google-places`)
- **Groq / Llama 3.1** — personalised openers
- **Apollo.io** — contact export + optional employee enrichment
- **dnspython** — MX record lookup
- **httpx + BeautifulSoup** — helpdesk detection + page scraping
- **SQLite** — caching (MX, contacts, openers, sourced domains)
- **pandas** — data wrangling throughout
