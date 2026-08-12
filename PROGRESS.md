# Bengaluru Renter's Copilot — Progress Log

Living document. Updated after every meaningful step so a new chat session can pick up with full context. **Read this top-to-bottom before continuing work.**

Last updated: 2026-08-12 (Step 3 DONE — regex baseline extracted 17 listings, moving to Step 4)

---

## The mission (one paragraph)

Automated Bengaluru rental-market intelligence. Weekly pipeline: scrape NoBroker + Telegram → SHA256 dedup into `raw_listings` → structured upsert into `listings` → DistilBERT extracts free-text fields → XGBoost prices each listing → flag deals where `value_score ≥ 0.15`. Deliverables: Streamlit dashboard on HF Spaces + Power BI report. Portfolio project showcasing ~14 skills; hosted free, publicly reproducible.

## Locked-in stack

| Layer | Tool |
|---|---|
| Sources | NoBroker (web) + Telegram groups |
| Storage | SQLite (`db/copilot.db`) |
| Extraction | DistilBERT fine-tuned locally on RTX 3050 |
| Extraction benchmarks | Gemini Flash + regex |
| Pricing | XGBoost on `log(rent_monthly)` |
| Dashboards | Streamlit on HF Spaces + Power BI Service |
| Orchestration | GitHub Actions, cron Sun 02:00 UTC |
| Language | Python 3.10 |

**Do not substitute.** Do not add MagicBricks / Housing / 99acres.

## User's environment

- Windows 11, Python 3.10.11, VS Code
- GPU: NVIDIA RTX 3050 (~4GB VRAM — batch 8–16 + grad accumulation)
- Repo location: `C:\coding\bengaluru-renter-copilot`
- GitHub: https://github.com/rajveer-2004/bengaluru-renter-copilot (public, main branch)
- User email (git): `rvs51101@gmail.com`, name: `Rajveer`
- Timezone: Asia/Calcutta (UTC+5:30)

## Repo layout (as scaffolded)

```
bengaluru-renter-copilot/
├── db/
│   ├── schema.sql          ✅ 8 tables, canonical
│   └── copilot.db          ✅ built by init_db.py, gitignored
├── scrapers/
│   ├── __init__.py         ✅
│   ├── db_utils.py         ✅ shared DB helpers
│   ├── nobroker.py         ✅ Playwright scraper (untested against live site)
│   └── telegram.py         ⏳ not started (blocked on API creds)
├── extraction/             ⏳ empty
├── pricing/                ⏳ empty
├── benchmark/              ⏳ empty
├── dashboards/
│   ├── streamlit/          ⏳ empty
│   └── powerbi/            ⏳ empty
├── pipeline/               ⏳ empty
├── .github/workflows/      ⏳ empty
├── scripts/
│   └── init_db.py          ✅ idempotent, verifies 8 tables
├── notebooks/              ⏳ empty
├── tests/                  ⏳ empty
├── .env                    (local only, gitignored)
├── .env.example            ✅
├── .gitignore              ✅
├── requirements.txt        ✅ (torch installed separately with CUDA index)
├── README.md               ✅
└── PROGRESS.md             ← this file
```

## Schema — the 8 tables (source of truth = `db/schema.sql`)

1. **scrape_runs** — one row per weekly pipeline invocation, everything references `run_id`
2. **raw_listings** — verbatim wire text, SHA256-deduped, never mutated
3. **listings** — canonical structured deduped rows, `dedup_key = locality_norm|bhk|rent_bucket|area_bucket`
4. **listing_observations** — append-only sighting log, powers days-on-market + time-based CV
5. **extractions** — one row per `(listing, extractor, version)`; DistilBERT/Gemini/regex all coexist
6. **localities** — reference table w/ lat/lon and pre-computed distances to metro/ORR/tech corridors
7. **predictions** — one row per `(listing, model_version)`, incl. optional p10/p90
8. **benchmark_runs** — aggregate metrics per extractor (per-field accuracy JSON, cost/1k, p50/p95 latency)

## Ground rules (don't break these)

- Schema is the contract. Ask before changing.
- Only NoBroker + Telegram. No other sources.
- Never commit `.env`, session files, or `copilot.db`.
- Always time-based CV, never random.
- Never use `sqlite3` shell — Python's `sqlite3` module only.
- Weekly pipeline must be idempotent per `run_id`.
- Ask before installing anything outside `requirements.txt`.
- Be honest on benchmarks — if regex wins on a field, say so.

