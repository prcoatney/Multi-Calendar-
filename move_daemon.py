"""
Move planner watcher daemon.

Cycle:
1. Pull bundle from reMarkable cloud.
2. Compare each page's .rm sidecar strokes to last-processed state.
3. For each NEW stroke:
   - Snap-line crossing a known event bbox → DELETE that Google event.
   - Snap-rectangle around freehand text → OCR via Claude Vision → parse (time, title) → CREATE Google event.
4. After detection: clean up the .rm files (remove processed snap strokes + the freehand inside an add-rectangle).
5. Re-render planner from baseline (Google has new state).
6. Push bundle back via rmapi put --force.
7. Persist watcher state.

State files:
  ~/claude/multi-calendar/move-render-manifest.json   (written by move_render.py)
  ~/claude/multi-calendar/move-watcher-state.json     (written by this daemon)

This is a one-shot cycle. Run it on a cron / loop for continuous operation.
"""
import base64
import io
import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from io import BytesIO
from zipfile import ZipFile
from zoneinfo import ZoneInfo

from rmscene import read_blocks, write_blocks
from PIL import Image, ImageDraw

# Re-use config + helpers from move_render
from move_render import (
    RMAPI, PLANNER_PATH, HERE, BASELINE_PDF, RENDER_MANIFEST,
    pull_bundle, push_bundle, render_planner, fetch_events,
    DAY_GRID_FIRST,
)

WATCHER_STATE = os.path.join(HERE, "move-watcher-state.json")

# Move scene→PDF transform (calibrated 2026-05-02)
SCENE_SCALE = 0.335
SCENE_OFFSET_X = 125.0
SCENE_OFFSET_Y = -13.6


def scene_to_pdf(sx: float, sy: float) -> tuple[float, float]:
    return sx * SCENE_SCALE + SCENE_OFFSET_X, sy * SCENE_SCALE + SCENE_OFFSET_Y


