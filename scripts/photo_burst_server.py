#!/usr/bin/env python3
"""Local review server for burst/series photo cleanup.

This app is intentionally conservative: it moves selected files to a Trash
folder, logs every action, supports restore, and refuses to remove the last
existing file in any burst group.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import mimetypes
import shutil
import sys
from collections import Counter, defaultdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local burst photo review app")
    parser.add_argument("burst_plan_csv", help="Path to burst_plan.csv")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--state-dir",
        default=None,
        help="Directory for logs/state. Defaults to burst_plan_csv parent/burst_review_app.",
    )
    parser.add_argument(
        "--trash-root",
        default=None,
        help="Trash/quarantine root. Defaults to ~/.Trash/codex-photo-bursts-<timestamp>.",
    )
    return parser.parse_args()


def now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def stable_id(abs_path: str) -> str:
    return hashlib.sha1(abs_path.encode("utf-8")).hexdigest()[:16]


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


def default_trash_root(state_dir: Path) -> Path:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    home_trash = Path.home() / ".Trash"
    if home_trash.exists():
        return home_trash / f"codex-photo-bursts-{timestamp}"
    return state_dir / f"trash-{timestamp}"


class BurstStore:
    def __init__(self, plan_csv: Path, state_dir: Path, trash_root: Path) -> None:
        self.plan_csv = plan_csv
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "state.json"
        self.log_file = self.state_dir / "actions.jsonl"
        self.trash_root = trash_root
        self.rows_by_id: dict[str, dict[str, Any]] = {}
        self.groups: dict[str, list[str]] = defaultdict(list)
        self.years: dict[str, list[str]] = defaultdict(list)
        self.moved_targets: dict[str, str] = {}
        self._load_rows()
        self._load_state()

    def _load_rows(self) -> None:
        with self.plan_csv.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                item_id = stable_id(row["abs_path"])
                year = row["date"][:4] if row.get("date") else "unknown"
                enriched = dict(row)
                enriched["id"] = item_id
                enriched["year"] = year
                enriched["size_int"] = int(row.get("size") or 0)
                self.rows_by_id[item_id] = enriched
                group_id = row["burst_group"]
                self.groups[group_id].append(item_id)
        for group_id in list(self.groups):
            self.groups[group_id].sort(
                key=lambda item_id: (
                    self.rows_by_id[item_id].get("date", ""),
                    self.rows_by_id[item_id].get("rel_path", ""),
                )
            )
            first_year = self.rows_by_id[self.groups[group_id][0]]["year"]
            self.years[first_year].append(group_id)
        for year in list(self.years):
            self.years[year].sort(key=lambda group_id: int(group_id))

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
            "plan_csv": str(self.plan_csv),
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
            "group": row["burst_group"],
            "year": row["year"],
            "state": state,
            "source_exists": source_exists,
            "target_exists": target_exists,
            "moved_target": moved_target or "",
        }

    def state_payload(self, year: str | None = None) -> dict[str, Any]:
        if year:
            group_ids = self.years.get(year, [])
            item_ids = [item_id for group_id in group_ids for item_id in self.groups[group_id]]
        else:
            group_ids = list(self.groups)
            item_ids = list(self.rows_by_id)
        items = {item_id: self.item_status(item_id) for item_id in item_ids}
        groups = {}
        for group_id in group_ids:
            ids = self.groups[group_id]
            visible_ids = [item_id for item_id in ids if item_id in items]
            existing = [item_id for item_id in visible_ids if items[item_id]["state"] == "exists"]
            groups[group_id] = {
                "ids": visible_ids,
                "existing_ids": existing,
                "existing_count": len(existing),
                "total": len(visible_ids),
            }
        return {
            "items": items,
            "groups": groups,
            "trash_root": str(self.trash_root),
        }

    def group_existing_ids(self, group_id: str) -> list[str]:
        return [
            item_id
            for item_id in self.groups[group_id]
            if self.item_status(item_id)["state"] == "exists"
        ]

    def trash_ids(self, item_ids: list[str]) -> dict[str, Any]:
        unknown = [item_id for item_id in item_ids if item_id not in self.rows_by_id]
        if unknown:
            return {"ok": False, "error": f"Unknown item id: {unknown[0]}"}

        by_group: dict[str, list[str]] = defaultdict(list)
        for item_id in item_ids:
            by_group[self.rows_by_id[item_id]["burst_group"]].append(item_id)
        for group_id, ids in by_group.items():
            existing = set(self.group_existing_ids(group_id))
            delete_existing = {item_id for item_id in ids if item_id in existing}
            if delete_existing and len(existing - delete_existing) < 1:
                return {
                    "ok": False,
                    "error": "Не убираю последний оставшийся кадр в серии.",
                    "group": group_id,
                }

        moved = []
        for item_id in item_ids:
            row = self.rows_by_id[item_id]
            status = self.item_status(item_id)
            if status["state"] != "exists":
                moved.append({"id": item_id, "status": status["state"], "skipped": True})
                continue
            source = Path(row["abs_path"])
            group_id = row["burst_group"]
            target = unique_target(self.trash_root / f"group_{int(group_id):05d}" / source.name)
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
                    "group": row["burst_group"],
                    "source": str(target),
                    "target": str(source),
                }
            )
        self._write_state()
        return {"ok": True, "restored": restored, "state": self.state_payload()}

    def image_path(self, item_id: str) -> Path | None:
        row = self.rows_by_id.get(item_id)
        if not row:
            return None
        source = Path(row["abs_path"])
        if source.exists():
            return source
        target_text = self.moved_targets.get(item_id)
        if target_text and Path(target_text).exists():
            return Path(target_text)
        return None


def page_shell(title: str, body: str, state: dict[str, Any] | None = None) -> str:
    state_json = json.dumps(state or {"items": {}, "groups": {}}, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f8fb;
      --ink: #17202a;
      --muted: #5f7082;
      --line: #d6dee8;
      --panel: #ffffff;
      --danger: #b91c1c;
      --danger-bg: #fee2e2;
      --ok: #166534;
      --ok-bg: #dcfce7;
      --mark: #1d4ed8;
      --mark-bg: #dbeafe;
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
      padding: 16px 22px;
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    h1 {{ font-size: 22px; margin: 0 0 4px; }}
    header p {{ color: var(--muted); margin: 0; }}
    main {{
      margin: 0 auto;
      max-width: 1380px;
      padding: 20px 22px 48px;
    }}
    a {{ color: #0f5b99; }}
    .year-grid {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
    }}
    .year-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      color: inherit;
      display: block;
      padding: 14px;
      text-decoration: none;
    }}
    .year-card strong {{
      display: block;
      font-size: 26px;
    }}
    .year-card span {{
      color: var(--muted);
      display: block;
      font-size: 13px;
    }}
    .group {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      margin-bottom: 18px;
      padding: 14px;
    }}
    .group-head {{
      align-items: baseline;
      display: flex;
      gap: 10px;
      justify-content: space-between;
    }}
    h2 {{ font-size: 18px; margin: 0; }}
    .group-head span, .group p {{
      color: var(--muted);
      font-size: 13px;
    }}
    .group p {{ margin: 6px 0 14px; }}
    .photo-grid {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
    }}
    .photo-card {{
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    .photo-card.trashed {{ opacity: .54; }}
    .photo-card.auto-keep {{
      border-color: #93c5fd;
      box-shadow: inset 0 0 0 1px #93c5fd;
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
    .status, .badge {{
      border-radius: 999px;
      color: #fff;
      font-size: 12px;
      padding: 3px 8px;
      position: absolute;
    }}
    .status {{
      background: rgba(22, 32, 42, .76);
      bottom: 8px;
      left: 8px;
    }}
    .exists .status {{ background: rgba(22, 101, 52, .88); }}
    .trashed .status {{ background: rgba(185, 28, 28, .88); }}
    .badge {{
      background: rgba(29, 78, 216, .88);
      right: 8px;
      top: 8px;
    }}
    .meta {{
      display: grid;
      gap: 4px;
      font-size: 12px;
      padding: 9px 10px 8px;
      word-break: break-word;
    }}
    .meta strong {{ font-size: 14px; }}
    code {{
      background: #eef3f8;
      border-radius: 4px;
      padding: 2px 4px;
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
    .toolbar {{
      align-items: center;
      display: flex;
      gap: 12px;
      justify-content: space-between;
      margin-bottom: 14px;
    }}
    #toast {{
      background: #111827;
      border-radius: 8px;
      bottom: 18px;
      color: #fff;
      display: none;
      left: 50%;
      max-width: 720px;
      padding: 10px 14px;
      position: fixed;
      transform: translateX(-50%);
      z-index: 4;
    }}
  </style>
</head>
<body>
  {body}
  <div id="toast"></div>
  <script>
    let state = {state_json};

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

    function mergeState(newState) {{
      state.items = {{ ...state.items, ...newState.items }};
      state.groups = {{ ...state.groups, ...newState.groups }};
      state.trash_root = newState.trash_root || state.trash_root;
    }}

    function updateCards(newState) {{
      if (newState) mergeState(newState);
      for (const [id, item] of Object.entries(state.items || {{}})) {{
        const card = document.getElementById(`card-${{id}}`);
        if (!card) continue;
        card.classList.remove('exists', 'trashed', 'missing');
        card.classList.add(item.state);
        const status = card.querySelector('[data-status]');
        status.textContent = item.state === 'exists' ? 'на месте' : item.state === 'trashed' ? 'в корзине' : 'нет файла';
      }}
      for (const [groupId, group] of Object.entries(state.groups || {{}})) {{
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
          toast('Кадр перемещен в корзину');
        }}
        if (action === 'keep-this') {{
          const groupId = state.items[id].group;
          const ids = state.groups[groupId].existing_ids.filter(otherId => otherId !== id);
          if (!ids.length) {{
            toast('В этой серии уже нечего убирать');
          }} else {{
            const data = await postJson('/api/trash', {{ ids }});
            updateCards(data.state);
            toast(`В корзину перемещено: ${{ids.length}}`);
          }}
        }}
        if (action === 'restore-one') {{
          const data = await postJson('/api/restore', {{ ids: [id] }});
          updateCards(data.state);
          toast('Кадр возвращен на место');
        }}
      }} catch (error) {{
        toast(error.message);
        updateCards();
      }} finally {{
        button.disabled = false;
      }}
    }});

    updateCards();
  </script>
</body>
</html>
"""


