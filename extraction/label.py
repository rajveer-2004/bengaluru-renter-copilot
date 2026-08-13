"""Interactive labeling CLI for extraction ground truth.

Design:
- Pulls listings from the DB (skips ones already labeled in the JSONL file).
- For each listing: shows the raw_text and what BOTH extractors said, then
  asks you to confirm/correct each field. Enter = accept default.
- Writes to data/labeled_v1.jsonl, one JSON object per line, appended as
  you go so Ctrl+C at any point preserves your progress.

Fields labeled:
  tenant_pref (family / bachelors / bachelors_male / bachelors_female / any / none)
  veg_only   (1 / 0 / none)
  is_owner   (1 / 0 / none)
  negotiable (1 / 0 / none)
  lock_in_months  (integer or none)
  amenities  (comma-separated tags from the fixed set)

Usage:
    python -m extraction.label                      # label all unlabeled listings
    python -m extraction.label --limit 5            # first 5 unlabeled only
    python -m extraction.label --relabel            # re-label everything from scratch
    python -m extraction.label --out data/labels_v2.jsonl
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

from scrapers.db_utils import get_conn

DEFAULT_OUT = Path("data") / "labeled_v1.jsonl"

TENANT_OPTIONS = {"family", "bachelors", "bachelors_male", "bachelors_female", "any"}
AMENITY_OPTIONS = {"lift","parking","gym","pool","security","power_backup",
                   "water_supply","clubhouse","garden","kids_play","wifi","ac",
                   "washing_machine","fridge","geyser","cctv"}

# Sentinel for "user pressed just Enter, use default"
_DEFAULT = object()


def _prompt(label: str, default: Any, allowed: Optional[set[str]] = None,
            parse: str = "str") -> Any:
    """Prompt for one field. Returns parsed value, or None."""
    hint = f" [{default}]" if default is not None else " [none]"
    if allowed:
        opts = "/".join(sorted(allowed))
        prompt = f"  {label} ({opts}){hint}: "
    else:
        prompt = f"  {label}{hint}: "

    raw = input(prompt).strip()
    if raw == "":
        return default
    if raw.lower() in {"none", "null", "-"}:
        return None
    if parse == "bool":
        if raw in {"1", "y", "yes", "true", "t"}:
            return 1
        if raw in {"0", "n", "no", "false", "f"}:
            return 0
        print(f"    (didn't understand '{raw}', treating as none)", flush=True)
        return None
    if parse == "int":
        try:
            return int(raw)
        except ValueError:
            print(f"    (not an integer, treating as none)", flush=True)
            return None
    if allowed and raw not in allowed:
        print(f"    (not in allowed set, keeping default {default!r})", flush=True)
        return default
    return raw


def _parse_amenities(default_list: list[str]) -> list[str]:
    default_str = ", ".join(default_list) if default_list else ""
    hint = f" [{default_str}]" if default_str else " [empty]"
    raw = input(f"  amenities (comma-separated, allowed: {sorted(AMENITY_OPTIONS)}){hint}: ").strip()
    if raw == "":
        return default_list
    if raw.lower() in {"none", "null", "-", "empty"}:
        return []
    tags = [t.strip().lower() for t in raw.split(",") if t.strip()]
    unknown = [t for t in tags if t not in AMENITY_OPTIONS]
    if unknown:
        print(f"    (dropping unknown tags: {unknown})", flush=True)
    return sorted({t for t in tags if t in AMENITY_OPTIONS})


def _load_labeled_ids(out_path: Path) -> set[int]:
    if not out_path.exists():
        return set()
    seen = set()
    for line in out_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            seen.add(int(json.loads(line)["listing_id"]))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return seen


def _fetch_pending(conn: sqlite3.Connection, skip_ids: set[int],
                   limit: Optional[int]) -> list[sqlite3.Row]:
    sql = """
        SELECT l.listing_id, l.locality, l.bhk, l.rent_monthly, l.area_sqft,
               r.raw_text, r.source_url
        FROM listings l JOIN raw_listings r ON r.raw_id = l.latest_raw_id
        ORDER BY l.listing_id
    """
    rows = conn.execute(sql).fetchall()
    rows = [r for r in rows if r["listing_id"] not in skip_ids]
    if limit:
        rows = rows[: int(limit)]
    return rows


def _fetch_hints(conn: sqlite3.Connection, listing_id: int) -> dict[str, dict]:
    """Look up what our extractors said, for smart defaults."""
    hints: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT extractor, tenant_pref, veg_only, is_owner, negotiable, "
        "lock_in_months, amenities_json "
        "FROM extractions WHERE listing_id=?", (listing_id,)
    ):
        hints[row["extractor"]] = {
            "tenant_pref": row["tenant_pref"],
            "veg_only": row["veg_only"],
            "is_owner": row["is_owner"],
            "negotiable": row["negotiable"],
            "lock_in_months": row["lock_in_months"],
            "amenities": json.loads(row["amenities_json"] or "[]"),
        }
    return hints


def _default_from_hints(field: str, hints: dict[str, dict]) -> Any:
    """
    Prefer gemini-flash > regex-v1. But if only one has a non-null answer, use it.
    """
    gemini = hints.get("gemini-flash", {}).get(field)
    regex = hints.get("regex-v1", {}).get(field)
    if gemini is not None:
        return gemini
    return regex


def label_one(conn: sqlite3.Connection, row: sqlite3.Row) -> Optional[dict[str, Any]]:
    hints = _fetch_hints(conn, row["listing_id"])
    print("\n" + "=" * 70)
    header = f"listing_id={row['listing_id']}  "
    if row["locality"]:
        header += f"locality={row['locality']}  "
    if row["bhk"] is not None:
        header += f"bhk={row['bhk']}  "
    if row["rent_monthly"] is not None:
        header += f"rent={row['rent_monthly']:.0f}  "
    if row["area_sqft"] is not None:
        header += f"area={row['area_sqft']:.0f}sqft"
    print(header)
    if row["source_url"]:
        print(f"url: {row['source_url']}")
    print("-" * 70)
    print(row["raw_text"])
    print("-" * 70)

    if hints:
        print("hints from extractors:")
        for name, h in hints.items():
            compact = {k: v for k, v in h.items() if v not in (None, [], "")}
            print(f"  {name}: {compact}")
        print()

    print("Enter values (press Enter to accept default in brackets, "
          "'none' for null, 's' to skip listing, 'q' to quit):")

    # Field 1: tenant_pref
    val = _prompt("tenant_pref", _default_from_hints("tenant_pref", hints),
                  allowed=TENANT_OPTIONS)
    if val == "s":
        return None
    if val == "q":
        sys.exit(0)
    tenant_pref = val

    veg_only = _prompt("veg_only", _default_from_hints("veg_only", hints), parse="bool")
    is_owner = _prompt("is_owner", _default_from_hints("is_owner", hints), parse="bool")
    negotiable = _prompt("negotiable", _default_from_hints("negotiable", hints), parse="bool")
    lock_in = _prompt("lock_in_months", _default_from_hints("lock_in_months", hints), parse="int")

    default_amens: list[str] = []
    if "gemini-flash" in hints:
        default_amens = hints["gemini-flash"]["amenities"] or []
    elif "regex-v1" in hints:
        default_amens = hints["regex-v1"]["amenities"] or []
    amenities = _parse_amenities(default_amens)

    return {
        "listing_id": row["listing_id"],
        "raw_text": row["raw_text"],
        "tenant_pref": tenant_pref,
        "veg_only": veg_only,
        "is_owner": is_owner,
        "negotiable": negotiable,
        "lock_in_months": lock_in,
        "amenities": amenities,
    }


def run(out_path: Path, limit: Optional[int], relabel: bool) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if relabel and out_path.exists():
        backup = out_path.with_suffix(out_path.suffix + ".bak")
        out_path.rename(backup)
        print(f"Existing labels moved to {backup}", flush=True)

    already = set() if relabel else _load_labeled_ids(out_path)
    if already:
        print(f"Already labeled: {len(already)} listings — skipping.", flush=True)

    with get_conn() as conn:
        pending = _fetch_pending(conn, already, limit)
        print(f"{len(pending)} listing(s) to label.\n", flush=True)
        if not pending:
            return

        n_labeled = 0
        with out_path.open("a", encoding="utf-8") as f:
            for row in pending:
                try:
                    result = label_one(conn, row)
                except KeyboardInterrupt:
                    print(f"\nInterrupted. {n_labeled} labeled so far -> {out_path}", flush=True)
                    return
                if result is None:
                    continue
                f.write(json.dumps(result, ensure_ascii=False) + "\n")
                f.flush()
                n_labeled += 1

        print(f"\nWrote {n_labeled} new label(s) to {out_path}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--relabel", action="store_true",
                    help="Ignore existing labels; move them to a .bak file")
    args = ap.parse_args()
    run(Path(args.out), args.limit, args.relabel)


if __name__ == "__main__":
    main()
