"""
Server-side Hyperpaper calendar overlay generator.
Reads base PDF, overlays handwritten events, returns merged PDF bytes.
"""

import calendar as cal_mod
import hashlib
import io
import json
import os
from datetime import date, datetime, timedelta
from fpdf import FPDF
import pikepdf

BASE_PDF = os.path.join(os.path.dirname(__file__), "hyperpaper_base.pdf")
FONT_PATH = os.path.join(os.path.dirname(__file__), "stroke_font.json")

with open(FONT_PATH) as _f:
    FONT = json.load(_f)

HP_W, HP_H = 452.0, 602.0
INK = (45, 50, 55)

MONTH_CAL_START = 7
DAY_PAGE_START = 73
MONTH_COL_X = [60.0, 116.0, 172.0, 228.0, 284.0, 340.0, 396.0]
MONTH_COL_W = 56.3
MONTH_GRID_TOP = 555.0
MONTH_GRID_BOT = 0.0

DAY_SCHED_X = 340
DAY_SCHED_W = 95
DAY_HOUR_Y = {
    6: 535.0, 7: 501.6, 8: 468.2, 9: 434.8, 10: 401.4,
    11: 368.0, 12: 334.6, 13: 301.2, 14: 267.8, 15: 234.4,
    16: 201.0, 17: 167.6, 18: 134.2, 19: 100.8, 20: 67.4, 21: 34.0,
}
DAY_SLOT_H = 33.4

WEEK_PAGE_START = 20
WEEK_COL_X = [52, 154, 256, 358]
WEEK_COL_W = 96
WEEK_TOP_Y_START = 543
WEEK_BOT_Y_START = 263
WEEK_HOUR_H = 16.7

SKIP_WORDS = {"lunch", "family", "driving", "dinner", "breakfast", "pickup", "drop off", "dropoff"}
SKIP_CONTAINS = {"dominion weekly", "fielder", "podcast", "marc hyde", "pizza", "test", "family", "unplug"}


def pdf_y_to_fpdf_y(pdf_y):
    return HP_H - pdf_y


def handwrite(pdf, text, x, y, scale=5, color=INK, max_width=None):
    r, g, b = color
    pdf.set_draw_color(r, g, b)
    pdf.set_line_width(0.5)
    cx, ly = x, y
    char_w = lambda ch: FONT[ch].get("width_ratio", 0.6) * scale + scale * 0.3 if ch in FONT else scale * 0.4
    if max_width:
        for word in text.split(' '):
            ww = sum(char_w(c) for c in word)
            if cx + ww > x + max_width and cx > x:
                cx = x; ly += scale * 1.4
            for ch in word:
                if ch not in FONT: continue
                cd = FONT[ch]
                for stroke in cd["strokes"]:
                    pts = stroke["points"]
                    for i in range(len(pts) - 1):
                        pdf.line(pts[i]["x"]*scale+cx, pts[i]["y"]*scale+ly,
                                 pts[i+1]["x"]*scale+cx, pts[i+1]["y"]*scale+ly)
                cx += cd.get("width_ratio", 0.6) * scale + scale * 0.3
            cx += scale * 0.4
        return ly - y
    else:
        for ch in text:
            if ch == ' ': cx += scale * 0.4; continue
            if ch not in FONT: continue
            cd = FONT[ch]
            for stroke in cd["strokes"]:
                pts = stroke["points"]
                for i in range(len(pts) - 1):
                    pdf.line(pts[i]["x"]*scale+cx, pts[i]["y"]*scale+ly,
                             pts[i+1]["x"]*scale+cx, pts[i+1]["y"]*scale+ly)
            cx += cd.get("width_ratio", 0.6) * scale + scale * 0.3
        return 0


def day_of_year(month, day, year=2026):
    return (date(year, month, day) - date(year, 1, 1)).days + 1

def day_page(month, day):
    return DAY_PAGE_START - 1 + day_of_year(month, day)

def month_page(month):
    return MONTH_CAL_START - 1 + month

def week_page(week_num):
    return WEEK_PAGE_START - 1 + week_num


