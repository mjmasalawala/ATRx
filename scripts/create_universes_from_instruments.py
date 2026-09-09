"""
One-off local script: builds screener_universes rows out of the full
`instruments` reference table (see 006_create_instruments_table.sql),
instead of the hand-picked ~80-per-tier lists screener_universes normally
holds.

Every stock in `instruments` is included -- for each index (NIFTY 100,
NIFTY MIDCAP 150, NIFTY SMALLCAP 250, NIFTY MICROCAP 250), its tickers are
split into batches of 75 (the largest count run_screener has been shown to
get through inside Vercel's 60s Hobby-plan timeout). Each batch becomes its
own tier named "<Index><counter>" -- spaces stripped from the index name,
counter starting at 1 -- e.g. NIFTY 100's 100 tickers become "NIFTY1001"
(75 tickers) and "NIFTY1002" (the remaining 25); NIFTY SMALLCAP 250 becomes
"NIFTYSMALLCAP2501".."NIFTYSMALLCAP2504".

Run manually, whenever `instruments` changes (e.g. after an index
reconstitution):
    python scripts/create_universes_from_instruments.py             # writes to screener_universes
    python scripts/create_universes_from_instruments.py --dry-run    # preview only, no writes

Needs DATABASE_URL (for db_store), same as any other local script here.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_store

BATCH_SIZE = 75


def chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview the tiers without writing to the DB.")
    args = parser.parse_args()

    by_index = db_store.list_instruments_by_index()
    if not by_index:
        print("instruments table is empty -- nothing to do. Run 006_create_instruments_table.sql first.")
        return

    total_tiers = 0
    for index_name, tickers in by_index.items():
        slug = index_name.replace(" ", "")
        batches = chunk(tickers, BATCH_SIZE)

        for counter, batch in enumerate(batches, start=1):
            tier = f"{slug}{counter}"
            note = f"{index_name}, batch {counter}/{len(batches)}"
            print(f"{tier}: {len(batch)} stocks ({note})")
            if not args.dry_run:
                db_store.save_universe(tier, batch, note=note)
            total_tiers += 1

    action = "Would write" if args.dry_run else "Wrote"
    print(f"\n{action} {total_tiers} universe tier(s) across {len(by_index)} index(es).")


if __name__ == "__main__":
    main()
