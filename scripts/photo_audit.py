#!/usr/bin/env python3
"""
Read-only photo folder audit.

The script scans a source folder, writes reports to an output folder, and never
modifies source files. It uses only Python's standard library so it can run on a
plain macOS install.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


IMAGE_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".heic",
    ".heif",
    ".webp",
    ".tif",
    ".tiff",
    ".bmp",
    ".dng",
    ".cr2",
    ".cr3",
    ".nef",
    ".arw",
    ".raf",
    ".rw2",
}

VIDEO_EXTS = {
    ".mov",
    ".mp4",
    ".m4v",
    ".avi",
    ".mkv",
    ".3gp",
    ".mts",
    ".m2ts",
    ".webm",
}

SIDECAR_EXTS = {".aae", ".json", ".xmp", ".thm", ".xml"}

DOC_EXTS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".txt",
    ".rtf",
    ".csv",
}

DATE_PATTERNS = [
    # Dropbox Camera Uploads: 2019-08-14 12.34.56.jpg
    re.compile(
        r"(?P<y>19\d{2}|20\d{2})[-_. ](?P<m>\d{2})[-_. ](?P<d>\d{2})"
        r"(?:[ T_-]+(?P<h>\d{2})[.:_-](?P<mi>\d{2})(?:[.:_-](?P<s>\d{2}))?)?"
    ),
    # Phone names: IMG_20190814_123456.jpg, PXL_20240511_181910123.jpg
    re.compile(
        r"(?P<y>19\d{2}|20\d{2})(?P<m>\d{2})(?P<d>\d{2})"
        r"(?:[_-]?(?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2}))?"
    ),
]

COPY_SUFFIX_RE = re.compile(
    r"(?i)(?:\s*\(\d+\)|\s*-\s*copy(?:\s*\d+)?|\s+copy(?:\s*\d+)?|_copy(?:_\d+)?)$"
)


@dataclass
class FileRow:
    rel_path: str
    abs_path: str
    ext: str
    kind: str
    size: int
    mtime: str
    date: str
    date_source: str
    year_month: str
    is_screenshot: bool
    is_camera_name: bool
    read_error: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only photo folder audit")
    parser.add_argument("source", help="Folder to scan")
    parser.add_argument(
        "--output",
        default="outputs/photo_audit",
        help="Output folder for CSV/HTML/JSON reports",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=1000,
        help="Print progress after this many files",
    )
    parser.add_argument(
        "--large-video-mb",
        type=int,
        default=500,
        help="Threshold for large_videos.csv",
    )
    return parser.parse_args()


def classify(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in SIDECAR_EXTS:
        return "sidecar"
    if ext in DOC_EXTS:
        return "document"
    return "other"


def parse_date_from_name(name: str) -> dt.datetime | None:
    for pattern in DATE_PATTERNS:
        match = pattern.search(name)
        if not match:
            continue
        parts = match.groupdict()
        try:
            return dt.datetime(
                int(parts["y"]),
                int(parts["m"]),
                int(parts["d"]),
                int(parts.get("h") or 0),
                int(parts.get("mi") or 0),
                int(parts.get("s") or 0),
            )
        except ValueError:
            continue
    return None


def iso_from_timestamp(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp).replace(microsecond=0).isoformat(sep=" ")


def is_screenshot(path: Path) -> bool:
    name = path.stem.lower()
    return any(
        marker in name
        for marker in (
            "screenshot",
            "screen shot",
            "screen_shot",
            "screen-shot",
            "screencapture",
            "снимок экрана",
        )
    )


def is_camera_name(path: Path) -> bool:
    stem = path.stem.lower()
    return bool(
        re.search(r"^(img|vid|dsc|dscf|pxl|wp|mvimg|photo|video)[-_]?\d", stem)
        or re.search(r"^(19\d{2}|20\d{2})[-_. ]\d{2}[-_. ]\d{2}", stem)
    )


def normalize_stem(path: Path) -> str:
    stem = path.stem.strip().lower()
    stem = COPY_SUFFIX_RE.sub("", stem)
    stem = re.sub(r"(?i)^img_e(?=\d)", "img_", stem)
    stem = re.sub(r"[^a-z0-9а-яё]+", "", stem)
    return stem


def walk_files(source: Path) -> Iterable[Path]:
    for root, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs if d not in {".DS_Store", "__MACOSX"}]
        for filename in files:
            if filename == ".DS_Store":
                continue
            yield Path(root) / filename


def sha256_file(path: Path) -> tuple[str, str]:
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest(), ""
    except OSError as exc:
        return "", str(exc)


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
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def build_rows(source: Path, progress_every: int) -> tuple[list[FileRow], list[Path]]:
    rows: list[FileRow] = []
    raw_paths: list[Path] = []
    for index, path in enumerate(walk_files(source), start=1):
        read_error = ""
        try:
            stat = path.stat()
        except OSError as exc:
            stat = None
            read_error = str(exc)

        size = stat.st_size if stat else 0
        mtime = iso_from_timestamp(stat.st_mtime) if stat else ""
        parsed_date = parse_date_from_name(path.name)
        if parsed_date:
            file_date = parsed_date.isoformat(sep=" ")
            date_source = "filename"
        else:
            file_date = mtime
            date_source = "filesystem_mtime" if mtime else "unknown"
        year_month = file_date[:7] if file_date else "unknown"
        kind = classify(path)
        rows.append(
            FileRow(
                rel_path=str(path.relative_to(source)),
                abs_path=str(path),
                ext=path.suffix.lower(),
                kind=kind,
                size=size,
                mtime=mtime,
                date=file_date,
                date_source=date_source,
                year_month=year_month,
                is_screenshot=is_screenshot(path),
                is_camera_name=is_camera_name(path),
                read_error=read_error,
            )
        )
        raw_paths.append(path)
        if progress_every and index % progress_every == 0:
            print(f"Scanned {index} files...", file=sys.stderr)
    return rows, raw_paths


def duplicate_rows(raw_paths: list[Path], rows: list[FileRow]) -> tuple[list[dict], list[dict]]:
    by_size: dict[int, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        if row.size > 0:
            by_size[row.size].append(i)

    hash_rows: list[dict] = []
    duplicates: list[dict] = []
    group_no = 0
    for size, indexes in sorted(by_size.items()):
        if len(indexes) < 2:
            continue
        by_hash: dict[str, list[int]] = defaultdict(list)
        for index in indexes:
            digest, error = sha256_file(raw_paths[index])
            hash_rows.append(
                {
                    "sha256": digest,
                    "size": size,
                    "size_human": human_size(size),
                    "rel_path": rows[index].rel_path,
                    "read_error": error,
                }
            )
            if digest:
                by_hash[digest].append(index)
        for digest, hash_indexes in by_hash.items():
            if len(hash_indexes) < 2:
                continue
            group_no += 1
            for index in hash_indexes:
                duplicates.append(
                    {
                        "duplicate_group": group_no,
                        "sha256": digest,
                        "size": size,
                        "size_human": human_size(size),
                        "kind": rows[index].kind,
                        "date": rows[index].date,
                        "rel_path": rows[index].rel_path,
                        "abs_path": rows[index].abs_path,
                    }
                )
    return duplicates, hash_rows


def similar_name_rows(rows: list[FileRow]) -> list[dict]:
    groups: dict[tuple[str, str], list[FileRow]] = defaultdict(list)
    for row in rows:
        if row.kind not in {"image", "video"}:
            continue
        key = (normalize_stem(Path(row.rel_path)), row.ext)
        if key[0]:
            groups[key].append(row)

    out: list[dict] = []
    group_no = 0
    for (stem, ext), group in sorted(groups.items()):
        if len(group) < 2:
            continue
        group_no += 1
        for row in sorted(group, key=lambda r: r.rel_path):
            out.append(
                {
                    "similar_group": group_no,
                    "normalized_name": stem,
                    "ext": ext,
                    "size": row.size,
                    "size_human": human_size(row.size),
                    "date": row.date,
                    "rel_path": row.rel_path,
                    "abs_path": row.abs_path,
                }
            )
    return out


def rapid_series_rows(rows: list[FileRow]) -> list[dict]:
    groups: dict[str, list[FileRow]] = defaultdict(list)
    for row in rows:
        if row.kind != "image" or row.date_source == "unknown":
            continue
        minute = row.date[:16]
        groups[minute].append(row)

    out: list[dict] = []
    group_no = 0
    for minute, group in sorted(groups.items()):
        if len(group) < 5:
            continue
        group_no += 1
        for row in sorted(group, key=lambda r: r.rel_path):
            out.append(
                {
                    "series_group": group_no,
                    "minute": minute,
                    "count_in_minute": len(group),
                    "size": row.size,
                    "size_human": human_size(row.size),
                    "rel_path": row.rel_path,
                    "abs_path": row.abs_path,
                }
            )
    return out


def make_html_report(
    output: Path,
    source: Path,
    rows: list[FileRow],
    duplicates: list[dict],
    similar: list[dict],
    rapid: list[dict],
    large_videos: list[dict],
    summary: dict,
) -> None:
    by_month = Counter(row.year_month for row in rows if row.kind in {"image", "video"})
    kind_counts = Counter(row.kind for row in rows)

    def table(headers: list[str], data: list[dict], limit: int = 50) -> str:
        if not data:
            return "<p>No rows.</p>"
        head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
        body = []
        for row in data[:limit]:
            cells = "".join(
                f"<td>{html.escape(str(row.get(h, '')))}</td>" for h in headers
            )
            body.append(f"<tr>{cells}</tr>")
        more = ""
        if len(data) > limit:
            more = f"<p>Showing {limit} of {len(data)} rows. See CSV for all rows.</p>"
        return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>{more}"

    month_rows = [
        {"month": month, "files": count}
        for month, count in sorted(by_month.items(), reverse=True)
    ]
    kind_rows = [
        {"kind": kind, "files": count}
        for kind, count in sorted(kind_counts.items(), key=lambda item: (-item[1], item[0]))
    ]

    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Photo Audit Report</title>
  <style>
    body {{
      color: #1f2933;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
      margin: 32px;
      max-width: 1180px;
    }}
    h1, h2 {{ color: #102a43; }}
    .metrics {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      margin: 20px 0;
    }}
    .metric {{
      border: 1px solid #d9e2ec;
      border-radius: 8px;
      padding: 14px;
    }}
    .metric strong {{
      display: block;
      font-size: 26px;
      line-height: 1.1;
    }}
    table {{
      border-collapse: collapse;
      font-size: 13px;
      margin: 12px 0 28px;
      width: 100%;
    }}
    th, td {{
      border-bottom: 1px solid #d9e2ec;
      padding: 7px 8px;
      text-align: left;
      vertical-align: top;
      word-break: break-word;
    }}
    th {{ background: #f0f4f8; }}
    code {{
      background: #f0f4f8;
      border-radius: 4px;
      padding: 2px 4px;
    }}
  </style>
</head>
<body>
  <h1>Photo Audit Report</h1>
  <p>Source: <code>{html.escape(str(source))}</code></p>
  <p>Generated: <code>{html.escape(summary["generated_at"])}</code></p>
  <div class="metrics">
    <div class="metric"><strong>{summary["total_files"]}</strong>Total files</div>
    <div class="metric"><strong>{summary["media_files"]}</strong>Photos/videos</div>
    <div class="metric"><strong>{summary["total_size_human"]}</strong>Total size</div>
    <div class="metric"><strong>{summary["duplicate_groups"]}</strong>Exact duplicate groups</div>
    <div class="metric"><strong>{summary["duplicate_files"]}</strong>Files in exact duplicate groups</div>
    <div class="metric"><strong>{summary["filename_date_missing"]}</strong>No date in filename</div>
    <div class="metric"><strong>{summary["screenshots"]}</strong>Screenshots</div>
    <div class="metric"><strong>{summary["large_videos"]}</strong>Large videos</div>
  </div>

  <h2>Counts by type</h2>
  {table(["kind", "files"], kind_rows, limit=20)}

  <h2>Photos and videos by month</h2>
  {table(["month", "files"], month_rows, limit=80)}

  <h2>Exact duplicate candidates</h2>
  {table(["duplicate_group", "size_human", "kind", "date", "rel_path"], duplicates)}

  <h2>Similar name groups</h2>
  {table(["similar_group", "normalized_name", "ext", "size_human", "date", "rel_path"], similar)}

  <h2>Rapid image series</h2>
  {table(["series_group", "minute", "count_in_minute", "size_human", "rel_path"], rapid)}

  <h2>Large videos</h2>
  {table(["size_human", "date", "rel_path"], large_videos)}
</body>
</html>
"""
    (output / "report.html").write_text(html_text, encoding="utf-8")


