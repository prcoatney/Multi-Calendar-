"""
Server-side planner PDF generator. Imported by app.py.
Generates a CFK Living Planner PDF with handwritten calendar events.
"""

import calendar as cal_mod
import hashlib
import io
import json
import os
from datetime import date, timedelta, datetime
from fpdf import FPDF

FONT_PATH = os.path.join(os.path.dirname(__file__), "stroke_font.json")
with open(FONT_PATH) as _f:
    FONT = json.load(_f)

PAGE_W, PAGE_H = 612.0, 792.0
M_L, M_R, M_T, M_B = 40, 572, 40, 752

NAVY = (45, 55, 80)
CYAN = (0, 160, 170)
MAGENTA = (180, 60, 100)
TEXT = (60, 60, 65)
INK = (45, 50, 55)
LIGHT = (160, 165, 170)
VLIGHT = (190, 192, 195)
WHITE = (255, 255, 255)
GRADIENT = [(45,55,80),(50,100,170),(0,160,170),(100,170,80),
            (200,180,50),(200,140,60),(180,90,100),(100,190,200)]

DAYS = ["SUNDAY","MONDAY","TUESDAY","WEDNESDAY","THURSDAY","FRIDAY","SATURDAY"]
MONTHS = ["","JANUARY","FEBRUARY","MARCH","APRIL","MAY","JUNE",
          "JULY","AUGUST","SEPTEMBER","OCTOBER","NOVEMBER","DECEMBER"]

NAV_Y, NAV_H = 20, 22
HY = NAV_Y + NAV_H + 12


def handwrite(pdf, text, x, y, scale=8, color=INK, max_width=None):
    r, g, b = color
    pdf.set_draw_color(r, g, b)
    pdf.set_line_width(0.8)
    cx = x
    line_y = y
    char_w = lambda ch: FONT[ch].get("width_ratio", 0.6) * scale + scale * 0.3 if ch in FONT else scale * 0.4
    if max_width:
        words = text.split(' ')
        for wi, word in enumerate(words):
            word_w = sum(char_w(ch) for ch in word)
            if cx + word_w > x + max_width and cx > x:
                cx = x
                line_y += scale * 1.4
            for ch in word:
                if ch not in FONT: continue
                cd = FONT[ch]
                for stroke in cd["strokes"]:
                    pts = stroke["points"]
                    for i in range(len(pts) - 1):
                        pdf.line(pts[i]["x"]*scale+cx, pts[i]["y"]*scale+line_y,
                                 pts[i+1]["x"]*scale+cx, pts[i+1]["y"]*scale+line_y)
                cx += cd.get("width_ratio", 0.6) * scale + scale * 0.3
            cx += scale * 0.4
        return line_y - y
    else:
        for ch in text:
            if ch == ' ':
                cx += scale * 0.4; continue
            if ch not in FONT: continue
            cd = FONT[ch]
            for stroke in cd["strokes"]:
                pts = stroke["points"]
                for i in range(len(pts) - 1):
                    pdf.line(pts[i]["x"]*scale+cx, pts[i]["y"]*scale+y,
                             pts[i+1]["x"]*scale+cx, pts[i+1]["y"]*scale+y)
            cx += cd.get("width_ratio", 0.6) * scale + scale * 0.3
        return 0


def gradient(pdf, y, h=4):
    sw = (M_R - M_L) / len(GRADIENT)
    for i, c in enumerate(GRADIENT):
        pdf.set_fill_color(*c); pdf.rect(M_L + i*sw, y, sw+0.5, h, style='F')


def footer(pdf):
    gradient(pdf, M_B + 5)
    pdf.set_font("Helvetica", "", 6); pdf.set_text_color(160,160,160)
    pdf.set_xy(M_L, M_B + 12); pdf.cell(200, 8, "Cross Formed Kids")


