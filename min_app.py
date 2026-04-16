from md_web import create_app, serve, mk_tag, html_doc, Datastar, Favicon
from md_web import patch_elements, patch_signals, signals

div  = mk_tag('div')
h1   = mk_tag('h1')
head = mk_tag('head')
body = mk_tag('body')
meta = mk_tag('meta')

messages = []

def render():
    return div(id='app')(
        *[div(m) for m in messages]
    )

app = create_app()

@app.get('/')
async def index(req):
    h = head(meta(charset='utf-8'), Datastar(), Favicon('💬'))
    b = body(
        div(id='sse-init', **{'data-init': "@get('/stream')"}),
        render(),
    )
    return html_doc(h, b)

@app.get('/stream')
async def stream(req):
    async def _stream():
        yield patch_elements(render())
    return _stream()

@app.post('/send')
async def send(req):
    data = await signals(req)
    messages.append(data.get('msg', ''))
    async def _stream():
        yield patch_elements(render())
        yield patch_signals({'msg': ''})
    return _stream()

if __name__ == '__main__':
    serve(app)
