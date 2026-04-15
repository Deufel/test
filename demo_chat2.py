"""
md-web chat demo — the Tao, taken seriously
============================================
Simplification: the server renders ONE thing — the full <body>.
Every SSE event is a fat morph of the entire body.
No surgical targeting, no per-component IDs to manage.
Both /events (read) and /send (write) are SSE streams.
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
title  = mk_tag('title')
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
main {
    flex: 1; overflow-y: auto; padding: 1rem 1.5rem;
    display: flex; flex-direction: column; gap: .5rem;
    scroll-behavior: smooth;
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

# ── Rendering — ONE function draws everything ─────────────────────────────────

def page_body():
    """The entire <body>. This is the only render function."""
    return body(id='body')(                                      # fat morph target
        {"data-signals": '{"msg":"","user":"anon"}'},
        {"data-on:load": "@get('/events')"},            # open SSE read stream

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

        main_(
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

def shell():
    """Static shell — served once. Body is immediately replaced by the SSE stream."""
    h = head(
        meta(charset='utf-8'),
        meta(name='viewport', content='width=device-width, initial-scale=1'),
        title('md-web chat'),
        Favicon('💬'),
        style(Safe(CSS)),
        Datastar(),
    )
    return html_doc(h, page_body())

# ── Routes ────────────────────────────────────────────────────────────────────

app = create_app()

@app.get('/')
async def index(req):
    print('[/] serving shell')
    return shell()

@app.get('/events')
async def events(req):
    """Long-lived SSE read stream. Sends full body on connect and on every message."""
    print('[/events] client connected')
    async def stream():
        print('[/events] sending initial body')
        yield patch_elements(page_body())
        async for _topic, _data in relay.subscribe('chat.message'):
            print(f'[/events] broadcasting, {len(messages)} messages')
            yield patch_elements(page_body())
    return stream()

@app.post('/send')
async def send(req):
    """Short-lived SSE write. Appends message, broadcasts, clears input signal."""
    data = await signals(req)
    text = (data.get('msg') or '').strip()
    name = (data.get('user') or 'anon').strip() or 'anon'
    print(f'[/send] {name!r}: {text!r}')

    async def stream():
        if text:
            messages.append({
                'name': name,
                'text': text,
                'color': random.choice(COLORS),
            })
            relay.publish('chat.message', None)
        # Clear the input signal on the sending client
        yield patch_signals({'msg': ''})

    return stream()

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('http://localhost:8000')
    serve(app, host='0.0.0.0', port=8000)
