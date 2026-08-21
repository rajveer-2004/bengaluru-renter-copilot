"""Re-parse raw_text for NoBroker listings and back-fill structured fields
the original scraper missed:

  - property_type    (apartment / independent_house / villa / pg / studio)
  - deposit          (INR)
  - floor_num / total_floors  (from "1/28" style tags)
  - age_years        (from "Age of Building" line, e.g. "5-10 Years" -> 7.5)
  - locality         (from card address text — corrects scraper mis-tagging
                     where NoBroker's proximity search returns cards outside
                     the queried locality's actual boundaries)

Idempotent — uses COALESCE so re-running only fills nulls, EXCEPT locality
which is always overwritten from address text (address is more trustworthy
than the search-query context we originally tagged with).

Why this matters: floor and property_type explain ~10-15% of within-locality
rent variance. The locality fix eliminates ghost "deals" from cards NoBroker
returned outside the queried area (e.g. a Jigani card tagged Koramangala
because that was the search center — model then predicts against Koramangala's
₹28/sqft prior instead of Jigani's ₹12/sqft and flags a fair-market rent as
a 57% deal).

Usage:
    python -m scripts.enrich_listings
    python -m scripts.enrich_listings --force   # overwrite existing values
"""
from __future__ import annotations

import argparse
import re
import sqlite3

from scrapers.db_utils import get_conn, normalize_locality
from scrapers.nobroker import LOCALITY_COORDS


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

# Property type from the FIRST line of the card (the title).
# Order matters: check more-specific patterns first.
_TYPE_HEADER = [
    (re.compile(r"\bindependent\s*house\b", re.I), "independent_house"),
    (re.compile(r"\bvilla\b", re.I),               "villa"),
    (re.compile(r"\bpg\b", re.I),                  "pg"),
    (re.compile(r"\bstudio\b", re.I),              "studio"),
    (re.compile(r"\b(apartment|flat)\b", re.I),    "apartment"),
    (re.compile(r"\bhouse\b", re.I),               "independent_house"),
]

# "<₹amount>\nDeposit" — deposit is on its own line, followed by the "Deposit" label.
_DEPOSIT_RE = re.compile(
    r"₹\s*([\d,]+(?:\.\d+)?)\s*\n\s*Deposit\b", re.I
)

# "<num>/<num>" — floor / total floors. NoBroker puts these on their own line
# between Furnishing and BHK. To avoid grabbing sqft ratios or other numerics,
# require the total to be a reasonable building height (1-60).
_FLOOR_RE = re.compile(r"(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})(?!\d)")

# "5-10 Years", "1-5 Years", "0-1 Years", "10+ Years", "5-10 Yrs"
_AGE_RE = re.compile(
    r"(\d+)\s*(?:-\s*(\d+))?\s*\+?\s*(?:years?|yrs?)\b(?!\s*old)?", re.I
)


# ---- Locality re-extraction from card address text -----------------------
#
# NoBroker cards look like:
#   "1 BHK Flat for Rent In HSR Layout, Bangalore\n..."
#   "Independent House, Jigani, Anekal Taluk, Near ICICI Bank\n..."
#
# The scraper originally stamped every card with the SEARCH QUERY's locality
# because NoBroker's proximity radius silently expands when there aren't
# enough matches, so a "Koramangala" search can return a Jigani card. We
# fix by scanning the card's own address text for a known Bengaluru locality.

# Common aliases → canonical LOCALITY_COORDS key. Extend as needed.
_LOCALITY_ALIASES = {
    "sarjapur road":     "Sarjapur",
    "hsr":               "HSR Layout",
    "btm":               "BTM Layout",
    "electronics city":  "Electronic City",
    "e-city":            "Electronic City",
    "kr pura":           "KR Puram",
    "krishnaraja puram": "KR Puram",
    "yeshwantpur":       "Yeshwanthpur",
    "rr nagar":          "Rajarajeshwari Nagar",
    "cv raman":          "CV Raman Nagar",
    "kalyannagar":       "Kalyan Nagar",
    "kammannahalli":     "Kammanahalli",
}

# Compile locality-detection regexes once. Prefer longer names first so
# "HSR Layout" matches before "HSR", "Sarjapur Road" before "Sarjapur".
_LOCALITY_NAMES = sorted(
    set(LOCALITY_COORDS.keys()) | set(k.title() for k in _LOCALITY_ALIASES),
    key=len, reverse=True,
)
_LOCALITY_RE = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in _LOCALITY_NAMES) + r")\b",
    re.IGNORECASE,
)