def main() -> int:
    args = parse_args()
    source = Path(args.source).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    if not source.exists() or not source.is_dir():
        print(f"Source folder does not exist: {source}", file=sys.stderr)
        return 2

    output.mkdir(parents=True, exist_ok=True)
    print(f"Scanning: {source}", file=sys.stderr)
    rows, raw_paths = build_rows(source, args.progress_every)
    print(f"Found {len(rows)} files. Checking exact duplicates by size/hash...", file=sys.stderr)
    duplicates, hash_rows = duplicate_rows(raw_paths, rows)
    similar = similar_name_rows(rows)
    rapid = rapid_series_rows(rows)
    large_threshold = args.large_video_mb * 1024 * 1024
    large_videos = [
        {
            "size": row.size,
            "size_human": human_size(row.size),
            "date": row.date,
            "rel_path": row.rel_path,
            "abs_path": row.abs_path,
        }
        for row in rows
        if row.kind == "video" and row.size >= large_threshold
    ]
    large_videos.sort(key=lambda row: row["size"], reverse=True)

    total_size = sum(row.size for row in rows)
    media_files = sum(1 for row in rows if row.kind in {"image", "video"})
    duplicate_groups = len({row["duplicate_group"] for row in duplicates})
    summary = {
        "generated_at": dt.datetime.now().replace(microsecond=0).isoformat(sep=" "),
        "source": str(source),
        "output": str(output),
        "total_files": len(rows),
        "media_files": media_files,
        "total_size": total_size,
        "total_size_human": human_size(total_size),
        "duplicate_groups": duplicate_groups,
        "duplicate_files": len(duplicates),
        "duplicate_bytes_if_keep_one_each": sum(
            (len(group_rows) - 1) * group_rows[0]["size"]
            for _, group_rows in group_duplicate_rows(duplicates).items()
        ),
        "filename_date_missing": sum(1 for row in rows if row.date_source != "filename"),
        "screenshots": sum(1 for row in rows if row.is_screenshot),
        "large_videos": len(large_videos),
        "similar_name_rows": len(similar),
        "rapid_series_rows": len(rapid),
        "read_errors": sum(1 for row in rows if row.read_error),
    }
    summary["duplicate_size_human_if_keep_one_each"] = human_size(
        summary["duplicate_bytes_if_keep_one_each"]
    )

    write_csv(output / "files.csv", [asdict(row) for row in rows], list(asdict(rows[0]).keys()) if rows else [])
    write_csv(
        output / "duplicates.csv",
        duplicates,
        ["duplicate_group", "sha256", "size", "size_human", "kind", "date", "rel_path", "abs_path"],
    )
    write_csv(
        output / "hashed_same_size_files.csv",
        hash_rows,
        ["sha256", "size", "size_human", "rel_path", "read_error"],
    )
    write_csv(
        output / "similar_names.csv",
        similar,
        ["similar_group", "normalized_name", "ext", "size", "size_human", "date", "rel_path", "abs_path"],
    )
    write_csv(
        output / "rapid_series.csv",
        rapid,
        ["series_group", "minute", "count_in_minute", "size", "size_human", "rel_path", "abs_path"],
    )
    write_csv(
        output / "large_videos.csv",
        large_videos,
        ["size", "size_human", "date", "rel_path", "abs_path"],
    )
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_summary_md(output, summary)
    make_html_report(output, source, rows, duplicates, similar, rapid, large_videos, summary)
    print(f"Done. Report: {output / 'report.html'}", file=sys.stderr)
    return 0


