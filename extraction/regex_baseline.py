"""Regex baseline extractor for listing free-text.

Purpose: cheapest possible extractor. Sets the floor accuracy any smarter
model (Gemini Flash, fine-tuned DistilBERT) must beat. Writes one row per
listing to `extractions` with extractor='regex-v1', extractor_version='1.0'.

Fields extracted:
  - tenant_pref: family | bachelors | bachelors_male | bachelors_female | any
  - veg_only:   1/0/None (True if listing requires vegetarian tenants)
  - is_owner:   1 = owner, 0 = broker, None unknown
  - negotiable: 1 if rent is negotiable
  - lock_in_months: integer if mentioned
  - amenities_json: JSON array of normalized amenity tags found in text

Usage:
    python -m extraction.regex_baseline                  # extract for all listings missing regex-v1
    python -m extraction.regex_baseline --force          # re-extract even if already present
    python -m extraction.regex_baseline --limit 5 --show # dry preview
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from scrapers.db_utils import get_conn, utcnow_iso

EXTRACTOR = "regex-v1"
EXTRACTOR_VERSION = "1.0"


# --- Field extractors ------------------------------------------------------

TENANT_PATTERNS = [
    # Order matters: more specific first
    (re.compile(r"\b(bachelor(?:s)?\s*(?:only)?\s*[-:\s]*male|male\s*bachelor(?:s)?)\b", re.I), "bachelors_male"),
    (re.compile(r"\b(bachelor(?:s)?\s*(?:only)?\s*[-:\s]*female|female\s*bachelor(?:s)?|working\s+women)\b", re.I), "bachelors_female"),
    (re.compile(r"\b(family|families)\s*(?:only|preferred)?\b", re.I), "family"),
    (re.compile(r"\bbachelor(?:s)?\b", re.I), "bachelors"),
    (re.compile(r"\b(any(?:one)?|all\s+tenants|no\s+restriction)\b", re.I), "any"),
    # "Preferred Tenants: All" or "Preferred Tenants All"
    (re.compile(r"preferred\s+tenants?[:\s]*all\b", re.I), "any"),
]


def extract_tenant_pref(text: str) -> Optional[str]:
    for pat, label in TENANT_PATTERNS:
        if pat.search(text):
            return label
    return None


VEG_PATTERNS = [
    re.compile(r"\b(veg(?:etarian)?\s*only|only\s*veg(?:etarian)?|pure\s*veg)\b", re.I),
    re.compile(r"\bno\s+non[-\s]?veg\b", re.I),
    re.compile(r"\bveg\s+tenants?\s+only\b", re.I),
]

NONVEG_OK_PATTERNS = [
    re.compile(r"\bnon[-\s]?veg(?:etarian)?\s*(?:allowed|ok|okay|accepted)\b", re.I),
    re.compile(r"\bany\s+food\b", re.I),
]


def extract_veg_only(text: str) -> Optional[int]:
    for p in VEG_PATTERNS:
        if p.search(text):
            return 1
    for p in NONVEG_OK_PATTERNS:
        if p.search(text):
            return 0
    return None


OWNER_PATTERNS = [
    re.compile(r"\b(owner\s+direct|direct\s+owner|no\s+broker|by\s+owner)\b", re.I),
    re.compile(r"\bposted\s+by\s+owner\b", re.I),
    re.compile(r"\bget\s+owner\s+details\b", re.I),  # NoBroker card label
]

BROKER_PATTERNS = [
    re.compile(r"\b(broker(?:age)?|agent\s+fee|agent\s+details)\b", re.I),
    re.compile(r"\bposted\s+by\s+(?:broker|agent)\b", re.I),
]


def extract_is_owner(text: str) -> Optional[int]:
    is_owner = any(p.search(text) for p in OWNER_PATTERNS)
    is_broker = any(p.search(text) for p in BROKER_PATTERNS)
    if is_owner and not is_broker:
        return 1
    if is_broker and not is_owner:
        return 0
    if is_owner and is_broker:
        return None  # ambiguous
    return None


NEGOTIABLE_PATTERNS = [
    re.compile(r"\bnegotiable\b", re.I),
    re.compile(r"\bprice\s+can\s+be\s+discussed\b", re.I),
    re.compile(r"\brent\s+is\s+negotiable\b", re.I),
]


def extract_negotiable(text: str) -> Optional[int]:
    for p in NEGOTIABLE_PATTERNS:
        if p.search(text):
            return 1
    return None  # absence != non-negotiable


LOCK_IN_PATTERN = re.compile(
    r"\b(?:lock[-\s]?in|minimum\s+stay)[:\s]*(\d+)\s*(month|year|yr|mo)s?\b", re.I
)


def extract_lock_in_months(text: str) -> Optional[int]:
    m = LOCK_IN_PATTERN.search(text)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    return n * 12 if unit.startswith("y") else n


AMENITY_KEYWORDS = {
    "lift":        re.compile(r"\blift|elevator\b", re.I),
    "parking":     re.compile(r"\bparking|car\s*park\b", re.I),
    "gym":         re.compile(r"\bgym|fitness\s+center\b", re.I),
    "pool":        re.compile(r"\bswimming\s*pool|pool\b", re.I),
    "security":    re.compile(r"\b24[/\s]?7\s*security|security\b", re.I),
    "power_backup":re.compile(r"\bpower\s*backup|generator|dg\s*backup\b", re.I),
    "water_supply":re.compile(r"\b(24[/\s]?7\s+water|water\s+plant|borewell|corp(?:oration)?\s+water)\b", re.I),
    "clubhouse":   re.compile(r"\bclub[- ]?house\b", re.I),
    "garden":      re.compile(r"\bgarden|lawn\b", re.I),
    "kids_play":   re.compile(r"\bkids?\s+play|children['\s]s\s+play|play\s+area\b", re.I),
    "wifi":        re.compile(r"\bwi[-\s]?fi\b", re.I),
    "ac":          re.compile(r"\b(?:air[-\s]?condition(?:ing)?|\bac\s+included|split\s+ac)\b", re.I),
    "washing_machine": re.compile(r"\bwashing\s+machine\b", re.I),
    "fridge":      re.compile(r"\bfridge|refrigerator\b", re.I),
    "geyser":      re.compile(r"\bgeyser|water\s+heater\b", re.I),
    "cctv":        re.compile(r"\bcctv|surveillance\b", re.I),
}


def extract_amenities(text: str) -> list[str]:
    found = []
    for tag, pat in AMENITY_KEYWORDS.items():
        if pat.search(text):
            found.append(tag)
    return sorted(found)


# --- Main pipeline ---------------------------------------------------------


def extract_one(text: str) -> dict[str, Any]:
    """Run all extractors on one text. Returns dict shaped for `extractions`."""
    return {
        "tenant_pref": extract_tenant_pref(text),
        "veg_only": extract_veg_only(text),
        "is_owner": extract_is_owner(text),
        "negotiable": extract_negotiable(text),
        "lock_in_months": extract_lock_in_months(text),
        "amenities_json": json.dumps(extract_amenities(text)),
    }


def find_pending_listings(conn: sqlite3.Connection, force: bool,
                          limit: Optional[int]) -> list[sqlite3.Row]:
    """Listings that need extraction. Join to their latest raw_text."""
    base = """
        SELECT l.listing_id, l.latest_raw_id, r.raw_text
        FROM listings l
        JOIN raw_listings r ON r.raw_id = l.latest_raw_id
    """
    if not force:
        base += """
        WHERE NOT EXISTS (
            SELECT 1 FROM extractions e
            WHERE e.listing_id = l.listing_id
              AND e.extractor = ? AND e.extractor_version = ?
        )
        """
        params: tuple = (EXTRACTOR, EXTRACTOR_VERSION)
    else:
        params = ()
    if limit:
        base += f" LIMIT {int(limit)}"
    return conn.execute(base, params).fetchall()


def run(force: bool, limit: Optional[int], show: bool) -> None:
    with get_conn() as conn:
        rows = find_pending_listings(conn, force=force, limit=limit)
        print(f"{len(rows)} listing(s) to extract", flush=True)
        if not rows:
            return

        n_written = 0
        for row in rows:
            t0 = time.perf_counter()
            fields = extract_one(row["raw_text"])
            latency_ms = (time.perf_counter() - t0) * 1000.0

            if show:
                print("---")
                print(f"listing_id={row['listing_id']}  ({latency_ms:.2f} ms)")
                for k, v in fields.items():
                    print(f"  {k}: {v}")
                continue

            if force:
                conn.execute(
                    "DELETE FROM extractions WHERE listing_id=? AND extractor=? AND extractor_version=?",
                    (row["listing_id"], EXTRACTOR, EXTRACTOR_VERSION),
                )

            conn.execute(
                "INSERT INTO extractions "
                "(listing_id, raw_id, extractor, extractor_version, extracted_at, "
                " tenant_pref, veg_only, is_owner, negotiable, lock_in_months, "
                " amenities_json, latency_ms, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["listing_id"], row["latest_raw_id"], EXTRACTOR, EXTRACTOR_VERSION,
                 utcnow_iso(), fields["tenant_pref"], fields["veg_only"],
                 fields["is_owner"], fields["negotiable"], fields["lock_in_months"],
                 fields["amenities_json"], latency_ms, 0.0),
            )
            n_written += 1

        if not show:
            print(f"Wrote {n_written} rows to extractions (extractor={EXTRACTOR})", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="Re-extract even for listings already covered by regex-v1")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--show", action="store_true",
                    help="Print results, don't write to DB")
    args = ap.parse_args()
    run(force=args.force, limit=args.limit, show=args.show)


if __name__ == "__main__":
    main()