def stroke_bbox_pdf(pts: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    pdf = [scene_to_pdf(x, y) for x, y in pts]
    xs = [p[0] for p in pdf]; ys = [p[1] for p in pdf]
    return min(xs), min(ys), max(xs), max(ys)


def stroke_length(pts) -> float:
    return sum(math.hypot(pts[i][0] - pts[i-1][0], pts[i][1] - pts[i-1][1])
               for i in range(1, len(pts)))


def line_residual(pts) -> float:
    if len(pts) < 2: return 0
    x0, y0 = pts[0]; x1, y1 = pts[-1]
    dx, dy = x1 - x0, y1 - y0
    L = math.hypot(dx, dy)
    if L < 1: return 0
    return sum(abs((dy * (x - x0) - dx * (y - y0)) / L) for x, y in pts) / len(pts)


def is_snap_rect(pts) -> bool:
    """Snap-to-shape rectangle: 5 points (4 corners + closure), closed loop."""
    if len(pts) != 5:
        return False
    return math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 5


def is_snap_line(pts) -> bool:
    """Snap-to-shape line / strike: low residual fit, length > 50, n_points 2-25.

    Must NOT be a closed shape — closed shapes (rectangles) trick the residual
    test because line_residual fits through endpoints which are identical."""
    n = len(pts)
    if n < 2 or n > 25:
        return False
    if stroke_length(pts) < 50:
        return False
    # Exclude closed shapes — those are rectangles/polygons, not lines
    if math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 5:
        return False
    return line_residual(pts) < 0.5


def stroke_fingerprint(pts) -> str:
    """Stable fingerprint for state-tracking. Rounded bbox + n_points."""
    if not pts: return ""
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return f"{len(pts)}|{round(min(xs), 1)},{round(min(ys), 1)},{round(max(xs), 1)},{round(max(ys), 1)}"


def parse_strokes_from_blocks(blocks):
    out = []
    for b in blocks:
        if type(b).__name__ == 'SceneLineItemBlock':
            v = b.item.value
            if v and hasattr(v, 'points') and v.points:
                out.append((b, [(p.x, p.y) for p in v.points]))
    return out


def bbox_overlaps(stroke_bbox, event_bbox, pad: float = 5.0) -> bool:
    sx0, sy0, sx1, sy1 = stroke_bbox
    ex0, ey0, ex1, ey1 = event_bbox
    return not (sx1 < ex0 - pad or sx0 > ex1 + pad or sy1 < ey0 - pad or sy0 > ey1 + pad)


def stroke_inside_rect(stroke_pts, rect_bbox_scene, pad: float = 0.0) -> bool:
    """Returns True if all of stroke's points are inside the given scene-coord bbox."""
    rx0, ry0, rx1, ry1 = rect_bbox_scene
    return all(rx0 - pad <= x <= rx1 + pad and ry0 - pad <= y <= ry1 + pad
               for x, y in stroke_pts)


def render_strokes_to_png(strokes_pts: list, page_w: float = 250.0, page_h: float = 444.0) -> bytes:
    """Render the given list of stroke point-lists to a PNG for OCR."""
    if not strokes_pts:
        return b""
    all_x = [x for s in strokes_pts for x, y in s]
    all_y = [y for s in strokes_pts for x, y in s]
    min_x, max_x = min(all_x), max(all_x)
    min_y, max_y = min(all_y), max(all_y)
    range_x = max(max_x - min_x, 1)
    range_y = max(max_y - min_y, 1)
    scale = min(2400 / range_x, 3200 / range_y, 8.0)
    w = int(range_x * scale) + 200
    h = int(range_y * scale) + 200
    img = Image.new('L', (w, h), 255)
    draw = ImageDraw.Draw(img)
    for pts in strokes_pts:
        scaled = [(int((x - min_x) * scale + 100), int((y - min_y) * scale + 100)) for x, y in pts]
        if len(scaled) >= 2:
            draw.line(scaled, fill=0, width=max(3, int(scale * 0.5)))
    buf = BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def claude_ocr_event(png_bytes: bytes) -> dict | None:
    """Send PNG to Claude Vision; expect a JSON response {time: 'H:MMam/pm', title: '...'}."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        out = subprocess.run(["railway", "variables", "-k"], capture_output=True, text=True, cwd=HERE)
        for line in out.stdout.splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                api_key = line.split("=", 1)[1]; break
    if not api_key:
        print("WARN: no ANTHROPIC_API_KEY; cannot OCR", file=sys.stderr)
        return None

    img_b64 = base64.b64encode(png_bytes).decode()
    body = json.dumps({
        "model": "claude-sonnet-4-6-20250929",
        "max_tokens": 200,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": (
                    "This is handwriting representing a calendar event. Read it and extract the time and "
                    "the title/description. Output ONLY a JSON object with keys 'time' (formatted like "
                    "'7am' or '2:30pm') and 'title' (string). If you cannot determine a time, set time to null. "
                    "If the handwriting is unreadable or empty, output {\"time\": null, \"title\": null}."
                )},
            ],
        }],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body, headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.load(r)
    text = "".join(c.get("text", "") for c in resp.get("content", []) if c.get("type") == "text")
    try:
        s = text.find("{"); e = text.rfind("}") + 1
        return json.loads(text[s:e])
    except Exception as ex:
        print(f"WARN: OCR parse failed: {ex} | text: {text[:200]}", file=sys.stderr)
        return None


def parse_time_str(s: str) -> tuple[int, int] | None:
    """'7am' / '2:30pm' / '14:30' / '7:00 am' → (hour24, minute)."""
    if not s: return None
    t = s.strip().lower().replace(" ", "")
    pm = "pm" in t
    am = "am" in t
    t = t.replace("am", "").replace("pm", "")
    parts = t.split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return None
    if pm and h < 12: h += 12
    if am and h == 12: h = 0
    return h, m


def call_calendar_api(api_key: str, summary: str, start_iso: str, end_iso: str) -> dict:
    body = json.dumps({
        "summary": summary,
        "start": start_iso,
        "end": end_iso,
        "member_names": ["Coat"],
    }).encode()
    req = urllib.request.Request(
        "https://multi-calendar-production.up.railway.app/cross-formed-kids/api/schedule-meeting",
        data=body, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def call_delete_event(event_id: str) -> dict:
    qs = urllib.parse.urlencode({"token": "rmpp-coat-001", "id": event_id})
    req = urllib.request.Request(
        f"https://multi-calendar-production.up.railway.app/api/hyperpaper/event/delete?{qs}",
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def date_for_day_grid_idx(idx: int) -> date | None:
    """Inverse of day_grid_idx: doc[69 + (DOY-1)*2] → date."""
    if idx < DAY_GRID_FIRST or (idx - DAY_GRID_FIRST) % 2 != 0:
        return None
    doy = (idx - DAY_GRID_FIRST) // 2 + 1
    return date(2026, 1, 1) + timedelta(days=doy - 1)


def load_state() -> dict:
    if os.path.exists(WATCHER_STATE):
        with open(WATCHER_STATE) as f:
            return json.load(f)
    return {"processed_strokes": {}, "last_run": None}


def save_state(state: dict):
    with open(WATCHER_STATE, "w") as f:
        json.dump(state, f, indent=2)


def cycle(work_dir: str = "/tmp/move-watch") -> dict:
    """Run one full cycle. Returns a summary dict."""
    summary = {"deleted": [], "added": [], "errors": []}

    if os.path.exists(work_dir):
        shutil.rmtree(work_dir)
    os.makedirs(work_dir)

    # 1) Pull bundle
    rmdoc, bundle_dir = pull_bundle(work_dir)

    # 2) Load manifest + state
    if not os.path.exists(RENDER_MANIFEST):
        print("No render manifest yet — run move_render.py at least once first.", file=sys.stderr)
        return summary
    with open(RENDER_MANIFEST) as f:
        manifest = json.load(f)
    by_uuid = manifest.get("by_page_uuid", {})

    state = load_state()
    processed = state.setdefault("processed_strokes", {})

    # 3) Walk .rm files
    content_files = [f for f in os.listdir(bundle_dir) if f.endswith(".content")]
    if not content_files:
        return summary
    with open(os.path.join(bundle_dir, content_files[0])) as f:
        content = json.load(f)
    page_uuid_by_idx = {i: p["id"] for i, p in enumerate(content.get("cPages", {}).get("pages", []))}
    idx_by_uuid = {v: k for k, v in page_uuid_by_idx.items()}

    rm_dir = os.path.join(bundle_dir, content_files[0].replace(".content", ""))
    if not os.path.isdir(rm_dir):
        return summary

    strokes_to_strip_per_page: dict[str, set] = {}

    for rm_file in sorted(os.listdir(rm_dir)):
        if not rm_file.endswith(".rm"):
            continue
        page_uuid = rm_file[:-3]
        idx = idx_by_uuid.get(page_uuid)
        if idx is None:
            continue
        page_date = date_for_day_grid_idx(idx)
        rm_path = os.path.join(rm_dir, rm_file)
        with open(rm_path, "rb") as f:
            blocks = list(read_blocks(f))
        block_strokes = parse_strokes_from_blocks(blocks)
        if not block_strokes:
            continue

        already = set(processed.get(page_uuid, []))
        new_items = [(blk, pts) for blk, pts in block_strokes if stroke_fingerprint(pts) not in already]
        if not new_items:
            continue

        page_manifest = by_uuid.get(page_uuid, [])

        for blk, pts in new_items:
            fp = stroke_fingerprint(pts)
            handled = False

            # Check rectangle FIRST — closed shapes look like very-low-residual lines
            # to is_snap_line, so order matters.
            if is_snap_rect(pts):
                xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                rect_scene = (min(xs), min(ys), max(xs), max(ys))
                interior = [p for blk2, p in block_strokes
                            if blk2 is not blk and stroke_inside_rect(p, rect_scene, pad=2)]
                if interior and page_date:
                    png = render_strokes_to_png(interior)
                    parsed = claude_ocr_event(png)
                    if parsed and parsed.get("title") and parsed.get("time"):
                        hm = parse_time_str(parsed["time"])
                        if hm:
                            h, m = hm
                            start_dt = datetime(page_date.year, page_date.month, page_date.day,
                                                h, m, tzinfo=ZoneInfo("America/Chicago"))
                            end_dt = start_dt + timedelta(hours=1)
                            try:
                                call_calendar_api(
                                    api_key=None,
                                    summary=parsed["title"],
                                    start_iso=start_dt.isoformat(),
                                    end_iso=end_dt.isoformat(),
                                )
                                summary["added"].append({
                                    "title": parsed["title"], "time": parsed["time"], "date": page_date.isoformat(),
                                })
                                strokes_to_strip_per_page.setdefault(page_uuid, set()).add(id(blk))
                                for ib in [blk2 for blk2, p in block_strokes
                                           if blk2 is not blk and stroke_inside_rect(p, rect_scene, pad=2)]:
                                    strokes_to_strip_per_page.setdefault(page_uuid, set()).add(id(ib))
                                handled = True
                            except Exception as e:
                                summary["errors"].append(f"create event: {e}")
                processed.setdefault(page_uuid, []).append(fp)
                continue

            if is_snap_line(pts):
                # Match against rendered events on this page
                pdf_bbox = stroke_bbox_pdf(pts)
                for ev in page_manifest:
                    if not ev.get("event_id"):
                        continue
                    if bbox_overlaps(pdf_bbox, ev["bbox_pdf"], pad=2):
                        try:
                            call_delete_event(ev["event_id"])
                            summary["deleted"].append({
                                "title": ev["title"], "time": ev["time_iso"], "page_uuid": page_uuid,
                            })
                        except Exception as e:
                            summary["errors"].append(f"delete {ev['title']}: {e}")
                        strokes_to_strip_per_page.setdefault(page_uuid, set()).add(id(blk))
                        handled = True
                        break

            # Mark this stroke as processed so we don't re-process next cycle
            processed.setdefault(page_uuid, []).append(fp)

    # 4) If anything happened, strip processed snap strokes from .rm and re-render + push
    if summary["deleted"] or summary["added"]:
        for page_uuid, strip_ids in strokes_to_strip_per_page.items():
            rm_path = os.path.join(rm_dir, page_uuid + ".rm")
            with open(rm_path, "rb") as f:
                blocks = list(read_blocks(f))
            kept = []
            for b in blocks:
                if type(b).__name__ == 'SceneLineItemBlock' and id(b) in strip_ids:
                    continue
                kept.append(b)
            with open(rm_path, "wb") as f:
                write_blocks(f, kept)

        # 5) Re-render planner from baseline using fresh Google state
        api_key = os.environ.get("CALENDAR_API_KEY")
        if not api_key:
            out = subprocess.run(["railway", "variables", "-k"], capture_output=True, text=True, cwd=HERE)
            for line in out.stdout.splitlines():
                if line.startswith("CALENDAR_API_KEY="):
                    api_key = line.split("=", 1)[1]; break
        events = fetch_events("2026-05-01", "2026-05-31", api_key)
        pdf_path = next(os.path.join(bundle_dir, f) for f in os.listdir(bundle_dir) if f.endswith(".pdf"))
        render_planner(events, pdf_path)

        # 6) Push bundle back
        push_bundle(bundle_dir, rmdoc)

    # Even with no gestures, re-render if Google's events have changed since the
    # rendered manifest was generated. This keeps the Move planner in sync with
    # Google Calendar edits made elsewhere (web, phone, etc.).
    if not (summary["deleted"] or summary["added"]):
        events_now = fetch_events("2026-01-01", "2026-12-31", _resolve_calendar_key())
        events_hash = _hash_events(events_now)
        last_hash = state.get("last_events_hash")
        if events_hash != last_hash:
            print(f"Google events hash changed ({last_hash} → {events_hash}); re-rendering.")
            pdf_path = next(os.path.join(bundle_dir, f) for f in os.listdir(bundle_dir) if f.endswith(".pdf"))
            render_planner(events_now, pdf_path)
            push_bundle(bundle_dir, rmdoc)
            state["last_events_hash"] = events_hash
            summary["resynced"] = True
    else:
        # Gestures fired — record current events hash so next idle cycle has a baseline
        events_now = fetch_events("2026-01-01", "2026-12-31", _resolve_calendar_key())
        state["last_events_hash"] = _hash_events(events_now)

    state["last_run"] = datetime.now(ZoneInfo("America/Chicago")).isoformat()
    save_state(state)
    return summary


def _resolve_calendar_key() -> str:
    api_key = os.environ.get("CALENDAR_API_KEY")
    if api_key:
        return api_key
    out = subprocess.run(["railway", "variables", "-k"], capture_output=True, text=True, cwd=HERE)
    for line in out.stdout.splitlines():
        if line.startswith("CALENDAR_API_KEY="):
            return line.split("=", 1)[1]
    raise RuntimeError("CALENDAR_API_KEY not set")


def _hash_events(events: list) -> str:
    import hashlib
    sig = sorted(
        (e.get("id", ""), e.get("summary", ""), e.get("start", ""), e.get("end", ""))
        for e in events
    )
    return hashlib.md5(json.dumps(sig).encode()).hexdigest()


def main():
    print("=== Move daemon cycle ===")
    s = cycle()
    print(json.dumps(s, indent=2))


if __name__ == "__main__":
    main()
