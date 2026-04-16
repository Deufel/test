"""
md_web.ui.layout
================
Dashboard layout primitives: card, grid, section.

These three components form the structural layer that holds everything
else together.  Every chart, stat card, and heatmap should live inside
a card; cards live inside grids; grids live inside sections.

Design rules
------------
- Layout components own structure, spacing, and surface color only.
  They never hard-code chart-specific styles.
- .surface appears exactly once per card — on the card wrapper.
  Everything inside inherits transparently.
- Grid uses CSS grid with auto-fit so it responds to container width
  without any JavaScript.
- Section is intentionally minimal — a semantic label + a content area.

Usage
-----
    from md_web.ui.layout import card, grid, section

    section('Revenue Overview')(
        grid(cols=4)(
            card('Monthly Revenue', subtitle='vs last month')(
                stat_card(...)
            ),
            card('Daily Trend', span=2)(
                bar_chart(...)
            ),
        )
    )
"""
from __future__ import annotations
from typing import Optional
from md_web.html import mk_tag, unpack

# ── Tag factories ─────────────────────────────────────────────────────────────

div   = mk_tag('div')
span  = mk_tag('span')
h2    = mk_tag('h2')
h3    = mk_tag('h3')
p     = mk_tag('p')
small = mk_tag('small')
header = mk_tag('header')
section_tag = mk_tag('section')


# ── card ──────────────────────────────────────────────────────────────────────

def card(
    title:    str = '',
    subtitle: str = '',
    *,
    span:     int  = 1,       # grid column span (1 = normal, 2 = wide, etc.)
    pad:      str  = '',      # override padding: 'sm' | 'lg' | 'none'
    cls:      str  = '',
    id:       Optional[str] = None,
):
    """Surface-wrapped card container.

    The card provides the surface background, border, border-radius,
    and padding.  Pass children by calling the returned object::

        card('Title', subtitle='Last 30 days')(
            bar_chart(...),
        )

    Parameters
    ----------
    title    : optional heading inside the card
    subtitle : optional subdued line beneath the title
    span     : CSS grid column span (applied as inline style)
    pad      : padding variant — '' (default) | 'sm' | 'lg' | 'none'
    cls      : extra CSS classes on the wrapper
    id       : HTML id on the wrapper
    """
    span_style = f'grid-column: span {span};' if span > 1 else ''
    pad_cls    = f' card--pad-{pad}' if pad else ''
    extra_cls  = f' {cls}' if cls else ''

    attrs = {
        'cls':   f'ui-card surface{pad_cls}{extra_cls}',
        'style': span_style if span_style else None,
    }
    if id:
        attrs['id'] = id

    # Build the optional header block
    header_els = []
    if title:
        header_els.append(h3(cls='card-title')(title))
    if subtitle:
        header_els.append(small(cls='card-subtitle')(subtitle))

    card_header = div(cls='card-header')(*header_els) if header_els else None

    def _build(*children):
        kids = unpack(children)
        return div(**{k: v for k, v in attrs.items() if v is not None})(
            card_header,
            div(cls='card-body')(*kids),
        )

    return _build


# ── grid ──────────────────────────────────────────────────────────────────────

def grid(
    cols:  int  = 3,
    gap:   str  = '',      # '' = default from CSS, 'sm' | 'lg'
    fixed: bool = False,   # True = equal cols (repeat N 1fr), False = auto-fit
    cls:   str  = '',
    id:    Optional[str] = None,
):
    """Responsive CSS grid container.

    Parameters
    ----------
    cols  : number of columns
    gap   : gap variant — '' (default) | 'sm' | 'lg'
    fixed : False (default) = auto-fit responsive columns that collapse on
            narrow screens.  True = always exactly `cols` equal columns —
            use this for side-by-side charts where you want strict 50/50
            or 33/33/33 splits regardless of content width.
    cls   : extra CSS classes
    id    : HTML id
    """
    gap_cls   = f' grid--gap-{gap}' if gap else ''
    extra_cls = f' {cls}' if cls else ''

    if fixed:
        # Strict equal columns — no auto-fit collapsing
        style = f'grid-template-columns: repeat({cols}, 1fr);'
        attrs = {
            'cls':   f'ui-grid{gap_cls}{extra_cls}',
            'style': style,
        }
    else:
        # Responsive auto-fit — collapses gracefully on narrow screens
        min_w = max(160, 1200 // cols - 32)
        attrs = {
            'cls':   f'ui-grid{gap_cls}{extra_cls}',
            'style': f'--grid-min-w: {min_w}px;',
        }

    if id:
        attrs['id'] = id

    def _build(*children):
        return div(**attrs)(*unpack(children))

    return _build


# ── section ───────────────────────────────────────────────────────────────────

def section(
    title:    str  = '',
    subtitle: str  = '',
    *,
    cls:  str  = '',
    id:   Optional[str] = None,
):
    """Labelled dashboard section.

    Groups related cards under a heading.  Intentionally minimal —
    just a semantic wrapper with a title and optional subtitle::

        section('Revenue Detail', subtitle='All figures in USD')(
            grid(cols=2)(
                card('By Category')(...),
                card('By Channel')(...),
            )
        )

    Parameters
    ----------
    title    : section heading
    subtitle : optional subdued description line
    cls      : extra CSS classes
    id       : HTML id
    """
    attrs = {'cls': f'ui-section{" " + cls if cls else ""}'}
    if id:
        attrs['id'] = id

    hdr_els = []
    if title:
        hdr_els.append(h2(cls='section-title')(title))
    if subtitle:
        hdr_els.append(p(cls='section-subtitle')(subtitle))

    section_header = div(cls='section-header')(*hdr_els) if hdr_els else None

    def _build(*children):
        return section_tag(**attrs)(
            section_header,
            *unpack(children),
        )

    return _build
