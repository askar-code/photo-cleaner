#!/usr/bin/env python3
"""
Build a dry-run organization plan from photo_audit.py CSV output.

This writes only a plan CSV. It does not move, copy, edit, or delete source
files.
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path


SAFE_NAME_RE = re.compile(r"[/:]")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a dry-run photo organization plan")
    parser.add_argument("audit_dir", help="Directory containing files.csv and duplicates.csv")
    parser.add_argument(
        "--output",
        default=None,
        help="Output CSV path. Defaults to organize_plan.csv inside audit_dir.",
    )
    return parser.parse_args()


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def safe_filename(name: str) -> str:
    return SAFE_NAME_RE.sub("_", name).strip() or "unnamed"


def unique_path(target: str, used: set[str]) -> str:
    if target not in used:
        used.add(target)
        return target
    path = Path(target)
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 2
    while True:
        candidate = str(parent / f"{stem}_{counter}{suffix}")
        if candidate not in used:
            used.add(candidate)
            return candidate
        counter += 1


def main() -> int:
    args = parse_args()
    audit_dir = Path(args.audit_dir).expanduser().resolve()
    files_csv = audit_dir / "files.csv"
    duplicates_csv = audit_dir / "duplicates.csv"
    output = Path(args.output).expanduser().resolve() if args.output else audit_dir / "organize_plan.csv"

    files = read_csv(files_csv)
    duplicates = read_csv(duplicates_csv) if duplicates_csv.exists() else []

    duplicate_by_rel: dict[str, str] = {}
    for row in duplicates:
        duplicate_by_rel[row["rel_path"]] = row["duplicate_group"]

    used_targets: set[str] = set()
    plan_rows: list[dict] = []
    counts = defaultdict(int)

    for row in files:
        source_rel = row["rel_path"]
        source_name = safe_filename(Path(source_rel).name)
        kind = row["kind"]
        year_month = row["year_month"]
        year = year_month[:4] if len(year_month) >= 4 else "unknown"
        duplicate_group = duplicate_by_rel.get(source_rel, "")

        if duplicate_group:
            action = "review_exact_duplicate"
            target = f"_duplicates_review/exact/group_{int(duplicate_group):04d}/{source_name}"
        elif kind in {"image", "video"} and row["date_source"] == "filename":
            action = "organize_by_month"
            target = f"{year}/{year_month}/{source_name}"
        elif kind in {"image", "video"}:
            action = "review_no_filename_date"
            target = f"_needs_review/no_filename_date/{source_name}"
        else:
            action = "review_other_file"
            target = f"_needs_review/other/{source_name}"

        target = unique_path(target, used_targets)
        counts[action] += 1
        plan_rows.append(
            {
                "action": action,
                "kind": kind,
                "date": row["date"],
                "date_source": row["date_source"],
                "size": row["size"],
                "duplicate_group": duplicate_group,
                "source_rel_path": source_rel,
                "source_abs_path": row["abs_path"],
                "target_rel_path": target,
            }
        )

    with output.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "action",
            "kind",
            "date",
            "date_source",
            "size",
            "duplicate_group",
            "source_rel_path",
            "source_abs_path",
            "target_rel_path",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(plan_rows)

    summary = audit_dir / "organize_plan_summary.txt"
    with summary.open("w", encoding="utf-8") as f:
        f.write("Dry-run organization plan\n")
        f.write("=========================\n\n")
        f.write("No source files were moved, copied, edited, or deleted.\n\n")
        for action, count in sorted(counts.items()):
            f.write(f"{action}: {count}\n")
        f.write(f"\nPlan CSV: {output}\n")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