def generate_hyperpaper(events, year=2026):
    """Generate merged Hyperpaper PDF with calendar overlay.

    Returns (pdf_bytes, manifest) where manifest is:
      {"page_w": 452.0, "page_h": 602.0,
       "pages": {"<page_idx>": [{"id":..., "title":..., "time":..., "bbox":[x,y,w,h]}, ...]}}
    bbox coordinates are in fpdf top-left origin (matches PDF render coords).
    Only day-page event positions are recorded — that's where strikethrough deletes happen.
    """
    # Count pages from base PDF
    tmp_base = pikepdf.open(BASE_PDF)
    num_pages = len(tmp_base.pages)
    tmp_base.close()

    overlay = FPDF(orientation='P', unit='pt', format=(HP_W, HP_H))
    manifest = {"page_w": HP_W, "page_h": HP_H, "pages": {}}

    for page_idx in range(num_pages):
        overlay.add_page()
        overlay.set_auto_page_break(auto=False)

        # Monthly calendar pages
        for m in range(1, 13):
            if page_idx == month_page(m):
                cal = cal_mod.Calendar(0)
                weeks = cal.monthdayscalendar(year, m)
                num_weeks = len(weeks)
                row_h = (MONTH_GRID_TOP - MONTH_GRID_BOT) / num_weeks
                for wi, week in enumerate(weeks):
                    for di, d in enumerate(week):
                        if d == 0: continue
                        evts = events.get((year, m, d), [])
                        if not evts: continue
                        row_top_y = MONTH_GRID_TOP - wi * row_h
                        cell_x = MONTH_COL_X[di] + 4
                        ey = pdf_y_to_fpdf_y(row_top_y) + 14
                        for t, title, _ev_id in evts[:3]:
                            extra = handwrite(overlay, "%s %s" % (t, title),
                                            cell_x, ey, scale=3.3, max_width=48)
                            ey += 18 + (extra or 0)
                break

        # Weekly planning pages
        for wn in range(1, 54):
            if page_idx == week_page(wn):
                jan4 = date(year, 1, 4)
                week_monday = jan4 + timedelta(weeks=wn - 1, days=-jan4.weekday())
                for dow in range(7):
                    d = week_monday + timedelta(days=dow)
                    if d.year != year: continue
                    evts = events.get((d.year, d.month, d.day), [])
                    if not evts: continue
                    if dow < 4:
                        col_x = WEEK_COL_X[dow]
                        base_y = WEEK_TOP_Y_START
                    else:
                        col_x = WEEK_COL_X[dow - 4]
                        base_y = WEEK_BOT_Y_START
                    for t, title, _ev_id in evts:
                        try:
                            t_clean = t.strip().lower()
                            pm = 'p' in t_clean
                            t_clean = t_clean.replace('a','').replace('p','').replace('m','')
                            parts = t_clean.split(':')
                            hr = int(parts[0])
                            mn = int(parts[1]) if len(parts) > 1 else 0
                            if pm and hr != 12: hr += 12
                            if not pm and hr == 12: hr = 0
                        except: continue
                        if hr < 6 or hr > 21: continue
                        slot_y = base_y - (hr - 6) * WEEK_HOUR_H
                        fpdf_y = pdf_y_to_fpdf_y(slot_y) + 4
                        handwrite(overlay, title, col_x + 2, fpdf_y,
                                  scale=5, max_width=WEEK_COL_W - 4)
                break

        # Day pages
        for m in range(1, 13):
            for d in range(1, 32):
                try:
                    date(year, m, d)
                except ValueError:
                    continue
                if page_idx == day_page(m, d):
                    evts = events.get((year, m, d), [])
                    for t, title, ev_id in evts:
                        try:
                            t_clean = t.strip().lower()
                            pm = 'p' in t_clean
                            t_clean = t_clean.replace('a','').replace('p','').replace('m','')
                            parts = t_clean.split(':')
                            hr = int(parts[0])
                            mn = int(parts[1]) if len(parts) > 1 else 0
                            if pm and hr != 12: hr += 12
                            if not pm and hr == 12: hr = 0
                        except: continue
                        if hr not in DAY_HOUR_Y: continue
                        sep_y = DAY_HOUR_Y[hr]
                        half_offset = DAY_SLOT_H * (mn / 60.0)
                        dot_y = pdf_y_to_fpdf_y(sep_y - half_offset)
                        # Title first, measure wrap height
                        title_y = dot_y - 7
                        title_extra = handwrite(overlay, title, DAY_SCHED_X, title_y,
                                  scale=4.5, max_width=DAY_SCHED_W)
                        # Time sits below title (accounting for wrap)
                        time_y = title_y + 8 + (title_extra or 0)
                        handwrite(overlay, t, DAY_SCHED_X, time_y,
                                  scale=2.5, color=(180, 60, 100))
                        # Manifest bbox: covers title + time, in fpdf coords (top-left origin)
                        bbox_h = (time_y + 4) - title_y
                        if ev_id:
                            manifest["pages"].setdefault(str(page_idx), []).append({
                                "id": ev_id,
                                "title": title,
                                "time": t,
                                "bbox": [DAY_SCHED_X, title_y, DAY_SCHED_W, bbox_h],
                            })
                    break
            else:
                continue
            break

    # Track which pages have overlay content
    pages_with_events = set()
    if events:
        for m in range(1, 13):
            mp = month_page(m)
            cal = cal_mod.Calendar(0)
            for week in cal.monthdayscalendar(year, m):
                for d in week:
                    if d and events.get((year, m, d)):
                        pages_with_events.add(mp)
        for (y, m, d) in events:
            if y == year:
                pages_with_events.add(day_page(m, d))
                dt = date(year, m, d)
                pages_with_events.add(week_page(dt.isocalendar()[1]))

    # Merge using pikepdf — preserves all links, named destinations, annotations
    overlay_bytes = overlay.output()
    base = pikepdf.open(BASE_PDF)
    ovl = pikepdf.open(io.BytesIO(overlay_bytes))

    for page_idx in pages_with_events:
        if page_idx < len(base.pages) and page_idx < len(ovl.pages):
            base.pages[page_idx].add_overlay(ovl.pages[page_idx])

    buf = io.BytesIO()
    base.save(buf)
    ovl.close()
    base.close()
    return buf.getvalue(), manifest


def events_hash(events):
    s = {"%d-%d-%d" % k: v for k, v in events.items()}
    return hashlib.md5(json.dumps(s, sort_keys=True).encode()).hexdigest()
