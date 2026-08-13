"""Gemini Flash extractor — the "ceiling" of the extraction benchmark.

Sends each listing's raw_text to Gemini 1.5 Flash with a structured-output
prompt, parses the JSON response, and writes to `extractions` with
extractor='gemini-flash'. Tracks per-call latency AND cost so the benchmark
can compare $/1000 later.

Free tier: 15 requests/minute, 1M tokens/day. For 17 listings that's trivial.
For 1000 listings/week you'd still be well under free-tier limits.

Pricing (as of Aug 2026): $0.075/1M input tokens, $0.30/1M output tokens.

Usage:
    python -m extraction.gemini_flash
    python -m extraction.gemini_flash --limit 3 --show
    python -m extraction.gemini_flash --force        # re-run even if already extracted
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from scrapers.db_utils import get_conn, utcnow_iso

EXTRACTOR = "gemini-flash"
EXTRACTOR_VERSION = "3.5-flash"
MODEL_NAME = "gemini-3.5-flash"

# Pricing per 1M tokens (USD). Update if Google changes rates.
INPUT_COST_PER_1M = 0.30
OUTPUT_COST_PER_1M = 2.50

# Free-tier RPM = 15. Sleep 4.5s between calls to stay comfortably under.
MIN_INTERVAL_S = 4.5

PROMPT_TEMPLATE = """You are extracting structured information from a Bengaluru rental listing.

Return a single JSON object with EXACTLY these keys. Use null when the listing does not clearly state a value — do NOT guess.

{{
  "tenant_pref":   one of ["family", "bachelors", "bachelors_male", "bachelors_female", "any"] or null,
  "veg_only":      true if the listing says vegetarian tenants only, false if it explicitly allows non-veg, null if unclear,
  "is_owner":      true if posted directly by the owner, false if posted by a broker/agent, null if unclear,
  "negotiable":    true if the rent is stated to be negotiable, null otherwise,
  "lock_in_months": integer number of months if a lock-in period is mentioned, null otherwise,
  "amenities":     array of short lowercase tags from this fixed set only:
                   ["lift","parking","gym","pool","security","power_backup","water_supply","clubhouse","garden","kids_play","wifi","ac","washing_machine","fridge","geyser","cctv"]
                   (empty array if none mentioned)
}}

Rules:
- Output ONLY the JSON object, no prose, no markdown fences.
- Do not invent facts. Prefer null over guessing.
- amenities MUST be a subset of the fixed tag set above.

LISTING TEXT:
```
{text}
```
"""


def load_key() -> str:
    load_dotenv()
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        sys.exit("ERROR: GEMINI_API_KEY not set. Add it to .env")
    return key


def _strip_json(raw: str) -> str:
    """Gemini sometimes wraps JSON in ```json ... ``` even when told not to."""
    m = re.search(r"\{[\s\S]*\}", raw)
    return m.group(0) if m else raw.strip()


def _normalize_bool(v: Any) -> Optional[int]:
    if v is True:
        return 1
    if v is False:
        return 0
    return None


def _parse_response(raw: str) -> dict[str, Any]:
    obj = json.loads(_strip_json(raw))

    tenant = obj.get("tenant_pref")
    if tenant not in (None, "family", "bachelors", "bachelors_male",
                      "bachelors_female", "any"):
        tenant = None

    amenities = obj.get("amenities") or []
    if not isinstance(amenities, list):
        amenities = []
    allowed = {"lift","parking","gym","pool","security","power_backup",
               "water_supply","clubhouse","garden","kids_play","wifi","ac",
               "washing_machine","fridge","geyser","cctv"}
    amenities = sorted({a for a in amenities if a in allowed})

    lock = obj.get("lock_in_months")
    try:
        lock_int = int(lock) if lock is not None else None
    except (TypeError, ValueError):
        lock_int = None

    return {
        "tenant_pref": tenant,
        "veg_only": _normalize_bool(obj.get("veg_only")),
        "is_owner": _normalize_bool(obj.get("is_owner")),
        "negotiable": _normalize_bool(obj.get("negotiable")),
        "lock_in_months": lock_int,
        "amenities_json": json.dumps(amenities),
    }


def _estimate_cost(usage_meta) -> float:
    """Best-effort cost estimate. Falls back to 0 if usage_meta unavailable."""
    if usage_meta is None:
        return 0.0
    try:
        in_tok = getattr(usage_meta, "prompt_token_count", 0) or 0
        out_tok = getattr(usage_meta, "candidates_token_count", 0) or 0
        return (in_tok / 1_000_000) * INPUT_COST_PER_1M + \
               (out_tok / 1_000_000) * OUTPUT_COST_PER_1M
    except Exception:
        return 0.0


def find_pending(conn: sqlite3.Connection, force: bool,
                 limit: Optional[int]) -> list[sqlite3.Row]:
    sql = """
        SELECT l.listing_id, l.latest_raw_id, r.raw_text
        FROM listings l JOIN raw_listings r ON r.raw_id = l.latest_raw_id
    """
    params: tuple = ()
    if not force:
        sql += """
        WHERE NOT EXISTS (
            SELECT 1 FROM extractions e
            WHERE e.listing_id = l.listing_id
              AND e.extractor = ? AND e.extractor_version = ?
        )
        """
        params = (EXTRACTOR, EXTRACTOR_VERSION)
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def run(force: bool, limit: Optional[int], show: bool) -> None:
    key = load_key()
    import google.generativeai as genai
    genai.configure(api_key=key)
    model = genai.GenerativeModel(MODEL_NAME)

    with get_conn() as conn:
        rows = find_pending(conn, force=force, limit=limit)
        print(f"{len(rows)} listing(s) to extract via Gemini Flash", flush=True)
        if not rows:
            return

        n_written = 0
        total_cost = 0.0
        last_call_t = 0.0

        for row in rows:
            # Politeness: keep under 15 RPM
            elapsed = time.time() - last_call_t
            if elapsed < MIN_INTERVAL_S:
                time.sleep(MIN_INTERVAL_S - elapsed)

            prompt = PROMPT_TEMPLATE.format(text=row["raw_text"])
            t0 = time.perf_counter()
            try:
                resp = model.generate_content(
                    prompt,
                    generation_config={"temperature": 0.0, "max_output_tokens": 2048},
                )
                latency_ms = (time.perf_counter() - t0) * 1000.0
                last_call_t = time.time()
                raw_text_out = resp.text or ""
                usage = getattr(resp, "usage_metadata", None)
                cost = _estimate_cost(usage)
                fields = _parse_response(raw_text_out)
            except Exception as e:  # noqa: BLE001 — one bad row shouldn't stop the batch
                print(f"  listing_id={row['listing_id']}: ERROR {type(e).__name__}: {e}", flush=True)
                continue

            total_cost += cost

            if show:
                print("---")
                print(f"listing_id={row['listing_id']}  ({latency_ms:.0f} ms, ${cost:.6f})")
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
                 fields["amenities_json"], latency_ms, cost),
            )
            n_written += 1
            print(f"  listing_id={row['listing_id']}: ok  ({latency_ms:.0f} ms, ${cost:.6f})", flush=True)

        if not show:
            print(f"\nWrote {n_written} rows to extractions (extractor={EXTRACTOR})", flush=True)
            print(f"Total est. cost: ${total_cost:.5f}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    run(force=args.force, limit=args.limit, show=args.show)


if __name__ == "__main__":
    main()
