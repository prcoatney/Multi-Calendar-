"""
Render Google Calendar events onto a hyperpaper.me Move planner PDF.

Pulls the user's Move planner bundle from reMarkable cloud, overlays events
onto day-grid pages (and eventually month + week views), repackages the bundle,
and pushes back via `rmapi put --force`.

Uses the same handwritten-font visual style as the rmpp Hyperpaper:
- Event title in dark ink handwriting
- Time below title in pink/magenta
"""
import fitz
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import urllib.parse
from datetime import date, datetime
from zoneinfo import ZoneInfo
from zipfile import ZipFile

RMAPI = "/Users/coat/claude/rmapi-new"
PLANNER_PATH = "/MOVE/planner-2026.2.6.4-prcoatney@gmail.com"
HERE = os.path.dirname(os.path.abspath(__file__))
BASELINE_PDF = os.path.join(HERE, "move-planner-baseline.pdf")
ABBREV_CACHE = os.path.join(HERE, "move-abbreviations.json")
ABBREV_MAX_CHARS = 14
ABBREV_MODEL = "claude-haiku-4-5-20251001"
RENDER_MANIFEST = os.path.join(HERE, "move-render-manifest.json")

with open(os.path.join(HERE, "stroke_font.json")) as _f:
    FONT = json.load(_f)

SKIP_WORDS = {"lunch", "family", "driving", "dinner", "breakfast", "pickup", "drop off", "dropoff"}
SKIP_CONTAINS = {"dominion weekly", "fielder", "podcast", "marc hyde", "pizza", "test", "family", "unplug"}

# Time-slot Y-coordinates on day_grid pages (PDF pt). 4am-3pm visible.
TIME_SLOTS = {
    4:  68.1, 5: 101.5, 6: 134.9, 7: 168.3, 8: 201.7, 9: 235.1,
    10: 268.5, 11: 301.9, 12: 335.3, 13: 368.7, 14: 402.1, 15: 435.5,
}
TIME_SLOT_PITCH = 33.4

# Day-grid event area (right of time labels at x=156-173, before page edge at 250)
DAY_SCHED_X = 175
DAY_SCHED_W = 70

# ──────── Week-page layout (doc[16..68]) ────────
# 6 day-cells (MON/TUE/WED row 1, THU/FRI/SAT row 2) + SUN tall column.
# MON-FRI have time grids (4am-2pm). SAT and SUN are list-style cells.
# Hour pitch ≈ 16.7 pt — half of day-grid pitch.
WEEK_PAGE_RANGE = (16, 68)  # 0-indexed doc[16..68], W1..W53

# Per-day cell config: time-grid hour-y plus event-area x-bounds.
# Extracted empirically from doc[35] (W20).
WEEK_CELL = {
    'MON': {'event_x': (18, 83),   'hour_y': {4: 52.9, 5: 69.6, 6: 86.3, 7: 103.0, 8: 119.7, 9: 136.4, 10: 153.1, 11: 169.8, 12: 186.5, 13: 203.2, 14: 219.9}},
    'TUE': {'event_x': (97, 160),  'hour_y': {4: 52.9, 5: 69.6, 6: 86.3, 7: 103.0, 8: 119.7, 9: 136.4, 10: 153.1, 11: 169.8, 12: 186.5, 13: 203.2, 14: 219.9}},
    'WED': {'event_x': (177, 242), 'hour_y': {4: 52.9, 5: 69.6, 6: 86.3, 7: 103.0, 8: 119.7, 9: 136.4, 10: 153.1, 11: 169.8, 12: 186.5, 13: 203.2, 14: 219.9}},
    'THU': {'event_x': (18, 83),   'hour_y': {4: 254.0, 5: 270.7, 6: 287.4, 7: 304.1, 8: 320.8, 9: 337.5, 10: 354.2, 11: 370.9, 12: 387.6, 13: 404.3, 14: 421.0}},
    'FRI': {'event_x': (97, 160),  'hour_y': {4: 254.0, 5: 270.7, 6: 287.4, 7: 304.1, 8: 320.8, 9: 337.5, 10: 354.2, 11: 370.9, 12: 387.6, 13: 404.3, 14: 421.0}},
    # SAT and SUN: list-style cells. Header y_top, event area x-bounds, list-start y.
    'SAT': {'event_x': (177, 242), 'list_start_y': 252.0, 'list_max_y': 332.0},
    'SUN': {'event_x': (177, 242), 'list_start_y': 346.0, 'list_max_y': 440.0},
}
WEEK_HOUR_PITCH = 16.7
WEEK_TITLE_SCALE = 3.45  # 1.5x prior 2.3 — patron confirmed cells have horizontal room
WEEK_TIME_SCALE = 2.25