## Build order (from handoff)

1. ✅ Setup & verify env (git, venv, deps, playwright, init_db → 8 tables)
2. ✅ **NoBroker scraper** — working. Dry-run confirmed card parsing. Real run wrote 17 unique listings to DB under `run_id=1` (30 scraped, 13 SHA256-deduped as cross-locality overlaps).
3. ✅ **Regex baseline extractor** — done. 17 rows in `extractions` with `extractor='regex-v1'`. Latency 0.4-2.1ms per listing. Catches `tenant_pref=family` and `is_owner=1` reliably (from NoBroker's structured labels); `veg_only`, `negotiable`, `lock_in_months` correctly all None (NoBroker cards don't advertise these — Telegram data would).

4. ⏳ Gemini Flash extractor → `extractions` w/ `extractor='gemini-flash'`, track cost/latency
5. ⏳ Labeling harness → `data/labeled_v1.jsonl`, ~500 examples
6. ⏳ DistilBERT fine-tune on RTX 3050 → `extractions` w/ `extractor='distilbert-v1'`
7. ⏳ Benchmark harness → `benchmark_runs`
8. ⏳ Localities seed data (lat/lon + distances)
9. ⏳ Feature engineering + XGBoost + time-based CV + locality-stratified eval
10. ⏳ Prediction script → `predictions`
11. ⏳ Streamlit app
12. ⏳ Deploy Streamlit to HF Spaces
13. ⏳ Power BI 4-page report → Power BI Service
14. ⏳ GitHub Actions weekly.yml (Sun 02:00 UTC)

## Credentials status

| Cred | Purpose | Status |
|---|---|---|
| GitHub | repo | ✅ created, first push done |
| Telegram `api_id` + `api_hash` | Telethon | ❌ my.telegram.org keeps returning generic `ERROR`. Deferred. Try again later in incognito after 30min cool-off. |
| Gemini API key | benchmark baseline | ⏳ not yet requested |
| Hugging Face token | Space deploy | ⏳ not yet requested |

**Telegram scraping is deferred** — decided to build the whole pipeline on NoBroker data first, slot Telegram in later. Not on the critical path.

## What's been done, session by session

### Session 1 (2026-08-12) — Steps 1 & 2

**Step 1: repo scaffold + env setup — DONE**
- Received attached `schema.sql` + `README.md` from user
- Generated repo skeleton in cloud sandbox: `.gitignore`, `.env.example`, `requirements.txt`, `scripts/init_db.py`, folder tree with `.gitkeep`s
- Sent zip to user (`bengaluru-renter-copilot.zip`)
- User unzipped to `C:\coding\bengaluru-renter-copilot\`
- Verified: `git --version` (2.52), `python -m venv .venv`, `Activate.ps1` (needed ExecutionPolicy tweak? no — worked first try)
- Installed torch CUDA build via `--index-url https://download.pytorch.org/whl/cu121`, then `pip install -r requirements.txt`, then `playwright install chromium`
- Ran `python scripts/init_db.py` → all 8 expected tables printed ✅
- Git config: `Rajveer` / `rvs51101@gmail.com`
- First commit `Step 1: repo scaffold, schema, init_db` (16 files), renamed master→main, pushed to `https://github.com/rajveer-2004/bengaluru-renter-copilot`
- Attempted Telegram API registration at my.telegram.org — kept getting generic `ERROR` (tried multiple combos of App title, Short name, URL, Description). Deferred.

**Step 2: NoBroker scraper — DONE**

Test results:
- Dry-run on HSR Layout: `article` selector fallback found cards, parsed BHK/area/rent/URLs correctly
- Real run (`--max-per-locality 10 --localities "HSR Layout" "Koramangala" "Whitefield"`): 30 scraped, 17 written (13 SHA256-deduped)
- All four core tables populated under `run_id=1`
- NoBroker did NOT block — no anti-bot triggered at this volume

