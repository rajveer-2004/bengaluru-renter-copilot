"""Apply targeted human corrections to labeled_v1.jsonl.

Reviewed all 36 Telegram listings in labeled_v1.jsonl. Gemini's systematic
weakness on this dataset: it doesn't tag is_owner=1 when a post says "NO
BROKERAGE!!" unless the word "OWNER" is also present. This script applies
the missing corrections and upgrades those rows' label_source to 'human'.

Also drops listing_id=18 (Filter false-positive: a SALE post that slipped
past the sale-exclusion regex because the sale price was per-sqft; it's not
a rental listing and shouldn't influence the extraction benchmark).

Usage:
    python -m scripts.apply_label_corrections
"""
from __future__ import annotations

import json
from pathlib import Path

LABELS = Path("data") / "labeled_v1.jsonl"

# {listing_id: {field: value, ...}} — fields present here get set on the row
# AND the row's label_source is upgraded to 'human'. Justification for each
# below. Amenities in additions are added on top of Gemini's existing set.
CORRECTIONS: dict[int, dict] = {
    20: {"is_owner": 1},   # "NO BROKERAGE!! Ms Chethana" — direct contact
    24: {"is_owner": 1},   # "NO BROKERAGE!!" flatmate post
    25: {"is_owner": 1},   # "NO BROKERAGE!!"
    29: {"is_owner": 1},   # "NO BROKERAGE!!"
    32: {"is_owner": 1},   # "NO BROKERAGE!! Mr. Ritik" — contact given
    37: {"is_owner": 1},   # "NO BROKERAGE!!"
    39: {"is_owner": 1},   # "NO BROKERAGE!! Mr. Harry" — contact given
    45: {"is_owner": 1},   # "NO BROKERAGE!!"
    47: {"is_owner": 1},   # "NO BROKERAGE!! Mr. Shashank" — contact given
    48: {"is_owner": 1},   # "NO BROKERAGE!! CONTACT WHATSAPP 7032485443"
    51: {"is_owner": 1},   # "NO BROKERAGE!!"
    52: {"veg_only": 0},   # "No restrictions" — explicitly no veg restriction
}

# listing_ids to DROP entirely from the label set. Reason: not a rental.
DROP_IDS = {
    18,  # SALE, not rent: "Expected price 10800/- per sqft ... 2BHK available for sale"
}


def main() -> None:
    rows = [json.loads(l) for l in LABELS.read_text(encoding="utf-8").splitlines() if l.strip()]
    changed = 0
    dropped = 0
    kept = []

    for r in rows:
        lid = int(r["listing_id"])

        if lid in DROP_IDS:
            dropped += 1
            print(f"  DROP listing_id={lid} (not a rental)")
            continue

        if lid in CORRECTIONS:
            fixes = CORRECTIONS[lid]
            before = {k: r.get(k) for k in fixes}
            r.update(fixes)
            r["label_source"] = "human"
            changed += 1
            print(f"  FIX  listing_id={lid}: {before} -> {fixes}")

        kept.append(r)

    LABELS.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in kept) + "\n",
        encoding="utf-8",
    )

    from collections import Counter
    c = Counter(r.get("label_source") for r in kept)
    print()
    print(f"Total rows: {len(kept)} (was {len(rows)}, dropped {dropped})")
    print(f"By label_source: {dict(c)}")
    print(f"Corrections applied: {changed}")


if __name__ == "__main__":
    main()