# Visual styling
TITLE_SCALE = 3.9   # 1.5x prior 2.6
TIME_SCALE = 2.55   # 1.5x prior 1.7
INK_COLOR = (0.05, 0.05, 0.4)
TIME_COLOR = (0.7, 0.23, 0.4)  # rmpp's (180, 60, 100) → 0-1 floats
LINE_WIDTH = 0.45
WORDS_PER_LINE = 2

# Page sections
DAY_GRID_FIRST = 69


def doy(d: date) -> int:
    return (d - date(d.year, 1, 1)).days + 1


def day_grid_idx(d: date) -> int:
    return DAY_GRID_FIRST + (doy(d) - 1) * 2


def filter_event(summary: str) -> bool:
    s = summary.strip().lower()
    if s in SKIP_WORDS:
        return True
    if any(c in s for c in SKIP_CONTAINS):
        return True
    return False


def fetch_events(start: str, end: str, api_key: str, member_id: int = 2,
                 org_slug: str = "cross-formed-kids") -> list:
    url = f"https://multi-calendar-production.up.railway.app/api/{org_slug}/members/{member_id}/events"
    qs = urllib.parse.urlencode({"start": start, "end": end, "max": 500, "key": api_key})
    with urllib.request.urlopen(f"{url}?{qs}") as r:
        return json.load(r)


def parse_event_dt(ev: dict):
    s = ev.get("start", "")
    if not s or "T" not in s:
        return None
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s[:-1] + "+00:00")
        else:
            dt = datetime.fromisoformat(s)
        return dt.astimezone(ZoneInfo("America/Chicago"))
    except Exception:
        return None


def time_label(dt: datetime) -> str:
    h, m = dt.hour, dt.minute
    ampm = "am" if h < 12 else "pm"
    h12 = h if h <= 12 else h - 12
    if h12 == 0:
        h12 = 12
    return f"{h12}{ampm}" if m == 0 else f"{h12}:{m:02d}{ampm}"


def y_for_time(dt: datetime):
    h, m = dt.hour, dt.minute
    if h < 4 or h > 15:
        return None
    if h == 15 and m > 30:
        return None
    return TIME_SLOTS[h] + (m / 60.0) * TIME_SLOT_PITCH


def char_width(ch: str, scale: float) -> float:
    if ch == ' ':
        return scale * 0.4
    if ch in FONT:
        return FONT[ch].get('width_ratio', 0.6) * scale + scale * 0.3
    return scale * 0.4


def measure_text(text: str, scale: float, max_width: float | None = None) -> tuple[float, int]:
    """Return (lines_count, total_width_first_line) — used for layout."""
    if not max_width:
        return 1, sum(char_width(c, scale) for c in text)
    lines = 1
    cx = 0.0
    for word in text.split(' '):
        ww = sum(char_width(c, scale) for c in word)
        if cx + ww > max_width and cx > 0:
            lines += 1
            cx = 0
        for ch in word:
            cx += char_width(ch, scale)
        cx += scale * 0.4
    return lines, max_width


