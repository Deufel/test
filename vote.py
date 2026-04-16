"""
md-web live poll demo
=====================
Real-time voting with SQLite + SSE. Demonstrates the full stack:

  md_web.db   → sync APSW, WAL, update_hook → asyncio relay + broadcaster
  md_web.app  → RSGI framework, CQRS routes
  md_web.html → tag-based rendering
  md_web.sse  → Datastar SSE events

Architecture
------------
  DB writes are plain sync APSW calls — fast, simple.
  The update_hook fires after every write, renders the app ONCE,
  caches the SSE string, and wakes all connected SSE streams via
  an asyncio.Event. Each stream yields the same cached string —
  O(1) renders per write regardless of how many clients are connected.

Run
---
  uv add apsw
  uv run python demo_poll.py
  open http://localhost:8000
"""

import asyncio
import time

from md_web import (
    Safe, html_doc, mk_tag,
    Datastar, Favicon,
    patch_elements, patch_signals,
    create_app, signals, serve,
)
from md_web.db import create_db, create_db_relay, migrate, query, write

# ── Tags ──────────────────────────────────────────────────────────────────────

head    = mk_tag('head')
body    = mk_tag('body')
meta    = mk_tag('meta')
title_  = mk_tag('title')
style   = mk_tag('style')
div     = mk_tag('div')
h1      = mk_tag('h1')
h2      = mk_tag('h2')
h3      = mk_tag('h3')
span    = mk_tag('span')
button  = mk_tag('button')
input_  = mk_tag('input')
label   = mk_tag('label')
small   = mk_tag('small')
footer  = mk_tag('footer')
header  = mk_tag('header')
section = mk_tag('section')

# ── Tuple column indexes ──────────────────────────────────────────────────────

# polls  (id, question, created_at)
POLL_ID, POLL_QUESTION = 0, 1

# options (id, poll_id, text, color)
OPT_ID, OPT_POLL_ID, OPT_TEXT, OPT_COLOR = 0, 1, 2, 3

# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
    id         INTEGER PRIMARY KEY,
    question   TEXT    NOT NULL,
    created_at REAL    NOT NULL DEFAULT (unixepoch('now','subsec'))
);
CREATE TABLE IF NOT EXISTS options (
    id       INTEGER PRIMARY KEY,
    poll_id  INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    text     TEXT    NOT NULL,
    color    TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS votes (
    id         INTEGER PRIMARY KEY,
    option_id  INTEGER NOT NULL REFERENCES options(id) ON DELETE CASCADE,
    voted_at   REAL    NOT NULL DEFAULT (unixepoch('now','subsec'))
);
CREATE INDEX IF NOT EXISTS votes_option ON votes(option_id);
"""

# ── Seed data ─────────────────────────────────────────────────────────────────

SEED_POLLS = [
    {
        "question": "What is your favourite programming language?",
        "options": [
            ("Python",     "#3b82f6"),
            ("Rust",       "#f97316"),
            ("TypeScript", "#8b5cf6"),
            ("Go",         "#10b981"),
        ],
    },
    {
        "question": "Best web architecture?",
        "options": [
            ("Hypermedia / SSR", "#e11d48"),
            ("SPA / React",      "#f59e0b"),
            ("Full-stack MVC",   "#6366f1"),
            ("Serverless",       "#0ea5e9"),
        ],
    },
]

def seed(conn):
    count = conn.execute("SELECT COUNT(*) FROM polls").fetchone()[0]
    if count > 0:
        return
    with conn:
        for poll in SEED_POLLS:
            conn.execute(
                "INSERT INTO polls(question) VALUES(?)", (poll["question"],)
            )
            poll_id = conn.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]
            for text, color in poll["options"]:
                conn.execute(
                    "INSERT INTO options(poll_id, text, color) VALUES(?,?,?)",
                    (poll_id, text, color),
                )

# ── CSS ───────────────────────────────────────────────────────────────────────

CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #0f172a;
    color: #e2e8f0;
    min-height: 100dvh;
    display: flex;
    flex-direction: column;
}
header {
    padding: 1.25rem 2rem;
    background: #1e293b;
    border-bottom: 1px solid #334155;
    display: flex;
    align-items: center;
    gap: .75rem;
}
header h1 { font-size: 1.1rem; font-weight: 700; color: #7c3aed; }
header small { color: #64748b; font-size: .8rem; margin-left: auto; }

#app {
    flex: 1;
    max-width: 720px;
    width: 100%;
    margin: 0 auto;
    padding: 2rem 1.5rem;
    display: flex;
    flex-direction: column;
    gap: 2rem;
}

/* Poll card */
.poll {
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: .75rem;
    padding: 1.5rem;
    display: flex;
    flex-direction: column;
    gap: 1rem;
}
.poll-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: .5rem;
}
.poll h3 {
    font-size: 1rem;
    font-weight: 600;
    color: #f1f5f9;
    line-height: 1.4;
    flex: 1;
}
.poll .total { font-size: .75rem; color: #64748b; }
.delete-btn {
    background: none;
    border: 1px solid #334155;
    border-radius: .35rem;
    color: #64748b;
    font-size: .75rem;
    padding: .2rem .5rem;
    cursor: pointer;
    flex-shrink: 0;
    transition: border-color .15s, color .15s;
}
.delete-btn:hover { border-color: #ef4444; color: #ef4444; }

/* Option rows */
.option { display: flex; flex-direction: column; gap: .3rem; }
.option-row {
    display: flex;
    align-items: center;
    gap: .75rem;
}
.opt-btn {
    border: none;
    border-radius: .4rem;
    padding: .45rem 1rem;
    font-size: .85rem;
    font-weight: 600;
    cursor: pointer;
    color: #fff;
    white-space: nowrap;
    min-width: 90px;
    transition: opacity .15s;
}
.opt-btn:hover { opacity: .85; }
.opt-label { font-size: .9rem; color: #cbd5e1; flex: 1; }
.opt-count { font-size: .78rem; color: #94a3b8; min-width: 48px; text-align: right; }
.bar-track {
    height: 6px;
    background: #334155;
    border-radius: 3px;
    overflow: hidden;
}
.bar-fill {
    height: 100%;
    border-radius: 3px;
    transition: width .4s ease;
}

/* New poll form */
.new-poll {
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: .75rem;
    padding: 1.5rem;
    display: flex;
    flex-direction: column;
    gap: .85rem;
}
.new-poll h2 {
    font-size: .95rem;
    font-weight: 600;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: .05em;
}
.field { display: flex; flex-direction: column; gap: .3rem; }
.field label { font-size: .8rem; color: #64748b; }
.field input[type=text] {
    background: #0f172a;
    border: 1px solid #334155;
    border-radius: .4rem;
    color: #e2e8f0;
    padding: .5rem .75rem;
    font-size: .9rem;
    outline: none;
    width: 100%;
}
.field input:focus { border-color: #7c3aed; }
.options-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: .5rem;
}
.submit-btn {
    background: #7c3aed;
    color: #fff;
    border: none;
    border-radius: .4rem;
    padding: .6rem 1.25rem;
    font-weight: 600;
    font-size: .9rem;
    cursor: pointer;
    align-self: flex-start;
}
.submit-btn:hover { opacity: .9; }

footer {
    text-align: center;
    padding: 1rem;
    color: #334155;
    font-size: .75rem;
    border-top: 1px solid #1e293b;
}
"""

# ── Rendering ─────────────────────────────────────────────────────────────────

def render_bar(count, total, color):
    pct = int(count / total * 100) if total else 0
    return div(cls='bar-track')(
        div(cls='bar-fill', style=f'width:{pct}%; background:{color}'),
    )

def render_option(opt, count, total):
    return div(cls='option')(
        div(cls='option-row')(
            button(
                cls='opt-btn',
                style=f'background:{opt[OPT_COLOR]}',
                **{"data-on:click": f"@post('/vote?option_id={opt[OPT_ID]}')"},
            )(opt[OPT_TEXT]),
            span(opt[OPT_TEXT], cls='opt-label'),
            span(f'{count} vote{"s" if count != 1 else ""}', cls='opt-count'),
        ),
        render_bar(count, total, opt[OPT_COLOR]),
    )

def render_poll(poll, options, counts):
    total = sum(counts.get(opt[OPT_ID], 0) for opt in options)
    return div(cls='poll', id=f'poll-{poll[POLL_ID]}')(
        div(cls='poll-header')(
            h3(poll[POLL_QUESTION]),
            button(
                cls='delete-btn',
                **{"data-on:click": f"@post('/delete-poll?poll_id={poll[POLL_ID]}')"},
            )('✕ delete'),
        ),
        span(f'{total} vote{"s" if total != 1 else ""} total', cls='total'),
        *[render_option(opt, counts.get(opt[OPT_ID], 0), total)
          for opt in options],
    )

def render_new_poll_form():
    return div(cls='new-poll')(
        h2('Create a poll'),
        div(cls='field')(
            label('Question'),
            input_(
                type='text',
                placeholder='Ask something…',
                data_bind='question',
                autocomplete='off',
            ),
        ),
        div(cls='field')(
            label('Options (fill at least 2)'),
            div(cls='options-grid')(
                *[
                    input_(
                        type='text',
                        placeholder=f'Option {i+1}',
                        data_bind=f'opt{i}',
                        autocomplete='off',
                    )
                    for i in range(4)
                ],
            ),
        ),
        button(
            cls='submit-btn',
            **{"data-on:click": "@post('/new-poll')"},
        )('Create Poll'),
    )

def render_app(conn):
    """Render the full #app from the database. Sync, fast."""
    polls   = query(conn, "SELECT * FROM polls ORDER BY id DESC LIMIT 20")
    options = query(conn, "SELECT * FROM options ORDER BY poll_id, id")
    vcounts = query(conn,
        "SELECT option_id, COUNT(*) as cnt FROM votes GROUP BY option_id"
    )

    opts_by_poll: dict = {}
    for opt in options:
        opts_by_poll.setdefault(opt[OPT_POLL_ID], []).append(opt)

    counts: dict = {row[0]: row[1] for row in vcounts}

    return div(
        id='app',
        **{"data-signals": '{"question":"","opt0":"","opt1":"","opt2":"","opt3":""}'},
    )(
        *[render_poll(p, opts_by_poll.get(p[POLL_ID], []), counts)
          for p in polls],
        render_new_poll_form(),
    )

def landing(initial_app):
    h = head(
        meta(charset='utf-8'),
        meta(name='viewport', content='width=device-width, initial-scale=1'),
        title_('md-web poll'),
        Favicon('🗳️'),
        style(Safe(CSS)),
        Datastar(),
    )
    b = body(
        div(id='sse-init', **{"data-init": "@get('/stream')"}),
        header(
            span('🗳️'),
            h1('Live Poll'),
            small('results update in real time'),
        ),
        initial_app,
        footer('powered by md-web + Datastar + SQLite'),
    )
    return html_doc(h, b)

# ── DB write helpers (sync) ───────────────────────────────────────────────────

OPTION_COLORS = [
    '#3b82f6','#f97316','#8b5cf6','#10b981',
    '#e11d48','#f59e0b','#6366f1','#0ea5e9',
]

def _do_vote(conn, option_id: int):
    conn.execute("INSERT INTO votes(option_id) VALUES(?)", (option_id,))

def _do_delete_poll(conn, poll_id: int):
    conn.execute("DELETE FROM polls WHERE id=?", (poll_id,))

def _do_new_poll(conn, question: str, opts: list[str]):
    conn.execute("INSERT INTO polls(question) VALUES(?)", (question,))
    poll_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for i, text in enumerate(opts):
        conn.execute(
            "INSERT INTO options(poll_id, text, color) VALUES(?,?,?)",
            (poll_id, text, OPTION_COLORS[i % len(OPTION_COLORS)]),
        )

# ── Module-level state ────────────────────────────────────────────────────────

db    = None
relay = None
_ready = asyncio.Event()

def startup(loop):
    global db, relay
    try:
        db    = create_db("poll.db")
        relay = create_db_relay(
            db, loop,
            render_fn=lambda: patch_elements(render_app(db)),
        )
        migrate(db, SCHEMA)
        seed(db)
        print('[startup] db ready')
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        loop.call_soon_threadsafe(_ready.set)

# ── App ───────────────────────────────────────────────────────────────────────

app = create_app(on_init=startup)

@app.get('/')
async def index(req):
    await _ready.wait()
    return landing(render_app(db))

@app.get('/stream')
async def stream(req):
    await _ready.wait()
    print('[/stream] client connected')
    async def _stream():
        # Initial state — rendered once for this client
        yield patch_elements(render_app(db))
        # All subsequent updates — pre-rendered once, shared by all clients
        async for event_str in relay.broadcaster.subscribe():
            if event_str:
                yield event_str
    return _stream()

@app.post('/vote')
async def vote(req):
    await _ready.wait()
    option_id = int(req['query'].get('option_id', 0))
    if option_id:
        write(db, _do_vote, option_id)
        print(f'[/vote] option {option_id}')
    async def _stream():
        yield patch_signals({})
    return _stream()

@app.post('/delete-poll')
async def delete_poll(req):
    await _ready.wait()
    poll_id = int(req['query'].get('poll_id', 0))
    if poll_id:
        write(db, _do_delete_poll, poll_id)
        print(f'[/delete-poll] poll {poll_id}')
    async def _stream():
        yield patch_signals({})
    return _stream()

@app.post('/new-poll')
async def new_poll(req):
    await _ready.wait()
    data     = await signals(req)
    question = (data.get('question') or '').strip()
    opts     = [(data.get(f'opt{i}') or '').strip() for i in range(4)]
    opts     = [o for o in opts if o]
    if question and len(opts) >= 2:
        write(db, _do_new_poll, question, opts)
        print(f'[/new-poll] {question!r} with {len(opts)} options')
    async def _stream():
        yield patch_signals({'question':'','opt0':'','opt1':'','opt2':'','opt3':''})
    return _stream()

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('http://localhost:8000')
#    serve(app, host='0.0.0.0', port=8000)
    serve(app, host='0.0.0.0', port=8000, backpressure=500)
