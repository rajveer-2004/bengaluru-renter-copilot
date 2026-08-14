"""Telegram rental-group scraper.

Uses Telethon to pull recent messages from Bengaluru rental groups the user
is a member of, filters likely rental-listing messages using cheap regex
heuristics, and writes them into the same schema the NoBroker scraper uses:
raw_listings + listings + listing_observations under one scrape_run.

Two-phase workflow:

  1. First run — LIST YOUR GROUPS:
       python -m scrapers.telegram --list-groups
     This walks your dialogs and prints every group/channel with its title
     and ID. You'll be prompted to sign in (phone number + SMS/app code)
     on the first run; the session persists in .telegram_session (gitignored).

  2. Actual scrape — pass group titles or IDs:
       python -m scrapers.telegram --groups "Bangalore Rentals No Broker" "HSR Flats"
     Or by numeric ID for precision:
       python -m scrapers.telegram --group-ids 1234567890 987654321
     Optional:
       --limit-per-group 200   how many recent messages to inspect per group
       --min-days 14           only pull messages newer than N days

Rental-listing filter: message must (a) mention BHK, area, or rent markers
AND (b) be at least 60 chars long. This drops one-liners and greetings.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

# Telethon imports guarded so --help works without deps loaded
try:
    from telethon import TelegramClient
    from telethon.tl.types import Channel, Chat  # noqa: F401
except ImportError:
    TelegramClient = None  # type: ignore

from scrapers.db_utils import (
    finish_scrape_run,
    get_conn,
    insert_raw_listing,
    log_observation,
    start_scrape_run,
    upsert_listing,
)

SESSION_NAME = ".telegram_session"  # gitignored
DEFAULT_LIMIT_PER_GROUP = 200
DEFAULT_MIN_DAYS = 14

# --- Filtering helpers -----------------------------------------------------

_RENT_MARKER = re.compile(
    r"(?i)(?:\d+\s*bhk|\b(rent|deposit|rental|for\s+rent)\b|₹|rs\.?\s*\d|"
    r"sqft|sq\.?\s*ft|preferred|bachelor|family|owner|broker)"
)

# Sale-listing markers: exclude these even if the rent marker matches. Sale
# posts typically say "for sale", "resale", or quote a per-sqft price
# (rentals never do — they quote total monthly rent).
_SALE_MARKER = re.compile(
    r"(?i)\bfor\s+sale\b|\bresale\b|\b\d[\d,]*\s*/?-?\s*per\s*sq\.?\s*ft\b|"
    r"\bexpected\s+price\b"
)


def looks_like_listing(text: str) -> bool:
    if not text or len(text) < 60:
        return False
    if _SALE_MARKER.search(text):
        return False
    return bool(_RENT_MARKER.search(text))


# --- Cheap structured hints from raw text ---------------------------------

_BHK_RE = re.compile(r"(\d+(?:\.\d+)?)\s*BHK", re.I)
_AREA_RE = re.compile(r"([\d,]+)\s*sq\.?\s*ft", re.I)
_RENT_RE = re.compile(
    # "rent" or "rental" followed within 80 non-digit chars by an amount.
    # 80 (up from 30) to survive filler like "for Rent in Whitefield, Bangalore. RENT: 27k".
    r"(?:rent|rental)[^0-9]{0,80}(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d+)?)\s*([KLkl])?",
    re.I,
)
_INR_ANY_RE = re.compile(r"(?:₹|rs\.?)\s*([\d,]+(?:\.\d+)?)\s*([KLkl])?", re.I)


def _parse_num(txt: str, suffix: Optional[str]) -> Optional[float]:
    try:
        n = float(txt.replace(",", ""))
    except ValueError:
        return None
    if suffix and suffix.lower() == "k":
        n *= 1_000
    elif suffix and suffix.lower() == "l":
        n *= 100_000
    return n


def _parse_bhk(text: str) -> Optional[float]:
    m = _BHK_RE.search(text)
    return float(m.group(1)) if m else None


def _parse_area(text: str) -> Optional[float]:
    m = _AREA_RE.search(text)
    return float(m.group(1).replace(",", "")) if m else None


def _parse_rent(text: str) -> Optional[float]:
    # Prefer a "rent"-anchored number
    m = _RENT_RE.search(text)
    if m:
        v = _parse_num(m.group(1), m.group(2))
        if v and 3000 <= v <= 500_000:
            return v
    # Fallback: any INR amount that looks like rent (between 5k and 500k)
    for m in _INR_ANY_RE.finditer(text):
        v = _parse_num(m.group(1), m.group(2))
        if v and 5_000 <= v <= 500_000:
            return v
    return None


# --- Locality guessing ---------------------------------------------------

# Match a locality name mentioned anywhere in text. Extend as we see more.
# Order matters: put more specific names first so they win over shorter prefixes.
KNOWN_LOCALITIES = [
    "HSR Layout", "HSR", "BTM Layout", "BTM", "Sarjapur Road", "Sarjapur",
    "JP Nagar", "Kalyan Nagar", "Kalyannagar", "Rajaji Nagar", "Rajajinagar",
    "Electronic City", "E-City", "Ecity",
    "Koramangala", "Indiranagar", "Whitefield", "Bellandur", "Marathahalli",
    "Jayanagar", "Bommanahalli", "Hebbal", "Yelahanka", "Malleshwaram",
    "Basavanagudi", "Banashankari", "Frazer Town", "Cooke Town", "Ulsoor",
    "Domlur", "CV Raman Nagar", "Silk Board", "Yeshwanthpur", "Kengeri",
    # Additions from Telegram run 2026-08-13:
    "Kadugodi", "Harlur", "Hoodi", "Yemalur", "Yamalur", "Seegehalli",
    "Nagarabhavi", "Battarahalli", "Krishnarajapuram", "Kadubeesanahalli",
    "Munnelkollal", "Hebbagodi", "Seetharampalya", "Attibele", "Chandapura",
    "Begur Road", "Manyata", "Hosahalli",
]


def guess_locality(text: str) -> Optional[str]:
    lower = text.lower()
    for loc in KNOWN_LOCALITIES:
        if loc.lower() in lower:
            return loc
    return None


# --- Telegram loop -------------------------------------------------------

def _load_creds() -> tuple[int, str]:
    load_dotenv()
    api_id_str = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    if not api_id_str or not api_hash:
        sys.exit("ERROR: TELEGRAM_API_ID or TELEGRAM_API_HASH missing in .env")
    try:
        return int(api_id_str), api_hash
    except ValueError:
        sys.exit(f"ERROR: TELEGRAM_API_ID is not an integer: {api_id_str!r}")


async def _list_groups(client: TelegramClient) -> None:
    print("Your groups and channels:\n", flush=True)
    async for dialog in client.iter_dialogs():
        ent = dialog.entity
        if isinstance(ent, (Channel, Chat)):
            kind = "channel" if isinstance(ent, Channel) and getattr(ent, "broadcast", False) else "group"
            title = dialog.name or "(no title)"
            print(f"  [{kind:7}] id={ent.id:>15}  {title!r}", flush=True)


async def _pick_entities(client: TelegramClient, group_names: list[str],
                         group_ids: list[int]) -> list[Any]:
    """Resolve group titles or IDs to Telethon entities."""
    wanted_names = {n.strip().lower() for n in group_names}
    wanted_ids = set(group_ids)
    picked = []
    async for dialog in client.iter_dialogs():
        ent = dialog.entity
        if not isinstance(ent, (Channel, Chat)):
            continue
        title = (dialog.name or "").strip().lower()
        if title in wanted_names or ent.id in wanted_ids:
            picked.append((dialog.name, ent))
    return picked


async def _scrape_group(client: TelegramClient, name: str, ent: Any,
                        limit: int, cutoff: datetime) -> list[dict]:
    print(f"[{name}]", flush=True)
    out = []
    async for msg in client.iter_messages(ent, limit=limit):
        if msg.date and msg.date < cutoff:
            break
        text = (msg.text or msg.message or "").strip()
        if not looks_like_listing(text):
            continue
        out.append({
            "text": text,
            "msg_id": str(msg.id),
            "sender_id": msg.sender_id,
            "date": msg.date,
        })
    print(f"    kept {len(out)} listing-like messages", flush=True)
    return out


async def _run(list_groups: bool, group_names: list[str], group_ids: list[int],
               limit: int, min_days: int) -> None:
    if TelegramClient is None:
        sys.exit("ERROR: telethon not installed. `pip install telethon==1.36.0`")

    api_id, api_hash = _load_creds()
    client = TelegramClient(SESSION_NAME, api_id, api_hash)
    await client.start()  # prompts for phone + code on first run

    if list_groups:
        await _list_groups(client)
        await client.disconnect()
        return

    if not group_names and not group_ids:
        sys.exit("ERROR: pass --groups or --group-ids (or --list-groups first)")

    picked = await _pick_entities(client, group_names, group_ids)
    if not picked:
        await client.disconnect()
        sys.exit("ERROR: none of the requested groups were found. Try --list-groups.")

    print(f"Scraping {len(picked)} group(s), last {limit} msgs each, "
          f"cutoff {min_days} days.\n", flush=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=min_days)

    all_msgs: list[tuple[str, dict]] = []
    for name, ent in picked:
        msgs = await _scrape_group(client, name, ent, limit, cutoff)
        for m in msgs:
            all_msgs.append((name, m))

    await client.disconnect()

    print(f"\nTotal listing-like messages: {len(all_msgs)}", flush=True)
    if not all_msgs:
        return

    # ---- write to DB ---------------------------------------------------
    n_raw = 0
    n_new = 0
    with get_conn() as conn:
        run_id = start_scrape_run(conn, "telegram")
        try:
            for group_name, m in all_msgs:
                source = f"telegram:{group_name}"
                raw_id = insert_raw_listing(
                    conn, run_id=run_id, source=source,
                    source_url=None, source_msg_id=m["msg_id"],
                    raw_text=m["text"], raw_json=None,
                )
                if raw_id is None:
                    continue
                n_raw += 1

                text = m["text"]
                rent = _parse_rent(text)  # parse once, reuse for both writes
                listing_id, is_new = upsert_listing(
                    conn, source=source,
                    locality=guess_locality(text),
                    bhk=_parse_bhk(text),
                    area_sqft=_parse_area(text),
                    rent_monthly=rent,
                    deposit=None,
                    raw_id=raw_id,
                )
                if is_new:
                    n_new += 1

                log_observation(
                    conn, listing_id=listing_id, run_id=run_id,
                    rent_monthly=rent, deposit=None,
                    raw_id=raw_id,
                )

            finish_scrape_run(conn, run_id, status="ok", n_raw=n_raw, n_new=n_new)
        except Exception as e:  # noqa: BLE001
            finish_scrape_run(conn, run_id, status="failed",
                              n_raw=n_raw, n_new=n_new, error=str(e))
            raise

    print(f"Wrote {n_raw} new raw rows, {n_new} new listings under run_id={run_id}",
          flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-groups", action="store_true",
                    help="Print every group/channel you're in, with IDs.")
    ap.add_argument("--groups", nargs="+", default=[],
                    help="Group titles (case-insensitive) to scrape.")
    ap.add_argument("--group-ids", type=int, nargs="+", default=[],
                    help="Group numeric IDs to scrape.")
    ap.add_argument("--limit-per-group", type=int, default=DEFAULT_LIMIT_PER_GROUP)
    ap.add_argument("--min-days", type=int, default=DEFAULT_MIN_DAYS)
    args = ap.parse_args()

    asyncio.run(_run(
        list_groups=args.list_groups,
        group_names=args.groups,
        group_ids=args.group_ids,
        limit=args.limit_per_group,
        min_days=args.min_days,
    ))


if __name__ == "__main__":
    main()
