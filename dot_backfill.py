"""
dot_backfill.py  –  one-shot backfill of dot/dot_master.csv → Supabase
=======================================================================
Run once after setting up the Supabase table to seed it with all
historical rows already in the local CSV.

Usage
-----
    SUPABASE_URL=https://xxx.supabase.co \
    SUPABASE_KEY=<service_role_key> \
    python dot_backfill.py [--csv path/to/dot_master.csv] [--table dot_documents] [--dry-run]

The script upserts in batches of 500 rows so large CSVs don't hit the
Supabase request-size limit.

Requires
--------
    pip install supabase
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

try:
    from supabase import create_client
except ImportError:
    print("ERROR: supabase-py not installed. Run: pip install supabase")
    sys.exit(1)


# ── config ───────────────────────────────────────────────────────────────────

DEFAULT_CSV   = Path("dot") / "dot_master.csv"
DEFAULT_TABLE = "dot_documents"
BATCH_SIZE    = 500

EXPECTED_FIELDS = {"id", "title", "publish_date", "pdf_url", "category", "scraped_at"}


# ── helpers ──────────────────────────────────────────────────────────────────

def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        print(f"ERROR: CSV not found at {path}")
        sys.exit(1)

    rows = []
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = EXPECTED_FIELDS - set(reader.fieldnames or [])
        if missing:
            print(f"WARNING: CSV is missing expected columns: {missing}")

        for row in reader:
            # Strip whitespace from all values
            clean = {k: (v.strip() if v else "") for k, v in row.items()}

            # Skip rows without a PDF URL (shouldn't happen but be safe)
            if not clean.get("pdf_url"):
                continue

            rows.append(clean)

    return rows


def chunked(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def upsert_batch(client, table: str, rows: list[dict], dry_run: bool) -> int:
    if dry_run:
        print(f"  [DRY RUN] would upsert {len(rows)} rows")
        return len(rows)

    try:
        result = client.table(table).upsert(rows, on_conflict="id").execute()
        return len(rows)
    except Exception as e:
        print(f"  ❌ Batch upsert failed: {e}")
        # Try row-by-row as fallback
        ok = 0
        for row in rows:
            try:
                client.table(table).upsert(row, on_conflict="id").execute()
                ok += 1
            except Exception as row_err:
                print(f"    ↳ row {row.get('id')} failed: {row_err}")
        return ok


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill dot_master.csv → Supabase")
    parser.add_argument("--csv",     default=str(DEFAULT_CSV), help="Path to CSV file")
    parser.add_argument("--table",   default=DEFAULT_TABLE,    help="Supabase table name")
    parser.add_argument("--dry-run", action="store_true",      help="Print counts without writing")
    args = parser.parse_args()

    supabase_url = os.getenv("SUPABASE_URL", "")
    supabase_key = os.getenv("SUPABASE_KEY", "")

    if not args.dry_run:
        if not supabase_url or not supabase_key:
            print("ERROR: SUPABASE_URL and SUPABASE_KEY must be set as env vars")
            sys.exit(1)

    print(f"📂 Loading CSV: {args.csv}")
    rows = load_csv(Path(args.csv))
    print(f"   {len(rows):,} rows loaded")

    # Summary by category
    from collections import Counter
    cats = Counter(r.get("category", "UNKNOWN") for r in rows)
    for cat, count in sorted(cats.items()):
        print(f"   {cat:<35} {count:>5}")

    if args.dry_run:
        print("\n[DRY RUN] No changes written to Supabase")
        upsert_batch(None, args.table, rows, dry_run=True)
        return

    client = create_client(supabase_url, supabase_key)

    total_ok = 0
    batch_num = 0

    for batch in chunked(rows, BATCH_SIZE):
        batch_num += 1
        n_batches = -(-len(rows) // BATCH_SIZE)  # ceiling div
        print(f"  Batch {batch_num}/{n_batches} ({len(batch)} rows)...", end=" ")
        ok = upsert_batch(client, args.table, batch, dry_run=False)
        total_ok += ok
        print(f"✅ {ok}")

    print(f"\n{'='*40}")
    print(f"Done – {total_ok:,} / {len(rows):,} rows upserted to '{args.table}'")


if __name__ == "__main__":
    main()
