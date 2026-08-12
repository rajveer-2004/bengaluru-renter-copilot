"""Initialize the SQLite database from db/schema.sql.

Idempotent: safe to run repeatedly. Prints the list of tables at the end so
you can eyeball that all 8 expected tables exist.

Usage:
    python scripts/init_db.py
    python scripts/init_db.py --db db/copilot.db --schema db/schema.sql
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

EXPECTED_TABLES = {
    "scrape_runs",
    "raw_listings",
    "listings",
    "listing_observations",
    "extractions",
    "localities",
    "predictions",
    "benchmark_runs",
}


def init_db(db_path: Path, schema_path: Path) -> None:
    if not schema_path.exists():
        sys.exit(f"ERROR: schema file not found: {schema_path}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = schema_path.read_text(encoding="utf-8")

    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema_sql)
        conn.commit()

        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()

    tables = [r[0] for r in rows]

    print(f"DB: {db_path.resolve()}")
    print(f"Tables ({len(tables)}):")
    for t in tables:
        marker = "  " if t in EXPECTED_TABLES else " ?"
        print(f"  {marker} {t}")

    missing = EXPECTED_TABLES - set(tables)
    extra = set(tables) - EXPECTED_TABLES
    if missing:
        print(f"\nMISSING expected tables: {sorted(missing)}")
        sys.exit(1)
    if extra:
        print(f"\nNote: unexpected tables present: {sorted(extra)}")

    print("\nOK: all 8 expected tables present.")


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description="Initialize copilot.db from schema.sql")
    ap.add_argument("--db", default=str(repo_root / "db" / "copilot.db"))
    ap.add_argument("--schema", default=str(repo_root / "db" / "schema.sql"))
    args = ap.parse_args()
    init_db(Path(args.db), Path(args.schema))


if __name__ == "__main__":
    main()
