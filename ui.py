"""
dashboard_test.py — md_web.ui full component library demo
==========================================================
A live retail analytics dashboard demonstrating all five components:

  stat_card       — KPI row at the top (6 metrics, live sparklines)
  bar_chart       — Revenue by category, vertical + horizontal variants
  activity_heatmap — 52-week daily revenue heatmap
  card            — surface wrapper for every chart
  grid            — responsive layout: 4-col KPIs, 2-col charts
  section         — labelled dashboard regions

Data is synthetic retail (MRTS-style categories) driven by a random
walk in SQLite.  Every 2s a tick writes new values → update_hook →
broadcaster renders the full #app once → all connected clients update.

Multiplayer: open two windows — both update in sync.
Compression: check DevTools Network tab for brotli ratio.

Run
---
    uv run python dashboard_test.py
    open http://localhost:8000
"""

import asyncio
import time
import json
import math
import random
from datetime import date, timedelta

from md_web import (
    Safe, html_doc, mk_tag,
    Datastar, Favicon,
    patch_elements,
    create_app, serve, static
)
from md_web.db import create_db, create_db_relay, migrate, query, write

from md_web.ui.stat_card   import stat_card
from md_web.ui.bar_chart   import bar_chart
from md_web.ui.line_chart  import line_chart
from md_web.ui.scatter_plot import scatter_plot, ScatterPoint
from md_web.ui.heatmap     import activity_heatmap
from md_web.ui.layout      import card, grid, section

# ── Page tags ─────────────────────────────────────────────────────────────────

head   = mk_tag('head')
body   = mk_tag('body')
meta   = mk_tag('meta')
link   = mk_tag('link')
title_ = mk_tag('title')
style  = mk_tag('style')
div    = mk_tag('div')
header = mk_tag('header')
main   = mk_tag('main')
footer = mk_tag('footer')
span   = mk_tag('span')
h1     = mk_tag('h1')
small  = mk_tag('small')

# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    id       INTEGER PRIMARY KEY,
    slug     TEXT NOT NULL UNIQUE,
    label    TEXT NOT NULL,
    value    REAL NOT NULL,
    history  TEXT NOT NULL DEFAULT '[]',
    tick     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS categories (
    id       INTEGER PRIMARY KEY,
    slug     TEXT NOT NULL UNIQUE,
    label    TEXT NOT NULL,
    value    REAL NOT NULL,
    prev     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS daily (
    id       INTEGER PRIMARY KEY,
    day      TEXT NOT NULL UNIQUE,
    value    REAL
);
CREATE INDEX IF NOT EXISTS daily_day ON daily(day);
CREATE TABLE IF NOT EXISTS cat_history (
    id       INTEGER PRIMARY KEY,
    slug     TEXT NOT NULL,
    tick     INTEGER NOT NULL,
    value    REAL NOT NULL,
    UNIQUE(slug, tick)
);
"""

HISTORY_LEN = 24
CAT_HISTORY_LEN = 20  # ticks of category history for line chart

#d ── Data definitions ──────────────────────────────────────────────────────────

# KPI metrics: (slug, label, start, volatility, fmt)
METRIC_DEFS = [
    ('revenue',    'Total Revenue',    6_240_000, 0.006, lambda v: f'${v/1e6:.2f}M'),
    ('orders',     'Total Orders',     48_210,    0.010, lambda v: f'{v:,.0f}'),
    ('aov',        'Avg Order Value',  129.40,    0.005, lambda v: f'${v:.2f}'),
    ('conversion', 'Conversion Rate',  3.42,      0.012, lambda v: f'{v:.2f}%'),
    ('ecomm_share','E-Comm Share',     18.4,      0.008, lambda v: f'{v:.1f}%'),
    ('margin',     'Gross Margin',     34.2,      0.006, lambda v: f'{v:.1f}%'),
    ('fps',        'Actual FPS',       60.0,      0.0,   lambda v: f'{v:.1f}'),
]

# Retail categories for bar charts: (slug, label, base_value_billions)
CATEGORY_DEFS = [
    ('food_bev',    'Food & Beverage',   847),
    ('auto',        'Auto & Parts',      124),
    ('health',      'Health & Personal', 312),
    ('clothing',    'Clothing',          248),
    ('electronics', 'Electronics',       198),
    ('general',     'General Merch.',    689),
    ('ecomm',       'Non-Store/E-Comm',  109),
    ('food_svc',    'Food Services',     421),
]

# Column indices
M_ID, M_SLUG, M_LABEL, M_VALUE, M_HISTORY, M_TICK = 0,1,2,3,4,5
C_ID, C_SLUG, C_LABEL, C_VALUE, C_PREV = 0,1,2,3,4

# ── Seed ─────────────────────────────────────────────────────────────────────

def seed(conn):

    # Metrics
    if conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 0:
        for slug, label, start, vol, _ in METRIC_DEFS:
            history = [
                start * (1 + 0.05*math.sin(i/3) + random.gauss(0, 0.015))
                for i in range(HISTORY_LEN)
            ]
            conn.execute(
                "INSERT INTO metrics(slug,label,value,history) VALUES(?,?,?,?)",
                (slug, label, history[-1], json.dumps(history)),
            )

    # Categories
    if conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
        for slug, label, base in CATEGORY_DEFS:
            v = base * random.gauss(1.0, 0.05)
            conn.execute(
                "INSERT INTO categories(slug,label,value,prev) VALUES(?,?,?,?)",
                (slug, label, v, v * random.gauss(1.0, 0.03)),
            )

    # Daily — 14 months of history
    if conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 0:
        today = date.today()
        base  = 210_000
        rows  = []
        for i in range(430):
            d   = today - timedelta(days=430-i)
            dow = d.weekday()
            if dow >= 5:
                if random.random() > 0.25:
                    rows.append((d.isoformat(), None))
                    continue
                rows.append((d.isoformat(), base * 0.30 * random.gauss(1,.15)))
                continue
            dow_f    = [0.92,0.97,1.06,1.04,0.95][dow]
            seasonal = 1 + 0.30*math.sin((d.timetuple().tm_yday/365)*2*math.pi - math.pi*0.5)
            rows.append((d.isoformat(), base * dow_f * seasonal * random.gauss(1,.09)))
        with conn:
            conn.executemany(
                "INSERT OR IGNORE INTO daily(day,value) VALUES(?,?)", rows
            )
    # Category history — seed with gentle random walk per category
    if conn.execute("SELECT COUNT(*) FROM cat_history").fetchone()[0] == 0:
        for slug, label, base in CATEGORY_DEFS:
            for tick in range(CAT_HISTORY_LEN):
                v = base * (1 + 0.04*math.sin(tick/4) + random.gauss(0, 0.012))
                conn.execute(
                    "INSERT OR IGNORE INTO cat_history(slug,tick,value) VALUES(?,?,?)",
                    (slug, tick, v),
                )
    print('[seed] done')

# ── DB write helpers ──────────────────────────────────────────────────────────

def _tick(conn):
    """Advance all metrics and category values one random-walk step."""
    defs = {d[0]: d for d in METRIC_DEFS}

    # Track actual frame time for FPS

    now = time.time()
    if not hasattr(_tick, '_last_time'):
        _tick._last_time = now
    actual_fps = 1.0 / (now - _tick._last_time) if (now - _tick._last_time) > 0 else 60
    _tick._last_time = now

    # Update FPS metric
    conn.execute(
        "UPDATE metrics SET value=?, history=? WHERE slug='fps'",
        (actual_fps, json.dumps([actual_fps] * HISTORY_LEN)),
    )


    # Metrics
    for row in conn.execute("SELECT * FROM metrics").fetchall():
        slug    = row[M_SLUG]
        _, _, start, vol, _ = defs[slug]
        cur     = row[M_VALUE]
        history = json.loads(row[M_HISTORY])
        new_val = max(cur + cur*vol*random.gauss(0,1) + (start-cur)*0.02, start*0.4)
        history.append(new_val)
        if len(history) > HISTORY_LEN:
            history = history[-HISTORY_LEN:]
        conn.execute(
            "UPDATE metrics SET value=?,history=?,tick=tick+1 WHERE slug=?",
            (new_val, json.dumps(history), slug),
        )

    # Categories
    for row in conn.execute("SELECT * FROM categories").fetchall():
        base  = dict(zip([d[0] for d in CATEGORY_DEFS],
                         [d[2] for d in CATEGORY_DEFS])).get(row[C_SLUG], 100)
        cur   = row[C_VALUE]
        new_v = max(cur + cur*0.015*random.gauss(0,1) + (base-cur)*0.01, base*0.5)
        conn.execute(
            "UPDATE categories SET prev=value, value=? WHERE slug=?",
            (new_v, row[C_SLUG]),
        )

    # Daily — append a new day occasionally
    last = conn.execute(
        "SELECT day FROM daily ORDER BY day DESC LIMIT 1"
    ).fetchone()
    if last:
        last_d = date.fromisoformat(last[0])
        next_d = last_d + timedelta(days=1)
        if next_d <= date.today() + timedelta(days=3):
            dow = next_d.weekday()
            if dow >= 5:
                val = None if random.random() > 0.25 else 210_000*0.30*random.gauss(1,.15)
            else:
                seasonal = 1+0.30*math.sin((next_d.timetuple().tm_yday/365)*2*math.pi-math.pi*0.5)
                val = 210_000*[0.92,0.97,1.06,1.04,0.95][dow]*seasonal*random.gauss(1,.09)
            conn.execute(
                "INSERT OR IGNORE INTO daily(day,value) VALUES(?,?)",
                (next_d.isoformat(), val),
            )

    # Append current category values as a new history tick
    max_tick = conn.execute("SELECT MAX(tick) FROM cat_history").fetchone()[0] or 0
    new_tick = max_tick + 1
    for row in conn.execute("SELECT * FROM categories").fetchall():
        conn.execute(
            "INSERT OR IGNORE INTO cat_history(slug,tick,value) VALUES(?,?,?)",
            (row[C_SLUG], new_tick, row[C_VALUE]),
        )
    # Keep only last CAT_HISTORY_LEN ticks
    conn.execute(
        "DELETE FROM cat_history WHERE tick <= ?",
        (new_tick - CAT_HISTORY_LEN,),
    )

# ── Rendering ─────────────────────────────────────────────────────────────────

def _delta(history):
    if len(history) < 2 or history[0] == 0:
        return None
    return (history[-1] - history[0]) / abs(history[0]) * 100

def render_app(conn):
    """Full #app render — called once per tick by the broadcaster."""
    defs = {d[0]: d for d in METRIC_DEFS}

    # ── KPI row ───────────────────────────────────────────────────────────
    metric_rows = query(conn, "SELECT * FROM metrics ORDER BY id")
    kpi_cards   = []
    for row in metric_rows:
        history = json.loads(row[M_HISTORY])
        slug, label, _, _, fmt = defs[row[M_SLUG]][:5]
        kpi_cards.append(
            card()(
                stat_card(
                    label  = row[M_LABEL],
                    value  = fmt(row[M_VALUE]),
                    delta  = _delta(history),
                    series = history,
                )
            )
        )

    kpi_section = section('Key Metrics', subtitle='live random walk · updates every 2s')(
        grid(cols=6, gap='sm')(*kpi_cards)
    )

    # ── Category bar charts ────────────────────────────────────────────────
    cat_rows = query(conn, "SELECT * FROM categories ORDER BY value DESC")

    # Vertical: top 6 categories by value
    top6 = [(r[C_LABEL], r[C_VALUE]) for r in cat_rows[:6]]

    # Horizontal: all 8 categories — good for label-heavy data
    all_cats = [(r[C_LABEL], r[C_VALUE]) for r in cat_rows]

    # YoY comparison: current vs previous tick as two-series grouped bar
    yoy = [
        ('Current', [(r[C_LABEL][:10], r[C_VALUE]) for r in cat_rows[:5]]),
        ('Previous',[(r[C_LABEL][:10], r[C_PREV])  for r in cat_rows[:5]]),
    ]

    chart_section = section('Retail Sales by Category', subtitle='$ billions · synthetic MRTS-style data')(
        grid(cols=2, fixed=True)(
            card('By Volume', subtitle='top 6 categories')(
                bar_chart(
                    all_cats,
                    orientation='vertical',
                    color_hue=220,
                    value_fmt=lambda v: f'${v:.0f}B',
                    show_grid=True,
                    h=240,
                )
            ),
            card('All Categories', subtitle='horizontal view')(
                bar_chart(
                    all_cats,
                    orientation='horizontal',
                    color_hue=142,
                    value_fmt=lambda v: f'${v:.0f}B',
                    h=240,
                )
            ),

        )
    )

    # ── Line chart: category trends over ticks ───────────────────────────
    cat_hist_rows = query(conn,
        "SELECT slug, tick, value FROM cat_history ORDER BY slug, tick"
    )
    # Build per-category series with readable relative labels
    # Group by slug, sort by tick, then label as "–N ticks ago" → "now"
    cat_by_slug: dict = {}
    for row in cat_hist_rows:
        cat_by_slug.setdefault(row[0], []).append((row[1], row[2]))

    cat_series: dict = {}
    for slug, tick_vals in cat_by_slug.items():
        tick_vals.sort(key=lambda t: t[0])
        n = len(tick_vals)
        labeled = []
        for i, (tick, val) in enumerate(tick_vals):
            if i == n - 1:
                lbl = 'now'
            elif (n - 1 - i) % max(1, (n - 1) // 5) == 0:
                lbl = f'–{n - 1 - i}'
            else:
                lbl = ''
            labeled.append((lbl, val))
        cat_series[slug] = labeled

    # Show top 4 categories by current value as multi-series line chart
    top4_slugs = [r[C_SLUG] for r in cat_rows[:4]]
    line_data  = [
        (dict(zip([d[0] for d in CATEGORY_DEFS],
                  [d[1] for d in CATEGORY_DEFS])).get(slug, slug),
         cat_series.get(slug, []))
        for slug in top4_slugs
        if cat_series.get(slug)
    ]

    trend_section = section('Category Trends', subtitle='top 4 categories · value by tick')(
        card('Sales Over Time', subtitle='multi-series · smooth curves')(
            line_chart(
                line_data,
                y_fmt       = lambda v: f'${v:.0f}B',
                area        = True,
                show_legend = True,
                show_dots   = False,
                h           = 220,
                w           = 900,
            )
        )
    )

    # ── Scatter: category e-comm share vs sales volume ────────────────────
    # E-comm category value as share of total = proxy for category e-comm mix
    total_cat = sum(r[C_VALUE] for r in cat_rows) or 1
    ecomm_val = next((r[C_VALUE] for r in cat_rows if r[C_SLUG] == 'ecomm'), 0)
    # Give each category a synthetic e-comm share based on its mix with total
    scatter_pts = [
        ScatterPoint(
            x     = round((r[C_VALUE] / total_cat) * 100 * random.gauss(1, 0.05), 2),
            y     = round(r[C_VALUE], 1),
            label = r[C_LABEL],
            size  = r[C_VALUE],
        )
        for r in cat_rows
    ]

    scatter_section = section('Category Analysis')(
        grid(cols=2, fixed=True)(
            card('Size vs Share', subtitle='bubble size = sales volume')(
                scatter_plot(
                    scatter_pts,
                    x_label     = 'Share of Total (%)',
                    y_label     = 'Sales $B',
                    x_fmt       = lambda v: f'{v:.1f}%',
                    y_fmt       = lambda v: f'${v:.0f}B',
                    show_labels = True,
                    max_radius  = 18,
                    h           = 260,
                    ref_lines   = [
                        {'axis': 'y', 'value': total_cat/len(cat_rows),
                         'color': 'oklch(60% 0.12 220)', 'label': 'avg'},
                    ],
                )
            ),
            card('Current vs Previous', subtitle='grouped — top 5 categories', span=1)(
                bar_chart(
                    yoy,
                    orientation = 'vertical',
                    value_fmt   = lambda v: f'${v:.0f}B',
                    show_values = True,
                    h           = 260,
                    w           = 440,
                )
            ),
        )
    )

    # ── Activity heatmap ──────────────────────────────────────────────────
    daily_rows = query(conn,
        "SELECT day, value FROM daily ORDER BY day DESC LIMIT 400"
    )
    daily_data = {r[0]: r[1] for r in daily_rows}

    heatmap_section = section('Daily Revenue Activity', subtitle='52 weeks · grey = no data')(
        card(pad='sm')(
            activity_heatmap(
                data      = daily_data,
                weeks     = 52,
                color_hue = 142,
                show_legend = True,
            ),
            activity_heatmap(
                data = daily_data,
                weeks = 52,
                color_hue = 142,
                outlier_percentiles = (15, 99),   # try this first
            )
        )
    )

    return div(id='app')(
        kpi_section,
        chart_section,
        trend_section,
        scatter_section,
        heatmap_section,
    )

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
    --cfg-hue:          254;
    --cfg-radius:       4px;
    --cfg-top-l:        88;
    --cfg-base-step:    4;
    --cfg-curve-k:      0.6;
    --cfg-surf-mid:     60.5;
    --cfg-surf-rng:     55;
    --cfg-surf-chroma:  0.018;
    --font-heading: 'Segoe UI', system-ui, sans-serif;
    --font-body:    'Segoe UI', system-ui, sans-serif;
    --font-mono:    ui-monospace, 'SF Mono', monospace;
}

@media (prefers-color-scheme: dark) {
    :root {
        --cfg-top-l:       33;
        --cfg-base-step:   2.5;
        --cfg-surf-chroma: 0.010;
        --cfg-surf-mid:    33.5;
        --cfg-surf-rng:    27.5;
    }
}

.surface {
    --_l1:    calc(var(--cfg-top-l) - var(--cfg-base-step) * 1.5);
    --_bg:    oklch(calc(var(--_l1) * 1%) var(--cfg-surf-chroma) var(--cfg-hue));
    --border: oklch(from var(--_bg) calc(l - 0.10) calc(c * 0.7) h);
    background: var(--_bg);
    color: oklch(from var(--_bg) calc(l - 0.55) 0.01 var(--cfg-hue));
}

body {
    font-family:    var(--font-body);
    background:     oklch(90% 0.012 220);
    color:          oklch(25% 0.015 220);
    min-height:     100svh;
    display:        flex;
    flex-direction: column;
}

@media (prefers-color-scheme: dark) {
    body    { background: oklch(13% 0.008 220); color: oklch(87% 0.008 220); }
    .surface { --_bg: oklch(20% 0.009 220); color: oklch(83% 0.007 220); }
}

header {
    padding:       0.9rem 1.5rem;
    border-bottom: 1px solid oklch(80% 0.010 220);
    display:       flex;
    align-items:   center;
    gap:           0.75rem;
}
@media (prefers-color-scheme: dark) {
    header { border-bottom-color: oklch(24% 0.009 220); }
}
header h1   { font-size: 1rem; font-weight: 600; letter-spacing: 0.02em; }
header small { margin-left: auto; font-size: 0.72rem; opacity: 0.4; font-family: var(--font-mono); }

main {
    flex:      1;
    padding:   1.5rem;
    max-width: 1280px;
    width:     100%;
    margin:    0 auto;
    display:   flex;
    flex-direction: column;
    gap:       2rem;
}

footer {
    text-align:  center;
    padding:     0.75rem;
    font-size:   0.7rem;
    opacity:     0.28;
    font-family: var(--font-mono);
    border-top:  1px solid currentColor;
}
"""

# ── Landing ───────────────────────────────────────────────────────────────────

def landing(conn):
    css = '\n'.join(filter(None, [_read_css('style.css'), _read_css('ui.css'), PAGE_CSS]))
    h = head(
        meta(charset='utf-8'),
        meta(name='viewport', content='width=device-width, initial-scale=1'),
        link(rel='stylesheet', href='static/style.css'),
        title_('md-web.ui — Dashboard Demo'),
        Favicon('📈'),
        style(Safe(css)),
        Datastar(),
    )
    b = body(
        div(id='sse-init', **{'data-init': "@get('/stream')"}),
        header(
            span('📈'),
            h1('md-web.ui — component library demo'),
            small('stat_card · bar_chart · line_chart · scatter_plot · activity_heatmap · bubble_map'),
        ),
        main(render_app(conn)),
        footer('md-web.ui · stat_card · bar_chart · line_chart · scatter_plot · activity_heatmap · card · grid · section'),
    )
    return html_doc(h, b)

# ── Tick loop ─────────────────────────────────────────────────────────────────



async def tick_loop():
    await _ready.wait()
    while True:
        await asyncio.sleep(0.0167)  # 16.7ms = 60 FPS target
        write(db, _tick)  # _tick handles everything including FPS

# ── Module state ──────────────────────────────────────────────────────────────

db     = None
relay  = None
_ready = asyncio.Event()

def startup(loop):
    global db, relay
    try:
        db    = create_db('dashboard_test.db')
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
static(app, "/static", "static/")

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
