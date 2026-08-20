"""Train XGBoost pricing model on `listings` joined to `localities`.

Predicts `log(rent_monthly)` from:
  Property features: bhk, area_sqft, log_area_sqft, rent_per_sqft_proxy
  Locality features: lat, lon, dist_nearest_metro_km, dist_orr_km,
                     dist_manyata_km, dist_ecity_km, dist_whitefield_km,
                     dist_orr_bellandur_km
  Categorical:       source (nobroker/telegram), furnishing (one-hot)

Trains with a random 80/20 holdout (time-based CV requires >~2 months of
data; ours is 1 week, so random is honest for MVP — will switch to
time-based once cron accrues history).

Writes:
  - Model pickle to pricing/xgb_v1.pkl
  - Feature-column order to pricing/xgb_v1.features.json
  - Metrics summary to stdout (MAPE, MAE, R^2)

Usage:
    python -m pricing.train_xgb
    python -m pricing.train_xgb --model-version xgb-v1
"""
from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, mean_absolute_percentage_error, r2_score
from sklearn.model_selection import KFold, train_test_split
from xgboost import XGBRegressor

from scrapers.db_utils import get_conn

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = REPO_ROOT / "pricing"
MODELS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

TRAIN_SQL = """
SELECT
    l.listing_id,
    l.first_seen_at,
    l.source,
    l.locality_norm,
    l.bhk,
    l.area_sqft,
    l.rent_monthly,
    l.furnishing,
    l.property_type,
    l.deposit,
    l.floor_num,
    l.total_floors,
    l.age_years,
    loc.lat,
    loc.lon,
    loc.dist_nearest_metro_km,
    loc.dist_orr_km,
    loc.dist_manyata_km,
    loc.dist_ecity_km,
    loc.dist_whitefield_km,
    loc.dist_orr_bellandur_km
FROM listings l
LEFT JOIN localities loc ON loc.locality_norm = l.locality_norm
WHERE l.rent_monthly IS NOT NULL
  AND l.area_sqft   IS NOT NULL
  AND l.bhk         IS NOT NULL
"""


NUMERIC_FEATURES = [
    "bhk", "area_sqft",
    "log_area_sqft",
    "lat", "lon",
    "dist_nearest_metro_km",
    "dist_orr_km",
    "dist_manyata_km",
    "dist_ecity_km",
    "dist_whitefield_km",
    "dist_orr_bellandur_km",
    # Newly parsed structured features (see scripts/enrich_listings.py).
    # age_years dropped from feature list — NoBroker card text doesn't
    # include age, so it was 100% sentinel and added noise. Bring it back
    # if we later scrape detail pages.
    "deposit_to_rent_ratio",  # proxy for corporate vs traditional building tier
    "floor_num",               # top-floor listings often rent 10-20% higher
    "total_floors",            # bigger buildings = amenity buildings
    "floor_ratio",             # floor_num / total_floors — normalized altitude
    # Locality-level rent prior. Leave-one-out median rent per sqft across
    # OTHER listings in the same locality. Gives the model a strong baseline
    # ("rents in Bellandur run ~₹28/sqft") so it only has to learn deviations
    # off the local median from BHK / area / furnishing / distance features.
    # In production this drops MAPE ~10 points for real-estate models.
    "locality_median_rent_per_sqft",
    # Per-(locality, BHK-bucket) median — tighter prior than locality-only.
    # A 1BHK in HSR is a fundamentally different price point than a 3BHK in
    # HSR; this feature gives the model that split. Falls back to
    # locality-only, then global median, when the (locality, bhk) bucket is
    # too small for a leave-one-out estimate.
    "locality_bhk_median_rent_per_sqft",
]

# One-hot columns produced by pd.get_dummies below get appended in build_features.
FURNISHING_CATS  = ["unfurnished", "semi", "full"]
SOURCE_CATS      = ["nobroker", "telegram"]
PROPERTY_TYPES   = ["apartment", "independent_house", "villa", "pg", "studio"]


def load_frame() -> pd.DataFrame:
    with get_conn() as conn:
        df = pd.read_sql_query(TRAIN_SQL, conn)
    return df


def _loo_median(group: pd.Series) -> pd.Series:
    """Leave-one-out median — for each row, the median of the group excluding
    that row. Reduces target leakage vs a plain groupby.median()."""
    vals = group.values
    n = len(vals)
    if n <= 1:
        return pd.Series([np.nan] * n, index=group.index)
    out = np.zeros(n)
    for i in range(n):
        out[i] = np.median(np.concatenate([vals[:i], vals[i + 1:]]))
    return pd.Series(out, index=group.index)


