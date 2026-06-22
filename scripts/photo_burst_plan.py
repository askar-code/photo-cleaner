#!/usr/bin/env python3
"""
Build a safe burst-cleaning plan from photo_audit.py output.

The plan treats fast consecutive photos as "burst" groups. It proposes keeping
one automatic candidate per group and quarantining the rest, but only writes
reports and CSV files. It never changes source photos.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
from collections import Counter, defaultdict
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create burst review/quarantine plan")
    parser.add_argument("audit_dir", help="Directory containing files.csv")
    parser.add_argument("--max-gap-seconds", type=float, default=2.0)
    parser.add_argument("--min-files", type=int, default=3)
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory. Defaults to audit_dir/burst_cleanup",
    )
    return parser.parse_args()


def read_file_rows(audit_dir: Path) -> list[dict]:
    with (audit_dir / "files.csv").open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_date(row: dict) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(row["date"])
    except (KeyError, ValueError):
        return None


def find_groups(rows: list[dict], max_gap_seconds: float, min_files: int) -> list[list[dict]]:
    image_rows: list[dict] = []
    for row in rows:
        if row.get("kind") != "image" or row.get("date_source") != "filename":
            continue
        parsed = parse_date(row)
        if not parsed:
            continue
        row = dict(row)
        row["_parsed_date"] = parsed
        image_rows.append(row)

    image_rows.sort(key=lambda r: (r["_parsed_date"], r["rel_path"]))
    groups: list[list[dict]] = []
    current: list[dict] = []
    last_date: dt.datetime | None = None
    for row in image_rows:
        row_date = row["_parsed_date"]
        if last_date is None or (row_date - last_date).total_seconds() <= max_gap_seconds:
            current.append(row)
        else:
            if len(current) >= min_files:
                groups.append(current)
            current = [row]
        last_date = row_date
    if len(current) >= min_files:
        groups.append(current)
    return groups


def choose_keep(group: list[dict]) -> dict:
    # Byte size is a crude proxy for detail when camera/settings are similar.
    # Prefer it only as an auto-review starting point, not a final aesthetic call.
    return max(group, key=lambda r: (int(r.get("size") or 0), r["_parsed_date"], r["rel_path"]))


def human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in fieldnames} for row in rows])


def make_plan(groups: list[list[dict]]) -> tuple[list[dict], list[dict]]:
    plan_rows: list[dict] = []
    group_rows: list[dict] = []
    for group_index, group in enumerate(groups, start=1):
        keep = choose_keep(group)
        start = group[0]["_parsed_date"]
        end = group[-1]["_parsed_date"]
        group_bytes = sum(int(row.get("size") or 0) for row in group)
        reject_bytes = group_bytes - int(keep.get("size") or 0)
        group_rows.append(
            {
                "burst_group": group_index,
                "year": start.strftime("%Y"),
                "start": start.isoformat(sep=" "),
                "end": end.isoformat(sep=" "),
                "span_seconds": int((end - start).total_seconds()),
                "files": len(group),
                "group_size": group_bytes,
                "group_size_human": human_size(group_bytes),
                "auto_keep_rel_path": keep["rel_path"],
                "candidate_reject_files": len(group) - 1,
                "candidate_reject_size": reject_bytes,
                "candidate_reject_size_human": human_size(reject_bytes),
            }
        )
        for row in group:
            action = "auto_keep" if row is keep else "candidate_quarantine"
            plan_rows.append(
                {
                    "burst_group": group_index,
                    "action": action,
                    "date": row["date"],
                    "size": row["size"],
                    "size_human": human_size(int(row.get("size") or 0)),
                    "rel_path": row["rel_path"],
                    "abs_path": row["abs_path"],
                    "auto_keep_rel_path": keep["rel_path"],
                    "group_start": start.isoformat(sep=" "),
                    "group_end": end.isoformat(sep=" "),
                    "span_seconds": int((end - start).total_seconds()),
                    "files_in_group": len(group),
                }
            )
    return group_rows, plan_rows


def file_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def make_review_pages(output_dir: Path, group_rows: list[dict], plan_rows: list[dict], summary: dict) -> None:
    review_dir = output_dir / "review_html"
    review_dir.mkdir(parents=True, exist_ok=True)

    rows_by_group: dict[int, list[dict]] = defaultdict(list)
    for row in plan_rows:
        rows_by_group[int(row["burst_group"])].append(row)

    groups_by_year: dict[str, list[dict]] = defaultdict(list)
    for row in group_rows:
        groups_by_year[str(row["year"])].append(row)

    css = """
