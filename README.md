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
| Daily cap per inbox across all runs, first emails + follow-ups (`INBOXES` caps / `DAILY_SEND_CAP`, default 15) | `inboxes.py`, `sent_log.remaining_today` |
| 40–90s random gap between sends; stops on Gmail 403/429 | `list_send`, `followup` |
| Opt-out line in every email + `List-Unsubscribe` header | `pitch.py`, `gmail_auth.py` |
| Any reply in the thread (even from a colleague) → never emailed again | `followup.py` |
| Bounce in the thread → address added to `data/suppress.txt` | `followup.py` |
| One follow-up max, threaded with the real Message-ID | `followup.py` |

### Before the first live send (new domain)

1. Send from a separate cold-email domain (see *Multiple sending inboxes*). In Google Workspace admin, turn on **DKIM** for it, and add SPF + DMARC records at your DNS provider.
2. Keep `DAILY_SEND_CAP` at 10–15 for the first 2–3 weeks; raise slowly only if replies come in and nothing lands in spam.
3. Always run `--test-to` first after editing `pitch.py`.

---

## Multiple sending inboxes

Cold email should go out from a **separate domain** (e.g. `getquelp.com`), never quelp.co.in —
spam complaints hit the whole domain. Put 2–3 inboxes on it and the pipeline rotates between them:

```bash
python inbox.py add        # browser sign-in, once per inbox → tokens/<address>.json
# .env:  INBOXES=adhitya@getquelp.com:15,team@getquelp.com:10   (address:daily cap)
python inbox.py list       # connected? sent today / cap
```

- `list_send.py` spreads each run round-robin across inboxes that still have capacity.
- `followup.py` replies from the **same inbox** that sent the first email (the thread lives there),
  counting against that inbox's cap.
- `--test-to` sends at least one sample from every inbox — check each lands in the inbox, not spam.
- A Gmail rate-limit on one inbox pauses only that inbox for the run.
- `--from-inbox ADDRESS` restricts a run to one inbox — needed for per-domain spam tests,
  since mail-tester issues a new address per test.
- More inboxes spread per-mailbox load, **not** domain reputation — keep totals modest while the domain is new.

No `INBOXES` set = the original single-account behaviour with `token.json`.

**Judging a domain:** `python followup.py` (dry run) records replies and bounces from every thread,
including replies to follow-ups; then `python stats.py --days 15` shows sent / replied / bounced per
domain and per inbox. Set `SENDER_DISPLAY_NAME` (e.g. `Adhitya from Quelp`) for the From line.

---

## Find emails cheaply (Prospeo + Clearout)

No lead list yet? `find_emails.py` builds one without paying for email reveals:

1. **Prospeo search only** — names, titles and company domains (1 credit per page of 25, no reveals).
   Filters live in `prospeo_filters.json` (default: sales leaders, AEs, SEs at 21–200 employees).
2. **Free checks first** — no MX record, free-mail domain, suppressed, already contacted, or a
   company already known to be catch-all → skipped without spending anything.
3. **Guess + verify** — up to 3 guesses (`first.last@`, `first@`, `firstlast@`) checked with
   Clearout (1 credit each, 0 for "unknown"), stopping at the first valid one. Once a pattern
   works at a company it's tried first for everyone else there.
4. **Catch-all** — the first catch-all result marks the whole company; nobody else there costs a credit.
5. **Optional `--finder`** — Clearout Email Finder (4 credits per hit) when guesses fail.

```bash
cd quelp_outreach
python find_emails.py --test                          # free: checks your Clearout key with test addresses
python find_emails.py --prospeo --pages 2             # plan only: balances + cost ceiling, nothing spent
python find_emails.py --prospeo --pages 2 --live      # search + verify (Clearout capped at 50 credits/run)
python find_emails.py --in people.csv --live          # or your own CSV with names + company domains
python list_send.py --in data/leads_found.csv         # then preview the emails as usual
```

Every Clearout result, Prospeo page, company pattern and catch-all flag is cached in
`cache.sqlite`, so re-running never pays twice. Results merge into `data/leads_found.csv`.

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
| `INBOXES` | Sending inboxes with optional caps, e.g. `a@getquelp.com:15,b@getquelp.com:10` |
| `DAILY_SEND_CAP` | Max emails per inbox per day, all runs combined (default: 15) |
| `SEND_DELAY_MIN` / `SEND_DELAY_MAX` | Random gap between sends in seconds (default: 40–90) |
| `FOLLOWUP_DAYS` | Days of silence before the follow-up (default: 3) |
| `MIN_EMPLOYEES` / `MAX_EMPLOYEES` | Size gate for `list_send.py` (default: 20–200) |
| `PROSPEO_API_KEY` / `CLEAROUT_API_KEY` | Lead finding (`find_emails.py`) |
| `CLEAROUT_BASE_URL` | Region-specific; see Clearout → Developer → Reference |
| `CLEAROUT_CREDIT_CAP` | Max Clearout credits per `find_emails.py` run (default: 50) |
| `GUESS_PATTERNS` | Email guess order (default: `first.last,first,firstlast`) |
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
prospeo search → find_emails.py → leads_found.csv
                                        ↓  (or any leads.csv)
                                  list_send.py  →  sent_log.csv  →  followup.py
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
