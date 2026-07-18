#!/usr/bin/env python3
"""Tally the distinct values (and how many rows have each) of one or more CSV
columns. Columns are matched by a case/accent-insensitive substring of the
header, so you don't have to type the exact AssoConnect header.

Examples:
  python3 oidc-e2e/csv_value_counts.py --csv export.csv
      # defaults: "fonction dans l'association" and "membres honoraires"
  python3 oidc-e2e/csv_value_counts.py --csv export.csv "statut dans la base" sexe
  python3 oidc-e2e/csv_value_counts.py --csv export.csv fonction --top 20
"""

import argparse
import csv
import unicodedata
from collections import Counter


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().replace("\u2019", "'").strip()


def find_column(fieldnames, needle):
    n = norm(needle)
    for h in fieldnames:
        if n in norm(h):
            return h
    return None


def main():
    ap = argparse.ArgumentParser(description="Tally CSV column values.")
    ap.add_argument("--csv", required=True, metavar="FILE")
    ap.add_argument(
        "columns", nargs="*",
        default=["fonction dans l'association", "membres honoraires"],
        help="header substrings to tally (default: fonction + membres honoraires)",
    )
    ap.add_argument("--top", type=int, default=0, help="show only the N most common (0 = all)")
    args = ap.parse_args()

    with open(args.csv, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        fields = reader.fieldnames or []

    for needle in args.columns:
        header = find_column(fields, needle)
        print(f"\n=== {needle!r} -> column {header!r} ===")
        if not header:
            print("  (no column header matched)")
            continue
        counts = Counter((r.get(header) or "").strip() for r in rows)
        empty = counts.pop("", 0)
        total = len(rows)
        print(f"  rows={total}  non-empty={total - empty}  empty={empty}  "
              f"distinct values={len(counts)}")
        for value, n in counts.most_common(args.top or None):
            print(f"  {n:>6}  {value}")


if __name__ == "__main__":
    main()
