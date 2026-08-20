"""Score every listing with the trained XGBoost pricing model and write
one row per (listing, model_version) to the `predictions` table.

For each listing:
    predicted_rent   = model.predict(features) transformed back from log-space
    value_score      = (predicted - actual) / predicted
    is_deal          = 1 iff value_score >= 0.15
                       (actual rent is >= 15% below predicted "fair" rent)

Skips listings with missing bhk/area/rent (can't score without them).

Usage:
    python -m pricing.predict
    python -m pricing.predict --model-version xgb-v1
    python -m pricing.predict --top 20     # also print the top-N deals
"""
from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from scrapers.db_utils import get_conn, utcnow_iso
from pricing.train_xgb import (
    TRAIN_SQL, build_features, MODELS_DIR,
)

DEAL_THRESHOLD = 0.15

# Rent-per-sqft plausibility floor. Bengaluru's cheapest real market rents
# are ~₹15/sqft even for basic accommodations; anything below ~₹10/sqft is
# almost certainly a data anomaly (mistyped rent, mislabelled maintenance
# charge, per-room flatmate rate, etc.) and should NOT be surfaced as a
# "deal" — the source data is what's wrong, not the price. We still SCORE
# these listings so the DB has predictions for them, but flag them as
# suspect and exclude from the top-deals view.
MIN_RENT_PER_SQFT = 10.0


def load_model(model_version: str):
    model_path    = MODELS_DIR / f"{model_version}.pkl"
    features_path = MODELS_DIR / f"{model_version}.features.json"
    if not model_path.exists():
        raise SystemExit(f"Model not found: {model_path}. "
                         f"Run `python -m pricing.train_xgb` first.")
    with model_path.open("rb") as f:
        model = pickle.load(f)
    features = json.loads(features_path.read_text())
    return model, features


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-version", default="xgb-v1")
    ap.add_argument("--top", type=int, default=10,
                    help="Print top-N deals by value_score (default 10)")
    args = ap.parse_args()

    model, features = load_model(args.model_version)
    feature_cols = features["feature_cols"]

    # Load and build features exactly like training. The build_features
    # function also filters out extreme rents/areas — this means we score
    # the model's IN-DOMAIN listings only, which is what we want for deal
    # detection anyway.
    with get_conn() as conn:
        df_raw = pd.read_sql_query(TRAIN_SQL, conn)
    if df_raw.empty:
        raise SystemExit("No listings with rent + area + bhk to score.")

    X, y_log_true, cols = build_features(df_raw)
    # Reindex to match training feature order EXACTLY (in case categories missing)
    X = X.reindex(columns=feature_cols, fill_value=0)

    # We also need listing_id aligned with X. build_features drops some rows
    # via filters, so re-run the same filters manually to get the surviving
    # index, then map back to listing_ids via df_raw.
    surviving_idx = X.index
    df_pred = df_raw.loc[surviving_idx].copy()

    # Predict (log-space -> rupees)
    y_pred_log = model.predict(X)
    df_pred["predicted_rent"] = np.exp(y_pred_log)
    df_pred["actual_rent"]    = df_pred["rent_monthly"]
    df_pred["value_score"]    = (
        (df_pred["predicted_rent"] - df_pred["actual_rent"])
        / df_pred["predicted_rent"]
    )

    # Plausibility filter: a "deal" only counts if the actual rent is
    # a physically plausible market number. Anything below MIN_RENT_PER_SQFT
    # is treated as suspect source data and cannot be flagged as a deal.
    df_pred["rent_per_sqft"] = df_pred["actual_rent"] / df_pred["area_sqft"]
    df_pred["is_plausible"]  = (df_pred["rent_per_sqft"] >= MIN_RENT_PER_SQFT).astype(int)
    df_pred["is_deal"] = (
        (df_pred["value_score"] >= DEAL_THRESHOLD)
        & (df_pred["is_plausible"] == 1)
    ).astype(int)

    n_suspect = int((df_pred["is_plausible"] == 0).sum())
    print(f"Plausibility filter: {n_suspect} listings excluded from deal detection "
          f"(rent/sqft < ₹{MIN_RENT_PER_SQFT:.0f}).")

    # Write to predictions table
    now = utcnow_iso()
    inserts = [
        (
            int(r["listing_id"]),
            args.model_version,
            float(r["predicted_rent"]),
            None, None,  # p10/p90 (quantile v2 later)
            float(r["value_score"]),
            int(r["is_deal"]),
            now,
        )
        for _, r in df_pred.iterrows()
    ]

    with get_conn() as conn:
        # Idempotent per (listing_id, model_version): delete then re-insert
        conn.execute(
            "DELETE FROM predictions WHERE model_version = ?",
            (args.model_version,),
        )
        conn.executemany(
            "INSERT INTO predictions "
            "(listing_id, model_version, predicted_rent, predicted_p10, "
            " predicted_p90, value_score, is_deal, predicted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            inserts,
        )

    n_deals = int(df_pred["is_deal"].sum())
    print(f"Scored {len(df_pred)} listings with {args.model_version}.")
    print(f"Deals flagged (value_score >= {DEAL_THRESHOLD:.0%}): {n_deals} "
          f"({n_deals/len(df_pred)*100:.1f}%)")

    # Show top deals — filter to is_deal=1 (already plausibility-checked)
    if args.top > 0 and n_deals > 0:
        top_deals = (
            df_pred[df_pred["is_deal"] == 1]
            .sort_values("value_score", ascending=False)
            .head(args.top)
        )
        print()
        print(f"Top {min(args.top, n_deals)} whole-flat deals:")
        print(f"{'id':>5}  {'locality':<18} {'bhk':>4}  {'area':>6}  "
              f"{'actual':>10}  {'predicted':>10}  {'save %':>7}")
        print("-" * 76)
        for _, r in top_deals.iterrows():
            loc = (r["locality_norm"] or "").replace("_", " ").title()[:18]
            print(
                f"{int(r['listing_id']):>5}  {loc:<18} {r['bhk']:>4.1f}  "
                f"{r['area_sqft']:>6.0f}  ₹{r['actual_rent']:>8,.0f}  "
                f"₹{r['predicted_rent']:>8,.0f}  {r['value_score']*100:>6.1f}%"
            )

    # ---- Flatmate-share deals (second pass) ----------------------------
    # For source='telegram_share:%' posts, rent_monthly is per-person. We
    # predict the whole-flat rent (by multiplying per-person by BHK to make
    # the row look "whole-flat" for the model), then divide the prediction
    # back down by BHK to get a per-person fair rent, then compare against
    # the actual per-person rent.
    _score_share_deals(model, feature_cols, args.model_version, args.top)


