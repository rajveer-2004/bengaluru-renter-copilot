"""Bengaluru Renter's Copilot — Streamlit dashboard.

Loads listings + predictions from db/copilot.db and surfaces underpriced
rentals as ranked "deals". Interactive filters for locality / BHK / rent /
area. Transparent about model MAPE and known limitations.

Run locally:
    streamlit run dashboards/streamlit/app.py

Deploy to HF Spaces:
    - Point the space at this repo
    - Include db/copilot.db in the deploy artifact (via .streamlit/deploy)
    - App config: python 3.10, streamlit >= 1.35
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

REPO_ROOT   = Path(__file__).resolve().parents[2]
DB_PATH     = REPO_ROOT / "db" / "copilot.db"
MODEL_META  = REPO_ROOT / "pricing" / "xgb-v1.features.json"

st.set_page_config(
    page_title="Bengaluru Renter's Copilot",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Data loading (cached)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def load_deals(db_path: str) -> pd.DataFrame:
    with sqlite3.connect(db_path) as conn:
        return pd.read_sql_query(
            """
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
                l.floor_num,
                l.total_floors,
                l.furnishing,
                l.first_seen_at,
                rl.source_url,
                p.predicted_rent,
                p.value_score,
                p.is_deal,
                loc.dist_nearest_metro_km,
                loc.nearest_metro_station
            FROM listings l
            LEFT JOIN raw_listings rl ON rl.raw_id = l.latest_raw_id
            LEFT JOIN predictions  p  ON p.listing_id = l.listing_id
                                     AND p.model_version = 'xgb-v1'
            LEFT JOIN localities   loc ON loc.locality_norm = l.locality_norm
            WHERE l.rent_monthly IS NOT NULL
              AND l.area_sqft   IS NOT NULL
              AND l.bhk         IS NOT NULL
              AND p.predicted_rent IS NOT NULL
            """,
            conn,
        )


@st.cache_data(ttl=3600)
def load_model_meta(meta_path: str) -> dict:
    try:
        return json.loads(Path(meta_path).read_text())
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("🏠 Bengaluru Renter's Copilot")
st.caption(
    "Automated rental-market intelligence: scrape → extract → price → "
    "flag deals. Weekly cron pipeline. "
    "[Repo](https://github.com/rajveer-2004/bengaluru-renter-copilot)"
)

if not DB_PATH.exists():
    st.error(f"Database not found at {DB_PATH}. Run scrape/train first.")
    st.stop()

df   = load_deals(str(DB_PATH))
meta = load_model_meta(str(MODEL_META))

if df.empty:
    st.warning("No scored listings yet. Run `python -m pricing.predict`.")
    st.stop()

# ---- Sidebar filters ------------------------------------------------------

st.sidebar.header("Filters")

# Locality
localities = sorted(df["locality"].dropna().unique().tolist())
default_locs = st.sidebar.multiselect(
    "Locality (blank = all)", localities, default=[]
)
if default_locs:
    df = df[df["locality"].isin(default_locs)]

# BHK
bhk_values = sorted(df["bhk"].dropna().unique().tolist())
bhk_sel = st.sidebar.multiselect(
    "BHK (blank = all)", bhk_values, default=[]
)
if bhk_sel:
    df = df[df["bhk"].isin(bhk_sel)]

# Rent
if len(df):
    rent_min = int(df["rent_monthly"].min())
    rent_max = int(df["rent_monthly"].max())
else:
    rent_min, rent_max = 5_000, 100_000
rent_range = st.sidebar.slider(
    "Rent (₹/month)",
    min_value=max(3_000, rent_min - 1_000),
    max_value=min(500_000, rent_max + 1_000),
    value=(rent_min, min(rent_max, 60_000)),
    step=1_000,
)
df = df[df["rent_monthly"].between(*rent_range)]

# Min area
area_min = st.sidebar.slider("Min area (sqft)", 100, 5_000, 400, step=50)
df = df[df["area_sqft"] >= area_min]

# Deal-only toggle
deals_only = st.sidebar.checkbox("Show deals only (≥ 15% below predicted)",
                                 value=True)
if deals_only:
    df = df[df["is_deal"] == 1]

# ---- KPI row --------------------------------------------------------------

k1, k2, k3, k4 = st.columns(4)
k1.metric("Listings", f"{len(df):,}")
k2.metric(
    "Median saving",
    f"{(df['value_score'].median() * 100 if len(df) else 0):.1f}%" if deals_only else "—",
)
k3.metric(
    "Median rent",
    f"₹{int(df['rent_monthly'].median()):,}" if len(df) else "—",
)
if meta.get("metrics"):
    k4.metric("Model MAPE", f"{meta['metrics']['mape']*100:.1f}%")
else:
    k4.metric("Model MAPE", "—")

st.divider()

# ---- Deals table ----------------------------------------------------------

st.subheader("Ranked deals" if deals_only else "All listings")

if not len(df):
    st.info("No listings match these filters.")
    st.stop()

display = (
    df.sort_values("value_score", ascending=False)
      .assign(
          save_pct=lambda d: (d["value_score"] * 100).round(1),
          rent_str=lambda d: d["rent_monthly"].map(lambda v: f"₹{int(v):,}"),
          pred_str=lambda d: d["predicted_rent"].map(lambda v: f"₹{int(v):,}"),
      )
      .loc[:, [
          "locality", "bhk", "area_sqft",
          "rent_str", "pred_str", "save_pct",
          "property_type", "furnishing", "source_url",
      ]]
      .rename(columns={
          "locality": "Locality",
          "bhk":       "BHK",
          "area_sqft": "Area (sqft)",
          "rent_str":  "Actual rent",
          "pred_str":  "Predicted rent",
          "save_pct":  "Save %",
          "property_type": "Type",
          "furnishing": "Furnishing",
          "source_url": "NoBroker",
      })
)

st.dataframe(
    display,
    use_container_width=True,
    hide_index=True,
    column_config={
        "NoBroker": st.column_config.LinkColumn(
            "NoBroker", display_text="Open ↗"
        ),
        "Area (sqft)": st.column_config.NumberColumn(format="%.0f"),
        "BHK": st.column_config.NumberColumn(format="%.1f"),
        "Save %": st.column_config.NumberColumn(format="%.1f%%"),
    },
    height=560,
)

# ---- Model transparency card ----------------------------------------------

with st.expander("📐 Model transparency & known limitations"):
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Model**")
        m = meta.get("metrics", {})
        st.markdown(
            f"- XGBoost regressor, MAE loss (`reg:absoluteerror`)\n"
            f"- 5-fold CV: MAPE **{m.get('mape', 0)*100:.1f}%**, "
            f"MAE **₹{m.get('mae_inr', 0):,.0f}**, R² **{m.get('r2', 0):.2f}**\n"
            f"- Trained on **{meta.get('n_total', 0)}** listings, "
            f"**{len(meta.get('feature_cols', []))}** features\n"
            f"- Target: `log(rent_monthly)`, transformed back to ₹ at scoring time"
        )
    with c2:
        st.markdown("**Honest limitations**")
        st.markdown(
            "- **Locality-level geography only** — no building name / exact address\n"
            "- **Card-level scraping** — no floor number on 38% of listings, no age data\n"
            "- **Rank-order deal detection** — a 20% MAPE means some noise "
            "in raw predictions; we compensate by comparing every listing to "
            "peers with similar features\n"
            "- **NoBroker card errors pass through** — verified top-1 deal "
            "(#1102 Domlur ₹15k 2BHK 1200sqft) matches NoBroker's own listing exactly"
        )
    st.markdown("**Feature importance (top 8)**")
    fi = meta.get("feature_cols", [])
    if fi:
        st.caption("Feature importance is stored in the model pickle; "
                   "view via `python -m pricing.train_xgb` output.")

st.caption(
    f"Data: {len(df):,} filtered listings · "
    f"Source: NoBroker + Telegram · "
    f"Model: xgb-v1 (MAPE {meta.get('metrics', {}).get('mape', 0)*100:.1f}%)"
)
