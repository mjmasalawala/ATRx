"""
One-off local script: dumps your current Kite holdings and seeds them into
cost_basis_baseline as the starting lot for each symbol. Trade history on
top of that baseline comes from uploading a tradebook CSV on the Cost
Basis page (see trade_csv_import.py), not from this script or any live
Kite API sync.

Run manually, whenever you need to seed a new symbol's starting position:
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

import cost_basis_state_sync
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

    recompute_result = cost_basis_state_sync.recompute_all()
    print(f"Recomputed cost_basis_state for {len(recompute_result['updated'])} symbol(s) -- they'll now show on the Cost Basis page.")
    if recompute_result["failed"]:
        print(f"{len(recompute_result['failed'])} symbol(s) FAILED to recompute and will not show up:")
        for f in recompute_result["failed"]:
            print(f"  {f['symbol']}: {f['error']}")


if __name__ == "__main__":
    main()
