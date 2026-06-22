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
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    parser.add_argument(
        "--sharpness",
        action="store_true",
        help="Score files with ImageMagick and choose auto-keep by sharpness.",
    )
    parser.add_argument("--magick", default="magick", help="ImageMagick executable")
    parser.add_argument("--sharpness-resize", type=int, default=768)
    parser.add_argument("--sharpness-timeout", type=float, default=20.0)
    parser.add_argument("--sharpness-workers", type=int, default=4)
    parser.add_argument(
        "--sharpness-hydration",
        choices=("local-only", "download"),
        default="local-only",
        help="local-only skips cloud placeholders; download may trigger Dropbox downloads.",
    )
    parser.add_argument(
        "--local-block-ratio",
        type=float,
        default=0.8,
        help="Minimum allocated-blocks/size ratio considered locally available.",
    )
    parser.add_argument(
        "--sharpness-cache",
        default=None,
        help="JSON cache path. Defaults to output_dir/sharpness_cache.json.",
    )
    parser.add_argument("--progress-every", type=int, default=250)
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


def choose_keep(group: list[dict], auto_keep_rule: str) -> tuple[dict, str]:
    if auto_keep_rule == "sharpness":
        scored = [row for row in group if row.get("_sharpness_score") is not None]
        if scored:
            return (
                max(
                    scored,
                    key=lambda r: (
                        float(r["_sharpness_score"]),
                        int(r.get("size") or 0),
                        r["_parsed_date"],
                        r["rel_path"],
                    ),
                ),
                "sharpness",
            )
    # Byte size is a crude proxy for detail when camera/settings are similar.
    # Prefer it only as an auto-review starting point, not a final aesthetic call.
    return (
        max(group, key=lambda r: (int(r.get("size") or 0), r["_parsed_date"], r["rel_path"])),
        "largest_file",
    )


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def sharpness_cache_key(path: Path, resize: int) -> str:
    try:
        stat = path.stat()
        blocks = getattr(stat, "st_blocks", "")
        fingerprint = f"{path}|{stat.st_size}|{stat.st_mtime_ns}|{blocks}|{resize}"
    except OSError:
        fingerprint = f"{path}|missing|{resize}"
    return hashlib_sha1(fingerprint)


def local_block_ratio(path: Path) -> float:
    try:
        stat = path.stat()
    except OSError:
        return 0.0
    if stat.st_size <= 0:
        return 1.0
    blocks = getattr(stat, "st_blocks", 0)
    return (blocks * 512) / stat.st_size


def is_probably_local(path: Path, min_ratio: float) -> bool:
    return local_block_ratio(path) >= min_ratio