def group_duplicate_rows(duplicates: list[dict]) -> dict[int, list[dict]]:
    groups: dict[int, list[dict]] = defaultdict(list)
    for row in duplicates:
        groups[int(row["duplicate_group"])].append(row)
    return groups


def write_summary_md(output: Path, summary: dict) -> None:
    text = f"""# Photo Audit Summary

Source: `{summary["source"]}`

Generated: `{summary["generated_at"]}`

This audit is read-only. It did not move, edit, or delete source files.

## Top-level numbers

- Total files: {summary["total_files"]}
- Photos/videos: {summary["media_files"]}
- Total size: {summary["total_size_human"]}
- Exact duplicate groups: {summary["duplicate_groups"]}
- Files in exact duplicate groups: {summary["duplicate_files"]}
- Potential duplicate space if one file per exact group is kept: {summary["duplicate_size_human_if_keep_one_each"]}
- Files without a date parsed from filename: {summary["filename_date_missing"]}
- Screenshots: {summary["screenshots"]}
- Large videos: {summary["large_videos"]}
- Similar-name rows: {summary["similar_name_rows"]}
- Rapid-series rows: {summary["rapid_series_rows"]}
- Read errors: {summary["read_errors"]}

## Files

- `report.html` - browser-friendly report.
- `files.csv` - all scanned files.
- `duplicates.csv` - exact duplicate candidates by SHA-256.
- `similar_names.csv` - files whose names look like copies/variants.
- `rapid_series.csv` - image bursts grouped by minute.
- `large_videos.csv` - videos above the configured size threshold.
- `summary.json` - machine-readable summary.
"""
    (output / "summary.md").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
