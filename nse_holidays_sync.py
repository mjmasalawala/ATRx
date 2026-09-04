"""
Fetches NSE's published trading-holiday calendar and stores it in Neon, so
the candidate-scan agent can tell a real trading day from a holiday without
guessing. Meant to run once a year (see /api/sync-nse-holidays).

nseindia.com blocks bare API requests without first looking like a browser
that's visited the site -- a plain GET to the API endpoint gets a 401/403.
The workaround (same one every community NSE-API wrapper uses): hit the
homepage first to pick up its session cookies, then reuse them for the API
call in the same requests.Session.
"""

import logging
from datetime import datetime

import requests

import db_store

logger = logging.getLogger("atrx.nse_holidays_sync")

_HOME_URL = "https://www.nseindia.com"
_HOLIDAY_API_URL = "https://www.nseindia.com/api/holiday-master?type=trading"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# NSE's holiday-master groups holidays by market segment; "CM" (Capital
# Market) is the equity cash-market segment this screener actually trades --
# other segments (FO, CD, ...) sometimes have slightly different calendars.
_SEGMENT = "CM"


def fetch_holidays(year: int | None = None) -> list[dict]:
    """Returns [{date: 'YYYY-MM-DD', description: str}, ...] for the CM
    segment. `year` filters client-side (NSE's API returns whatever its
    current published calendar covers, usually just the current year)."""
    session = requests.Session()
    session.headers.update(_HEADERS)

    resp = session.get(_HOME_URL, timeout=15)
    resp.raise_for_status()

    resp = session.get(_HOLIDAY_API_URL, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    segment_rows = data.get(_SEGMENT, [])
    holidays = []
    for row in segment_rows:
        raw_date = row.get("tradingDate")
        if not raw_date:
            continue
        parsed = datetime.strptime(raw_date, "%d-%b-%Y").date()
        if year is not None and parsed.year != year:
            continue
        holidays.append({"date": parsed.isoformat(), "description": row.get("description", "")})

    return holidays


def sync_year(year: int | None = None) -> dict:
    """Fetches and persists the holiday calendar for `year` (default: the
    current year). Raises on fetch/parse failure -- the caller (the cron
    endpoint) decides how to report that."""
    year = year or datetime.now().year
    holidays = fetch_holidays(year)
    if not holidays:
        raise RuntimeError(f"NSE holiday API returned no {_SEGMENT}-segment holidays for {year}")
    written = db_store.replace_nse_holidays(year, holidays)
    logger.info("Stored %d NSE trading holidays for %d", written, year)
    return {"year": year, "count": written, "holidays": holidays}