def nav_bar(pdf, links, current, days):
    y, h = NAV_Y, NAV_H
    x = M_L
    def tab(label, key, w, accent=CYAN):
        nonlocal x
        cur = (key == current)
        if cur:
            pdf.set_fill_color(*accent); pdf.rect(x, y, w, h, style='F')
            pdf.set_text_color(*WHITE)
        else:
            pdf.set_draw_color(*LIGHT); pdf.set_line_width(0.4)
            pdf.rect(x, y, w, h); pdf.set_text_color(*TEXT)
        pdf.set_font("Helvetica", "B" if cur else "", 8)
        pdf.set_xy(x, y+4); pdf.cell(w, h-8, label, align="C")
        if not cur and key in links: pdf.link(x, y, w, h, links[key])
        x += w + 3

    tab("APR", "m_apr", 38); tab("MAY", "m_may", 38); x += 2; tab("WEEK", "week", 42)
    x += 5
    dl = ["S","M","T","W","T","F","S"]
    for i in range(7):
        cur = current in ("d_%d"%i, "j_%d"%i)
        accent = MAGENTA if current == "j_%d"%i else CYAN
        if cur:
            pdf.set_fill_color(*accent); pdf.rect(x, y, 32, h, style='F'); pdf.set_text_color(*WHITE)
        else:
            pdf.set_draw_color(*LIGHT); pdf.set_line_width(0.4)
            pdf.rect(x, y, 32, h); pdf.set_text_color(*TEXT)
        pdf.set_font("Helvetica", "B" if cur else "", 7)
        pdf.set_xy(x, y+2); pdf.cell(32, 10, dl[i], align="C")
        pdf.set_font("Helvetica", "B" if cur else "", 8)
        pdf.set_xy(x, y+10); pdf.cell(32, 10, str(days[i].day), align="C")
        if not cur and "d_%d"%i in links: pdf.link(x, y, 32, h, links["d_%d"%i])
        x += 35
    x += 2
    is_j = current.startswith("j_")
    if is_j:
        pdf.set_fill_color(*MAGENTA); pdf.rect(x, y, 55, h, style='F'); pdf.set_text_color(*WHITE)
    else:
        pdf.set_draw_color(*LIGHT); pdf.rect(x, y, 55, h); pdf.set_text_color(*MAGENTA)
    pdf.set_font("Helvetica", "B" if is_j else "", 8)
    pdf.set_xy(x, y+4); pdf.cell(55, h-8, "JOURNAL", align="C")
    if not is_j and current.startswith("d_"):
        jk = "j_" + current[2:]
        if jk in links: pdf.link(x, y, 55, h, links[jk])


def parse_hour(t):
    t = t.strip().lower()
    pm = 'p' in t
    t = t.replace('a','').replace('p','').replace('m','')
    parts = t.split(':')
    hr = int(parts[0]); mn = int(parts[1]) if len(parts) > 1 else 0
    if pm and hr != 12: hr += 12
    if not pm and hr == 12: hr = 0
    return hr + mn/60.0


def page_month(pdf, year, month, events, links):
    pdf.add_page(); pdf.set_auto_page_break(auto=False)
    pdf.set_font("Helvetica", "B", 26); pdf.set_text_color(*NAVY)
    pdf.set_xy(M_L, HY); pdf.cell(M_R-M_L, 30, MONTHS[month], align="C")
    pdf.set_font("Helvetica", "", 9); pdf.set_text_color(*TEXT)
    pdf.set_xy(M_L, HY+2); pdf.cell(50, 12, str(year))
    gradient(pdf, HY+33)
    gt = HY + 46; cw = (M_R - M_L) / 7
    pdf.set_font("Helvetica", "B", 7); pdf.set_text_color(*TEXT)
    for i, d in enumerate(DAYS):
        pdf.set_xy(M_L + i*cw, gt); pdf.cell(cw, 12, d[:3], align="C")
    gy = gt + 14; rh = 80
    for week in cal_mod.Calendar(6).monthdatescalendar(year, month):
        for di, day in enumerate(week):
            x = M_L + di*cw
            pdf.set_draw_color(*LIGHT); pdf.set_line_width(0.4); pdf.rect(x, gy, cw, rh)
            if day.month == month:
                pdf.set_font("Helvetica", "B", 8); pdf.set_text_color(*NAVY)
                pdf.set_xy(x+2, gy+1); pdf.cell(16, 10, str(day.day))
                ey = gy + 14
                cell_w = cw - 4
                for t, title in sorted(events.get((day.year, day.month, day.day), []))[:6]:
                    extra = handwrite(pdf, "%s %s" % (t, title), x+2, ey, scale=3, color=INK, max_width=cell_w)
                    ey += 9 + extra
            else:
                pdf.set_font("Helvetica", "", 7); pdf.set_text_color(*LIGHT)
                pdf.set_xy(x+2, gy+1); pdf.cell(16, 10, str(day.day))
        gy += rh
    footer(pdf)


