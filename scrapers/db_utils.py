"""Shared DB helpers used by every scraper.

Design notes:
- Every scraper opens a `scrape_run`, writes to raw_listings + listings +
  listing_observations under that run_id, and closes the run at the end.
- raw_listings is deduped by (source, content_hash) — SHA256 of normalized text.
- listings is deduped by dedup_key = 'locality_norm|bhk|rent_bucket|area_bucket'.
- On repeat sightings we UPDATE last_seen_at + insert a listing_observations row.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "db" / "copilot.db"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_sha() -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, stderr=subprocess.DEVNULL
        )
        return out.decode().strip()
    except Exception:
        return None


def normalize_text(text: str) -> str:
    """Lowercase, collapse whitespace. Used for hashing raw text for dedup."""
    return re.sub(r"\s+", " ", text.strip().lower())


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def normalize_locality(locality: Optional[str]) -> Optional[str]:
    if not locality:
        return None
    s = locality.lower().strip()
    # strip 'sector 2', 'phase 1', etc. for coarse bucketing later
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.replace(" ", "_")


def rent_bucket(rent: Optional[float]) -> str:
    if rent is None:
        return "unknown"
    # 5k-wide buckets up to 100k, then 20k-wide
    if rent < 100_000:
        lo = int(rent // 5000) * 5000
        return f"{lo}-{lo+5000}"
    lo = int(rent // 20_000) * 20_000
    return f"{lo}-{lo+20_000}"


def area_bucket(area: Optional[float]) -> str:
    if area is None:
        return "unknown"
    lo = int(area // 200) * 200
    return f"{lo}-{lo+200}"


def dedup_key(locality_norm: Optional[str], bhk: Optional[float],
              rent: Optional[float], area: Optional[float]) -> str:
    return f"{locality_norm or 'unknown'}|{bhk if bhk is not None else 'unk'}|" \
           f"{rent_bucket(rent)}|{area_bucket(area)}"


@contextmanager
def get_conn(db_path: Path = DEFAULT_DB) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def start_scrape_run(conn: sqlite3.Connection, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO scrape_runs (started_at, source, status, git_sha) "
        "VALUES (?, ?, 'running', ?)",
        (utcnow_iso(), source, git_sha()),
    )
    return cur.lastrowid


def finish_scrape_run(conn: sqlite3.Connection, run_id: int, *,
                      status: str, n_raw: int = 0, n_new: int = 0,
                      error: Optional[str] = None) -> None:
    conn.execute(
        "UPDATE scrape_runs SET finished_at=?, status=?, n_raw=?, n_new=?, error=? "
        "WHERE run_id=?",
        (utcnow_iso(), status, n_raw, n_new, error, run_id),
    )


def insert_raw_listing(conn: sqlite3.Connection, *, run_id: int, source: str,
                       source_url: Optional[str], source_msg_id: Optional[str],
                       raw_text: str, raw_json: Optional[str]) -> Optional[int]:
    """Insert into raw_listings. Returns raw_id, or None if duplicate."""
    h = content_hash(raw_text)
    try:
        cur = conn.execute(
            "INSERT INTO raw_listings "
            "(run_id, source, source_url, source_msg_id, scraped_at, "
            " raw_text, raw_json, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, source, source_url, source_msg_id, utcnow_iso(),
             raw_text, raw_json, h),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        # UNIQUE(source, content_hash) hit — already seen this exact text
        return None


def upsert_listing(conn: sqlite3.Connection, *, source: str,
                   locality: Optional[str], bhk: Optional[float],
                   area_sqft: Optional[float], rent_monthly: Optional[float],
                   deposit: Optional[float], raw_id: int,
                   extras: Optional[dict[str, Any]] = None) -> tuple[int, bool]:
    """Upsert into listings by dedup_key. Returns (listing_id, is_new)."""
    extras = extras or {}
    locality_norm = normalize_locality(locality)
    key = dedup_key(locality_norm, bhk, rent_monthly, area_sqft)
    now = utcnow_iso()

    row = conn.execute(
        "SELECT listing_id FROM listings WHERE dedup_key=?", (key,)
    ).fetchone()

    if row:
        conn.execute(
            "UPDATE listings SET last_seen_at=?, latest_raw_id=? WHERE listing_id=?",
            (now, raw_id, row["listing_id"]),
        )
        return int(row["listing_id"]), False

    cols = {
        "first_seen_at": now, "last_seen_at": now, "source": source,
        "dedup_key": key, "locality": locality, "locality_norm": locality_norm,
        "bhk": bhk, "area_sqft": area_sqft, "rent_monthly": rent_monthly,
        "deposit": deposit, "latest_raw_id": raw_id, **extras,
    }
    keys = ", ".join(cols.keys())
    placeholders = ", ".join(["?"] * len(cols))
    cur = conn.execute(
        f"INSERT INTO listings ({keys}) VALUES ({placeholders})",
        list(cols.values()),
    )
    return int(cur.lastrowid), True


def log_observation(conn: sqlite3.Connection, *, listing_id: int, run_id: int,
                    rent_monthly: Optional[float], deposit: Optional[float],
                    raw_id: int) -> None:
    conn.execute(
        "INSERT INTO listing_observations "
        "(listing_id, run_id, observed_at, rent_monthly, deposit, raw_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (listing_id, run_id, utcnow_iso(), rent_monthly, deposit, raw_id),
    )