def draw_char(page: fitz.Page, ch: str, ox: float, oy: float, scale: float, color: tuple, line_width: float):
    if ch == ' ' or ch not in FONT:
        return
    cd = FONT[ch]
    for stroke in cd['strokes']:
        pts = stroke['points']
        for i in range(len(pts) - 1):
            p1 = (pts[i]['x'] * scale + ox, pts[i]['y'] * scale + oy)
            p2 = (pts[i + 1]['x'] * scale + ox, pts[i + 1]['y'] * scale + oy)
            page.draw_line(p1, p2, color=color, width=line_width)


def handwrite(page: fitz.Page, text: str, x: float, y: float, scale: float = 2.5,
              color: tuple = INK_COLOR, max_width: float | None = None,
              line_width: float = LINE_WIDTH) -> float:
    """Draw text in handwriting style. Returns extra Y offset added by line wraps."""
    cx, ly = x, y
    line_height = scale * 1.4
    if max_width:
        for word in text.split(' '):
            ww = sum(char_width(c, scale) for c in word)
            if cx + ww > x + max_width and cx > x:
                cx = x
                ly += line_height
            for ch in word:
                draw_char(page, ch, cx, ly, scale, color, line_width)
                cx += char_width(ch, scale)
            cx += scale * 0.4
        return ly - y
    else:
        for ch in text:
            if ch == ' ':
                cx += scale * 0.4
                continue
            draw_char(page, ch, cx, ly, scale, color, line_width)
            cx += char_width(ch, scale)
        return 0


def split_lines(title: str, words_per_line: int = WORDS_PER_LINE) -> list[str]:
    words = title.split()
    return [' '.join(words[i:i + words_per_line]) for i in range(0, len(words), words_per_line)]


def _load_abbrev_cache() -> dict:
    if os.path.exists(ABBREV_CACHE):
        with open(ABBREV_CACHE) as f:
            return json.load(f)
    return {}