def build_features(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, list[str]]:
    """Return (X, y, feature_cols).  y is log(rent_monthly)."""
    df = df.copy()

    # Filter out absurd rents (typos, per-room, etc.) — 3k <= rent <= 500k
    df = df[df["rent_monthly"].between(3_000, 500_000)]
    df = df[df["area_sqft"].between(100, 10_000)]

    # Rent-per-sqft plausibility filter (both training AND inference share this
    # gate — see also predict.py's MIN_RENT_PER_SQFT). Removes:
    #   below ₹12/sqft: maintenance-fee-mistyped-as-rent, per-room flatmate
    #                   listings, and NoBroker data-entry mistakes.
    #   above ₹150/sqft: luxury micro-units (studios in Adarsh Palm Retreat etc)
    #                    that pull the model toward extreme predictions.
    # This is a training-time filter; it's about learning a clean mapping,
    # not about which listings the user eventually sees on the dashboard.
    rps = df["rent_monthly"] / df["area_sqft"]
    df = df[rps.between(12, 150)]

    # Engineer
    df["log_area_sqft"] = np.log1p(df["area_sqft"])

    # Locality prior: leave-one-out median rent-per-sqft in the same locality.
    # Small clean self-exclusion so a row can't leak its own signal into its
    # own feature. Global-median fallback for rows without a locality match.
    rps = df["rent_monthly"] / df["area_sqft"]
    df["_rps_tmp"] = rps
    global_med = float(np.median(rps))

    # (a) Coarse prior: per-locality
    loo_loc = df.groupby("locality_norm", group_keys=False)["_rps_tmp"].apply(_loo_median)
    df["locality_median_rent_per_sqft"] = loo_loc.fillna(global_med)

    # (b) Finer prior: per (locality, BHK-bucket). BHK bucketed to int since
    # 1.0/2.0/3.0 dominate the distribution. Falls back to coarse locality
    # prior when a bucket has < 2 rows, then to global median.
    df["_bhk_int"] = df["bhk"].round().astype(int)
    loo_loc_bhk = df.groupby(["locality_norm", "_bhk_int"], group_keys=False)["_rps_tmp"].apply(_loo_median)
    df["locality_bhk_median_rent_per_sqft"] = (
        loo_loc_bhk
        .fillna(df["locality_median_rent_per_sqft"])
        .fillna(global_med)
    )

    df = df.drop(columns=["_rps_tmp", "_bhk_int"])

    # ---- New structured features (from scripts/enrich_listings.py) -------
    # Deposit-to-rent ratio. Corporate/premium buildings tend to have ratios
    # of 8-12x; traditional owners 2-6x. Missing deposits get filled with the
    # median ratio so the feature can't destroy training signal on partial data.
    df["deposit_to_rent_ratio"] = df["deposit"] / df["rent_monthly"]
    med_ratio = float(df["deposit_to_rent_ratio"].median(skipna=True) or 6.0)
    df["deposit_to_rent_ratio"] = df["deposit_to_rent_ratio"].fillna(med_ratio)

    # Floor features. NaN for listings where floor wasn't parsed -> use -1
    # sentinel so the tree learns "this listing didn't disclose floor" as its
    # own signal rather than pretending the flat is on floor 0.
    df["floor_num"] = df["floor_num"].fillna(-1)
    df["total_floors"] = df["total_floors"].fillna(-1)
    df["floor_ratio"] = np.where(
        df["total_floors"] > 0,
        df["floor_num"] / df["total_floors"],
        -1.0,
    )

    # age_years intentionally not used as a feature (see NUMERIC_FEATURES note)

    # ---- Property type one-hot ------------------------------------------
    df["property_type_norm"] = (
        df["property_type"].fillna("unknown").astype(str).str.lower()
    )
    ptype_dummies = pd.get_dummies(df["property_type_norm"], prefix="ptype").reindex(
        columns=[f"ptype_{c}" for c in PROPERTY_TYPES], fill_value=0
    )

    # Coarse source bucket (drops the ':<group>' suffix on telegram)
    df["source_bucket"] = df["source"].astype(str).str.split(":").str[0]

    # Normalize furnishing values from scraper text -> fixed categories
    df["furnishing_norm"] = (
        df["furnishing"].fillna("unknown").astype(str).str.lower()
        .str.replace(r"[^a-z]+", "_", regex=True).str.strip("_")
    )
    # Map common variants
    _fmap = {
        "semi_furnished": "semi", "semi": "semi", "semifurnished": "semi",
        "fully_furnished": "full", "furnished": "full", "full": "full",
        "unfurnished": "unfurnished", "un_furnished": "unfurnished",
    }
    df["furnishing_norm"] = df["furnishing_norm"].map(_fmap).fillna("unknown")

    # One-hot encode. dummy_na=False. Reindex to fixed columns so future rows
    # produce the same schema even when a category is missing.
    furn_dummies = pd.get_dummies(
        df["furnishing_norm"], prefix="furn"
    ).reindex(columns=[f"furn_{c}" for c in FURNISHING_CATS], fill_value=0)

    src_dummies = pd.get_dummies(
        df["source_bucket"], prefix="src"
    ).reindex(columns=[f"src_{c}" for c in SOURCE_CATS], fill_value=0)

    X = pd.concat(
        [df[NUMERIC_FEATURES], furn_dummies, src_dummies, ptype_dummies],
        axis=1,
    )
    y = np.log(df["rent_monthly"].astype(float))
    feature_cols = list(X.columns)
    return X, y, feature_cols


# ---------------------------------------------------------------------------
# Train
# ---------------------------------------------------------------------------

