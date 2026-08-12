"""NoBroker rental listings scraper for Bengaluru.

Design:
- Uses Playwright (Chromium) because NoBroker's SPA is JS-rendered.
- Fetches search-result pages for a configured set of localities.
- For each listing card, captures the structured summary + description text,
  writes to raw_listings + upserts into listings + logs an observation.
- Politeness: ~2s delay between navigations, standard desktop user-agent,
  respects a per-run cap.

Usage:
    python -m scrapers.nobroker                       # default settings
    python -m scrapers.nobroker --max-per-locality 20 # smaller run for testing
    python -m scrapers.nobroker --headed              # show browser (debug)
    python -m scrapers.nobroker --dry-run             # scrape but don't write DB

Note: NoBroker's exact selectors change. This scraper is defensive — it captures
raw HTML+text into raw_listings even when structured extraction partially fails,
so we can re-parse from stored raw data without re-scraping.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from .db_utils import (
    finish_scrape_run,
    get_conn,
    insert_raw_listing,
    log_observation,
    start_scrape_run,
    upsert_listing,
)

SOURCE = "nobroker"

# NoBroker uses locality slugs in the URL path. Start with a small set;
# grow after we verify the scrape works end to end.
DEFAULT_LOCALITIES = [
    "HSR Layout",
    "Koramangala",
    "Indiranagar",
    "Whitefield",
    "Bellandur",
    "Marathahalli",
    "Electronic City",
    "Jayanagar",
    "BTM Layout",
    "Sarjapur Road",
]

SEARCH_URL_TMPL = (
    "https://www.nobroker.in/property/rent/bangalore/{q}?"
    "searchParam=W3sibGF0IjoxMi45NzE1OTg3LCJsb24iOjc3LjU5NDU2Miwic2hvd01hcCI6ZmFsc2V9XQ==&"
    "radius=2.0&city=bangalore"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36"
)


@dataclass
class Listing:
    """One parsed listing card."""
    locality_query: str
    raw_text: str
    raw_json: Optional[str] = None
    source_url: Optional[str] = None

    # Structured fields (best effort — may be None)
    locality: Optional[str] = None
    bhk: Optional[float] = None
    area_sqft: Optional[float] = None
    rent_monthly: Optional[float] = None
    deposit: Optional[float] = None
    property_type: Optional[str] = None
    furnishing: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)


# --- Parsing helpers -------------------------------------------------------

_NUM_RE = re.compile(r"[\d,]+(?:\.\d+)?")


def _parse_bhk(text: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*BHK", text, re.IGNORECASE)
    return float(m.group(1)) if m else None


def _parse_area(text: str) -> Optional[float]:
    # "1200 sqft", "1,200 sq ft", "1200 sq.ft."
    m = re.search(r"([\d,]+)\s*sq\.?\s*ft", text, re.IGNORECASE)
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


def _parse_inr(text: str) -> Optional[float]:
    """Parse '₹32,000', 'Rs 32000', '32K', '1.2L' into a rupee amount."""
    text = text.replace(",", "")
    # try lakh/K first
    m = re.search(r"(?:₹|rs\.?)\s*([\d.]+)\s*L", text, re.IGNORECASE)
    if m:
        return float(m.group(1)) * 100_000
    m = re.search(r"(?:₹|rs\.?)\s*([\d.]+)\s*K", text, re.IGNORECASE)
    if m:
        return float(m.group(1)) * 1_000
    m = re.search(r"(?:₹|rs\.?)\s*([\d.]+)", text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    # bare number fallback near 'rent' keyword
    m = re.search(r"rent[^0-9]{0,20}([\d.]+)", text, re.IGNORECASE)
    return float(m.group(1)) if m else None


def _parse_furnishing(text: str) -> Optional[str]:
    t = text.lower()
    if "fully furnished" in t or "full furnish" in t:
        return "full"
    if "semi furnished" in t or "semi-furnished" in t:
        return "semi"
    if "unfurnished" in t or "not furnished" in t:
        return "unfurnished"
    return None


def _parse_card(card_text: str, locality_query: str,
                source_url: Optional[str]) -> Listing:
    return Listing(
        locality_query=locality_query,
        raw_text=card_text,
        source_url=source_url,
        locality=locality_query,  # coarse; real locality lives in text
        bhk=_parse_bhk(card_text),
        area_sqft=_parse_area(card_text),
        rent_monthly=_parse_inr(card_text),
        furnishing=_parse_furnishing(card_text),
    )


# --- Scraping --------------------------------------------------------------


def _polite_sleep(min_s: float = 2.0, max_s: float = 3.5) -> None:
    time.sleep(random.uniform(min_s, max_s))


def _scrape_locality(page: Page, locality: str, max_cards: int) -> list[Listing]:
    url = SEARCH_URL_TMPL.format(q=quote(locality))
    print(f"  -> {url}", flush=True)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except PWTimeout:
        print(f"     timeout loading {locality}", flush=True)
        return []

    # NoBroker lazy-loads. Scroll a few times to trigger card rendering.
    for _ in range(4):
        page.mouse.wheel(0, 4000)
        time.sleep(1.2)

    # Card selectors change across NoBroker's redesigns. Try a set of known
    # container selectors and take whichever yields results.
    candidates = [
        "div[data-testid='property-card']",
        "div.card.card-padding",
        "article",
        "div[itemtype*='Product']",
    ]

    cards = []
    for sel in candidates:
        found = page.query_selector_all(sel)
        if found:
            cards = found
            print(f"     selector '{sel}' -> {len(cards)} cards", flush=True)
            break

    if not cards:
        print("     no cards found (selectors may have changed)", flush=True)
        return []

    listings: list[Listing] = []
    for card in cards[:max_cards]:
        try:
            txt = (card.inner_text() or "").strip()
            if len(txt) < 40:
                continue
            href = None
            link = card.query_selector("a[href]")
            if link:
                href = link.get_attribute("href")
                if href and href.startswith("/"):
                    href = "https://www.nobroker.in" + href
            listings.append(_parse_card(txt, locality, href))
        except Exception as e:  # noqa: BLE001 — card-level errors shouldn't stop the run
            print(f"     card parse error: {e}", flush=True)
    return listings


def scrape(max_per_locality: int, headed: bool, dry_run: bool,
           localities: list[str]) -> None:
    print(f"NoBroker scrape starting: {len(localities)} localities, "
          f"max {max_per_locality} cards each, "
          f"{'HEADED' if headed else 'headless'}, "
          f"{'DRY RUN' if dry_run else 'writing to DB'}", flush=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        context = browser.new_context(user_agent=USER_AGENT,
                                      viewport={"width": 1440, "height": 900})
        page = context.new_page()

        all_listings: list[Listing] = []
        for loc in localities:
            print(f"[{loc}]", flush=True)
            all_listings.extend(_scrape_locality(page, loc, max_per_locality))
            _polite_sleep()

        browser.close()

    print(f"\nTotal listings captured: {len(all_listings)}", flush=True)

    if dry_run:
        # print a sample so you can eyeball parsing
        for lst in all_listings[:3]:
            print("---")
            print(f"locality={lst.locality_query}  bhk={lst.bhk}  "
                  f"area={lst.area_sqft}  rent={lst.rent_monthly}  "
                  f"url={lst.source_url}")
            print(lst.raw_text[:300])
        return

    n_raw_inserted = 0
    n_new_listings = 0
    with get_conn() as conn:
        run_id = start_scrape_run(conn, SOURCE)
        try:
            for lst in all_listings:
                raw_id = insert_raw_listing(
                    conn, run_id=run_id, source=SOURCE,
                    source_url=lst.source_url, source_msg_id=None,
                    raw_text=lst.raw_text, raw_json=lst.raw_json,
                )
                if raw_id is None:
                    continue  # duplicate raw
                n_raw_inserted += 1

                listing_id, is_new = upsert_listing(
                    conn, source=SOURCE, locality=lst.locality, bhk=lst.bhk,
                    area_sqft=lst.area_sqft, rent_monthly=lst.rent_monthly,
                    deposit=lst.deposit, raw_id=raw_id,
                    extras={"property_type": lst.property_type,
                            "furnishing": lst.furnishing},
                )
                if is_new:
                    n_new_listings += 1

                log_observation(
                    conn, listing_id=listing_id, run_id=run_id,
                    rent_monthly=lst.rent_monthly, deposit=lst.deposit,
                    raw_id=raw_id,
                )

            finish_scrape_run(conn, run_id, status="ok",
                              n_raw=n_raw_inserted, n_new=n_new_listings)
        except Exception as e:  # noqa: BLE001
            finish_scrape_run(conn, run_id, status="failed",
                              n_raw=n_raw_inserted, n_new=n_new_listings,
                              error=str(e))
            raise

    print(f"Wrote {n_raw_inserted} new raw rows, {n_new_listings} new listings "
          f"under run_id={run_id}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-per-locality", type=int, default=30)
    ap.add_argument("--headed", action="store_true",
                    help="Show the browser window (for debugging)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Scrape and print, don't write to DB")
    ap.add_argument("--localities", nargs="+", default=DEFAULT_LOCALITIES)
    args = ap.parse_args()

    try:
        scrape(max_per_locality=args.max_per_locality, headed=args.headed,
               dry_run=args.dry_run, localities=args.localities)
    except KeyboardInterrupt:
        print("\ninterrupted", flush=True)
        sys.exit(130)


if __name__ == "__main__":
    main()