def _save_abbrev_cache(cache: dict):
    with open(ABBREV_CACHE, 'w') as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def abbreviate_titles(titles: list[str]) -> dict[str, str]:
    """Batch-abbreviate titles using Claude Haiku, caching by original title.

    Returns {original: abbreviated}. Cache lives at ABBREV_CACHE.
    Cost is trivial: ~$0.0014 per batch of 50 titles.
    """
    cache = _load_abbrev_cache()
    distinct = sorted(set(t.strip() for t in titles if t.strip()))
    needed = [t for t in distinct if t not in cache]
    if not needed:
        return cache

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        out = subprocess.run(["railway", "variables", "-k"], capture_output=True, text=True, cwd=HERE)
        for line in out.stdout.splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                api_key = line.split("=", 1)[1]
                break
    if not api_key:
        print("WARN: ANTHROPIC_API_KEY not set; skipping abbreviation, using full titles", file=sys.stderr)
        return {t: t for t in distinct}

    # Batch in chunks of 50
    BATCH = 50
    for i in range(0, len(needed), BATCH):
        chunk = needed[i:i + BATCH]
        numbered = "\n".join(f"{n+1}. {t}" for n, t in enumerate(chunk))
        prompt = (
            f"Abbreviate each calendar event title to {ABBREV_MAX_CHARS} characters or fewer "
            f"while preserving the load-bearing detail.\n\n"
            f"Rules:\n"
            f"- If the title contains a PERSON'S NAME, the name is the most important part — keep it. "
            f"Drop the event-type word if needed to fit (e.g., 'Imago Tour: Ashley O'Brien' → "
            f"'Tour: Ashley' or 'Ashley O'Brien'; 'Interview: Cindy Strauss' → 'Intvw Cindy').\n"
            f"- Use first name + last initial if both don't fit (e.g., 'Alexandria F.').\n"
            f"- Drop filler verbs/types only when no name is present: 'Meeting' → 'Mtg', "
            f"'Conference' → 'Conf', 'Interview' → 'Intvw', 'Webinar' → 'Webinar' (keep, it's already short).\n"
            f"- Keep punctuation only if essential to meaning.\n"
            f"- If the title is already ≤ {ABBREV_MAX_CHARS} chars, return it unchanged.\n\n"
            f"Output ONLY a JSON object mapping each original title (verbatim) to its abbreviation, "
            f"no other text.\n\nTitles:\n{numbered}"
        )
        body = json.dumps({
            "model": ABBREV_MODEL,
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.load(r)
        text = "".join(c.get("text", "") for c in resp.get("content", []) if c.get("type") == "text")
        # Extract JSON object
        try:
            start = text.find("{")
            end = text.rfind("}") + 1
            mapping = json.loads(text[start:end])
        except Exception as e:
            print(f"WARN: failed to parse abbreviation response: {e}", file=sys.stderr)
            print(f"  raw: {text[:500]}", file=sys.stderr)
            continue
        for orig, abbr in mapping.items():
            if isinstance(abbr, str) and abbr.strip():
                cache[orig] = abbr.strip()
        _save_abbrev_cache(cache)
        print(f"  abbreviated {len(mapping)} titles (batch {i//BATCH + 1})")

    return cache


def render_day_grid(page: fitz.Page, events_for_day: list) -> list[dict]:
    """events_for_day: list of (datetime, title, event_id) sorted by time.

    Events render at their TRUE time-slot Y position; overlap on dense days is
    accepted (patron-decided). Returns a manifest list of:
      [{event_id, time_iso, title, bbox_pdf: [x0,y0,x1,y1]}]
    for use by the watcher daemon to map snap-strike strokes back to events."""
    title_line_h = TITLE_SCALE * 1.4
    manifest = []
    for tup in events_for_day:
        if len(tup) == 3:
            dt, title, event_id = tup
        else:
            dt, title = tup; event_id = None
        y = y_for_time(dt)
        if y is None:
            continue
        title_y = y - TITLE_SCALE * 1.0
        cur_y = title_y
        for line in split_lines(title):
            handwrite(page, line, DAY_SCHED_X, cur_y, scale=TITLE_SCALE, color=INK_COLOR)
            cur_y += title_line_h
        time_y = cur_y + 1
        handwrite(page, time_label(dt), DAY_SCHED_X, time_y,
                  scale=TIME_SCALE, color=TIME_COLOR)
        bbox = [DAY_SCHED_X, title_y - 1, DAY_SCHED_X + DAY_SCHED_W, time_y + TIME_SCALE * 1.4]
        manifest.append({
            'event_id': event_id,
            'time_iso': dt.isoformat(),
            'title': title,
            'bbox_pdf': bbox,
        })
    return manifest


DAY_NAMES = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']

# ──────── Month-overview layout (doc[3..14]) ────────
# 7 columns × 5 rows, day-number at top-right of each cell.
# Patron rule: render TIME ONLY (no titles) — month cells are too small for words.
MONTH_PAGE_FIRST = 3        # doc[3] = January
MONTH_COL_X_LEFT = 10       # left margin
MONTH_COL_W = 33            # cell width
MONTH_ROW_Y_TOPS = {
    # week_index_in_month → row top y. Row containing day 1 may be partial (W18 in May).
    # Empirically extracted from May (doc[7]):
    0: 42, 1: 129, 2: 207, 3: 285, 4: 363,
}
MONTH_ROW_H = 78
MONTH_DAY_NUM_OFFSET_Y = 9  # y from row_top to bottom of day-number text (approx)
MONTH_TIME_SCALE = 4.8         # 3x prior 1.6
MONTH_TIME_LINE_H = MONTH_TIME_SCALE * 1.5


def iso_week_to_doc_idx(d: date) -> int | None:
    """Map a date to the week-page doc_idx via ISO week. The hyperpaper.me
    Move planner uses W1=Dec 29, 2025 (year-spanning ISO week)."""
    # W1 in this planner = Dec 29 2025 - Jan 4 2026
    # That's ISO 2026 W1 (which actually starts Dec 29 if Jan 1 is in a week with mostly 2026 days)
    # Empirically: doc[16] = W1 (DEC 29-JAN 4); doc[16+(n-1)] = Wn
    # Map by date → week number based on starting Mon.
    week_start = date(2025, 12, 29)
    if d < week_start:
        return None
    week_num = (d - week_start).days // 7 + 1
    if week_num < 1 or week_num > 53:
        return None
    return WEEK_PAGE_RANGE[0] + week_num - 1


def day_name(d: date) -> str:
    return DAY_NAMES[d.weekday()]


def short_time(dt: datetime) -> str:
    """Compact time label for month overview cells: 8a, 10a, 12p, 1:30p."""
    h, m = dt.hour, dt.minute
    ampm = 'a' if h < 12 else 'p'
    h12 = h if h <= 12 else h - 12
    if h12 == 0:
        h12 = 12
    return f"{h12}{ampm}" if m == 0 else f"{h12}:{m:02d}{ampm}"


def month_cell_bbox(d: date) -> tuple[float, float, float, float]:
    """Returns (x_left, y_top, x_right, y_bottom) for a date's cell on its month overview page.
    y_top is the cell's top edge; the day number sits in the top-right corner."""
    # Figure out which "row" within the month this date falls in
    first = date(d.year, d.month, 1)
    first_weekday = first.weekday()  # 0=Mon
    day_num = d.day
    week_in_month = (first_weekday + day_num - 1) // 7
    col = d.weekday()  # 0=Mon
    x_left = MONTH_COL_X_LEFT + col * MONTH_COL_W
    x_right = x_left + MONTH_COL_W
    y_top = MONTH_ROW_Y_TOPS.get(week_in_month, 42 + week_in_month * MONTH_ROW_H)
    y_bot = y_top + MONTH_ROW_H
    return x_left, y_top, x_right, y_bot


def render_month_overview(page: fitz.Page, events_by_day: dict[date, list]):
    """Render TIME-ONLY markers on the month overview page. No titles."""
    for d, evs in events_by_day.items():
        x_left, y_top, x_right, y_bot = month_cell_bbox(d)
        cell_w = x_right - x_left
        cur_y = y_top + 16
        for item in evs:
            dt = item[0]
            if cur_y + MONTH_TIME_LINE_H > y_bot - 2:
                break
            handwrite(page, short_time(dt), x_left + 5, cur_y,
                      scale=MONTH_TIME_SCALE, color=TIME_COLOR)
            cur_y += MONTH_TIME_LINE_H


def is_dense(evs: list) -> bool:
    """A day's events are 'dense' if 3+ fall within any 3-hour window.
    When dense, we switch to abbreviated titles to avoid heavy overlap."""
    if len(evs) < 3:
        return False
    times = sorted(t[0].hour + t[0].minute / 60.0 for t in evs)
    for i in range(len(times) - 2):
        if times[i + 2] - times[i] <= 3.0:
            return True
    return False


def render_week_planning(page: fitz.Page, events_by_day: dict[date, list],
                         abbrev_map: dict[str, str] | None = None):
    """Render events on a week page. events_by_day maps date → [(dt, title), ...]
    for dates that fall in this week. abbrev_map is consulted for dense days only."""
    title_line_h = WEEK_TITLE_SCALE * 1.4
    abbrev_map = abbrev_map or {}
    for d, evs in events_by_day.items():
        dn = day_name(d)
        cell = WEEK_CELL.get(dn)
        if not cell:
            continue
        x_left, x_right = cell['event_x']
        cell_w = x_right - x_left
        # Dense days fall back to abbreviated titles to keep overlap manageable
        dense = is_dense(evs)
        evs_to_render = [(item[0], abbrev_map.get(item[1], item[1]) if dense else item[1])
                         for item in evs]
        if 'hour_y' in cell:
            # Time-grid cell (MON/TUE/WED/THU/FRI)
            for dt, title in evs_to_render:
                h = dt.hour
                m = dt.minute
                if h not in cell['hour_y']:
                    continue
                slot_y = cell['hour_y'][h] + (m / 60.0) * WEEK_HOUR_PITCH
                title_y = slot_y - WEEK_TITLE_SCALE * 1.0
                title_extra = handwrite(page, title, x_left, title_y,
                                        scale=WEEK_TITLE_SCALE, color=INK_COLOR,
                                        max_width=cell_w)
                time_y = title_y + title_line_h + title_extra + 0.5
                handwrite(page, time_label(dt), x_left, time_y,
                          scale=WEEK_TIME_SCALE, color=TIME_COLOR)
        else:
            # List-style cell (SAT/SUN)
            cur_y = cell['list_start_y']
            for dt, title in evs_to_render:
                if cur_y > cell['list_max_y']:
                    break
                handwrite(page, time_label(dt), x_left, cur_y,
                          scale=WEEK_TIME_SCALE, color=TIME_COLOR)
                cur_y += WEEK_TIME_SCALE * 1.4
                title_extra = handwrite(page, title, x_left, cur_y,
                                        scale=WEEK_TITLE_SCALE, color=INK_COLOR,
                                        max_width=cell_w)
                cur_y += title_line_h + title_extra + 1.5


def render_planner(events: list, planner_pdf_path: str, year: int = 2026, use_abbrev: bool = True):
    # Always start from the clean baseline; never re-render on top of an
    # already-rendered cloud copy. Output is written to planner_pdf_path,
    # replacing whatever was in the bundle.
    doc = fitz.open(BASELINE_PDF)

    # Collect titles + abbreviate once
    raw_titles = []
    parsed = []  # (dt, title, event_id)
    for ev in events:
        title = ev.get("summary", "").strip()
        if not title or filter_event(title):
            continue
        dt = parse_event_dt(ev)
        if dt is None:
            continue
        ev_id = ev.get("id", "")
        parsed.append((dt, title, ev_id))
        raw_titles.append(title)

    abbrev_map = abbreviate_titles(raw_titles) if use_abbrev else {t: t for t in raw_titles}

    # Day-page render uses abbreviated titles; week-page uses full titles ("whole").
    by_date_abbrev = {}
    by_date_full = {}
    for dt, title, ev_id in parsed:
        by_date_abbrev.setdefault(dt.date(), []).append((dt, abbrev_map.get(title, title), ev_id))
        by_date_full.setdefault(dt.date(), []).append((dt, title, ev_id))
    for d in by_date_abbrev:
        by_date_abbrev[d].sort(key=lambda x: (x[0].hour, x[0].minute))
        by_date_full[d].sort(key=lambda x: (x[0].hour, x[0].minute))

    # Map page idx → page UUID via cPages.pages
    bundle_dir = os.path.dirname(planner_pdf_path)
    content_files = [f for f in os.listdir(bundle_dir) if f.endswith('.content')]
    page_uuid_by_idx = {}
    if content_files:
        with open(os.path.join(bundle_dir, content_files[0])) as f:
            content = json.load(f)
        for i, p in enumerate(content.get('cPages', {}).get('pages', [])):
            page_uuid_by_idx[i] = p.get('id')

    manifest_by_uuid: dict[str, list] = {}
    rendered_days = 0
    rendered_events = 0
    for d, evs in sorted(by_date_abbrev.items()):
        idx = day_grid_idx(d)
        if idx >= doc.page_count:
            continue
        page = doc[idx]
        before = sum(1 for e in evs if y_for_time(e[0]) is not None)
        page_manifest = render_day_grid(page, evs)
        uid = page_uuid_by_idx.get(idx)
        if uid and page_manifest:
            manifest_by_uuid[uid] = page_manifest
        if before:
            rendered_days += 1
            rendered_events += before

    # Group events by week and render week pages with FULL titles
    weeks: dict[int, dict[date, list]] = {}
    for d, evs in by_date_full.items():
        widx = iso_week_to_doc_idx(d)
        if widx is None:
            continue
        weeks.setdefault(widx, {})[d] = evs
    rendered_weeks = 0
    for widx, days in weeks.items():
        if widx >= doc.page_count:
            continue
        render_week_planning(doc[widx], days, abbrev_map=abbrev_map)
        rendered_weeks += 1
    print(f'  Week pages rendered: {rendered_weeks}')

    # Group by month and render month overviews (time-only markers)
    months: dict[int, dict[date, list]] = {}
    for d, evs in by_date_full.items():
        midx = MONTH_PAGE_FIRST + (d.month - 1)
        months.setdefault(midx, {})[d] = evs
    rendered_months = 0
    for midx, days in months.items():
        if midx >= doc.page_count:
            continue
        render_month_overview(doc[midx], days)
        rendered_months += 1
    print(f'  Month overviews rendered: {rendered_months}')
    # Full save (not incremental) to planner_pdf_path; this REPLACES the bundle's
    # embedded PDF with our freshly-rendered version on top of the clean baseline.
    doc.save(planner_pdf_path, garbage=4, deflate=True)
    doc.close()

    # Persist manifest for the watcher daemon
    with open(RENDER_MANIFEST, 'w') as f:
        json.dump({
            'generated_at': datetime.now(ZoneInfo('America/Chicago')).isoformat(),
            'by_page_uuid': manifest_by_uuid,
        }, f, indent=2)
    return rendered_days, rendered_events


def pull_bundle(local_dir: str):
    os.makedirs(local_dir, exist_ok=True)
    subprocess.run([RMAPI, "refresh"], check=True, capture_output=True)
    subprocess.run([RMAPI, "get", PLANNER_PATH], cwd=local_dir, check=True, capture_output=True)
    rmdoc = os.path.join(local_dir, "planner-2026.2.6.4-prcoatney@gmail.com.rmdoc")
    bundle_dir = os.path.join(local_dir, "bundle")
    if os.path.exists(bundle_dir):
        shutil.rmtree(bundle_dir)
    with ZipFile(rmdoc) as z:
        z.extractall(bundle_dir)
    return rmdoc, bundle_dir


def push_bundle(bundle_dir: str, out_rmdoc: str):
    if os.path.exists(out_rmdoc):
        os.remove(out_rmdoc)
    cwd_save = os.getcwd()
    os.chdir(bundle_dir)
    try:
        subprocess.run(["zip", "-q", "-r", out_rmdoc, "."], check=True)
    finally:
        os.chdir(cwd_save)
    subprocess.run([RMAPI, "put", "--force", out_rmdoc, "/MOVE/"], check=True, capture_output=True)


def main():
    work = "/tmp/move-render"
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(work)

    api_key = os.environ.get("CALENDAR_API_KEY")
    if not api_key:
        out = subprocess.run(["railway", "variables", "-k"], capture_output=True, text=True, cwd=HERE)
        for line in out.stdout.splitlines():
            if line.startswith("CALENDAR_API_KEY="):
                api_key = line.split("=", 1)[1]
                break
    if not api_key:
        print("CALENDAR_API_KEY not found", file=sys.stderr)
        sys.exit(1)

    print("Fetching May 2026 events...")
    events = fetch_events("2026-05-01", "2026-05-31", api_key)
    print(f"  {len(events)} raw events")

    print("Pulling Move planner bundle...")
    rmdoc, bundle_dir = pull_bundle(work)
    pdf_path = None
    for f in os.listdir(bundle_dir):
        if f.endswith(".pdf"):
            pdf_path = os.path.join(bundle_dir, f)
            break
    print(f"  PDF: {os.path.basename(pdf_path)}")

    print("Rendering events...")
    days, evts = render_planner(events, pdf_path)
    print(f"  Rendered {evts} events across {days} days")

    print("Repackaging and pushing...")
    out_rmdoc = os.path.join(work, "planner-2026.2.6.4-prcoatney@gmail.com.rmdoc")
    push_bundle(bundle_dir, out_rmdoc)
    print("  Pushed.")

    print("Done. Sync the Move (close and reopen the planner) to see events.")


if __name__ == "__main__":
    main()
