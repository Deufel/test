"""
md-web chat demo
================
A minimal live chat room that follows the Tao of Datastar:

  - ALL state lives on the server (message list, user colours)
  - ONE long-lived GET /events stream per client (CQRS read)
  - ONE short POST /send per message (CQRS write)
  - Signals are used ONLY to carry the draft message up to the server
  - The server drives every DOM update via patch_elements (fat morph)

Run:
    pip install granian
    python demo_chat.py

Then open http://localhost:8000
"""

import random
from md_web import (
    # HTML construction
    Safe, Tag, html_doc, mk_tag,
    Datastar, Favicon,
    # SSE
    patch_elements, patch_signals,
    # App
    create_app, create_relay, signals, serve,
)

# ── Tag shorthands ────────────────────────────────────────────────────────────

head   = mk_tag('head')
body   = mk_tag('body')
meta   = mk_tag('meta')
title  = mk_tag('title')
style  = mk_tag('style')
div    = mk_tag('div')
header = mk_tag('header')
main   = mk_tag('main')
footer = mk_tag('footer')
form   = mk_tag('form')
input_ = mk_tag('input')
button = mk_tag('button')
ul     = mk_tag('ul')
li     = mk_tag('li')
span   = mk_tag('span')
p      = mk_tag('p')
h1     = mk_tag('h1')
small  = mk_tag('small')

# ── Server state (lives here, not in the browser) ─────────────────────────────

messages: list[dict] = []   # [{name, text, color}]
relay = create_relay()

COLORS = [
    '#e06c75', '#61afef', '#98c379',
    '#e5c07b', '#c678dd', '#56b6c2',
    '#d19a66',
]

def _color():
    return random.choice(COLORS)

# ── HTML components ───────────────────────────────────────────────────────────

CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #1e2030;
    color: #cdd6f4;
    height: 100dvh;
    display: flex;
    flex-direction: column;
}

header {
    padding: 1rem 1.5rem;
    background: #181926;
    border-bottom: 1px solid #313244;
    display: flex;
    align-items: center;
    gap: .75rem;
}

