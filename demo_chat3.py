"""
md-web chat demo
================
Two distinct response types — this is the core pattern:

  GET /        → text/html       — full page, served once
                                   contains data-init="@get('/stream')"
                                   which Datastar uses to open the SSE connection

  GET /stream  → text/event-stream — long-lived SSE, yields patch_elements forever
                                     fat-morphs #app on every message

  POST /send   → text/event-stream — short-lived SSE write, clears input signal
"""

import random
from md_web import (
    Safe, html_doc, mk_tag,
    Datastar, Favicon,
    patch_elements, patch_signals,
    create_app, create_relay, signals, serve,
)

# ── Tags ──────────────────────────────────────────────────────────────────────

head   = mk_tag('head')
body   = mk_tag('body')
meta   = mk_tag('meta')
title_ = mk_tag('title')
style  = mk_tag('style')
div    = mk_tag('div')
header = mk_tag('header')
main_  = mk_tag('main')
footer = mk_tag('footer')
input_ = mk_tag('input')
button = mk_tag('button')
ul     = mk_tag('ul')
li     = mk_tag('li')
span   = mk_tag('span')
h1     = mk_tag('h1')
small  = mk_tag('small')
label  = mk_tag('label')

# ── State ─────────────────────────────────────────────────────────────────────

messages: list[dict] = []
relay = create_relay()

COLORS = ['#e06c75','#61afef','#98c379','#e5c07b','#c678dd','#56b6c2','#d19a66']

# ── CSS ───────────────────────────────────────────────────────────────────────

CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #1e2030; color: #cdd6f4;
    height: 100dvh; display: flex; flex-direction: column;
}
header {
    padding: 1rem 1.5rem; background: #181926;
    border-bottom: 1px solid #313244;
    display: flex; align-items: center; gap: .75rem;
}
header h1 { font-size: 1.1rem; font-weight: 600; color: #cba6f7; }
header small { color: #6c7086; font-size: .8rem; }
.name-row {
    display: flex; gap: .5rem; align-items: center;
    padding: .75rem 1.5rem; background: #181926;
    border-bottom: 1px solid #313244;
}
.name-row label { font-size: .85rem; color: #6c7086; }
#app {
    flex: 1; display: flex; flex-direction: column; overflow: hidden;
}
#chat {
    flex: 1; overflow-y: auto; padding: 1rem 1.5rem;
    display: flex; flex-direction: column;
}
#msgs {
    display: flex; flex-direction: column; gap: .5rem;
    min-height: 100%; justify-content: flex-end;
}
.msg { display: flex; flex-direction: column; gap: .15rem; max-width: 70%; }
.msg .meta { font-size: .72rem; padding-left: .25rem; }
.msg .bubble {
    background: #313244; border-radius: 1rem 1rem 1rem .25rem;
    padding: .5rem .85rem; line-height: 1.5;
    word-break: break-word; font-size: .92rem;
}
footer {
    padding: .75rem 1.5rem; background: #181926;
    border-top: 1px solid #313244;
    display: flex; gap: .5rem; align-items: center;
}
input[type=text] {
    flex: 1; background: #313244; border: 1px solid #45475a;
    border-radius: .5rem; color: #cdd6f4;
    padding: .55rem .9rem; font-size: .95rem; outline: none;
}
input[type=text]:focus { border-color: #cba6f7; }
button {
    background: #cba6f7; color: #1e2030; border: none;
    border-radius: .5rem; padding: .55rem 1.1rem;
    font-weight: 600; font-size: .95rem; cursor: pointer;
}
button:hover { opacity: .85; }
"""

# ── Components ────────────────────────────────────────────────────────────────

def app_content():
    """The morphed region. Sent on every SSE update via patch_elements."""
    return div(
        id='app',
        **{"data-signals": '{"msg":"","user":"anon"}'},
    )(
        header(
            span('💬'),
            h1('md-web chat'),
            small('powered by Datastar + md-web'),
        ),
        div(cls='name-row')(
            label(_for='name-input')('your name:'),
            input_(
                id='name-input', type='text',
                placeholder='anon', data_bind='user',
                autocomplete='off', spellcheck='false',
            ),
        ),
        div(id='chat')(
            ul(id='msgs')(
                *[
                    li(cls='msg')(
                        span(m['name'], cls='meta', style=f'color:{m["color"]}'),
                        span(m['text'], cls='bubble'),
                    )
                    for m in messages
                ],
            ),
        ),
        footer(
            input_(
                type='text', placeholder='type a message…',
                data_bind='msg', autocomplete='off',
                **{"data-on:keydown.enter": "@post('/send')"},
            ),
            button(**{"data-on:click": "@post('/send')"})('Send'),
        ),
    )

def landing():
    """
    Full HTML page — returned once as text/html.

    The data-init on #app is what Datastar uses to open the SSE stream.
    It fires when Datastar initialises the element, before any morphing
    has happened, so the connection is always established.
    """
    h = head(
        meta(charset='utf-8'),
        meta(name='viewport', content='width=device-width, initial-scale=1'),
        title_('md-web chat'),
        Favicon('💬'),
        style(Safe(CSS)),
        Datastar(),
    )
    b = body(
        # data-init opens the SSE stream as soon as Datastar is ready.
        # This div IS the morph target — but data-init fires before
        # any patch arrives, so the connection is established first.
        app_content()(
            **{"data-init": "@get('/stream')"},
        ),
    )
    return html_doc(h, b)

# ── Routes ────────────────────────────────────────────────────────────────────

app = create_app()

@app.get('/')
async def index(req):
    """Return the full HTML page — text/html, served once."""
    print('[/] serving landing page')
    return landing()

@app.get('/stream')
async def stream(req):
    """
    Long-lived SSE stream — text/event-stream.
    Sends the full #app on connect, then on every message.
    """
    print('[/stream] client connected')
    async def _stream():
        print('[/stream] sending initial state')
        yield patch_elements(app_content())
        async for _topic, _data in relay.subscribe('chat.message'):
            print(f'[/stream] push → {len(messages)} messages')
            yield patch_elements(app_content())
    return _stream()

@app.post('/send')
async def send(req):
    """Short-lived SSE write — appends message, broadcasts, clears input."""
    data = await signals(req)
    text = (data.get('msg') or '').strip()
    name = (data.get('user') or 'anon').strip() or 'anon'
    print(f'[/send] {name!r}: {text!r}')

    async def _stream():
        if text:
            messages.append({
                'name': name,
                'text': text,
                'color': random.choice(COLORS),
            })
            relay.publish('chat.message', None)
        yield patch_signals({'msg': ''})

    return _stream()

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('http://localhost:8000')
    serve(app, host='0.0.0.0', port=8000)
