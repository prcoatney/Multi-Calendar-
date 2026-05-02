"""
Per-planner configuration for the multi-planner daemon.

Each planner config knows how to:
- Identify itself in the cloud (doc_path)
- Map a date to a day-grid page index
- Render a fresh PDF for the planner from a list of events
- Convert .rm scene coordinates to PDF coordinates (for stroke→event matching)
- Build the per-event bbox manifest used by snap-strike detection

A planner config is consumed by `cycle()` in `move_daemon.py` (refactored to be
planner-agnostic) for both the Move and the rmpp Hyperpaper.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

HERE = os.path.dirname(os.path.abspath(__file__))


@dataclass
class PlannerConfig:
    name: str                                  # "move" / "rmpp"
    doc_path: str                              # /MOVE/planner-... or /2026 Hyperpaper
    page_w: float
    page_h: float
    scene_scale: float                         # multiplier for both axes
    scene_offset_x: float
    scene_offset_y: float
    day_grid_first_idx: int                    # doc[N] = day-grid page for DOY 1
    pages_per_day: int                         # 1 or 2
    state_file: str                            # per-planner state JSON path
    manifest_file: str                         # per-planner render manifest path
    abbreviations_file: str                    # per-planner abbrev cache
    # Plug-in renderer: takes the bundle's PDF path + events, writes a fresh PDF
    # in place AND returns a {page_uuid: [{event_id, time_iso, title, bbox_pdf}]} dict.
    render_fn: Callable[[list, str, dict], dict]
    # Optional: planner-specific filename hint when pushing back
    push_dir: str = "/"


def _move_render(events: list, planner_pdf_path: str, page_uuid_by_idx: dict) -> dict:
    """Render handler for the Move planner — overlays onto the commercial baseline."""
    # Lazy import to avoid circular deps at module load
    from move_render import render_planner
    rendered_days, rendered_events = render_planner(events, planner_pdf_path)
    # render_planner writes a manifest to RENDER_MANIFEST; load + return it.
    import json
    with open(os.path.join(HERE, "move-render-manifest.json")) as f:
        return json.load(f).get("by_page_uuid", {})


def _rmpp_render(events: list, planner_pdf_path: str, page_uuid_by_idx: dict) -> dict:
    """Render handler for the rmpp Hyperpaper — generates the PDF from scratch.

    Uses the existing hyperpaper_gen module which renders all 711 pages and
    captures per-event bbox in PDF coords. Translates page_idx → page_uuid using
    the bundle's cPages mapping."""
    from hyperpaper_gen import generate_hyperpaper

    # Convert events from API shape {summary, start, id} to the {(year,m,d): [(time, title, ev_id)]} form
    # that hyperpaper_gen expects, applying the same SKIP rules.
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from move_render import filter_event

    by_date: dict[tuple[int, int, int], list[tuple[str, str, str]]] = {}
    for ev in events:
        title = ev.get("summary", "").strip()
        if not title or filter_event(title):
            continue
        start = ev.get("start", "")
        if not start or "T" not in start:
            continue
        try:
            if start.endswith("Z"):
                dt = datetime.fromisoformat(start[:-1] + "+00:00")
            else:
                dt = datetime.fromisoformat(start)
            dt = dt.astimezone(ZoneInfo("America/Chicago"))
        except Exception:
            continue
        h, m = dt.hour, dt.minute
        ampm = "a" if h < 12 else "p"
        h12 = h if h <= 12 else h - 12
        if h12 == 0: h12 = 12
        t_label = f"{h12}:{m:02d}{ampm}" if m else f"{h12}{ampm}"
        ev_id = ev.get("id", "")
        by_date.setdefault((dt.year, dt.month, dt.day), []).append((t_label, title, ev_id))
    for k in by_date:
        by_date[k].sort()

    pdf_bytes, manifest = generate_hyperpaper(by_date)
    with open(planner_pdf_path, "wb") as f:
        f.write(pdf_bytes)

    # hyperpaper_gen manifest is keyed by page_idx (str). Convert to page_uuid.
    by_uuid: dict[str, list] = {}
    for page_idx_str, evs in manifest.get("pages", {}).items():
        try:
            idx = int(page_idx_str)
        except ValueError:
            continue
        uid = page_uuid_by_idx.get(idx)
        if not uid:
            continue
        by_uuid[uid] = []
        for entry in evs:
            by_uuid[uid].append({
                "event_id": entry.get("id", ""),
                "time_iso": entry.get("time", ""),
                "title": entry.get("title", ""),
                "bbox_pdf": entry.get("bbox", []),
            })
    return by_uuid


MOVE = PlannerConfig(
    name="move",
    doc_path="/MOVE/planner-2026.2.6.4-prcoatney@gmail.com",
    page_w=250.0,
    page_h=444.0,
    scene_scale=0.335,
    scene_offset_x=125.0,
    scene_offset_y=-13.6,
    day_grid_first_idx=69,
    pages_per_day=2,
    state_file=os.path.join(HERE, "move-watcher-state.json"),
    manifest_file=os.path.join(HERE, "move-render-manifest.json"),
    abbreviations_file=os.path.join(HERE, "move-abbreviations.json"),
    render_fn=_move_render,
    push_dir="/MOVE/",
)

RMPP = PlannerConfig(
    name="rmpp",
    doc_path="/2026 Hyperpaper",
    page_w=452.0,
    page_h=602.0,
    scene_scale=0.322,
    scene_offset_x=226.0,
    scene_offset_y=0.0,
    day_grid_first_idx=72,        # DAY_PAGE_START=73 → 1-indexed page 73 = doc[72]
    pages_per_day=1,
    state_file=os.path.join(HERE, "rmpp-watcher-state.json"),
    manifest_file=os.path.join(HERE, "rmpp-render-manifest.json"),
    abbreviations_file=os.path.join(HERE, "rmpp-abbreviations.json"),
    render_fn=_rmpp_render,
    push_dir="/",
)


def date_for_day_grid_idx(config: PlannerConfig, doc_idx: int, year: int = 2026) -> date | None:
    if doc_idx < config.day_grid_first_idx:
        return None
    if config.pages_per_day == 2 and (doc_idx - config.day_grid_first_idx) % 2 != 0:
        return None
    offset = doc_idx - config.day_grid_first_idx
    doy = offset // config.pages_per_day + 1
    if doy < 1 or doy > 366:
        return None
    return date(year, 1, 1) + timedelta(days=doy - 1)


def day_grid_idx(config: PlannerConfig, d: date) -> int:
    doy = (d - date(d.year, 1, 1)).days + 1
    return config.day_grid_first_idx + (doy - 1) * config.pages_per_day