header h1 { font-size: 1.1rem; font-weight: 600; color: #cba6f7; }
header small { color: #6c7086; font-size: .8rem; }

main {
    flex: 1;
    overflow-y: auto;
    padding: 1rem 1.5rem;
    display: flex;
    flex-direction: column;
    gap: .5rem;
    scroll-behavior: smooth;
}

#msg-list {
    display: flex;
    flex-direction: column;
    gap: .5rem;
    min-height: 100%;
    justify-content: flex-end;
}

.msg {
    display: flex;
    flex-direction: column;
    gap: .15rem;
    max-width: 70%;
    animation: fadein .2s ease;
}

.msg .meta {
    font-size: .72rem;
    color: #6c7086;
    padding-left: .25rem;
}

.msg .bubble {
    background: #313244;
    border-radius: 1rem 1rem 1rem .25rem;
    padding: .5rem .85rem;
    line-height: 1.5;
    word-break: break-word;
    font-size: .92rem;
}

footer {
    padding: .75rem 1.5rem;
    background: #181926;
    border-top: 1px solid #313244;
    display: flex;
    gap: .5rem;
    align-items: center;
}

input[type=text] {
    flex: 1;
    background: #313244;
    border: 1px solid #45475a;
    border-radius: .5rem;
    color: #cdd6f4;
    padding: .55rem .9rem;
    font-size: .95rem;
    outline: none;
    transition: border-color .15s;
}
input[type=text]:focus { border-color: #cba6f7; }

button[type=submit] {
    background: #cba6f7;
    color: #1e2030;
    border: none;
    border-radius: .5rem;
    padding: .55rem 1.1rem;
    font-weight: 600;
    font-size: .95rem;
    cursor: pointer;
    transition: opacity .15s;
    white-space: nowrap;
}
button[type=submit]:hover { opacity: .85; }

.name-row {
    display: flex;
    gap: .5rem;
    align-items: center;
    padding: .75rem 1.5rem;
    background: #181926;
    border-bottom: 1px solid #313244;
}
.name-row label { font-size: .85rem; color: #6c7086; }
.name-row input {
    flex: 1;
    max-width: 240px;
    background: #313244;
    border: 1px solid #45475a;
    border-radius: .5rem;
    color: #cdd6f4;
    padding: .4rem .75rem;
    font-size: .88rem;
    outline: none;
}
.name-row input:focus { border-color: #cba6f7; }

@keyframes fadein {
    from { opacity: 0; transform: translateY(4px); }
    to   { opacity: 1; transform: translateY(0); }
}
"""

def msg_item(m):
    """Single chat bubble."""
    return li(
        cls='msg',
        style=f'--accent:{m["color"]}',
    )(
        span(m['name'], cls='meta', style=f'color:{m["color"]}'),
        span(m['text'], cls='bubble'),
    )

def msg_list():
    """The full message list — this is what gets morphed on every update."""
    return ul(id='msg-list')(
        *[msg_item(m) for m in messages],
    )

def page():
    """Full page — only rendered once on initial load."""
    h = head(
        meta(charset='utf-8'),
        meta(name='viewport', content='width=device-width, initial-scale=1'),
        title('md-web chat'),
        Favicon('💬'),
        style(Safe(CSS)),
        Datastar(),
    )

    b = body(
        # Signals: draft message text + display name (only used for input)
        {"data-signals": '{"msg":"","user":"anon"}'},
        # On load: open the long-lived SSE read stream (CQRS)
        {"data-on:load": "@get('/events')"},

        header(
            span('💬'),
            h1('md-web chat'),
            small('powered by Datastar + md-web'),
        ),

        # Name row — signal only, never hits the server on its own
        div(cls='name-row')(
            mk_tag('label')(_for='name-input')('your name:'),
            input_(
                id='name-input',
                type='text',
                placeholder='anon',
                data_bind='user',
                autocomplete='off',
                spellcheck='false',
            ),
        ),

        main(
            msg_list(),   # initial render of message list
        ),

        footer(
            input_(
                type='text',
                placeholder='type a message…',
                data_bind='msg',
                autocomplete='off',
                # Send on Enter key (CQRS write)
                **{"data-on:keydown.enter": "@post('/send')"},
            ),
            button(
                type='submit',
                # Send on click (CQRS write)
                **{"data-on:click": "@post('/send')"},
            )('Send'),
        ),
    )

    return html_doc(h, b)

# ── Routes ────────────────────────────────────────────────────────────────────

app = create_app()

@app.get('/')
async def index(req):
    """Serve the full page once."""
    return page()

@app.get('/events')
async def events(req):
    """Long-lived SSE read stream (CQRS read).

    Immediately sends the current message list, then blocks waiting for
    relay publishes. Every publish sends a fresh fat-morph of the list.
    """
    async def stream():
        # Immediately hydrate the client with current state
        yield patch_elements(msg_list())

        # Then block on the relay until the client disconnects
        async for _topic, _data in relay.subscribe('chat.message'):
            yield patch_elements(msg_list())

    return stream()

@app.post('/send')
async def send(req):
    """Short-lived CQRS write.

    Reads the two signals (msg + user), appends to server state,
    broadcasts to all /events streams, returns nothing (204).
    """
    data = await signals(req)
    text = (data.get('msg') or '').strip()
    name = (data.get('user') or 'anon').strip() or 'anon'

    if text:
        messages.append({
            'name':  name,
            'text':  text,
            'color': _color(),
        })
        relay.publish('chat.message', None)

    # Clear the draft signal so the input empties after send
    return patch_signals({'msg': ''})

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('http://localhost:8000')
    serve(app, host='0.0.0.0', port=8000)
