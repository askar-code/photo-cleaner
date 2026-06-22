#!/usr/bin/env python3
"""Build a visual review page for exact duplicate groups."""

from __future__ import annotations

import argparse
import csv
import html
from collections import defaultdict
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create exact duplicate review HTML")
    parser.add_argument("duplicates_csv", help="Path to duplicates.csv")
    parser.add_argument("--output", required=True, help="Output HTML path")
    return parser.parse_args()


def file_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def main() -> int:
    args = parse_args()
    groups: dict[str, list[dict]] = defaultdict(list)
    with Path(args.duplicates_csv).open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            groups[row["duplicate_group"]].append(row)

    sections = []
    for group_id, rows in sorted(groups.items(), key=lambda item: int(item[0])):
        cards = []
        for row in rows:
            uri = file_uri(row["abs_path"])
            cards.append(
                f"""
                <article class="card">
                  <a href="{html.escape(uri)}"><img loading="lazy" src="{html.escape(uri)}" alt=""></a>
                  <div class="meta">
                    <strong>{html.escape(row["size_human"])}</strong><br>
                    {html.escape(row["date"])}<br>
                    {html.escape(row["rel_path"])}
                  </div>
                </article>
                """
            )
        sections.append(
            f"""
            <section class="group">
              <h2>Group {html.escape(group_id)} · {len(rows)} identical files</h2>
              <p><code>{html.escape(rows[0]["sha256"])}</code></p>
              <div class="grid">{''.join(cards)}</div>
            </section>
            """
        )

    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Exact Duplicate Review</title>
  <style>
    body {{
      color: #17202a;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.4;
      margin: 28px;
    }}
    .group {{
      border-top: 1px solid #cbd5e1;
      padding: 18px 0 24px;
    }}
    h1 {{ margin-bottom: 6px; }}
    h2 {{ font-size: 18px; margin-bottom: 4px; }}
    .grid {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    }}
    .card {{
      border: 1px solid #d9e2ec;
      border-radius: 8px;
      overflow: hidden;
    }}
    img {{
      aspect-ratio: 1 / 1;
      background: #f0f4f8;
      display: block;
      object-fit: cover;
      width: 100%;
    }}
    .meta {{
      font-size: 12px;
      padding: 8px;
      word-break: break-word;
    }}
    code {{
      background: #f0f4f8;
      border-radius: 4px;
      padding: 2px 4px;
    }}
  </style>
</head>
<body>
  <h1>Exact Duplicate Review</h1>
  <p>These files have identical SHA-256 hashes. Keeping one file per group is usually safe, but this page only helps review them; it does not change anything.</p>
  {''.join(sections)}
</body>
</html>
""",
        encoding="utf-8",
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
