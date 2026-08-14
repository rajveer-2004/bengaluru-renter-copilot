"""Quick inspection of what the Telegram scraper wrote.

Prints the Telegram listings currently in the DB, showing each one's
parsed structured fields plus the raw text (truncated) so we can eyeball
whether the filter is keeping real listings and whether the parsers are
extracting the right fields.

Usage:
    python -m scripts.inspect_telegram
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "db" / "copilot.db"


def main() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # 1) All scrape_runs, so we see how many Telegram runs happened
    print("=== scrape_runs ===")
    for r in conn.execute(
        "SELECT run_id, source, status, n_raw, n_new, started_at "
        "FROM scrape_runs ORDER BY run_id DESC LIMIT 10"
    ):
        print(f"  run_id={r['run_id']:<3} source={r['source']:<25} "
              f"status={r['status']:<8} n_raw={r['n_raw']:<3} n_new={r['n_new']:<3} "
              f"{r['started_at']}")

    # 2) Telegram-sourced listings joined to their latest raw text
    print("\n=== telegram listings ===")
    rows = list(conn.execute("""
        SELECT l.listing_id, l.source, l.locality, l.bhk, l.rent_monthly,
               l.area_sqft, l.first_seen_at, rl.raw_text
        FROM listings l
        JOIN raw_listings rl ON rl.raw_id = l.latest_raw_id
        WHERE l.source LIKE 'telegram:%'
        ORDER BY l.listing_id
    """))
    if not rows:
        print("  (no telegram listings in DB yet)")
    for r in rows:
        print(f"\n--- listing_id={r['listing_id']}  source={r['source']}")
        print(f"    locality={r['locality']!r}  bhk={r['bhk']}  "
              f"rent={r['rent_monthly']}  area={r['area_sqft']}")
        print(f"    first_seen_at={r['first_seen_at']}")
        text = (r['raw_text'] or "")[:600]
        # Indent each line of the raw text for readability
        for line in text.splitlines():
            print(f"    | {line}")

    # 3) Any raw rows from telegram that didn't become listings (shouldn't happen
    #    with current code, but useful signal)
    print("\n=== telegram raw_listings count ===")
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM raw_listings WHERE source LIKE 'telegram:%'"
    ).fetchone()["c"]
    print(f"  total raw telegram rows: {n}")

    conn.close()


if __name__ == "__main__":
    main()