def index_page(store: BurstStore) -> str:
    group_counts = {year: len(group_ids) for year, group_ids in store.years.items()}
    file_counts = {
        year: sum(len(store.groups[group_id]) for group_id in group_ids)
        for year, group_ids in store.years.items()
    }
    candidate_counts = {
        year: file_counts[year] - group_counts[year]
        for year in store.years
    }
    cards = []
    for year in sorted(store.years):
        cards.append(
            f"""
            <a class="year-card" href="/year/{html.escape(year)}">
              <strong>{html.escape(year)}</strong>
              <span>{group_counts[year]} серий</span>
              <span>{file_counts[year]} кадров</span>
              <span>можно убрать до {candidate_counts[year]}</span>
            </a>
            """
        )
    total_groups = sum(group_counts.values())
    total_files = sum(file_counts.values())
    total_candidates = total_files - total_groups
    body = f"""
  <header>
    <h1>Серии кадров</h1>
    <p>{total_groups} серий от 2 кадров, {total_files} фото. Потенциально убрать до {total_candidates}, оставляя минимум один кадр в серии.</p>
  </header>
  <main>
    <div class="year-grid">{''.join(cards)}</div>
  </main>
"""
    return page_shell("Серии кадров", body)


def year_page(store: BurstStore, year: str) -> str:
    group_ids = store.years.get(year, [])
    state = store.state_payload(year)
    sections = []
    for group_id in group_ids:
        ids = store.groups[group_id]
        rows = [store.rows_by_id[item_id] for item_id in ids]
        start = rows[0]["date"]
        end = rows[-1]["date"]
        group_size = sum(row["size_int"] for row in rows)
        cards = []
        for item_id in ids:
            row = store.rows_by_id[item_id]
            is_auto_keep = row.get("action") == "auto_keep"
            auto_keep_rule = row.get("auto_keep_rule") or "largest_file"
            keep_class = " auto-keep" if is_auto_keep else ""
            badge_text = "sharpest" if auto_keep_rule == "sharpness" else "auto keep"
            badge = f'<span class="badge">{badge_text}</span>' if is_auto_keep else ""
            sharpness_meta = ""
            if row.get("sharpness_score"):
                sharpness_meta = f'<span>резкость {html.escape(row["sharpness_score"])}</span>'
            elif row.get("sharpness_error"):
                sharpness_meta = f'<span>резкость: ошибка</span>'
            cards.append(
                f"""
                <article class="photo-card{keep_class}" id="card-{item_id}" data-id="{item_id}" data-group="{html.escape(group_id)}">
                  <div class="image-wrap">
                    <img src="/image/{item_id}" alt="" loading="lazy">
                    <span class="status" data-status>status</span>
                    {badge}
                  </div>
                  <div class="meta">
                    <strong>{html.escape(row["size_human"])}</strong>
                    {sharpness_meta}
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
        sections.append(
            f"""
            <section class="group" id="group-{html.escape(group_id)}" data-group="{html.escape(group_id)}">
              <div class="group-head">
                <h2>Серия {html.escape(group_id)} · {html.escape(start)}</h2>
                <span data-group-count>{state["groups"][group_id]["existing_count"]} из {len(ids)} на месте</span>
              </div>
              <p>{len(ids)} кадров · {html.escape(start)} - {html.escape(end)} · всего {human_size(group_size)}</p>
              <div class="photo-grid">{''.join(cards)}</div>
            </section>
            """
        )
    body = f"""
  <header>
    <h1>Серии {html.escape(year)}</h1>
    <p>Это похожие кадры, а не точные дубли. Если план построен по резкости, бейдж `sharpest` показывает самый резкий кандидат в серии.</p>
  </header>
  <main>
    <div class="toolbar">
      <a href="/">Все годы</a>
      <span>{len(group_ids)} серий</span>
    </div>
    {''.join(sections)}
  </main>
"""
    return page_shell(f"Серии {year}", body, state)


class BurstHandler(BaseHTTPRequestHandler):
    store: BurstStore

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
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, data, "application/json; charset=utf-8")

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_bytes(HTTPStatus.OK, index_page(self.store).encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/year/"):
            year = parsed.path.rsplit("/", 1)[-1]
            if year not in self.store.years:
                self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Unknown year"})
                return
            self.send_bytes(HTTPStatus.OK, year_page(self.store, year).encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/state":
            self.send_json(HTTPStatus.OK, {"ok": True, "state": self.store.state_payload()})
            return
        if parsed.path.startswith("/api/state/"):
            year = parsed.path.rsplit("/", 1)[-1]
            self.send_json(HTTPStatus.OK, {"ok": True, "state": self.store.state_payload(year)})
            return
        if parsed.path.startswith("/image/"):
            item_id = parsed.path.rsplit("/", 1)[-1]
            path = self.store.image_path(item_id)
            if not path:
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


def main() -> int:
    args = parse_args()
    plan_csv = Path(args.burst_plan_csv).expanduser().resolve()
    if not plan_csv.exists():
        print(f"Not found: {plan_csv}", file=sys.stderr)
        return 2
    state_dir = Path(args.state_dir).expanduser().resolve() if args.state_dir else plan_csv.parent / "burst_review_app"
    trash_root = Path(args.trash_root).expanduser().resolve() if args.trash_root else default_trash_root(state_dir)
    store = BurstStore(plan_csv, state_dir, trash_root)
    BurstHandler.store = store
    server = ThreadingHTTPServer((args.host, args.port), BurstHandler)
    print(f"Burst review app: http://{args.host}:{args.port}/", flush=True)
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