def hashlib_sha1(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def compute_sharpness(
    magick: str,
    path: Path,
    resize: int,
    timeout: float,
) -> tuple[float | None, str]:
    if not path.exists():
        return None, "file_missing"
    command = [
        magick,
        str(path),
        "-auto-orient",
        "-resize",
        f"{resize}x{resize}>",
        "-colorspace",
        "Gray",
        "-define",
        "convolve:scale=!",
        "-morphology",
        "Convolve",
        "Laplacian",
        "-format",
        "%[fx:standard_deviation]",
        "info:",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError as exc:
        return None, str(exc)
    if result.returncode != 0:
        error = (result.stderr or result.stdout or f"exit_{result.returncode}").strip()
        return None, error[:240]
    try:
        return float(result.stdout.strip()), ""
    except ValueError:
        return None, f"bad_output: {result.stdout.strip()[:80]}"


def score_sharpness_for_groups(
    groups: list[list[dict]],
    magick: str,
    cache_path: Path,
    resize: int,
    timeout: float,
    workers: int,
    hydration: str,
    min_local_ratio: float,
    progress_every: int,
) -> dict:
    magick_path = shutil.which(magick)
    if not magick_path:
        raise SystemExit(f"ImageMagick executable not found: {magick}")

    cache = load_json(cache_path)
    unique_rows: dict[str, dict] = {}
    for group in groups:
        for row in group:
            unique_rows[row["abs_path"]] = row

    cache_hits = 0
    skipped_cloud_only = 0
    pending: list[tuple[dict, Path, str]] = []
    for index, row in enumerate(unique_rows.values(), start=1):
        path = Path(row["abs_path"])
        key = sharpness_cache_key(path, resize)
        cached = cache.get(key)
        if cached:
            row["_sharpness_score"] = cached.get("score")
            row["_sharpness_error"] = cached.get("error") or ""
            cache_hits += 1
        elif hydration == "local-only" and not is_probably_local(path, min_local_ratio):
            ratio = local_block_ratio(path)
            row["_sharpness_score"] = None
            row["_sharpness_error"] = "cloud_only_not_downloaded"
            cache[key] = {
                "path": str(path),
                "resize": resize,
                "score": None,
                "error": "cloud_only_not_downloaded",
                "local_block_ratio": ratio,
            }
            skipped_cloud_only += 1
        else:
            pending.append((row, path, key))

    computed = 0
    errors = 0
    completed = cache_hits + skipped_cloud_only
    workers = max(1, workers)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(compute_sharpness, magick_path, path, resize, timeout): (row, path, key)
            for row, path, key in pending
        }
        for future in as_completed(future_map):
            row, path, key = future_map[future]
            score, error = future.result()
            row["_sharpness_score"] = score
            row["_sharpness_error"] = error
            cache[key] = {
                "path": str(path),
                "resize": resize,
                "score": score,
                "error": error,
                "local_block_ratio": local_block_ratio(path),
            }
            computed += 1
            completed += 1
            if error:
                errors += 1
            if progress_every and completed % progress_every == 0:
                write_json(cache_path, cache)
                print(
                    f"Sharpness scored {completed}/{len(unique_rows)} files "
                    f"({cache_hits} cached, {computed} computed, "
                    f"{skipped_cloud_only} cloud-only skipped, {errors} errors)...",
                    file=sys.stderr,
                )

    if progress_every and completed and completed % progress_every != 0:
        print(
            f"Sharpness scored {completed}/{len(unique_rows)} files "
            f"({cache_hits} cached, {computed} computed, "
            f"{skipped_cloud_only} cloud-only skipped, {errors} errors).",
            file=sys.stderr,
        )

    for row in unique_rows.values():
        if "_sharpness_score" not in row:
            row["_sharpness_score"] = None
            row["_sharpness_error"] = "not_scored"

    if cache_hits:
        if progress_every:
            write_json(cache_path, cache)

    write_json(cache_path, cache)
    return {
        "sharpness_cache": str(cache_path),
        "sharpness_files": len(unique_rows),
        "sharpness_cache_hits": cache_hits,
        "sharpness_computed": computed,
        "sharpness_errors": sum(
            1
            for row in unique_rows.values()
            if row.get("_sharpness_error")
            and row.get("_sharpness_error") != "cloud_only_not_downloaded"
        ),
        "sharpness_cloud_only_skipped": sum(
            1
            for row in unique_rows.values()
            if row.get("_sharpness_error") == "cloud_only_not_downloaded"
        ),
        "sharpness_hydration": hydration,
    }


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


def make_plan(groups: list[list[dict]], auto_keep_rule: str) -> tuple[list[dict], list[dict]]:
    plan_rows: list[dict] = []
    group_rows: list[dict] = []
    for group_index, group in enumerate(groups, start=1):
        keep, keep_reason = choose_keep(group, auto_keep_rule)
        start = group[0]["_parsed_date"]
        end = group[-1]["_parsed_date"]
        group_bytes = sum(int(row.get("size") or 0) for row in group)
        reject_bytes = group_bytes - int(keep.get("size") or 0)
        keep_score = keep.get("_sharpness_score")
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
                "auto_keep_rule": keep_reason,
                "auto_keep_sharpness_score": keep_score if keep_score is not None else "",
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
                    "auto_keep_rule": keep_reason,
                    "sharpness_score": row.get("_sharpness_score")
                    if row.get("_sharpness_score") is not None
                    else "",
                    "sharpness_error": row.get("_sharpness_error") or "",
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
  <p>Auto-keep rule: {html.escape(summary["auto_keep_rule_label"])}. Treat it as a shortcut, not a final judgment.</p>
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
                sharpness_line = ""
                if frame.get("sharpness_score"):
                    sharpness_line = f"<br>sharpness {html.escape(str(frame['sharpness_score']))}"
                elif frame.get("sharpness_error"):
                    sharpness_line = f"<br>sharpness error: {html.escape(str(frame['sharpness_error']))}"
                pieces.append(
                    f"<article class=\"{css_class}\">"
                    f"<a href=\"{html.escape(uri)}\"><img loading=\"lazy\" src=\"{html.escape(uri)}\" alt=\"\"></a>"
                    f"<div class=\"meta\"><span class=\"badge\">{badge}</span><br>"
                    f"{html.escape(frame['date'])}<br>{html.escape(frame['size_human'])}{sharpness_line}<br>"
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
- Auto-keep rule: {summary["auto_keep_rule_label"]}

## Result

- Burst groups: {summary["burst_groups"]}
- Files in burst groups: {summary["burst_files"]}
- Auto-kept files: {summary["auto_keep_files"]}
- Candidate quarantine files: {summary["candidate_reject_files"]}
- Candidate quarantine size: {summary["candidate_reject_size_human"]}
- Sharpness files considered: {summary.get("sharpness_files", 0)}
- Sharpness hydration mode: {summary.get("sharpness_hydration", "")}
- Sharpness cache hits: {summary.get("sharpness_cache_hits", 0)}
- Sharpness newly computed: {summary.get("sharpness_computed", 0)}
- Sharpness cloud-only skipped: {summary.get("sharpness_cloud_only_skipped", 0)}
- Sharpness errors: {summary.get("sharpness_errors", 0)}

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
    auto_keep_rule = "sharpness" if args.sharpness else "largest_file"
    score_stats: dict = {}
    if args.sharpness:
        cache_path = (
            Path(args.sharpness_cache).expanduser().resolve()
            if args.sharpness_cache
            else output_dir / "sharpness_cache.json"
        )
        print(f"Scoring sharpness with ImageMagick; cache: {cache_path}", file=sys.stderr)
        score_stats = score_sharpness_for_groups(
            groups,
            args.magick,
            cache_path,
            args.sharpness_resize,
            args.sharpness_timeout,
            args.sharpness_workers,
            args.sharpness_hydration,
            args.local_block_ratio,
            args.progress_every,
        )
    group_rows, plan_rows = make_plan(groups, auto_keep_rule)
    candidate_reject_size = sum(
        int(row.get("size") or 0) for row in plan_rows if row["action"] == "candidate_quarantine"
    )
    action_counts = Counter(row["action"] for row in plan_rows)
    auto_keep_rule_label = (
        f"sharpness score via ImageMagick Laplacian (resize {args.sharpness_resize}px)"
        if args.sharpness
        else "largest file size in each burst group"
    )
    summary = {
        "audit_dir": str(audit_dir),
        "output_dir": str(output_dir),
        "generated_at": dt.datetime.now().replace(microsecond=0).isoformat(sep=" "),
        "max_gap_seconds": args.max_gap_seconds,
        "min_files": args.min_files,
        "auto_keep_rule": auto_keep_rule,
        "auto_keep_rule_label": auto_keep_rule_label,
        "sharpness_hydration": args.sharpness_hydration if args.sharpness else "",
        "burst_groups": len(group_rows),
        "burst_files": len(plan_rows),
        "auto_keep_files": action_counts["auto_keep"],
        "candidate_reject_files": action_counts["candidate_quarantine"],
        "candidate_reject_size": candidate_reject_size,
        "candidate_reject_size_human": human_size(candidate_reject_size),
        **score_stats,
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
            "auto_keep_rule",
            "auto_keep_sharpness_score",
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
            "auto_keep_rule",
            "sharpness_score",
            "sharpness_error",
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
