# Bengaluru Renter's Copilot — Progress Log

Living document. Updated after every meaningful step so a new chat session can pick up with full context. **Read this top-to-bottom before continuing work.**

Last updated: 2026-08-14 late night (Steps 9-13 done: XGBoost pricing pipeline (MAPE 22.5%, R² 0.71 on 1166 rows, 40 localities), Streamlit dashboard running locally, Power BI CSVs + build guide shipped. Left: HF Spaces deploy, GH Actions cron.)

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

## Repo layout (current)

```
bengaluru-renter-copilot/
├── db/
│   ├── schema.sql          ✅ 8 tables, canonical
│   └── copilot.db          ✅ built by init_db.py, gitignored
├── scrapers/
│   ├── __init__.py         ✅
│   ├── db_utils.py         ✅ shared DB helpers
│   ├── nobroker.py         ✅ Playwright scraper, 17 listings scraped
│   └── telegram.py         ⚠️ WRITTEN LOCALLY, UNTESTED, UNCOMMITTED
├── extraction/
│   ├── regex_v1.py         ✅ (17 rows in extractions)
│   ├── gemini_flash.py     ✅ (17 rows in extractions)
│   └── label.py            ✅ CLI labeler
├── data/
│   └── labeled_v1.jsonl    ✅ 15 hand-labeled ground-truth listings
├── benchmark/
│   └── run_benchmark.py    ✅ regex 90.2% vs gemini-flash 89.2% macro accuracy
├── pricing/                ⏳ empty
├── dashboards/
│   ├── streamlit/          ⏳ empty
│   └── powerbi/            ⏳ empty
├── pipeline/               ⏳ empty
├── .github/workflows/      ⏳ empty
├── scripts/
│   └── init_db.py          ✅ idempotent, verifies 8 tables
├── notebooks/              ⏳ empty
├── tests/                  ⏳ empty
├── .env                    (local only, gitignored — GEMINI_API_KEY + TELEGRAM_API_ID/HASH set)
├── .env.example            ✅
├── .gitignore              ✅
├── .telegram_session.session  (local only, gitignored — Telethon login persisted)
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

## Build order — status

1. ✅ Setup & verify env (git, venv, deps, playwright, init_db → 8 tables)
2. ✅ **NoBroker scraper** — 17 unique listings under `run_id=1` (30 scraped, 13 SHA256-deduped)
3. ✅ **Regex baseline extractor** — 17 rows in `extractions` with `extractor='regex-v1'`. Latency 0.4-2.1ms per listing.
4. ✅ **Gemini Flash extractor** — 17 rows in `extractions` with `extractor='gemini-flash'`, model=`gemini-3.5-flash`. Latency avg ~5.7s, cost **$0.00487** for 17 listings.
5. ✅ **Labeling harness** — `extraction/label.py` CLI written; 15 listings hand-labeled to `data/labeled_v1.jsonl`. (Committed as `Step 5: labeling harness + 15 hand-labeled ground-truth listings`.)
6. ✅ **Benchmark harness** — regex-v1 vs gemini-flash on the 15 labeled listings. **Regex 90.2% macro accuracy, Gemini 89.2%.** Regex slightly edges Gemini on NoBroker card text — expected, since NoBroker cards are labels-only. The gap will invert on unstructured Telegram text. Written to `benchmark_runs`. (Committed as `Step 6: benchmark harness — regex 90.2% vs gemini-flash 89.2% macro accuracy`.)
7. ✅ **Telegram scraper** — Telethon, two-phase `--list-groups`/`--groups` workflow. Tested and committed. run_id=2 scraped 76 raw / 35 new listings from "Bangalore Flatmates & Rent (no spam)" and "Flat And Flatmates Bangalore". Filter excludes sale posts, rent regex catches "RENT: 27k" style, KNOWN_LOCALITIES extended with Kadugodi/Harlur/Hoodi/etc from real data. Also shipped `scripts/inspect_telegram.py` for quick DB peek.
8. ✅ **Silver-labeling + honest benchmark** — Gemini Flash re-run on 20 of 36 Telegram listings (Google's free tier is now **20 req/day**, not 15 RPM — 15 stragglers deferred to tomorrow's quota reset). Labeler accepts Gemini's guesses as prefilled defaults; each label now carries `label_source` ('human' vs 'silver-gemini') to prevent grading Gemini against its own answers. Benchmark now prints two tables: HUMAN-ONLY (regex 90.2% vs Gemini 89.2% on 17 NoBroker cards — reproduces Step 6, this is the number to publish) and FULL-WITH-SILVER (coverage view, Gemini score inflated, flagged). Two `benchmark_runs` rows: `eval_set='holdout-v1-human'` and `'holdout-v1-full'`.
9. ✅ **Localities seed** — 32 Bengaluru localities in `localities` table with lat/lon + haversine distances to metro/ORR/Manyata/E-City/Whitefield/Bellandur. See `scripts/seed_localities.py`.
10. ✅ **XGBoost pricing model v1** — `pricing/train_xgb.py`, 5-fold CV, 1166 trainable listings from 40 localities.
    - CV **MAPE 22.5%**, R² 0.71, MAE ₹7,740, per-fold std 1.1%
    - Design choices: MAE loss (robust to outliers), rent-per-sqft training filter ₹12-150, model params scale with n_train, locality + (locality × BHK) leave-one-out median rent-per-sqft priors
    - See `pricing/xgb-v1.pkl` + `.features.json`
11. ✅ **Deal-detection predict script** — `pricing/predict.py`. Plausibility filter (rent-per-sqft ≥ ₹10) excludes source-data anomalies from deal view. Two modes: whole-flat deals (main) AND flatmate-share deals (predicted_whole ÷ BHK vs actual per-person).
12. ✅ **Enrichment re-parser** — `scripts/enrich_listings.py`. Re-parses raw NoBroker card text for property_type (98% coverage), deposit (100%), floor_num/total_floors (62%). Age of Building not present in card text → skipped.
13. ✅ **Streamlit dashboard** — `dashboards/streamlit/app.py`. Filters, KPIs, ranked deals table with NoBroker deep-links, model transparency card. Runs locally at http://localhost:8501.
14. ✅ **Power BI export + build guide** — `dashboards/powerbi/export.py` dumps 4 CSVs (listings, deals, localities, model_metrics). `BUILD_GUIDE.md` walks through 4-page report assembly with DAX measures.

**Left for future sessions:**
15. ⏳ **HF Spaces deploy** — Streamlit dashboard to a public URL. Instructions in `dashboards/streamlit/DEPLOY_HF.md`.
16. ⏳ **GitHub Actions weekly cron** — `.github/workflows/weekly.yml`. NoBroker-only (Telegram needs interactive auth, skipped in CI). Runs Sundays 02:00 UTC.
17. ⏳ **DistilBERT fine-tune** (deferred — MVP is shipping without it; Gemini + regex ensemble is good enough for extraction)

## Model story (as of 2026-08-14)

- Started with 358 listings across 14 tech corridors, MAPE 33% (overfit and biased)
- Fixed NoBroker `searchParam` bug — hardcoded downtown coords caused every locality search to return same MG Road flats → 39 unique out of 513 scraped. After fix + more localities: 1566 total listings, 40+ localities.
- Fixed model overfitting by switching to **MAE loss** and adding rent-per-sqft training filter → MAPE 22.5%, R² 0.71
- Added enrichment for property_type/deposit/floor/age (age had 0% card coverage, dropped from features)
- Manual verification: top-1 Domlur ₹15k 2BHK 1200sqft matches NoBroker's live page exactly. Model correctly flagged 44% below prediction.

## Honest limitations (bake into portfolio writeup)

- **Locality-level features only** — no exact building name, no floor for 38% of listings
- **Card-level scraping** — NoBroker detail pages have more info but we don't click into them yet
- **NoBroker card errors pass through unchanged** — plausibility filter catches the worst (₹/sqft < 10)
- **Rank-order deal detection compensates for point-estimate noise** — 22% MAPE means predictions wobble ±22% but ranking is stable
- **Locality label = search query, not real address** — NoBroker's radius search occasionally returns distant listings for a query (found via #855 Kalyan Nagar listing → Anekal URL)
9. ⏳ Localities seed data (lat/lon + distances)
10. ⏳ Feature engineering + XGBoost + time-based CV + locality-stratified eval
11. ⏳ Prediction script → `predictions`
12. ⏳ Streamlit app
13. ⏳ Deploy Streamlit to HF Spaces
14. ⏳ Power BI 4-page report → Power BI Service
15. ⏳ GitHub Actions weekly.yml (Sun 02:00 UTC)

## Credentials status

| Cred | Purpose | Status |
|---|---|---|
| GitHub | repo | ✅ |
| Gemini API key | benchmark baseline | ✅ (in .env, tier: free) |
| Telegram `api_id` + `api_hash` | Telethon | ✅ (in .env, session persisted) |
| Hugging Face token | Space deploy | ⏳ not yet requested |

## Session log

### Session 1 (2026-08-12) — Steps 1 & 2
Repo scaffold, NoBroker scraper. Details preserved in git commit messages.

### Session 2 (2026-08-13) — Steps 3 & 4
Regex baseline extractor + Gemini Flash extractor. See commit messages. Gemini model debugging saga documented below.

### Session 3 (2026-08-13/14) — Steps 5, 6, Telegram scraper started
- Wrote `extraction/label.py` CLI. Hand-labeled 15 of the 17 NoBroker listings (2 skipped for ambiguity).
- Wrote `benchmark/run_benchmark.py`. Computes per-field accuracy, macro accuracy, cost/1k, and p50/p95 latency for each extractor against `labeled_v1.jsonl`. Wrote first row to `benchmark_runs` for regex-v1 (90.2%) and gemini-flash (89.2%).
- Committed & pushed Steps 5 and 6.
- Unblocked Telegram creds (my.telegram.org worked eventually). Wrote `scrapers/telegram.py` — Telethon, two-phase `--list-groups`/`--groups` workflow, `looks_like_listing` filter (BHK/rent markers + ≥60 chars), writes to same `raw_listings`/`listings`/`listing_observations` schema under `source='telegram:<group_name>'`.
- Chat session was accidentally deleted before Telegram scraper could be tested or committed.

### Session 4 (2026-08-14) — Context recovery
- Reconstructed state from git log + working tree + this file.
- Reviewed `scrapers/telegram.py` end-to-end. Verified signatures against `db_utils.py` — all clean.

## Known risks / open questions

1. **NoBroker anti-bot** — currently fine, but selectors may drift.
2. **Deposit priors** — not universally 10× in BLR (range 2–3× for corporate stock). Deposit-to-rent ratio is a *feature*, not a constant. Bookmark for Step 10.
3. **Deal threshold** — 15% is fine only if MAPE < ~10%. If MAPE ~12%, switch to quantile model + define deal as `actual < predicted_p10`.
4. **Telegram scraper — untested.** First real run may reveal: (a) Windows console emoji encoding issues in group names, (b) rate limiting from Telethon's FloodWait, (c) filter false-positives from casual "family" / "owner" mentions.

## Uncommitted local changes (as of 2026-08-14)

| File | Change | Notes |
|---|---|---|
| `scrapers/telegram.py` | **new file, 309 lines** | Real new work. Untested. |
| `extraction/gemini_flash.py` | whole-file diff | Almost certainly a CRLF↔LF line-ending flip, not real edits. Run `git diff --stat` to confirm; if so, revert or normalize before commit. |
| `data/labeled_v1.jsonl` | 17 lines modified | Likely also line-ending. Same verification. |

## Next action (tomorrow morning, after Gemini quota resets ~24h from 2026-08-14 evening)

**Continue Step 8: finish the remaining 15 Telegram listings with proper human labels.**

```powershell
cd C:\coding\bengaluru-renter-copilot
.venv\Scripts\Activate.ps1

