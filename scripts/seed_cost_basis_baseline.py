"""
One-off local script: dumps your current Kite holdings and seeds them into
cost_basis_baseline as the starting lot for each symbol, before the daily
sync (kite_sync.py) starts appending trades on top.

Run manually, once:
    python scripts/seed_cost_basis_baseline.py
    python scripts/seed_cost_basis_baseline.py --force   # overwrite existing baseline rows

Needs the same env vars as any other local script here: KITE_API_KEY /
KITE_API_SECRET (for the interactive login) and DATABASE_URL (for db_store).
"""

import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_store
from kite_auth import get_kite_session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Overwrite existing baseline rows.")
    args = parser.parse_args()

    kite = get_kite_session()
    holdings = kite.holdings()

    rows = [
        {
            "symbol": h["tradingsymbol"],
            "quantity": h["quantity"],
            "avg_price": h["average_price"],
            "as_of_date": date.today().isoformat(),
        }
        for h in holdings
        if h["quantity"] > 0
    ]

    if not rows:
        print("No holdings with quantity > 0 found. Nothing to seed.")
        return

    written = db_store.save_baseline(rows, force=args.force)
    print(f"Seeded {written} baseline row(s) out of {len(rows)} holding(s) fetched.")
    if written < len(rows) and not args.force:
        print("Some symbols already had a baseline row and were left alone -- re-run with --force to overwrite.")


if __name__ == "__main__":
    main()
