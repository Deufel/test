"""
bubble_map_test.py — md_web.ui bubble_map component test
=========================================================
15 US metro markets with synthetic sales data, live random walk.
Every 2s a tick writes new values → broadcaster → all clients update.

Demonstrates:
  - bubble_map: Albers projection, size-encodes value, CSS color ramp
  - bubble_map + stat_card + bar_chart composing in one dashboard section
  - Multiplayer: open two windows, both update in sync

Run
---
    uv run python bubble_map_test.py
    open http://localhost:8000
"""

import asyncio
import json
import random
import math

from md_web import (
    Safe, html_doc, mk_tag,
    Datastar, Favicon,
    patch_elements,
    create_app, serve,
)
from md_web.db import create_db, create_db_relay, migrate, query, write

from md_web.ui.bubble_map import bubble_map, BubblePoint
from md_web.ui.stat_card  import stat_card
from md_web.ui.bar_chart  import bar_chart
from md_web.ui.layout     import card, grid, section

# ── Page tags ─────────────────────────────────────────────────────────────────

head   = mk_tag('head')
body   = mk_tag('body')
meta   = mk_tag('meta')
title_ = mk_tag('title')
style  = mk_tag('style')
div    = mk_tag('div')
header = mk_tag('header')
main_  = mk_tag('main')
footer = mk_tag('footer')
span   = mk_tag('span')
h1     = mk_tag('h1')
small  = mk_tag('small')

# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    id      INTEGER PRIMARY KEY,
    slug    TEXT    NOT NULL UNIQUE,
    label   TEXT    NOT NULL,
    lat     REAL    NOT NULL,
    lon     REAL    NOT NULL,
    value   REAL    NOT NULL,
    history TEXT    NOT NULL DEFAULT '[]'
);
"""

HISTORY_LEN = 20

# ── Market definitions ────────────────────────────────────────────────────────
# (slug, label, lat, lon, base_value)

MARKET_DEFS = [
    ('nyc',   'New York',      40.71,  -74.01,  1_240_000),
    ('la',    'Los Angeles',   34.05, -118.24,    890_000),
    ('chi',   'Chicago',       41.88,  -87.63,    670_000),
    ('hou',   'Houston',       29.76,  -95.37,    520_000),
    ('phx',   'Phoenix',       33.45, -112.07,    480_000),
    ('phi',   'Philadelphia',  39.95,  -75.17,    445_000),
    ('sea',   'Seattle',       47.61, -122.33,    610_000),
    ('den',   'Denver',        39.74, -104.99,    390_000),
    ('dal',   'Dallas',        32.78,  -96.80,    570_000),
    ('bos',   'Boston',        42.36,  -71.06,    430_000),
    ('sfo',   'San Francisco', 37.77, -122.42,    750_000),
    ('nas',   'Nashville',     36.17,  -86.78,    280_000),
    ('clt',   'Charlotte',     35.23,  -80.84,    310_000),
    ('mia',   'Miami',         25.77,  -80.19,    380_000),
    ('atl',   'Atlanta',       33.75,  -84.39,    460_000),
]

M_ID, M_SLUG, M_LABEL, M_LAT, M_LON, M_VALUE, M_HISTORY = 0,1,2,3,4,5,6

# ── Seed ──────────────────────────────────────────────────────────────────────

def seed(conn):
    if conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0] > 0:
        return
    for slug, label, lat, lon, base in MARKET_DEFS:
        history = [
            base * (1 + 0.04*math.sin(i/3) + random.gauss(0, 0.02))
            for i in range(HISTORY_LEN)
        ]
        conn.execute(
            "INSERT INTO markets(slug,label,lat,lon,value,history) VALUES(?,?,?,?,?,?)",
            (slug, label, lat, lon, history[-1], json.dumps(history)),
        )
    print('[seed] markets ready')

# ── DB write helper ───────────────────────────────────────────────────────────

def _tick(conn):
    bases = {d[0]: d[4] for d in MARKET_DEFS}
    for row in conn.execute("SELECT * FROM markets").fetchall():
        base    = bases[row[M_SLUG]]
        cur     = row[M_VALUE]
        history = json.loads(row[M_HISTORY])
        new_val = max(
            cur + cur*0.018*random.gauss(0,1) + (base-cur)*0.03,
            base * 0.4,
        )
        history.append(new_val)
        if len(history) > HISTORY_LEN:
            history = history[-HISTORY_LEN:]
        conn.execute(
            "UPDATE markets SET value=?,history=? WHERE slug=?",
            (new_val, json.dumps(history), row[M_SLUG]),
        )

# ── Rendering ─────────────────────────────────────────────────────────────────

def _delta(history):
    if len(history) < 2 or history[0] == 0: return None
    return (history[-1] - history[0]) / abs(history[0]) * 100

def render_app(conn):
    rows = query(conn, "SELECT * FROM markets ORDER BY value DESC")

    total   = sum(r[M_VALUE] for r in rows)
    top     = rows[0]
    avg     = total / len(rows) if rows else 0
    hist_top = json.loads(top[M_HISTORY])

    # ── KPI row ───────────────────────────────────────────────────────────
    all_hists = [json.loads(r[M_HISTORY]) for r in rows]
    total_hist = [sum(h[i] for h in all_hists) for i in range(HISTORY_LEN)]

    kpis = section('Market Overview')(
        grid(cols=3, gap='sm')(
            card()(stat_card(
                label  = 'Total Revenue',
                value  = f'${total/1e6:.2f}M',
                delta  = _delta(total_hist),
                series = total_hist,
            )),
            card()(stat_card(
                label  = f'Top Market — {top[M_LABEL]}',
                value  = f'${top[M_VALUE]/1e3:.0f}K',
                delta  = _delta(hist_top),
                series = hist_top,
            )),
            card()(stat_card(
                label  = 'Avg per Market',
                value  = f'${avg/1e3:.0f}K',
                series = total_hist,
            )),
        )
    )

    # ── Bubble map ────────────────────────────────────────────────────────
    points = [
        BubblePoint(
            lat   = r[M_LAT],
            lon   = r[M_LON],
            value = r[M_VALUE],
            label = r[M_LABEL],
        )
        for r in rows
    ]

    map_section = section('Revenue by Market', subtitle='bubble size = revenue · Albers projection')(
        card(pad='sm')(
            bubble_map(
                points,
                color_hue   = 220,
                value_fmt   = lambda v: f'${v/1e3:.0f}K',
                show_labels = True,
                show_legend = True,
                max_radius  = 38,
            )
        )
    )

    # ── Bar chart: top 10 markets ranked ─────────────────────────────────
    top10 = [(r[M_LABEL], r[M_VALUE]) for r in rows[:10]]

    bar_section = section('Top 10 Markets')(
        card('Revenue Ranking', subtitle='sorted by current value')(
            bar_chart(
                top10,
                orientation = 'horizontal',
                color_hue   = 220,
                value_fmt   = lambda v: f'${v/1e3:.0f}K',
                h           = 280,
            )
        )
    )

    return div(id='app')(kpis, map_section, bar_section)

# ── CSS ───────────────────────────────────────────────────────────────────────

import os
_HERE = os.path.dirname(os.path.abspath(__file__))

def _read_css(name):
    for p in [
        os.path.join(_HERE, 'src', 'md_web', 'ui', 'css', name),
        os.path.join(_HERE, 'src', 'md_web', 'ui', name),
        os.path.join(_HERE, 'ui', 'css', name),
        os.path.join(_HERE, 'ui', name),
    ]:
        if os.path.exists(p):
            with open(p) as f: return f.read()
    return ''

PAGE_CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
    --cfg-hue: 220; --cfg-radius: 8px;
    --cfg-top-l: 88; --cfg-base-step: 4; --cfg-surf-chroma: 0.018;
    --font-heading: 'Segoe UI', system-ui, sans-serif;
    --font-body:    'Segoe UI', system-ui, sans-serif;
    --font-mono:    ui-monospace, 'SF Mono', monospace;
}
@media (prefers-color-scheme: dark) {
    :root { --cfg-top-l: 33; --cfg-base-step: 2.5; --cfg-surf-chroma: 0.010; }
}
.surface {
    --_l1: calc(var(--cfg-top-l) - var(--cfg-base-step) * 1.5);
    --_bg: oklch(calc(var(--_l1) * 1%) var(--cfg-surf-chroma) var(--cfg-hue));
    --border: oklch(from var(--_bg) calc(l - 0.10) calc(c * 0.7) h);
    --Border: oklch(from var(--_bg) calc(l - 0.22) calc(c * 1.2) h);
    background: var(--_bg);
    color: oklch(from var(--_bg) calc(l - 0.55) 0.01 var(--cfg-hue));
}
body {
    font-family: var(--font-body);
    background: oklch(90% 0.012 220); color: oklch(25% 0.015 220);
    min-height: 100dvh; display: flex; flex-direction: column;
}
@media (prefers-color-scheme: dark) {
    body    { background: oklch(13% 0.008 220); color: oklch(87% 0.008 220); }
    .surface { --_bg: oklch(20% 0.009 220); color: oklch(83% 0.007 220); }
}
header {
    padding: 0.9rem 1.5rem;
    border-bottom: 1px solid oklch(80% 0.010 220);
    display: flex; align-items: center; gap: 0.75rem;
}
@media (prefers-color-scheme: dark) {
    header { border-bottom-color: oklch(24% 0.009 220); }
}
header h1   { font-size: 1rem; font-weight: 600; letter-spacing: 0.02em; }
header small { margin-left: auto; font-size: 0.72rem; opacity: 0.4; font-family: var(--font-mono); }
main {
    flex: 1; padding: 1.5rem;
    max-width: 1100px; width: 100%; margin: 0 auto;
    display: flex; flex-direction: column; gap: 2rem;
}
footer {
    text-align: center; padding: 0.75rem;
    font-size: 0.7rem; opacity: 0.28;
    font-family: var(--font-mono);
    border-top: 1px solid currentColor;
}
"""

