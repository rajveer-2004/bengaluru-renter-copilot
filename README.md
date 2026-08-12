# Bengaluru Renter's Copilot

Live, automated rental-market intelligence for Bengaluru. Scrapes NoBroker and Telegram rental groups every Sunday, extracts structured fields from listing descriptions with a locally fine-tuned DistilBERT, prices each property with XGBoost, and flags listings ≥15% below predicted market value as genuine deals. Delivered via a public Streamlit app on Hugging Face Spaces and a 4-page Power BI report.

## Repo layout

```
bengaluru-renter-copilot/
├── db/
│   ├── schema.sql              # Canonical SQLite schema (source of truth)
│   └── copilot.db              # Built by scripts/init_db.py; gitignored
├── scrapers/
│   ├── nobroker.py             # Requests/Playwright scraper for nobroker.in
│   └── telegram.py             # Telethon client for rental groups
├── extraction/
│   ├── distilbert_finetune.py  # Fine-tuning loop for RTX 3050
│   ├── distilbert_infer.py     # Inference wrapper
│   ├── gemini_flash.py         # Benchmark baseline via Google API
│   └── regex_baseline.py       # Cheap-and-dumb baseline
├── pricing/
│   ├── features.py             # Feature engineering (locality, geo, etc.)
│   ├── train_xgb.py            # XGBoost + time-based CV
│   └── predict.py              # Score new listings, write predictions
├── benchmark/
│   └── run_benchmark.py        # DistilBERT vs Gemini vs regex on holdout
├── dashboards/
│   ├── streamlit/app.py        # Live public deals board
│   └── powerbi/                # .pbix + DAX measure docs
├── pipeline/
│   └── run_weekly.py           # Orchestration entrypoint
├── .github/workflows/
│   └── weekly.yml              # Sunday cron
├── notebooks/                  # EDA + model diagnostics
└── tests/
```

## Data model — key decisions

The schema separates concerns so any downstream stage can be re-run without re-scraping:

- **`raw_listings`** — exactly what came off the wire, hashed and deduped. Never mutated.
- **`listings`** — canonical, structured, deduped. One row per real-world listing across sources.
- **`listing_observations`** — append-only log of every time we saw a listing. Powers *days-on-market*, price-change detection, and time-based cross-validation.
- **`extractions`** — one row per (listing, extractor). Lets DistilBERT / Gemini / regex coexist on the same data and be benchmarked head-to-head.
- **`predictions`** — one row per (listing, model_version). Historical predictions are preserved for model drift analysis.
- **`localities`** — reference table with pre-computed geospatial features (distance to metro, ORR, Manyata / Ecity / Whitefield / Bellandur tech corridors).

## Pricing target and features

**Target:** `log(rent_monthly)` — rent is right-skewed; log stabilizes the loss and behaves well with MAPE-style eval.

**Features, by tier of extraction difficulty:**

| Tier | Features | Source |
|---|---|---|
| 1 — structured, must-have | locality, BHK, area_sqft, furnishing, property_type, deposit | scraper |
| 2 — structured, strong | floor/total_floors, age, facing, bathrooms, balconies, parking, amenity count | scraper |
| 3 — geospatial | dist to metro / ORR / Manyata / ECity / Whitefield / Bellandur | `localities` table |
| 4 — from unstructured text | tenant_pref, veg_only, is_owner, negotiable, lock_in, amenities list | DistilBERT |
| 5 — temporal | listing_month, days_on_market | `listing_observations` |

**Eval:** MAE + MAPE on original scale, **stratified by locality** (a model can look fine overall while being terrible in HSR). CV is **time-based** — train on weeks 1..N, validate on N+1. Random splits leak because listings recur across weeks.

**Deal threshold:** value_score = (predicted − actual) / predicted. `is_deal = 1` iff value_score ≥ 0.15. If model MAPE is 12%, a 15% threshold is barely signal — consider training a quantile model and defining "deal" as *actual < predicted_p10* instead.

## Weekly pipeline

`pipeline/run_weekly.py` executes: scrape → dedup + upsert → extract → feature-build → predict → refresh dashboards. Idempotent per `scrape_runs.run_id`. GitHub Actions cron: Sundays 02:00 UTC.