Known parsing gaps (fix later, not blockers):
- `locality` field currently stores the search query, not the actual property locality (which lives in the URL/text). Fix in Step 3 extraction.
- Deposit not parsed yet (needs a dedicated regex — it's on a separate line under "Deposit" label).

**Original code details:**
- Wrote `scrapers/db_utils.py`:
  - `utcnow_iso()`, `git_sha()`, `normalize_text()`, `content_hash()` (SHA256)
  - `normalize_locality()`, `rent_bucket()`, `area_bucket()`, `dedup_key()`
  - `get_conn()` context manager (with `PRAGMA foreign_keys = ON`, `row_factory = sqlite3.Row`)
  - `start_scrape_run()` / `finish_scrape_run()`
  - `insert_raw_listing()` — returns None on duplicate (UNIQUE(source, content_hash))
  - `upsert_listing()` — returns `(listing_id, is_new)`, keyed by `dedup_key`
  - `log_observation()`
- Wrote `scrapers/nobroker.py`:
  - Playwright headless Chromium, desktop user-agent, 1440×900 viewport
  - `DEFAULT_LOCALITIES` = HSR, Koramangala, Indiranagar, Whitefield, Bellandur, Marathahalli, Ecity, Jayanagar, BTM, Sarjapur
  - `SEARCH_URL_TMPL` uses NoBroker Bangalore rent search with `radius=2.0`
  - Scrolls 4× to trigger lazy-loading, then tries 4 candidate card selectors: `div[data-testid='property-card']`, `div.card.card-padding`, `article`, `div[itemtype*='Product']`
  - Per-card: captures `inner_text()`, tries to extract BHK / area sqft / rent (parses ₹/Rs, K, L suffixes) / furnishing via regex
  - Politeness: `time.sleep(random.uniform(2.0, 3.5))` between locality pages
  - Writes to raw_listings + listings + listing_observations under a single `scrape_run`
  - CLI flags: `--dry-run`, `--headed`, `--max-per-locality N`, `--localities ...`
- Sent zip to user (`bengaluru-renter-copilot-step2.zip`)
- ⏳ **AWAITING**: user to run `python -m scrapers.nobroker --dry-run --max-per-locality 5 --localities "HSR Layout"` and paste output

## Known risks / open questions

1. **NoBroker anti-bot** — they actively fight scrapers. Selectors are best guesses; first run may find 0 cards. Fallback plan: user runs `--headed` mode, screenshots the browser, we iterate on selectors from the live HTML.
2. **Telegram deferred** — need to unblock cred registration or move without it.
3. **Deposit priors** — schema notes explicit reminder: deposit is NOT universally 10 months in BLR (range 2–3 for corporate stock, up to 10 for traditional). Deposit-to-rent ratio is a feature, not a constant. Keep this in mind when we get to feature engineering (Step 9).
4. **Deal threshold** — 15% is fine only if MAPE < ~10%. If MAPE ~12%, switch to quantile model + define deal as `actual < predicted_p10`.

## Next action

Step 4: Gemini Flash extractor. Uses Google Gemini 1.5 Flash via the free tier (15 req/min, 1M tokens/day) as the "ceiling" of the benchmark. Sends the listing's `raw_text` + a JSON-schema prompt asking for the same 6 fields the regex baseline outputs, plus tracks `latency_ms` and `cost_usd` per call. Writes to `extractions` with `extractor='gemini-flash'`. Needs `GEMINI_API_KEY` in `.env` (user grabs from https://aistudio.google.com/apikey).

**Step 3 session notes:**
- User first tried extracting via File Explorer's Extract wizard; hit the "Replace or Skip" dialog. Confirmed .gitkeep files are 0-byte identical, safe to replace. Also gave the `Expand-Archive -Force` PowerShell fallback for future zips.
- Dry-preview via `--show --limit 5` confirmed extraction shape; then real run wrote 17 rows.
- Verified via `SELECT COUNT(1) FROM extractions` → 17.
- Committed as `Step 3: regex baseline extractor (17 listings, regex-v1)`.

## How I keep this file current

After every meaningful action (new code shipped, test result, decision made, credential added, blocker hit) I:
1. Update the appropriate section above.
2. Bump the "Last updated" line at the top.
3. Ship the updated PROGRESS.md to the user in the same message.
4. Remind the user to commit it to Git (`git add PROGRESS.md && git commit -m "progress: <what changed>"`).

If a new chat session starts, the user should paste this file's contents (or the GitHub raw URL) at the top of the first message so context is restored.