# ── Landing ───────────────────────────────────────────────────────────────────

def landing(conn):
    css = '\n'.join(filter(None, [_read_css('style.css'), _read_css('ui.css'), PAGE_CSS]))
    h = head(
        meta(charset='utf-8'),
        meta(name='viewport', content='width=device-width, initial-scale=1'),
        title_('bubble_map — UI Component Test'),
        Favicon('🗺️'),
        style(Safe(css)),
        Datastar(),
    )
    b = body(
        div(id='sse-init', **{'data-init': "@get('/stream')"}),
        header(
            span('🗺️'),
            h1('bubble_map component test'),
            small('15 metro markets · live random walk · open two windows'),
        ),
        main_(render_app(conn)),
        footer('md-web · Datastar · SQLite · SSE · Albers Equal-Area Conic'),
    )
    return html_doc(h, b)

# ── Tick loop ─────────────────────────────────────────────────────────────────

async def tick_loop():
    await _ready.wait()
    while True:
        await asyncio.sleep(2.0)
        write(db, _tick)

# ── Module state ──────────────────────────────────────────────────────────────

db     = None
relay  = None
_ready = asyncio.Event()

def startup(loop):
    global db, relay
    try:
        db    = create_db('bubble_map_test.db')
        relay = create_db_relay(
            db, loop,
            render_fn=lambda: patch_elements(render_app(db)),
        )
        migrate(db, SCHEMA)
        seed(db)
        loop.create_task(tick_loop())
        print('[startup] ready — http://localhost:8000')
    except Exception:
        import traceback; traceback.print_exc()
    finally:
        loop.call_soon_threadsafe(_ready.set)

# ── App ───────────────────────────────────────────────────────────────────────

app = create_app(on_init=startup)

@app.get('/')
async def index(req):
    await _ready.wait()
    return landing(db)

@app.get('/stream')
async def stream(req):
    await _ready.wait()
    async def _stream():
        yield patch_elements(render_app(db))
        async for ev in relay.broadcaster.subscribe():
            if ev: yield ev
    return _stream()

# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    serve(app, host='0.0.0.0', port=8000, backpressure=500)
