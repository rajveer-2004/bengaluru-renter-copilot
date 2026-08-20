"""Seed the `localities` table with lat/lon + distances to Bengaluru anchors.

For each Bengaluru locality we scrape, we precompute:
  - lat/lon (from a curated dict)
  - dist_nearest_metro_km + nearest_metro_station (from a metro station list)
  - dist_orr_km            (nearest ORR access point)
  - dist_manyata_km        (Manyata Tech Park, north)
  - dist_ecity_km          (Electronic City, south)
  - dist_whitefield_km     (ITPL Whitefield, east)
  - dist_orr_bellandur_km  (ORR-Bellandur tech corridor)

These become features for the XGBoost pricing model. Idempotent: safe to
re-run — uses INSERT OR REPLACE.

Usage:
    python -m scripts.seed_localities
"""
from __future__ import annotations

import math
from pathlib import Path

from scrapers.db_utils import get_conn, normalize_locality


# ---------------------------------------------------------------------------
# Coordinates
# ---------------------------------------------------------------------------

# Localities we scrape (keep in sync with scrapers/nobroker.py::LOCALITY_COORDS).
LOCALITY_COORDS: dict[str, tuple[float, float]] = {
    "HSR Layout":      (12.9081, 77.6476),
    "Koramangala":     (12.9352, 77.6245),
    "Indiranagar":     (12.9784, 77.6408),
    "Whitefield":      (12.9698, 77.7500),
    "Bellandur":       (12.9250, 77.6820),
    "Marathahalli":    (12.9591, 77.6974),
    "Electronic City": (12.8452, 77.6602),
    "E-City":          (12.8452, 77.6602),
    "Jayanagar":       (12.9250, 77.5938),
    "BTM Layout":      (12.9166, 77.6101),
    "BTM":             (12.9166, 77.6101),
    "Sarjapur":        (12.9010, 77.6874),
    "Sarjapur Road":   (12.9010, 77.6874),
    "JP Nagar":        (12.9081, 77.5831),
    "Bommanahalli":    (12.8973, 77.6212),
    "Hebbal":          (13.0358, 77.5970),
    "Yelahanka":       (13.1006, 77.5963),
    "Rajajinagar":     (12.9915, 77.5522),
    "Malleshwaram":    (13.0035, 77.5647),
    "Basavanagudi":    (12.9422, 77.5738),
    "Banashankari":    (12.9256, 77.5468),
    "Kalyan Nagar":    (13.0219, 77.6437),
    "Frazer Town":     (12.9987, 77.6118),
    "Cooke Town":      (13.0068, 77.6209),
    "Ulsoor":          (12.9799, 77.6248),
    "Domlur":          (12.9611, 77.6386),
    "CV Raman Nagar":  (13.0117, 77.6600),
    "Silk Board":      (12.9179, 77.6224),
    "Yeshwanthpur":    (13.0290, 77.5401),
    "Kengeri":         (12.9081, 77.4820),
    "HSR":             (12.9081, 77.6476),
    "Manyata":         (13.0447, 77.6207),
}

# Metro stations (Purple + Green lines currently operational, plus known Yellow
# line stations under construction).
METRO_STATIONS: dict[str, tuple[float, float]] = {
    "MG Road":            (12.9758, 77.6041),
    "Trinity":            (12.9723, 77.6152),
    "Halasuru":           (12.9776, 77.6222),
    "Indiranagar":        (12.9784, 77.6408),
    "Byappanahalli":      (12.9917, 77.6470),
    "Cubbon Park":        (12.9750, 77.5952),
    "Vidhana Soudha":     (12.9793, 77.5906),
    "Majestic":           (12.9758, 77.5729),
    "Rajajinagar":        (12.9915, 77.5522),
    "Yeshwanthpur":       (13.0290, 77.5401),
    "Nagasandra":         (13.0463, 77.5062),
    "Yelachenahalli":     (12.8829, 77.5771),
    "Konanakunte Cross":  (12.8697, 77.5581),
    "Silk Institute":     (12.8452, 77.5892),
    "Jayanagar":          (12.9250, 77.5938),
    "Jayadeva":           (12.9166, 77.5960),
    "Banashankari":       (12.9256, 77.5468),
    "JP Nagar":           (12.9081, 77.5831),
    "KR Puram":           (13.0064, 77.6957),
    "Whitefield":         (12.9855, 77.7368),
    "Hoodi":              (12.9910, 77.7134),
    "Kadugodi":           (12.9878, 77.7566),
}