def page_week(pdf, ws, events, links):
    pdf.add_page(); pdf.set_auto_page_break(auto=False)
    we = ws + timedelta(days=6)
    pdf.set_font("Helvetica", "B", 16); pdf.set_text_color(*NAVY)
    pdf.set_xy(M_L, HY)
    pdf.cell(M_R-M_L, 22, "%s %d - %s %d" % (MONTHS[ws.month][:3], ws.day, MONTHS[we.month][:3], we.day), align="C")
    gradient(pdf, HY+25)
    cw = (M_R - M_L) / 7; ty = HY + 35; ch = M_B - ty - 10
    for i in range(7):
        d = ws + timedelta(days=i); x = M_L + i*cw
        pdf.set_fill_color(*NAVY); pdf.rect(x, ty, cw-1, 18, style='F')
        pdf.set_text_color(*WHITE); pdf.set_font("Helvetica", "B", 7)
        pdf.set_xy(x, ty+1); pdf.cell(cw-1, 8, DAYS[i][:3], align="C")
        pdf.set_font("Helvetica", "B", 9); pdf.set_xy(x, ty+8); pdf.cell(cw-1, 9, str(d.day), align="C")
        if "d_%d"%i in links: pdf.link(x, ty, cw-1, 18, links["d_%d"%i])
        pdf.set_draw_color(*LIGHT); pdf.set_line_width(0.4); pdf.rect(x, ty+18, cw-1, ch-18)
        ey = ty + 22
        col_inner = cw - 6
        for t, title in sorted(events.get((d.year, d.month, d.day), [])):
            handwrite(pdf, t, x+2, ey, scale=4, color=(140,150,155), max_width=col_inner)
            extra = handwrite(pdf, title, x+2, ey+7, scale=5, color=INK, max_width=col_inner)
            ey += 20 + extra
    footer(pdf)


def page_day(pdf, d, day_idx, events):
    pdf.add_page(); pdf.set_auto_page_break(auto=False)
    dow = (d.weekday() + 1) % 7
    pdf.set_font("Helvetica", "B", 20); pdf.set_text_color(*NAVY)
    pdf.set_xy(M_L, HY); pdf.cell(300, 25, "%s  %s %d" % (DAYS[dow].title(), MONTHS[d.month][:3], d.day))
    pdf.set_font("Helvetica", "", 8); pdf.set_text_color(*CYAN)
    pdf.set_xy(M_L, HY+23); pdf.cell(200, 10, "C R O S S   F O R M E D   K I D S")
    gradient(pdf, HY+35)
    st = HY + 48; sw = (M_R - M_L) * 0.55; tc = 30
    hours = list(range(6, 20)); sh = (M_B - st - 15) / len(hours)
    for hi, h in enumerate(hours):
        y = st + hi * sh; hr = h if h <= 12 else h - 12
        pdf.set_font("Helvetica", "", 7); pdf.set_text_color(*LIGHT)
        pdf.set_xy(M_L, y+1); pdf.cell(tc, sh*0.5, "%d%s" % (hr, "a" if h<12 else "p"), align="R")
        pdf.set_draw_color(*VLIGHT); pdf.set_line_width(0.4); pdf.line(M_L+tc+4, y, M_L+sw, y)
    for t, title in sorted(events.get((d.year, d.month, d.day), [])):
        try: hr = parse_hour(t)
        except: continue
        if hr < 6 or hr >= 20: continue
        ey = st + (hr - 6) * sh + 3; ex = M_L + tc + 8
        pdf.set_draw_color(*CYAN); pdf.set_line_width(1.5)
        pdf.line(M_L+tc+4, ey-1, M_L+tc+4, ey+sh*0.7)
        handwrite(pdf, title, ex, ey, scale=9, color=INK)
        handwrite(pdf, t, ex, ey+11, scale=5.5, color=(140,145,150))
    rx = M_L + sw + 15; rw = M_R - rx
    pdf.set_fill_color(*CYAN); pdf.rect(rx, st, 6, 6, style='F')
    pdf.set_font("Helvetica", "B", 7); pdf.set_text_color(*CYAN)
    pdf.set_xy(rx+10, st-1); pdf.cell(60, 8, "TASKS")
    ty = st + 14
    for i in range(12):
        pdf.set_draw_color(*LIGHT); pdf.set_line_width(0.3)
        pdf.rect(rx+2, ty+1, 7, 7); pdf.line(rx+13, ty+8, rx+rw, ty+8); ty += 18
    ny = ty + 10
    pdf.set_fill_color(*NAVY); pdf.rect(rx, ny, 6, 6, style='F')
    pdf.set_font("Helvetica", "B", 7); pdf.set_text_color(*NAVY)
    pdf.set_xy(rx+10, ny-1); pdf.cell(60, 8, "NOTES")
    ny += 14; pdf.set_draw_color(*VLIGHT)
    while ny <= M_B - 5: pdf.line(rx, ny, rx+rw, ny); ny += 16
    footer(pdf)


