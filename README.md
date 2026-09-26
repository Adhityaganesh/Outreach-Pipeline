# Outbound Pipeline

Cold-email outreach for [Quelp](https://quelp.co.in) — an AI call agent for B2B sales teams.
Give it a lead list, it sends a personalised first email from your Gmail, follows up once in the
same thread, and stops the moment someone replies, bounces or opts out.

**Who it's for:** sales leaders, AEs and SEs at 20–200 person B2B SaaS.

---

## Quick start: send to a lead list

```bash
cd quelp_outreach

# 1. Preview (default — nothing sends). Full bodies land in data/preview.csv
python list_send.py --in ~/Downloads/leads.csv

# 2. Send 3 samples to yourself — check inbox AND spam
python list_send.py --in ~/Downloads/leads.csv --test-to adhitya@quelp.co.in

# 3. Send for real (asks you to type SEND; stops at the daily cap)
python list_send.py --in ~/Downloads/leads.csv --live

# 4. Every morning: follow up on anyone silent for 3+ days
python followup.py            # preview
python followup.py --live

# Someone replied "no"? Never email them (or their whole company) again:
python suppress.py add jane@acme.com @acme.com
```

**Lead list format:** any CSV with an email column — Apollo, Hunter, Snov, Prospeo, Sales Navigator
exports or a hand-made sheet. Name, company, title, industry and company size are picked up
automatically if present (see `leads.py` for the header names it recognises). Rows are dropped
for invalid/bounced emails, role addresses (info@, sales@…), duplicates, anyone already contacted,
anyone on the suppression list, and companies outside 20–200 employees (`--no-size-gate` to keep them).

**Changing the pitch:** all copy — subject, first email, follow-up, opt-out line, opener prompt —
lives in `pitch.py`. Edit it there; every script picks it up. Follow-ups only go to leads who got
the *current* subject, so old campaigns never get a mismatched nudge.

### Safety rails

| Rail | Where |
|---|---|
| Dry run is the default; live needs a typed `SEND` | all senders |
| Daily cap across all runs, first emails + follow-ups (`DAILY_SEND_CAP`, default 15) | `sent_log.remaining_today` |
| 40–90s random gap between sends; stops on Gmail 403/429 | `list_send`, `followup` |
| Opt-out line in every email + `List-Unsubscribe` header | `pitch.py`, `gmail_auth.py` |
| Any reply in the thread (even from a colleague) → never emailed again | `followup.py` |
| Bounce in the thread → address added to `data/suppress.txt` | `followup.py` |
| One follow-up max, threaded with the real Message-ID | `followup.py` |

### Before the first live send (new domain)

1. In Google Workspace admin, turn on **DKIM** for quelp.co.in, and check SPF + DMARC records exist at GoDaddy.
2. Keep `DAILY_SEND_CAP` at 10–15 for the first 2–3 weeks; raise slowly only if replies come in and nothing lands in spam.
3. Always run `--test-to` first after editing `pitch.py`.

---

## Setup

### 1. Clone & create virtualenv

```bash
git clone https://github.com/Adhityaganesh/Outreach-Pipeline.git
cd Outreach-Pipeline/quelp_outreach
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
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google OAuth client (Gmail API enabled) |
| `SENDER_NAME` / `SENDER_TITLE` / `SENDER_SITE` | Signature lines |
| `GROQ_API_KEY` | Optional — LLM openers; without it a safe template line is used |
| `DAILY_SEND_CAP` | Max emails per day, all runs combined (default: 15) |
| `SEND_DELAY_MIN` / `SEND_DELAY_MAX` | Random gap between sends in seconds (default: 40–90) |
| `FOLLOWUP_DAYS` | Days of silence before the follow-up (default: 3) |
| `MIN_EMPLOYEES` / `MAX_EMPLOYEES` | Size gate for `list_send.py` (default: 20–200) |
| `APIFY_TOKEN`, `APOLLO_API_KEY`, `HUNTER_KEY`, … | Legacy Blocks 0–3 only |

### 3. Gmail OAuth

On first run, a browser window will open for Google OAuth consent (send + read-only — read access is
used only to detect replies/bounces and to thread follow-ups). Sign in with the account you send from.
The token is cached in `token.json` (gitignored) and shared by every script. Tokens from the old
send-only setup trigger one re-consent automatically.

Your Google Cloud project needs the **Gmail API** enabled and `http://localhost:8080` registered as an authorised redirect URI.

---

## Legacy: scrape-and-enrich pipeline (Blocks 0–5)

> Built for the earlier support-inbox ICP (founders of ≤15-person companies, Google Workspace only,
> no helpdesk tool). The gates in Blocks 1–3 don't match the sales-team ICP; `personalize.py` and
> `send.py` now use the new pitch, but for sales leads prefer `list_send.py` with a sourced list.

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

## Apollo exports

`apollo_send.py` still works but just forwards to `list_send.py`.

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
leads.csv  →  list_send.py  →  sent_log.csv  →  followup.py
                  ↓                                  ↓
            preview.csv                     suppress.txt (bounces, opt-outs)

legacy:  raw_companies.csv → mx_passed.csv → helpdesk_passed.csv
           → enriched.csv → ready_to_send.csv → send.py → sent_log.csv
```

All intermediate files land in `quelp_outreach/data/` (gitignored — may contain PII).

---

## Stack

- **Gmail API** — sending, reply/bounce detection, threading
- **Apify** — Google Maps scraper (`compass/crawler-google-places`)
- **Groq / Llama 3.3** — personalised openers (optional)
- **Apollo.io** — contact export + optional employee enrichment
- **dnspython** — MX record lookup
- **httpx + BeautifulSoup** — helpdesk detection + page scraping
- **SQLite** — caching (MX, contacts, openers, sourced domains)
- **pandas** — data wrangling throughout