# Named anchors for the pricing model
MANYATA         = (13.0447, 77.6207)
ECITY           = (12.8452, 77.6602)
WHITEFIELD_ITPL = (12.9855, 77.7368)
ORR_BELLANDUR   = (12.9250, 77.6820)

# ORR access points — used to compute a "distance to any ORR" feature by
# taking the min across these anchors on the ring road.
ORR_ANCHORS: list[tuple[float, float]] = [
    (12.9179, 77.6224),  # Silk Board (south)
    (12.9591, 77.6974),  # Marathahalli (east)
    (13.0064, 77.6957),  # KR Puram (northeast)
    (13.0358, 77.5970),  # Hebbal (north)
    (13.0290, 77.5401),  # Yeshwanthpur (northwest)
    (12.9081, 77.5831),  # JP Nagar (south-central proxy)
]


# ---------------------------------------------------------------------------
# Distance helpers
# ---------------------------------------------------------------------------

def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km between two (lat, lon) pairs."""
    R = 6371.0
    lat1, lon1 = a
    lat2, lon2 = b
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def nearest_metro(coord: tuple[float, float]) -> tuple[str, float]:
    best_name, best_km = "", float("inf")
    for name, station_coord in METRO_STATIONS.items():
        d = haversine_km(coord, station_coord)
        if d < best_km:
            best_name, best_km = name, d
    return best_name, best_km


def nearest_orr_km(coord: tuple[float, float]) -> float:
    return min(haversine_km(coord, a) for a in ORR_ANCHORS)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    rows_to_write = []
    for display_name, coord in LOCALITY_COORDS.items():
        norm = normalize_locality(display_name)
        metro_name, metro_km = nearest_metro(coord)
        rows_to_write.append({
            "locality_norm": norm,
            "display_name":  display_name,
            "lat":           coord[0],
            "lon":           coord[1],
            "dist_nearest_metro_km": round(metro_km, 3),
            "nearest_metro_station": metro_name,
            "dist_orr_km":           round(nearest_orr_km(coord), 3),
            "dist_manyata_km":       round(haversine_km(coord, MANYATA), 3),
            "dist_ecity_km":         round(haversine_km(coord, ECITY), 3),
            "dist_whitefield_km":    round(haversine_km(coord, WHITEFIELD_ITPL), 3),
            "dist_orr_bellandur_km": round(haversine_km(coord, ORR_BELLANDUR), 3),
        })

    with get_conn() as conn:
        # De-duplicate on locality_norm — some display names map to the same
        # normalized key (e.g. "HSR Layout" and "HSR" both -> "hsr_layout"
        # if the norm collapses; here they differ so both survive).
        seen = set()
        deduped = []
        for r in rows_to_write:
            if r["locality_norm"] in seen:
                continue
            seen.add(r["locality_norm"])
            deduped.append(r)

        cols = list(deduped[0].keys())
        placeholders = ", ".join(["?"] * len(cols))
        sql = (
            f"INSERT OR REPLACE INTO localities ({', '.join(cols)}) "
            f"VALUES ({placeholders})"
        )
        conn.executemany(sql, [tuple(r[c] for c in cols) for r in deduped])

        # Print a quick preview
        print(f"Seeded {len(deduped)} localities.\n")
        print(f"{'locality':<22} {'metro':<22} {'m_km':>6} {'orr':>6} "
              f"{'many':>6} {'ecity':>6} {'wfld':>6}")
        print("-" * 82)
        for r in conn.execute(
            "SELECT display_name, nearest_metro_station, dist_nearest_metro_km, "
            "dist_orr_km, dist_manyata_km, dist_ecity_km, dist_whitefield_km "
            "FROM localities ORDER BY display_name"
        ):
            print(f"{r['display_name']:<22} {r['nearest_metro_station']:<22} "
                  f"{r['dist_nearest_metro_km']:>6.2f} {r['dist_orr_km']:>6.2f} "
                  f"{r['dist_manyata_km']:>6.2f} {r['dist_ecity_km']:>6.2f} "
                  f"{r['dist_whitefield_km']:>6.2f}")


if __name__ == "__main__":
    main()
