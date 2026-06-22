#!/usr/bin/env python3
"""
Apply a burst cleanup plan by moving candidate files into a quarantine folder.

Default mode is dry-run. Use --execute only after reviewing burst_plan.csv and
backups/sync state. This script never deletes files.
"""

from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Move burst candidate rejects to quarantine")
    parser.add_argument("burst_plan_csv", help="Path to burst_plan.csv")
    parser.add_argument("quarantine_dir", help="Where candidate files should be moved")
    parser.add_argument("--execute", action="store_true", help="Actually move files")
    parser.add_argument("--limit", type=int, default=0, help="Optional max number of files to process")
    return parser.parse_args()


def unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def main() -> int:
    args = parse_args()
    plan_csv = Path(args.burst_plan_csv).expanduser().resolve()
    quarantine_dir = Path(args.quarantine_dir).expanduser().resolve()

    with plan_csv.open(newline="", encoding="utf-8") as f:
        candidates = [row for row in csv.DictReader(f) if row["action"] == "candidate_quarantine"]
    if args.limit:
        candidates = candidates[: args.limit]

    total_size = sum(int(row.get("size") or 0) for row in candidates)
    print(f"Candidate files: {len(candidates)}")
    print(f"Candidate bytes: {total_size}")
    print(f"Quarantine dir: {quarantine_dir}")
    print("Mode:", "EXECUTE" if args.execute else "DRY-RUN")

    moved = 0
    missing = 0
    for row in candidates:
        source = Path(row["abs_path"])
        group = int(row["burst_group"])
        target = quarantine_dir / f"group_{group:04d}" / Path(row["rel_path"]).name
        target = unique_target(target)
        if not source.exists():
            missing += 1
            print(f"MISSING: {source}")
            continue
        print(f"{'MOVE' if args.execute else 'WOULD MOVE'}: {source} -> {target}")
        if args.execute:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
        moved += 1

    print(f"Processed: {moved}")
    print(f"Missing: {missing}")
    if not args.execute:
        print("Dry-run only. Re-run with --execute to move files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