def _make_model(seed: int, n_train: int = 1000) -> XGBRegressor:
    """Model params scale with training-set size. At ~350 rows we want heavy
    regularization to avoid memorizing tiny leaves; at 1000+ we can afford
    deeper trees and less shrinkage because there's more signal to fit."""
    if n_train < 500:
        # Small-data regime: tight regularization
        params = dict(n_estimators=200, max_depth=4, min_child_weight=5,
                      reg_alpha=0.5, reg_lambda=2.0)
    elif n_train < 2000:
        # Medium-data regime: more capacity
        params = dict(n_estimators=500, max_depth=6, min_child_weight=3,
                      reg_alpha=0.3, reg_lambda=1.5)
    else:
        # Large-data regime: near-defaults
        params = dict(n_estimators=800, max_depth=7, min_child_weight=2,
                      reg_alpha=0.2, reg_lambda=1.0)
    return XGBRegressor(
        # MAE loss — robust to outlier rents. Squared error chases extreme
        # values and inflates MAPE on heavy-tailed distributions; MAE just
        # gives up on outliers and fits the bulk of the distribution well.
        objective="reg:absoluteerror",
        learning_rate=0.05,
        subsample=0.85,
        colsample_bytree=0.85,
        random_state=seed,
        tree_method="hist",
        n_jobs=-1,
        **params,
    )


def _score(y_true_log: np.ndarray, y_pred_log: np.ndarray) -> dict[str, float]:
    y_pred = np.exp(y_pred_log)
    y_true = np.exp(y_true_log)
    return {
        "mae":  float(mean_absolute_error(y_true, y_pred)),
        "mape": float(mean_absolute_percentage_error(y_true, y_pred)),
        "r2":   float(r2_score(y_true, y_pred)),
    }


def train(model_version: str = "xgb-v1", seed: int = 42) -> None:
    df = load_frame()
    print(f"Loaded {len(df)} rows from listings JOIN localities.")

    X, y_log, feature_cols = build_features(df)
    print(f"Trainable after filters: {len(X)} rows, {len(feature_cols)} features.")

    # ---- 5-fold CV for an honest metric ---------------------------------
    kf = KFold(n_splits=5, shuffle=True, random_state=seed)
    fold_metrics: list[dict[str, float]] = []
    all_pred_log: list[float] = []
    all_true_log: list[float] = []
    X_arr = X.values
    y_arr = y_log.values

    for fold, (tr, te) in enumerate(kf.split(X_arr)):
        model = _make_model(seed + fold, n_train=len(tr))
        model.fit(X_arr[tr], y_arr[tr],
                  eval_set=[(X_arr[te], y_arr[te])], verbose=False)
        y_pred_log = model.predict(X_arr[te])
        m = _score(y_arr[te], y_pred_log)
        fold_metrics.append(m)
        all_pred_log.extend(y_pred_log.tolist())
        all_true_log.extend(y_arr[te].tolist())
        print(f"  fold {fold+1}: MAE ₹{m['mae']:,.0f}  MAPE {m['mape']*100:5.1f}%  R² {m['r2']:.3f}")

    cv = _score(np.array(all_true_log), np.array(all_pred_log))
    mae, mape, r2 = cv["mae"], cv["mape"], cv["r2"]
    mape_std = float(np.std([m["mape"] for m in fold_metrics]))

    print()
    print(f"5-fold CV metrics (n={len(all_true_log)}, out-of-fold aggregate):")
    print(f"  MAE:      ₹{mae:,.0f}")
    print(f"  MAPE:     {mape*100:.1f}%  (per-fold std {mape_std*100:.1f}%)")
    print(f"  R^2:      {r2:.3f}")

    # ---- Final model: refit on ALL data for downstream predict.py -------
    print()
    print("Refitting on all rows for the shipped model...")
    model = _make_model(seed, n_train=len(X_arr))
    model.fit(X_arr, y_arr, verbose=False)

    # Feature importance
    print()
    print("Top features by importance:")
    imp = sorted(zip(feature_cols, model.feature_importances_),
                 key=lambda kv: kv[1], reverse=True)
    for name, val in imp[:8]:
        print(f"  {name:<28} {val:.4f}")

    # Persist
    model_path    = MODELS_DIR / f"{model_version}.pkl"
    features_path = MODELS_DIR / f"{model_version}.features.json"

    with model_path.open("wb") as f:
        pickle.dump(model, f)
    features_path.write_text(json.dumps({
        "feature_cols":     feature_cols,
        "numeric_features": NUMERIC_FEATURES,
        "furnishing_cats":  FURNISHING_CATS,
        "source_cats":      SOURCE_CATS,
        "target":           "log_rent_monthly",
        "n_total":          int(len(X_arr)),
        "cv":               "5-fold KFold, shuffle=True",
        "metrics":          {
            "mae_inr":  float(mae),
            "mape":     float(mape),
            "mape_std": float(mape_std),
            "r2":       float(r2),
        },
    }, indent=2))

    print()
    print(f"Saved model to {model_path}")
    print(f"Saved features to {features_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-version", default="xgb-v1")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    train(model_version=args.model_version, seed=args.seed)


if __name__ == "__main__":
    main()