body {
  color: #17202a;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  line-height: 1.4;
  margin: 28px;
}
a { color: #0f5b99; }
.metrics {
  display: grid;
  gap: 10px;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  margin: 16px 0 22px;
}
.metric {
  border: 1px solid #d9e2ec;
  border-radius: 8px;
  padding: 12px;
}
.metric strong { display: block; font-size: 24px; }
.group {
  border-top: 1px solid #bcccdc;
  padding: 18px 0 20px;
}
.group h2 { font-size: 18px; margin: 0 0 10px; }
.frames {
  display: grid;
  gap: 10px;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
}
.frame {
  border: 2px solid #d9e2ec;
  border-radius: 8px;
  overflow: hidden;
}
.frame.keep { border-color: #248232; }
.frame.reject { opacity: .72; }
.frame img {
  aspect-ratio: 1 / 1;
  background: #f0f4f8;
  display: block;
  object-fit: cover;
  width: 100%;
}
.meta {
  font-size: 12px;
  padding: 7px;
  word-break: break-word;
}
.badge {
  background: #d9f99d;
  border-radius: 999px;
  display: inline-block;
  font-size: 11px;
  margin-bottom: 4px;
  padding: 2px 7px;
}
.reject .badge { background: #fee2e2; }
code {
  background: #f0f4f8;
  border-radius: 4px;
  padding: 2px 4px;
}
table {
  border-collapse: collapse;
  width: 100%;
}
th, td {
  border-bottom: 1px solid #d9e2ec;
  padding: 7px 8px;
  text-align: left;
}
th { background: #f0f4f8; }
"""

    index_rows = []
    for year, groups in sorted(groups_by_year.items()):
        reject_files = sum(int(g["candidate_reject_files"]) for g in groups)
        reject_size = sum(int(g["candidate_reject_size"]) for g in groups)
        index_rows.append(
            f"<tr><td><a href=\"bursts_{html.escape(year)}.html\">{html.escape(year)}</a></td>"
            f"<td>{len(groups)}</td><td>{reject_files}</td><td>{human_size(reject_size)}</td></tr>"
        )

    index_html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Burst Cleanup Review</title>
  <style>{css}</style>
</head>
<body>
  <h1>Burst Cleanup Review</h1>
  <p>This is a dry-run review. It does not move or delete source files.</p>
  <p>Auto-keep is chosen by largest file size inside each fast sequence. Treat it as a shortcut, not a final judgment.</p>
  <div class="metrics">
    <div class="metric"><strong>{summary["burst_groups"]}</strong>Burst groups</div>
    <div class="metric"><strong>{summary["burst_files"]}</strong>Files in bursts</div>
    <div class="metric"><strong>{summary["candidate_reject_files"]}</strong>Candidate rejects</div>
    <div class="metric"><strong>{summary["candidate_reject_size_human"]}</strong>Candidate space</div>
  </div>
  <table>
    <thead><tr><th>Year</th><th>Groups</th><th>Candidate rejects</th><th>Candidate space</th></tr></thead>
    <tbody>{''.join(index_rows)}</tbody>
  </table>
</body>
</html>
"""
    (review_dir / "index.html").write_text(index_html, encoding="utf-8")

    for year, groups in sorted(groups_by_year.items()):
        pieces = [
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
            f"<title>Burst Review {html.escape(year)}</title><style>{css}</style></head><body>",
            f"<p><a href=\"index.html\">Back to index</a></p><h1>Burst Review {html.escape(year)}</h1>",
        ]
        for group in groups:
            group_id = int(group["burst_group"])
            frames = rows_by_group[group_id]
            pieces.append(
                f"<section class=\"group\"><h2>Group {group_id} · {html.escape(group['start'])}"
                f" · {group['files']} files · candidate space {html.escape(group['candidate_reject_size_human'])}</h2>"
                f"<div class=\"frames\">"
            )
            for frame in frames:
                is_keep = frame["action"] == "auto_keep"
                css_class = "frame keep" if is_keep else "frame reject"
                badge = "auto keep" if is_keep else "candidate quarantine"
                uri = file_uri(frame["abs_path"])
                pieces.append(
                    f"<article class=\"{css_class}\">"
                    f"<a href=\"{html.escape(uri)}\"><img loading=\"lazy\" src=\"{html.escape(uri)}\" alt=\"\"></a>"
                    f"<div class=\"meta\"><span class=\"badge\">{badge}</span><br>"
                    f"{html.escape(frame['date'])}<br>{html.escape(frame['size_human'])}<br>"
                    f"{html.escape(frame['rel_path'])}</div></article>"
                )
            pieces.append("</div></section>")
        pieces.append("</body></html>")
        (review_dir / f"bursts_{year}.html").write_text("".join(pieces), encoding="utf-8")


def write_summary(output_dir: Path, summary: dict) -> None:
    text = f"""# Burst Cleanup Plan

This is a dry-run plan. It did not move, edit, or delete source files.

## Settings

- Max gap between consecutive photos: {summary["max_gap_seconds"]} seconds
- Minimum files per burst: {summary["min_files"]}
- Auto-keep rule: largest file size in each burst group

## Result

- Burst groups: {summary["burst_groups"]}
- Files in burst groups: {summary["burst_files"]}
- Auto-kept files: {summary["auto_keep_files"]}
- Candidate quarantine files: {summary["candidate_reject_files"]}
- Candidate quarantine size: {summary["candidate_reject_size_human"]}

## Files

- `burst_groups.csv` - one row per burst group.
- `burst_plan.csv` - one row per file in a burst, with `auto_keep` or `candidate_quarantine`.
- `review_html/index.html` - visual review pages by year.
"""
    (output_dir / "summary.md").write_text(text, encoding="utf-8")


def main() -> int:
    args = parse_args()
    audit_dir = Path(args.audit_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else audit_dir / "burst_cleanup"
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = read_file_rows(audit_dir)
    groups = find_groups(rows, args.max_gap_seconds, args.min_files)
    group_rows, plan_rows = make_plan(groups)
    candidate_reject_size = sum(
        int(row.get("size") or 0) for row in plan_rows if row["action"] == "candidate_quarantine"
    )
    action_counts = Counter(row["action"] for row in plan_rows)
    summary = {
        "audit_dir": str(audit_dir),
        "output_dir": str(output_dir),
        "generated_at": dt.datetime.now().replace(microsecond=0).isoformat(sep=" "),
        "max_gap_seconds": args.max_gap_seconds,
        "min_files": args.min_files,
        "burst_groups": len(group_rows),
        "burst_files": len(plan_rows),
        "auto_keep_files": action_counts["auto_keep"],
        "candidate_reject_files": action_counts["candidate_quarantine"],
        "candidate_reject_size": candidate_reject_size,
        "candidate_reject_size_human": human_size(candidate_reject_size),
    }

    write_csv(
        output_dir / "burst_groups.csv",
        group_rows,
        [
            "burst_group",
            "year",
            "start",
            "end",
            "span_seconds",
            "files",
            "group_size",
            "group_size_human",
            "auto_keep_rel_path",
            "candidate_reject_files",
            "candidate_reject_size",
            "candidate_reject_size_human",
        ],
    )
    write_csv(
        output_dir / "burst_plan.csv",
        plan_rows,
        [
            "burst_group",
            "action",
            "date",
            "size",
            "size_human",
            "rel_path",
            "abs_path",
            "auto_keep_rel_path",
            "group_start",
            "group_end",
            "span_seconds",
            "files_in_group",
        ],
    )
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_summary(output_dir, summary)
    make_review_pages(output_dir, group_rows, plan_rows, summary)
    print(output_dir / "summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
