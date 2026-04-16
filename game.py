"""
md-web multiplayer Conway's Game of Life
=========================================
Every connected browser sees the same board in real time.
Any player can draw, step, run, pause, or clear — all changes
fan out to all watchers via the O(1) broadcaster.

Architecture
------------
  Board state lives in SQLite as a single JSON blob row.
  The tick loop runs as an asyncio task; each tick does one
  DB write → update_hook fires → broadcaster renders ONCE →
  all SSE clients wake and yield the same cached string.

  Drawing: POST /toggle?x=N&y=N  flips one cell, triggers broadcast.
  Controls: POST /step, /run, /pause, /clear, /random

Run
---
  uv run python game.py
  open http://localhost:8000
"""

import asyncio
import json
import random

from md_web import (
    Safe, html_doc, mk_tag,
    Datastar, Favicon,
    patch_elements, patch_signals,
    create_app, serve,
)
from md_web.db import create_db, create_db_relay, migrate, query, write

# ── Tags ──────────────────────────────────────────────────────────────────────

head   = mk_tag('head')
body   = mk_tag('body')
meta   = mk_tag('meta')
title_ = mk_tag('title')
style  = mk_tag('style')
div    = mk_tag('div')
span   = mk_tag('span')
button = mk_tag('button')
footer = mk_tag('footer')
header = mk_tag('header')
svg    = mk_tag('svg')
rect   = mk_tag('rect')

# ── Board constants ───────────────────────────────────────────────────────────

COLS      = 60
ROWS      = 40
CELL_PX   = 14          # rendered cell size in pixels
GAP_PX    = 1
TICK_MS   = 120         # ms between generations when running

# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS game (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    cells   TEXT    NOT NULL,
    running INTEGER NOT NULL DEFAULT 0,
    gen     INTEGER NOT NULL DEFAULT 0
);
"""

# ── Board helpers ─────────────────────────────────────────────────────────────

def empty_board() -> list[list[int]]:
    return [[0] * COLS for _ in range(ROWS)]

def random_board(density: float = 0.30) -> list[list[int]]:
    return [[1 if random.random() < density else 0 for _ in range(COLS)]
            for _ in range(ROWS)]

def step_board(cells: list[list[int]]) -> list[list[int]]:
    """One Conway generation — standard B3/S23 rules."""
    nxt = empty_board()
    for r in range(ROWS):
        for c in range(COLS):
            live = 0
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    nr, nc = (r + dr) % ROWS, (c + dc) % COLS
                    live += cells[nr][nc]
            if cells[r][c]:
                nxt[r][c] = 1 if live in (2, 3) else 0
            else:
                nxt[r][c] = 1 if live == 3 else 0
    return nxt

# ── DB write helpers ──────────────────────────────────────────────────────────

def _db_init(conn):
    row = conn.execute("SELECT id FROM game WHERE id=1").fetchone()
    if row is None:
        cells = random_board()
        conn.execute(
            "INSERT INTO game(id, cells, running, gen) VALUES(1,?,0,0)",
            (json.dumps(cells),),
        )

def _db_set_cells(conn, cells, gen):
    conn.execute(
        "UPDATE game SET cells=?, gen=? WHERE id=1",
        (json.dumps(cells), gen),
    )

def _db_set_running(conn, running: int):
    conn.execute("UPDATE game SET running=? WHERE id=1", (running,))

def _db_toggle(conn, x: int, y: int):
    row   = conn.execute("SELECT cells, gen FROM game WHERE id=1").fetchone()
    cells = json.loads(row[0])
    gen   = row[1]
    cells[y][x] ^= 1
    conn.execute(
        "UPDATE game SET cells=?, gen=? WHERE id=1",
        (json.dumps(cells), gen),
    )

def _db_clear(conn):
    conn.execute(
        "UPDATE game SET cells=?, running=0, gen=0 WHERE id=1",
        (json.dumps(empty_board()),),
    )

def _db_random(conn):
    conn.execute(
        "UPDATE game SET cells=?, running=0, gen=0 WHERE id=1",
        (json.dumps(random_board()),),
    )

def _db_step_once(conn):
    row   = conn.execute("SELECT cells, gen FROM game WHERE id=1").fetchone()
    cells = json.loads(row[0])
    nxt   = step_board(cells)
    conn.execute(
        "UPDATE game SET cells=?, gen=? WHERE id=1",
        (json.dumps(nxt), row[1] + 1),
    )

# ── CSS ───────────────────────────────────────────────────────────────────────

CSS = f"""
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

