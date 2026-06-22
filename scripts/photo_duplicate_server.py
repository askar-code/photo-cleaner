#!/usr/bin/env python3
"""Local review server for exact photo duplicates.

The server exposes only files listed in duplicates.csv. Trash actions move files
to a folder inside macOS Trash and keep at least one existing file per duplicate
group.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import mimetypes
import os
import shutil
import sys
from collections import defaultdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local exact duplicate review app")
    parser.add_argument("duplicates_csv", help="Path to duplicates.csv from photo_audit.py")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--state-dir",
        default=None,
        help="Directory for logs/state. Defaults to duplicates_csv parent/duplicate_review_app.",
    )
    parser.add_argument(
        "--trash-root",
        default=None,
        help="Trash/quarantine root. Defaults to ~/.Trash/codex-photo-duplicates-<timestamp>.",
    )
    return parser.parse_args()


def now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def stable_id(abs_path: str) -> str:
    return hashlib.sha1(abs_path.encode("utf-8")).hexdigest()[:16]


def human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


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


class DuplicateStore:
    def __init__(self, duplicates_csv: Path, state_dir: Path, trash_root: Path) -> None:
        self.duplicates_csv = duplicates_csv
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "state.json"
        self.log_file = self.state_dir / "actions.jsonl"
        self.trash_root = trash_root
        self.rows_by_id: dict[str, dict[str, Any]] = {}
        self.groups: dict[str, list[str]] = defaultdict(list)
        self.moved_targets: dict[str, str] = {}
        self._load_rows()
        self._load_state()

    def _load_rows(self) -> None:
        with self.duplicates_csv.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                item_id = stable_id(row["abs_path"])
                row = dict(row)
                row["id"] = item_id
                row["size_int"] = int(row.get("size") or 0)
                self.rows_by_id[item_id] = row
                self.groups[row["duplicate_group"]].append(item_id)
        for group_id in list(self.groups):
            self.groups[group_id].sort(
                key=lambda item_id: (
                    self.rows_by_id[item_id].get("date", ""),
                    self.rows_by_id[item_id].get("rel_path", ""),
                )
            )

    def _load_state(self) -> None:
        if not self.state_file.exists():
            self._write_state()
            return
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.moved_targets = {
            item_id: target
            for item_id, target in state.get("moved_targets", {}).items()
            if item_id in self.rows_by_id
        }

    def _write_state(self) -> None:
        payload = {
            "updated_at": now_iso(),
            "duplicates_csv": str(self.duplicates_csv),
            "trash_root": str(self.trash_root),
            "moved_targets": self.moved_targets,
        }
        self.state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def log(self, event: dict[str, Any]) -> None:
        event = {"ts": now_iso(), **event}
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def item_status(self, item_id: str) -> dict[str, Any]:
        row = self.rows_by_id[item_id]
        source = Path(row["abs_path"])
        moved_target = self.moved_targets.get(item_id)
        target_exists = bool(moved_target and Path(moved_target).exists())
        source_exists = source.exists()
        if source_exists:
            state = "exists"
        elif target_exists:
            state = "trashed"
        else:
            state = "missing"
        return {
            "id": item_id,
            "group": row["duplicate_group"],
            "state": state,
            "source_exists": source_exists,
            "target_exists": target_exists,
            "moved_target": moved_target or "",
        }

    def group_existing_ids(self, group_id: str) -> list[str]:
        return [
            item_id
            for item_id in self.groups[group_id]
            if self.item_status(item_id)["state"] == "exists"
        ]

    def state_payload(self) -> dict[str, Any]:
        items = {item_id: self.item_status(item_id) for item_id in self.rows_by_id}
        groups = {}
        for group_id, ids in self.groups.items():
            existing = [item_id for item_id in ids if items[item_id]["state"] == "exists"]
            groups[group_id] = {
                "ids": ids,
                "existing_ids": existing,
                "existing_count": len(existing),
                "total": len(ids),
            }
        return {"items": items, "groups": groups, "trash_root": str(self.trash_root)}

    def trash_ids(self, item_ids: list[str]) -> dict[str, Any]:
        unknown = [item_id for item_id in item_ids if item_id not in self.rows_by_id]
        if unknown:
            return {"ok": False, "error": f"Unknown item id: {unknown[0]}"}

        by_group: dict[str, list[str]] = defaultdict(list)
        for item_id in item_ids:
            by_group[self.rows_by_id[item_id]["duplicate_group"]].append(item_id)
        for group_id, ids in by_group.items():
            existing = set(self.group_existing_ids(group_id))
            delete_existing = {item_id for item_id in ids if item_id in existing}
            if delete_existing and len(existing - delete_existing) < 1:
                return {
                    "ok": False,
                    "error": "Refusing to trash the last remaining file in a duplicate group.",
                    "group": group_id,
                }

        moved = []
        for item_id in item_ids:
            row = self.rows_by_id[item_id]
            source = Path(row["abs_path"])
            status = self.item_status(item_id)
            if status["state"] != "exists":
                moved.append({"id": item_id, "status": status["state"], "skipped": True})
                continue
            group_id = row["duplicate_group"]
            target = unique_target(self.trash_root / f"group_{int(group_id):04d}" / source.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            self.moved_targets[item_id] = str(target)
            moved.append({"id": item_id, "status": "trashed", "target": str(target)})
            self.log(
                {
                    "action": "trash",
                    "id": item_id,
                    "group": group_id,
                    "source": str(source),
                    "target": str(target),
                }
            )
        self._write_state()
        return {"ok": True, "moved": moved, "state": self.state_payload()}

    def restore_ids(self, item_ids: list[str]) -> dict[str, Any]:
        unknown = [item_id for item_id in item_ids if item_id not in self.rows_by_id]
        if unknown:
            return {"ok": False, "error": f"Unknown item id: {unknown[0]}"}
        restored = []
        for item_id in item_ids:
            row = self.rows_by_id[item_id]
            source = Path(row["abs_path"])
            target_text = self.moved_targets.get(item_id)
            if not target_text:
                restored.append({"id": item_id, "skipped": True, "reason": "No moved target recorded"})
                continue
            target = Path(target_text)
            if source.exists():
                restored.append({"id": item_id, "skipped": True, "reason": "Source already exists"})
                continue
            if not target.exists():
                restored.append({"id": item_id, "skipped": True, "reason": "Moved target is missing"})
                continue
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(source))
            self.moved_targets.pop(item_id, None)
            restored.append({"id": item_id, "status": "exists"})
            self.log(
                {
                    "action": "restore",
                    "id": item_id,
                    "group": row["duplicate_group"],
                    "source": str(target),
                    "target": str(source),
                }
            )
        self._write_state()
        return {"ok": True, "restored": restored, "state": self.state_payload()}


def build_html(store: DuplicateStore) -> str:
    total_groups = len(store.groups)
    total_files = len(store.rows_by_id)
    group_sections = []
    state = store.state_payload()

    for group_id in sorted(store.groups, key=lambda value: int(value)):
        ids = store.groups[group_id]
        first = store.rows_by_id[ids[0]]
        cards = []
        for item_id in ids:
            row = store.rows_by_id[item_id]
            item_state = state["items"][item_id]["state"]
            css_state = html.escape(item_state)
            cards.append(
                f"""
                <article class="photo-card {css_state}" id="card-{item_id}" data-id="{item_id}" data-group="{html.escape(group_id)}">
                  <div class="image-wrap">
                    <img src="/image/{item_id}" alt="" loading="lazy">
                    <span class="status" data-status>Status: {html.escape(item_state)}</span>
                  </div>
                  <div class="meta">
                    <strong>{html.escape(row["size_human"])}</strong>
                    <span>{html.escape(row["date"])}</span>
                    <code>{html.escape(row["rel_path"])}</code>
                  </div>
                  <div class="actions">
                    <button type="button" class="danger" data-action="trash-one" data-id="{item_id}">В корзину</button>
                    <button type="button" data-action="keep-this" data-id="{item_id}">Оставить это</button>
                    <button type="button" class="restore" data-action="restore-one" data-id="{item_id}">Вернуть</button>
                  </div>
                </article>
                """
            )
        group_sections.append(
            f"""
            <section class="group" id="group-{html.escape(group_id)}" data-group="{html.escape(group_id)}">
              <div class="group-head">
                <h2>Группа {html.escape(group_id)}</h2>
                <span data-group-count>{state["groups"][group_id]["existing_count"]} из {len(ids)} на месте</span>
              </div>
              <p><code>{html.escape(first["sha256"])}</code></p>
              <div class="photo-grid">{''.join(cards)}</div>
            </section>
            """
        )

    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Удаление точных дублей</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f9fb;
      --ink: #16202a;
      --muted: #607080;
      --line: #d6dee8;
      --panel: #ffffff;
      --danger: #b91c1c;
      --danger-bg: #fee2e2;
      --ok: #166534;
      --ok-bg: #dcfce7;
      --focus: #2563eb;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
      margin: 0;
    }}
    header {{
      background: #ffffff;
      border-bottom: 1px solid var(--line);
      padding: 18px 24px;
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    h1 {{
      font-size: 22px;
      margin: 0 0 4px;
    }}
    header p {{
      color: var(--muted);
      margin: 0;
    }}
    main {{
      margin: 0 auto;
      max-width: 1320px;
      padding: 20px 24px 44px;
    }}
    .group {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      margin-bottom: 18px;
      padding: 16px;
    }}
    .group-head {{
      align-items: baseline;
      display: flex;
      gap: 12px;
      justify-content: space-between;
    }}
    h2 {{
      font-size: 18px;
      margin: 0;
    }}
    .group-head span, .group p {{
      color: var(--muted);
      font-size: 13px;
    }}
    .group p {{
      margin: 6px 0 14px;
      word-break: break-all;
    }}
    code {{
      background: #eef3f8;
      border-radius: 4px;
      padding: 2px 4px;
    }}
    .photo-grid {{
      display: grid;
      gap: 14px;
      grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
    }}
    .photo-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      background: #fff;
    }}
    .photo-card.trashed {{
      opacity: 0.52;
    }}
    .image-wrap {{
      background: #e8eef5;
      position: relative;
    }}
    img {{
      aspect-ratio: 1 / 1;
      display: block;
      object-fit: cover;
      width: 100%;
    }}
    .status {{
      background: rgba(22, 32, 42, .76);
      border-radius: 999px;
      bottom: 8px;
      color: #fff;
      font-size: 12px;
      left: 8px;
      padding: 3px 8px;
      position: absolute;
    }}
    .exists .status {{ background: rgba(22, 101, 52, .88); }}
    .trashed .status {{ background: rgba(185, 28, 28, .88); }}
    .missing .status {{ background: rgba(75, 85, 99, .88); }}
    .meta {{
      display: grid;
      gap: 4px;
      font-size: 12px;
      padding: 10px 10px 8px;
      word-break: break-word;
    }}
    .meta strong {{
      font-size: 14px;
    }}
    .actions {{
      display: grid;
      gap: 8px;
      grid-template-columns: 1fr 1fr;
      padding: 0 10px 10px;
    }}
    button {{
      appearance: none;
      background: #eef3f8;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      color: var(--ink);
      cursor: pointer;
      font: inherit;
      font-size: 13px;
      min-height: 34px;
      padding: 6px 8px;
    }}
    button:hover {{ border-color: var(--focus); }}
    button:disabled {{
      cursor: not-allowed;
      opacity: .45;
    }}
    button.danger {{
      background: var(--danger-bg);
      border-color: #fecaca;
      color: var(--danger);
    }}
    button.restore {{
      background: var(--ok-bg);
      border-color: #bbf7d0;
      color: var(--ok);
      grid-column: 1 / -1;
    }}
    .exists button.restore {{ display: none; }}
    .trashed button.danger, .trashed button[data-action="keep-this"],
    .missing button.danger, .missing button[data-action="keep-this"] {{
      display: none;
    }}
    #toast {{
      background: #111827;
      border-radius: 8px;
      bottom: 18px;
      color: #fff;
      display: none;
      left: 50%;
      max-width: 680px;
      padding: 10px 14px;
      position: fixed;
      transform: translateX(-50%);
      z-index: 4;
    }}
  </style>
</head>
<body>
  <header>
    <h1>Точные дубли</h1>
    <p>{total_groups} групп, {total_files} файлов. Кнопка “В корзину” перемещает файл в Trash, а не удаляет навсегда.</p>
  </header>
  <main>
    {''.join(group_sections)}
  </main>
  <div id="toast"></div>
  <script>
    let state = {json.dumps(state, ensure_ascii=False)};

    function toast(message) {{
      const el = document.getElementById('toast');
      el.textContent = message;
      el.style.display = 'block';
      clearTimeout(window.__toastTimer);
      window.__toastTimer = setTimeout(() => el.style.display = 'none', 3200);
    }}

    async function postJson(url, payload) {{
      const response = await fetch(url, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload)
      }});
      const data = await response.json();
      if (!response.ok || !data.ok) {{
        throw new Error(data.error || `HTTP ${{response.status}}`);
      }}
      return data;
    }}

    function updateCards(newState) {{
      state = newState;
      for (const [id, item] of Object.entries(state.items)) {{
        const card = document.getElementById(`card-${{id}}`);
        if (!card) continue;
        card.classList.remove('exists', 'trashed', 'missing');
        card.classList.add(item.state);
        const status = card.querySelector('[data-status]');
        status.textContent = item.state === 'exists' ? 'на месте' : item.state === 'trashed' ? 'в корзине' : 'нет файла';
      }}
      for (const [groupId, group] of Object.entries(state.groups)) {{
        const section = document.querySelector(`[data-group="${{groupId}}"]`);
        if (!section) continue;
        const count = section.querySelector('[data-group-count]');
        count.textContent = `${{group.existing_count}} из ${{group.total}} на месте`;
        const isLast = group.existing_count <= 1;
        for (const id of group.ids) {{
          const card = document.getElementById(`card-${{id}}`);
          if (!card) continue;
          const trashButton = card.querySelector('[data-action="trash-one"]');
          if (trashButton) trashButton.disabled = isLast && state.items[id].state === 'exists';
        }}
      }}
    }}

    document.addEventListener('click', async (event) => {{
      const button = event.target.closest('button[data-action]');
      if (!button) return;
      const id = button.dataset.id;
      const action = button.dataset.action;
      button.disabled = true;
      try {{
        if (action === 'trash-one') {{
          const data = await postJson('/api/trash', {{ ids: [id] }});
          updateCards(data.state);
          toast('Файл перемещен в корзину');
        }}
        if (action === 'keep-this') {{
          const groupId = state.items[id].group;
          const ids = state.groups[groupId].existing_ids.filter(otherId => otherId !== id);
          if (!ids.length) {{
            toast('В этой группе уже нечего убирать');
          }} else {{
            const data = await postJson('/api/trash', {{ ids }});
            updateCards(data.state);
            toast(`В корзину перемещено: ${{ids.length}}`);
          }}
        }}
        if (action === 'restore-one') {{
          const data = await postJson('/api/restore', {{ ids: [id] }});
          updateCards(data.state);
          toast('Файл возвращен на место');
        }}
      }} catch (error) {{
        toast(error.message);
        updateCards(state);
      }} finally {{
        button.disabled = false;
      }}
    }});

    updateCards(state);
  </script>
</body>
</html>
"""


