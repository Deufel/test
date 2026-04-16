"""HTML/SVG tag construction and rendering."""
import types, json
from html import escape
from html.parser import HTMLParser
from urllib.parse import quote

VOID = frozenset('area base br col embed hr img input link meta source track wbr'.split())
RAW  = frozenset('script style'.split())

SVG_VOID = frozenset(
    'circle ellipse line path polygon polyline rect stop set image use '
    'feBlend feColorMatrix feComposite feConvolveMatrix feDisplacementMap '
    'feDistantLight feDropShadow feFlood feFuncA feFuncB feFuncG feFuncR '
    'feGaussianBlur feImage feMergeNode feMorphology feOffset fePointLight '
    'feSpotLight feTile feTurbulence'.split()
)
MATH_VOID = frozenset('mprescripts none'.split())

NS_RULES = {
    'html': (VOID,      False),
    'svg':  (SVG_VOID,  True),
    'math': (MATH_VOID, False),
}
NS_ATTRS = {
    'svg':  'xmlns="http://www.w3.org/2000/svg"',
    'math': 'xmlns="http://www.w3.org/1998/Math/MathML"',
}
ATTR_MAP = {
    'cls':    'class',
    '_class': 'class',
    '_for':   'for',
    '_from':  'from',
    '_in':    'in',
    '_is':    'is',
}


# ── Core types ───────────────────────────────────────────────────────────────

class Safe(str):
    """A string that is already HTML-safe and will not be escaped on render."""
    def __html__(self): return self


class Tag:
    """A lazily-rendered HTML/SVG element."""

    def __init__(self, tag, cs=(), attrs=None):
        self.tag      = tag
        self.children = cs
        self.attrs    = attrs or {}

    def __call__(self, *c, **kw):
        c, kw = _preproc(c, kw)
        if c:  self.children = self.children + c
        if kw: self.attrs    = {**self.attrs, **kw}
        return self

    def __html__(self):
        return render(self)

    def __repr__(self):
        return f'{self.tag}({self.children}, {self.attrs})'


# ── Internal helpers ─────────────────────────────────────────────────────────

def unpack(items):
    """Flatten nested iterables, dropping None and False."""
    out = []
    for o in items:
        if o is None or o is False:
            continue
        elif isinstance(o, (list, tuple, types.GeneratorType)):
            out.extend(unpack(o))
        else:
            out.append(o)
    return tuple(out)


def _preproc(c, kw):
    """Separate positional children from dict-style attr overrides."""
    ch, d = [], {}
    for o in c:
        if isinstance(o, dict): d.update(o)
        else:                   ch.append(o)
    d.update(kw)
    return unpack(ch), d


# ── Rendering ────────────────────────────────────────────────────────────────

def render_attrs(d):
    """Render an attribute dict to an HTML attribute string."""
    out = []
    for k, v in d.items():
        k = ATTR_MAP.get(k, k.rstrip('_').replace('_', '-'))
        if v is True:
            out.append(f' {k}')
        elif v not in (False, None):
            out.append(f' {k}="{escape(str(v))}"')
    return ''.join(out)


def render(node, ns='html', depth=0, indent=2):
    """Recursively render a Tag (or any value) to an HTML string."""
    if isinstance(node, Safe):
        return str(node)
    if not isinstance(node, Tag):
        return ' ' * (indent * depth) + escape(str(node))

    tag, children, a = node.tag, node.children, node.attrs

    # Namespace tracking for SVG / MathML / foreignObject
    new_ns = ns
    if   tag == 'svg':           new_ns = 'svg'
    elif tag == 'math':          new_ns = 'math'
    elif tag == 'foreignObject': new_ns = 'html'

    voids, self_close = NS_RULES[new_ns]
    attr_str = render_attrs(a)
    if tag in NS_ATTRS:
        attr_str = f' {NS_ATTRS[tag]}' + attr_str

    pad = ' ' * (indent * depth)

    if tag in voids:
        return f'{pad}<{tag}{attr_str} />' if self_close else f'{pad}<{tag}{attr_str}>'
    if tag in RAW:
        return f'{pad}<{tag}{attr_str}>{"".join(str(c) for c in children)}</{tag}>'
    if len(children) == 1 and not isinstance(children[0], (Tag, Safe)):
        return f'{pad}<{tag}{attr_str}>{escape(str(children[0]))}</{tag}>'

    inner = '\n'.join(render(c, new_ns, depth + 1, indent) for c in children)
    return f'{pad}<{tag}{attr_str}>\n{inner}\n{pad}</{tag}>'


