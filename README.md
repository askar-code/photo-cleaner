# Photo Cleaner

Small local tools for auditing and cleaning a plain photo folder.

The tools are designed for cautious cleanup:

- source photos are never changed during audit or planning;
- exact duplicates are detected by SHA-256;
- burst/series photos are grouped by timestamp gaps;
- interactive cleanup apps move selected files to macOS Trash instead of deleting permanently;
- each review app refuses to remove the last remaining file in a group.

Everything runs locally with Python's standard library.

## Quick Start

```bash
python3 -B scripts/photo_audit.py "/path/to/Camera Uploads" --output outputs/photo_audit
```

Open the generated report:

```bash
open outputs/photo_audit/report.html
```

## Exact Duplicate Review

Build a static visual page:

```bash
python3 -B scripts/photo_exact_duplicate_review.py \
  outputs/photo_audit/duplicates.csv \
  --output outputs/photo_audit/exact_duplicates_review.html
```

Or start the local button-based app:

```bash
python3 -B scripts/photo_duplicate_server.py \
  outputs/photo_audit/duplicates.csv \
  --port 8765
```

Then open:

```text
http://127.0.0.1:8765/
```

Buttons:

- `В корзину` moves one duplicate to Trash.
- `Оставить это` keeps that file and moves the other duplicates in the group to Trash.
- `Вернуть` restores a file moved by the app.

## Burst / Series Review

Create a burst plan. This example treats photos taken within 2 seconds of each
other as a series, starting from groups of 2:

```bash
python3 -B scripts/photo_burst_plan.py \
  outputs/photo_audit \
  --max-gap-seconds 2 \
  --min-files 2 \
  --output-dir outputs/photo_audit/burst_cleanup_min2
```

Start the local button-based app:

```bash
python3 -B scripts/photo_burst_server.py \
  outputs/photo_audit/burst_cleanup_min2/burst_plan.csv \
  --port 8766
```

Then open:

```text
http://127.0.0.1:8766/
```

The app shows groups by year. `auto keep` is only a rough suggestion based on
largest file size in the group; use your eyes before removing important frames.

## Dry-Run Organization Plan

Create a CSV showing where files could be organized by year/month:

```bash
python3 -B scripts/photo_make_organize_plan.py outputs/photo_audit
```

This only writes `organize_plan.csv`; it does not move files.

## Safe Apply Script For Bursts

For batch movement after review, there is a dry-run-first script:

```bash
python3 -B scripts/photo_apply_burst_plan.py \
  outputs/photo_audit/burst_cleanup_min2/burst_plan.csv \
  "/path/to/quarantine"
```

It prints what it would move. Add `--execute` only when you are ready:

```bash
python3 -B scripts/photo_apply_burst_plan.py \
  outputs/photo_audit/burst_cleanup_min2/burst_plan.csv \
  "/path/to/quarantine" \
  --execute
```

## Notes

- The audit date parser works well with Dropbox Camera Uploads names such as
  `2024-08-14 12.34.56.jpg` and phone-style names like `IMG_20240814_123456.jpg`.
- Generated reports, state files, and CSVs can contain personal filenames and
  should stay out of git.
- The local servers bind to `127.0.0.1` by default.