body {{
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #0a0f1e;
    color: #e2e8f0;
    min-height: 100dvh;
    display: flex;
    flex-direction: column;
}}

header {{
    padding: .85rem 1.5rem;
    background: #0f172a;
    border-bottom: 1px solid #1e293b;
    display: flex;
    align-items: center;
    gap: 1rem;
    flex-wrap: wrap;
}}
header h1 {{
    font-size: 1rem;
    font-weight: 700;
    color: #6ee7b7;
    letter-spacing: .04em;
    flex-shrink: 0;
}}

.controls {{
    display: flex;
    gap: .5rem;
    flex-wrap: wrap;
}}
.btn {{
    border: 1px solid #1e293b;
    border-radius: .35rem;
    background: #1e293b;
    color: #cbd5e1;
    font-size: .8rem;
    font-weight: 600;
    padding: .35rem .8rem;
    cursor: pointer;
    transition: background .12s, border-color .12s, color .12s;
    white-space: nowrap;
}}
.btn:hover  {{ background: #334155; border-color: #475569; color: #f1f5f9; }}
.btn.run    {{ background: #064e3b; border-color: #065f46; color: #6ee7b7; }}
.btn.run:hover {{ background: #065f46; }}
.btn.pause  {{ background: #3b1f00; border-color: #78350f; color: #fbbf24; }}
.btn.pause:hover {{ background: #78350f; }}
.btn.danger {{ border-color: #7f1d1d; color: #fca5a5; }}
.btn.danger:hover {{ background: #7f1d1d; color: #fee2e2; }}

.meta {{
    margin-left: auto;
    display: flex;
    gap: 1rem;
    align-items: center;
    font-size: .75rem;
    color: #475569;
}}
.meta .gen {{ color: #94a3b8; font-variant-numeric: tabular-nums; }}
.meta .live {{ color: #6ee7b7; }}

#board-wrap {{
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 1.5rem;
    overflow: auto;
}}

#board {{
    display: block;
    cursor: crosshair;
    border: 1px solid #1e293b;
    border-radius: 4px;
}}

.cell-alive {{ fill: #6ee7b7; }}
.cell-dead  {{ fill: #0f1b2d; }}
.cell-alive:hover {{ fill: #a7f3d0; }}
.cell-dead:hover  {{ fill: #1e3a5f; }}

footer {{
    text-align: center;
    padding: .6rem;
    color: #1e293b;
    font-size: .7rem;
    border-top: 1px solid #0f172a;
}}
"""

# ── SVG board renderer ────────────────────────────────────────────────────────

TOTAL_W = COLS * (CELL_PX + GAP_PX) + GAP_PX
TOTAL_H = ROWS * (CELL_PX + GAP_PX) + GAP_PX

def render_board_svg(cells: list[list[int]]):
    """Render the grid as an SVG Tag tree.
    The framework auto-namespaces svg and handles rect as SVG void elements."""
    cells_flat = []
    for r in range(ROWS):
        y = GAP_PX + r * (CELL_PX + GAP_PX)
        for c in range(COLS):
            x   = GAP_PX + c * (CELL_PX + GAP_PX)
            cls = 'cell-alive' if cells[r][c] else 'cell-dead'
            cells_flat.append(
                rect(
                    cls=cls, x=str(x), y=str(y),
                    width=str(CELL_PX), height=str(CELL_PX), rx='1',
                    **{"data-on:click": f"@post('/toggle?x={c}&y={r}')"},
                )
            )
    return svg(
        id='board',
        width=str(TOTAL_W), height=str(TOTAL_H),
        viewBox=f'0 0 {TOTAL_W} {TOTAL_H}',
    )(*cells_flat)

def render_app(conn) -> str:
    """Full #app render from DB. Called once per DB write."""
    row     = conn.execute("SELECT cells, running, gen FROM game WHERE id=1").fetchone()
    cells   = json.loads(row[0])
    running = row[1]
    gen     = row[2]
    live    = sum(cells[r][c] for r in range(ROWS) for c in range(COLS))

    # Controls
    if running:
        run_btn = button(
            cls='btn pause',
            **{"data-on:click": "@post('/pause')"},
        )('⏸ Pause')
    else:
        run_btn = button(
            cls='btn run',
            **{"data-on:click": "@post('/run')"},
        )('▶ Run')

    ctrl = div(cls='controls')(
        run_btn,
        button(cls='btn', **{"data-on:click": "@post('/step')"})('⏭ Step'),
        button(cls='btn', **{"data-on:click": "@post('/random')"})('🎲 Random'),
        button(cls='btn danger', **{"data-on:click": "@post('/clear')"})('✕ Clear'),
    )

    meta_bar = div(cls='meta')(
        span(f'gen ', cls='gen')(span(f'{gen:,}')),
        span(f'{live:,} alive', cls='live'),
    )

    app_div = div(id='app')(
        div(id='board-wrap')(render_board_svg(cells)),
    )

    # Header is outside #app so it can show gen/live without re-triggering
    # the data-init.  We patch both elements together.
    return (
        div(id='header-inner')(ctrl, meta_bar),
        app_div,
    )

def render_patch(conn) -> str:
    """Produce two patch_elements SSE events: header controls + board.
    Concatenated into one string so the broadcaster can cache and fan
    out both as a single yield from the stream generator."""
    header_inner, app_div = render_app(conn)
    return patch_elements(header_inner) + patch_elements(app_div)

def landing(conn) -> str:
    header_inner, app_div = render_app(conn)

    h = head(
        meta(charset='utf-8'),
        meta(name='viewport', content='width=device-width, initial-scale=1'),
        title_('Game of Life'),
        Favicon('🧬'),
        style(Safe(CSS)),
        Datastar(),
    )
    b = body(
        div(id='sse-init', **{"data-init": "@get('/stream')"}),
        header(
            span('🧬'),
            mk_tag('h1')('Multiplayer Game of Life'),
            div(id='header-inner')(header_inner),
        ),
        app_div,
        footer('md-web · Datastar · SQLite — every click is shared'),
    )
    return html_doc(h, b)

# ── Tick loop ─────────────────────────────────────────────────────────────────

async def tick_loop():
    """Advance one generation every TICK_MS when running=1."""
    await _ready.wait()
    while True:
        await asyncio.sleep(TICK_MS / 1000)
        row = db.execute("SELECT running FROM game WHERE id=1").fetchone()
        if row and row[0]:
            write(db, _db_step_once)   # update_hook → broadcaster fires

# ── Module state ──────────────────────────────────────────────────────────────

db     = None
relay  = None
_ready = asyncio.Event()

def startup(loop):
    global db, relay
    try:
        db    = create_db("life.db")
        relay = create_db_relay(
            db, loop,
            render_fn=lambda: render_patch(db),
        )
        migrate(db, SCHEMA)
        write(db, _db_init)
        loop.create_task(tick_loop())
        print('[startup] ready')
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
        yield render_patch(db)
        async for ev in relay.broadcaster.subscribe():
            if ev:
                yield ev
    return _stream()

@app.post('/toggle')
async def toggle(req):
    await _ready.wait()
    x = int(req['query'].get('x', -1))
    y = int(req['query'].get('y', -1))
    if 0 <= x < COLS and 0 <= y < ROWS:
        write(db, _db_toggle, x, y)
    async def _s():
        yield patch_signals({})
    return _s()

@app.post('/step')
async def step(req):
    await _ready.wait()
    write(db, _db_step_once)
    async def _s():
        yield patch_signals({})
    return _s()

@app.post('/run')
async def run(req):
    await _ready.wait()
    write(db, _db_set_running, 1)
    async def _s():
        yield patch_signals({})
    return _s()

@app.post('/pause')
async def pause(req):
    await _ready.wait()
    write(db, _db_set_running, 0)
    async def _s():
        yield patch_signals({})
    return _s()

@app.post('/clear')
async def clear(req):
    await _ready.wait()
    write(db, _db_clear)
    async def _s():
        yield patch_signals({})
    return _s()

@app.post('/random')
async def random_board_route(req):
    await _ready.wait()
    write(db, _db_random)
    async def _s():
        yield patch_signals({})
    return _s()

# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('http://localhost:8000')
    serve(app, host='0.0.0.0', port=8000, backpressure=500)