def html_doc(head, body, lang='en'):
    """Wrap head + body in a full <!DOCTYPE html> document."""
    h = Tag('html', (head, body), {'lang': lang})
    return Safe(f'<!DOCTYPE html>\n{render(h)}')


# ── Tag factory ──────────────────────────────────────────────────────────────

def mk_tag(name):
    """Return a callable that creates Tag instances for the given element name.

    Trailing underscores and underscores are converted to hyphens so that
    Python-reserved names and custom elements work naturally::

        data_list = mk_tag('data_list')   # → <data-list>
        my_component = mk_tag('my_component_')  # → <my-component>
    """
    tag_name = name.rstrip('_').replace('_', '-')
    def _tag(*c, **kw):
        c, kw = _preproc(c, kw)
        return Tag(tag_name, c, kw)
    _tag.__name__ = tag_name
    return _tag


# ── HTML → Tag parser ────────────────────────────────────────────────────────

def html_to_tag(s):
    """Parse an HTML string into a Tag tree (or tuple of Tags)."""
    stack, root = [[]], []

    class P(HTMLParser):
        def handle_starttag(self, tag, a):
            d = {k: (v if v is not None else True) for k, v in a}
            if tag in VOID | SVG_VOID:
                stack[-1].append(Tag(tag, (), d))
            else:
                stack.append([])
                root.append((tag, d))

        def handle_endtag(self, tag):
            if tag in VOID | SVG_VOID:
                return
            children, (t, d) = tuple(stack.pop()), root.pop()
            stack[-1].append(Tag(t, children, d))

        def handle_data(self, data):
            if data.strip():
                stack[-1].append(data.strip())

    P().feed(s)
    res = stack[0]
    return res[0] if len(res) == 1 else tuple(res)


# ── CDN / utility tags ───────────────────────────────────────────────────────

def Datastar(v='1.0.0-RC.8'):
    """Script tag for the Datastar client library."""
    return Tag('script', (), {
        'type': 'module',
        'src':  f'https://cdn.jsdelivr.net/gh/starfederation/datastar@{v}/bundles/datastar.js',
    })


def MeCSS(v='v1.0.1'):
    """Script tag for the me_css.js helper."""
    return Tag('script', (), {
        'src': f'https://cdn.jsdelivr.net/gh/Deufel/toolbox@{v}/js/me_css.js',
    })


def Pointer(v='v1.0.1'):
    """Script tag for the pointer_events.js helper."""
    return Tag('script', (), {
        'src': f'https://cdn.jsdelivr.net/gh/Deufel/toolbox@{v}/js/pointer_events.js',
    })


def Favicon(emoji):
    """Link tag that sets an emoji as the page favicon via an inline SVG data URI."""
    s = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
         f'<text y=".9em" font-size="90">{emoji}</text></svg>')
    return Tag('link', (), {
        'rel':  'icon',
        'href': f'data:image/svg+xml,{quote(s, safe=":/@!,")}',
    })


def heatmap(rows):
    """Build a 52-week GitHub-style heatmap SVG from a list of {date, cases} dicts."""
    from datetime import date, timedelta

    today = date.today()
    start = today - timedelta(weeks=52)
    by_date = {r['date']: r['cases'] for r in rows}
    mx = max(by_date.values(), default=1) or 1

    CELL, STEP = 11, 13
    svg  = mk_tag('svg')
    rect = mk_tag('rect')

    cells = []
    for week in range(52):
        for dow in range(7):
            d = start + timedelta(weeks=week, days=dow)
            if d > today:
                continue
            i = by_date.get(d.isoformat(), 0) / mx
            cells.append(rect(
                x=str(week * STEP), y=str(dow * STEP),
                width=str(CELL),    height=str(CELL),
                rx='2',
                fill=f'oklch({int(96 - i*52)}% {0.04 + i*0.16:.3f} 142)',
            ))

    return svg(
        {"viewBox": f"0 0 {52*STEP} {7*STEP}"},
        width='100%', height=str(7 * STEP), style='display:block',
    )(*cells)
