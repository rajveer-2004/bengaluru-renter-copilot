"""Export SQLite data to CSVs for the Power BI dashboard.

Produces four CSVs in dashboards/powerbi/data/ that Power BI can load
directly via Get Data > Text/CSV:

  - deals.csv          — deal-flagged listings, one row per deal
  - listings.csv       — every scored listing (whole-flat + share)
  - localities.csv     — per-locality medians and counts
  - model_metrics.csv  — CV metrics for the transparency page

Usage:
    python -m dashboards.powerbi.export
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH   = REPO_ROOT / "db" / "copilot.db"
OUT_DIR   = REPO_ROOT / "dashboards" / "powerbi" / "data"
META_PATH = REPO_ROOT / "pricing" / "xgb-v1.features.json"


LISTINGS_SQL = """
SELECT
    l.listing_id,
    l.source,
    l.locality,
    l.locality_norm,
    l.bhk,
    l.area_sqft,
    l.rent_monthly,
    l.deposit,
    l.property_type,
    l.furnishing,
    l.floor_num,
    l.total_floors,
    l.first_seen_at,
    rl.source_url,
    p.predicted_rent,
    p.value_score,
    p.is_deal,
    loc.lat,
    loc.lon,
    loc.dist_nearest_metro_km,
    loc.nearest_metro_station,
    loc.dist_orr_km,
    loc.dist_whitefield_km,
    loc.dist_ecity_km,
    loc.dist_manyata_km
FROM listings l
LEFT JOIN raw_listings rl ON rl.raw_id = l.latest_raw_id
LEFT JOIN predictions  p  ON p.listing_id = l.listing_id
                         AND p.model_version = 'xgb-v1'
LEFT JOIN localities   loc ON loc.locality_norm = l.locality_norm
WHERE l.rent_monthly IS NOT NULL
  AND l.area_sqft   IS NOT NULL
  AND l.bhk         IS NOT NULL
"""


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"DB not found: {DB_PATH}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as conn:
        listings = pd.read_sql_query(LISTINGS_SQL, conn)
        localities = pd.read_sql_query("SELECT * FROM localities", conn)

    # Feature engineering for BI convenience
    listings["rent_per_sqft"] = listings["rent_monthly"] / listings["area_sqft"]
    listings["save_pct"]      = (listings["value_score"] * 100).round(1)
    listings["deposit_to_rent"] = (
        listings["deposit"] / listings["rent_monthly"]
    ).round(2)
    listings["bhk_int"]       = listings["bhk"].round().astype("Int64")
    listings["floor_ratio"]   = (
        listings["floor_num"] / listings["total_floors"]
    ).round(2)
    listings["source_bucket"] = listings["source"].astype(str).str.split(":").str[0]

    deals = (
        listings[listings["is_deal"] == 1]
        .sort_values("value_score", ascending=False)
        .copy()
    )

    # Per-locality summary
    loc_summary = (
        listings.groupby("locality_norm", dropna=False)
        .agg(
            n_listings=("listing_id", "size"),
            median_rent=("rent_monthly", "median"),
            median_rent_per_sqft=("rent_per_sqft", "median"),
            n_deals=("is_deal", "sum"),
            median_save_pct=("save_pct", "median"),
        )
        .reset_index()
        .merge(localities, on="locality_norm", how="left")
    )

    # Model metrics table
    metrics_rows = []
    if META_PATH.exists():
        meta = json.loads(META_PATH.read_text())
        m = meta.get("metrics", {})
        metrics_rows = [
            {"metric": "MAPE",             "value": m.get("mape", 0)},
            {"metric": "MAE (INR)",        "value": m.get("mae_inr", 0)},
            {"metric": "R^2",              "value": m.get("r2", 0)},
            {"metric": "MAPE per-fold std", "value": m.get("mape_std", 0)},
            {"metric": "N training rows",  "value": meta.get("n_total", 0)},
            {"metric": "N features",       "value": len(meta.get("feature_cols", []))},
        ]
    metrics = pd.DataFrame(metrics_rows)

    listings.to_csv(OUT_DIR / "listings.csv", index=False)
    deals.to_csv(OUT_DIR / "deals.csv", index=False)
    loc_summary.to_csv(OUT_DIR / "localities.csv", index=False)
    metrics.to_csv(OUT_DIR / "model_metrics.csv", index=False)

    print(f"Wrote 4 CSVs to {OUT_DIR}:")
    print(f"  listings.csv       {len(listings):,} rows")
    print(f"  deals.csv          {len(deals):,} rows")
    print(f"  localities.csv     {len(loc_summary):,} rows")
    print(f"  model_metrics.csv  {len(metrics):,} rows")


if __name__ == "__main__":
    main()