def page_journal(pdf, d, day_idx):
    pdf.add_page(); pdf.set_auto_page_break(auto=False)
    dow = (d.weekday() + 1) % 7
    pdf.set_font("Helvetica", "B", 16); pdf.set_text_color(*NAVY)
    pdf.set_xy(M_L, HY)
    pdf.cell(300, 22, "Journal  -  %s %s %d" % (DAYS[dow].title(), MONTHS[d.month][:3], d.day))
    pdf.set_font("Helvetica", "", 8); pdf.set_text_color(*CYAN)
    pdf.set_xy(M_L, HY+20); pdf.cell(200, 10, "C R O S S   F O R M E D   K I D S")
    gradient(pdf, HY+33)
    pdf.set_draw_color(*VLIGHT); pdf.set_line_width(0.3)
    y = HY + 48
    while y <= M_B - 5: pdf.line(M_L, y, M_R, y); y += 22
    footer(pdf)


def generate_planner(events, week_start):
    """Generate full planner PDF. Returns bytes."""
    pdf = FPDF(orientation='P', unit='pt', format='letter')
    days = [week_start + timedelta(days=i) for i in range(7)]

    links = {}; page = 1
    for key in ["m_apr", "m_may", "week"]:
        links[key] = pdf.add_link(); pdf.set_link(links[key], page=page); page += 1
    for i in range(7):
        links["d_%d"%i] = pdf.add_link(); pdf.set_link(links["d_%d"%i], page=page); page += 1
    for i in range(7):
        links["j_%d"%i] = pdf.add_link(); pdf.set_link(links["j_%d"%i], page=page); page += 1

    page_month(pdf, week_start.year, week_start.month, events, links)
    nav_bar(pdf, links, "m_apr", days)
    end_month = (week_start + timedelta(days=6)).month
    m2 = end_month if end_month != week_start.month else week_start.month + 1
    page_month(pdf, week_start.year, m2, events, links)
    nav_bar(pdf, links, "m_may", days)
    page_week(pdf, week_start, events, links)
    nav_bar(pdf, links, "week", days)
    for i in range(7):
        page_day(pdf, days[i], i, events); nav_bar(pdf, links, "d_%d"%i, days)
    for i in range(7):
        page_journal(pdf, days[i], i); nav_bar(pdf, links, "j_%d"%i, days)

    buf = io.BytesIO()
    pdf.output(buf)
    return buf.getvalue()


def events_hash(events):
    s = {"%d-%d-%d" % k: v for k, v in events.items()}
    return hashlib.md5(json.dumps(s, sort_keys=True).encode()).hexdigest()