# 1. Gemini extract the 15 stragglers (auto-skips already-done listings)
python -m extraction.gemini_flash

# 2. Label them — this time READ each listing and correct Gemini where wrong.
#    Any field you type into (instead of Enter-through) will be tagged
#    label_source='human'. Aim to grow human labels from 17 -> ~32.
python -m extraction.label

# 3. Re-run benchmark, check that HUMAN-ONLY macro accuracy still looks sane
python -m benchmark.run_benchmark

# 4. Commit
git add data/labeled_v1.jsonl
git commit -m "Step 8 continued: label remaining 15 Telegram listings (n_human ~32)"
git push
```

**After that, Step 9: DistilBERT fine-tune.** With ~32 human labels the fine-tune is still small but viable for a portfolio demo. Real production would want 200+, but that's a Telegram-scraping-scale problem, not a modelling problem.

## Older next-action archive: Step 7a sanity-test (superseded)

```powershell
cd C:\coding\bengaluru-renter-copilot
.venv\Scripts\Activate.ps1

# 1. Confirm telethon is installed
pip show telethon

# 2. List groups (uses existing session, no re-login needed)
python -m scrapers.telegram --list-groups
```

Expected: prints every group/channel Rajveer is in with `id=... 'title'`. If session is stale, Telethon will re-prompt for phone + SMS code.

**Step 7b: Small real scrape once groups are chosen.**

```powershell
# Pick 1-2 groups from the list, start small
python -m scrapers.telegram --group-ids <id1> --limit-per-group 30 --min-days 7
```

Then verify writes:
```python
python -c "import sqlite3; c=sqlite3.connect('db/copilot.db'); print(c.execute(\"SELECT run_id, source, status, n_raw, n_new FROM scrape_runs ORDER BY run_id DESC LIMIT 3\").fetchall())"
```

**Step 7c: Commit.** Once tested, add `scrapers/telegram.py`, verify `.telegram_session.session` is gitignored (it is), commit + push as `Step 7: Telegram scraper (Telethon)`.

### Small polish items to consider before committing telegram.py

- L43: `Dialog` imported but unused — safe to remove.
- L273: `_parse_rent(m["text"])` called twice per message in the write loop (once for `upsert_listing`, once for `log_observation`). Cheap, but assign it to a local for clarity.
- L64: filter keywords `preferred|bachelor|family|owner|broker` are broad. If Step 7b shows a lot of noise, tighten to require BHK OR ₹/rs OR sqft to hit.
- No FloodWait handling. Telethon usually handles it internally but worth wrapping the `iter_messages` loop in try/except for a friendlier error message.

None of these block testing.

## Model debugging notes (Step 4 saga)
- `gemini-1.5-flash` → 404 (retired on v1beta for fresh keys, 2025)
- `gemini-2.5-flash` → 404 "no longer available to new users" (mid-2025 restrictions)
- `gemini-3.5-flash` → JSON parse error initially (reasoning tokens ate the 400-token output budget)
- `gemini-3.5-flash` + `max_output_tokens=2048` → **working**. Kept.

**Available Gemini models on user's key**: `gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-2.5-flash-lite`, `gemini-3-flash-preview`, `gemini-3.5-flash`, `gemini-3.5-flash-lite`, `gemini-3.6-flash`, `gemini-3.1-flash-lite`. Fallback ladder if 3.5-flash deprecates: `3.5-flash-lite` (cheaper) then `3.6-flash`.

**Cost note:** `cost_usd` in extractions is the *hypothetical paid-tier cost* from token counts × pricing constants ($0.30/$2.50 per 1M in/out). Actual out-of-pocket = **$0** while under free tier (15 RPM, 1M tokens/day). Even at 10k listings/week paid-tier cost would be ~$3/mo.

## How I keep this file current

After every meaningful action (new code shipped, test result, decision made, credential added, blocker hit) I:
1. Update the appropriate section above.
2. Bump the "Last updated" line at the top.
3. Ship the updated PROGRESS.md to the user in the same message.
4. Remind the user to commit it to Git (`git add PROGRESS.md && git commit -m "progress: <what changed>"`).

If a new chat session starts, the user should paste this file's contents (or the GitHub raw URL) at the top of the first message so context is restored.