def parse_locality(text: str, current: str | None) -> tuple[str | None, bool]:
    """Extract the true locality from a card's address text.

    Returns (locality, changed).

    Strategy:
      1. Restrict search to the first 400 chars (title + address line).
      2. Find every known-locality mention in that window.
      3. If current tag appears in text → keep it (scraper was right).
      4. Else if any other known locality appears → use the first one
         (address usually leads: "House, Jigani, Anekal Taluk...").
      5. Else → return "outside_coverage" so the LEFT JOIN on localities
         table produces NULL priors → row won't get a prediction → won't
         appear as a deal. Better to lose the row than surface a ghost.
    """
    head = text[:400] if text else ""
    matches = list(_LOCALITY_RE.finditer(head))
    if not matches:
        return "outside_coverage", (current or "") != "outside_coverage"

    # Canonicalize each match through the alias map + LOCALITY_COORDS keys.
    canon_hits: list[str] = []
    for m in matches:
        raw = m.group(1).lower()
        canon = _LOCALITY_ALIASES.get(raw)
        if canon is None:
            # find case-preserving canonical name from LOCALITY_COORDS
            for k in LOCALITY_COORDS:
                if k.lower() == raw:
                    canon = k
                    break
        if canon:
            canon_hits.append(canon)

    if not canon_hits:
        return "outside_coverage", (current or "") != "outside_coverage"

    # If the current tag already matches something in the text, trust it.
    if current and any(c.lower() == current.lower() for c in canon_hits):
        return current, False

    # Otherwise take the first hit (address is usually near the top).
    new = canon_hits[0]
    return new, new != current


def parse_property_type(text: str) -> str | None:
    # Look at first 200 chars only — the title is at the top.
    head = text[:200]
    for pat, val in _TYPE_HEADER:
        if pat.search(head):
            return val
    return None


def parse_deposit(text: str) -> float | None:
    m = _DEPOSIT_RE.search(text)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    # Sanity: deposit between ₹1k and ₹50L. Excludes obvious mis-parses.
    if 1_000 <= v <= 5_000_000:
        return v
    return None


def parse_floor(text: str) -> tuple[int | None, int | None]:
    # Search within the "structured fields" region — usually after "sqft" and
    # before "BHK\nApartment Type". Fall back to whole-text if not found.
    m = _FLOOR_RE.search(text)
    if not m:
        return None, None
    a, b = int(m.group(1)), int(m.group(2))
    # 'floor / total_floors' — floor <= total, both plausible building heights
    if 0 <= a <= b <= 60:
        return a, b
    return None, None


def parse_age(text: str) -> float | None:
    m = _AGE_RE.search(text)
    if not m:
        return None
    lo = float(m.group(1))
    hi = float(m.group(2)) if m.group(2) else lo
    return (lo + hi) / 2 if 0 <= (lo + hi) / 2 <= 100 else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing values (default: only fill NULLs)")
    args = ap.parse_args()

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT l.listing_id, l.locality, rl.raw_text
            FROM listings l
            JOIN raw_listings rl ON rl.raw_id = l.latest_raw_id
            WHERE l.source = 'nobroker'
        """).fetchall()

        updates = []
        loc_updates = []
        n_prop, n_dep, n_flr, n_age = 0, 0, 0, 0
        n_loc_changed, n_outside = 0, 0
        for r in rows:
            text = r["raw_text"] or ""
            pt   = parse_property_type(text)
            dep  = parse_deposit(text)
            fnum, ftot = parse_floor(text)
            age  = parse_age(text)
            new_loc, changed = parse_locality(text, r["locality"])
            n_prop += pt is not None
            n_dep  += dep is not None
            n_flr  += fnum is not None
            n_age  += age is not None
            n_loc_changed += changed
            n_outside += new_loc == "outside_coverage"
            updates.append((pt, dep, fnum, ftot, age, r["listing_id"]))
            new_loc_norm = normalize_locality(new_loc) if new_loc else None
            loc_updates.append((new_loc, new_loc_norm, r["listing_id"]))

        if args.force:
            sql = """
                UPDATE listings SET
                    property_type = ?, deposit = ?,
                    floor_num = ?, total_floors = ?, age_years = ?
                WHERE listing_id = ?
            """
        else:
            sql = """
                UPDATE listings SET
                    property_type = COALESCE(property_type, ?),
                    deposit       = COALESCE(deposit, ?),
                    floor_num     = COALESCE(floor_num, ?),
                    total_floors  = COALESCE(total_floors, ?),
                    age_years     = COALESCE(age_years, ?)
                WHERE listing_id = ?
            """
        conn.executemany(sql, updates)

        # Locality is ALWAYS overwritten from address text (not COALESCE)
        # because the pre-existing value is scraper-tagged, not truth.
        conn.executemany(
            "UPDATE listings SET locality = ?, locality_norm = ? "
            "WHERE listing_id = ?",
            loc_updates,
        )

    print(f"Processed {len(rows)} NoBroker listings.")
    print(f"  property_type parsed:  {n_prop}  ({n_prop/len(rows)*100:.0f}%)")
    print(f"  deposit parsed:        {n_dep}  ({n_dep/len(rows)*100:.0f}%)")
    print(f"  floor parsed:          {n_flr}  ({n_flr/len(rows)*100:.0f}%)")
    print(f"  age parsed:            {n_age}  ({n_age/len(rows)*100:.0f}%)")
    print(f"  locality re-assigned:  {n_loc_changed}  "
          f"({n_loc_changed/len(rows)*100:.0f}%)")
    print(f"  outside coverage:      {n_outside}  "
          f"({n_outside/len(rows)*100:.0f}%)  "
          f"[will get no prediction → filtered from deals]")


if __name__ == "__main__":
    main()