SHARE_SQL = """
SELECT
    l.listing_id, l.first_seen_at, l.source, l.locality_norm,
    l.bhk, l.area_sqft, l.rent_monthly, l.furnishing,
    l.property_type, l.deposit, l.floor_num, l.total_floors, l.age_years,
    loc.lat, loc.lon,
    loc.dist_nearest_metro_km, loc.dist_orr_km,
    loc.dist_manyata_km, loc.dist_ecity_km,
    loc.dist_whitefield_km, loc.dist_orr_bellandur_km
FROM listings l
LEFT JOIN localities loc ON loc.locality_norm = l.locality_norm
WHERE l.source LIKE 'telegram_share:%'
  AND l.rent_monthly IS NOT NULL
  AND l.area_sqft   IS NOT NULL
  AND l.bhk         IS NOT NULL
"""


def _score_share_deals(model, feature_cols, model_version, top_n) -> None:
    with get_conn() as conn:
        df_share = pd.read_sql_query(SHARE_SQL, conn)
    if df_share.empty:
        print("\n(No flatmate-share listings to score.)")
        return

    # Reconstruct whole-flat-equivalent rent so build_features doesn't drop
    # these rows to its rent-per-sqft filter. The model then predicts the
    # whole-flat rent; we divide back by BHK for the per-person comparison.
    df_for_feat = df_share.copy()
    df_for_feat["bhk_int"] = df_for_feat["bhk"].round().clip(lower=1).astype(int)
    df_for_feat["_actual_per_person"] = df_for_feat["rent_monthly"]
    df_for_feat["rent_monthly"] = (
        df_for_feat["rent_monthly"] * df_for_feat["bhk_int"]
    )

    X, _, _ = build_features(df_for_feat)
    X = X.reindex(columns=feature_cols, fill_value=0)
    if X.empty:
        print("\n(All share listings filtered out during feature build.)")
        return

    surviving = df_for_feat.loc[X.index].copy()
    y_pred_log = model.predict(X)
    surviving["predicted_whole_rent"] = np.exp(y_pred_log)
    surviving["predicted_per_person"] = (
        surviving["predicted_whole_rent"] / surviving["bhk_int"]
    )
    surviving["actual_per_person"] = surviving["_actual_per_person"]
    surviving["value_score"] = (
        (surviving["predicted_per_person"] - surviving["actual_per_person"])
        / surviving["predicted_per_person"]
    )
    surviving["is_deal"] = (surviving["value_score"] >= DEAL_THRESHOLD).astype(int)

    n_share = len(surviving)
    n_deals = int(surviving["is_deal"].sum())
    print()
    print("=" * 76)
    print(f"Scored {n_share} flatmate-share listings. "
          f"{n_deals} deals flagged (per-person actual >= 15% below predicted).")
    print("=" * 76)

    if top_n > 0 and n_deals > 0:
        top = (surviving[surviving["is_deal"] == 1]
               .sort_values("value_score", ascending=False)
               .head(top_n))
        print(f"\nTop {min(top_n, n_deals)} flatmate-share deals:")
        print(f"{'id':>5}  {'locality':<18} {'bhk':>4}  {'area':>6}  "
              f"{'/person':>10}  {'predicted':>10}  {'save %':>7}")
        print("-" * 76)
        for _, r in top.iterrows():
            loc = (r["locality_norm"] or "").replace("_", " ").title()[:18]
            print(
                f"{int(r['listing_id']):>5}  {loc:<18} {r['bhk']:>4.1f}  "
                f"{r['area_sqft']:>6.0f}  ₹{r['actual_per_person']:>8,.0f}  "
                f"₹{r['predicted_per_person']:>8,.0f}  "
                f"{r['value_score']*100:>6.1f}%"
            )


if __name__ == "__main__":
    main()