class DuplicateHandler(BaseHTTPRequestHandler):
    store: DuplicateStore

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    def send_bytes(self, status: HTTPStatus, content: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        self.send_bytes(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_bytes(HTTPStatus.OK, build_html(self.store).encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/state":
            self.send_json(HTTPStatus.OK, {"ok": True, "state": self.store.state_payload()})
            return
        if parsed.path.startswith("/image/"):
            item_id = parsed.path.rsplit("/", 1)[-1]
            row = self.store.rows_by_id.get(item_id)
            if not row:
                self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Unknown item"})
                return
            path = Path(row["abs_path"])
            if not path.exists():
                self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "File is not present"})
                return
            content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            try:
                data = path.read_bytes()
            except OSError as exc:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                return
            self.send_bytes(HTTPStatus.OK, data, content_type)
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self.read_json()
            ids = payload.get("ids") or []
            if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
                self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Expected ids array"})
                return
            if parsed.path == "/api/trash":
                result = self.store.trash_ids(ids)
                self.send_json(HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST, result)
                return
            if parsed.path == "/api/restore":
                result = self.store.restore_ids(ids)
                self.send_json(HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST, result)
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
        except Exception as exc:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})


def default_trash_root(state_dir: Path) -> Path:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    home_trash = Path.home() / ".Trash"
    if home_trash.exists():
        return home_trash / f"codex-photo-duplicates-{timestamp}"
    return state_dir / f"trash-{timestamp}"


def main() -> int:
    args = parse_args()
    duplicates_csv = Path(args.duplicates_csv).expanduser().resolve()
    if not duplicates_csv.exists():
        print(f"Not found: {duplicates_csv}", file=sys.stderr)
        return 2
    state_dir = Path(args.state_dir).expanduser().resolve() if args.state_dir else duplicates_csv.parent / "duplicate_review_app"
    trash_root = Path(args.trash_root).expanduser().resolve() if args.trash_root else default_trash_root(state_dir)
    store = DuplicateStore(duplicates_csv, state_dir, trash_root)
    DuplicateHandler.store = store
    server = ThreadingHTTPServer((args.host, args.port), DuplicateHandler)
    print(f"Duplicate review app: http://{args.host}:{args.port}/", flush=True)
    print(f"Trash root: {trash_root}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
