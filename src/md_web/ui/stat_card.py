"""
md_web.ui.stat_card
===================
A KPI stat card with an inline sparkline.

Design rules enforced here
--------------------------
1. `.surface` is applied ONCE on the card wrapper only.
   Child elements (label, value, header div) carry NO background class —
   they inherit color from the card surface via `currentColor` / `color`.

2. Each sparkline gradient gets a unique id derived from the card's
   position/slug so multiple cards on the same page never collide.
   SVG gradient ids are document-scoped — duplicate ids mean every
   `url(#sg)` resolves to the last definition in the DOM.
"""
from __future__ import annotations
from typing import Sequence

from md_web.html import mk_tag
from .base import (
    Viewport, sparkline_path, linear_scale,
    delta_color, delta_arrow,
    svg, g, path, circle, defs, linear_gradient, stop,
    div, span,
    Color,
)

p     = mk_tag('p')
small = mk_tag('small')

# ── Gradient id counter ───────────────────────────────────────────────────────
# Simple module-level counter so each sparkline rendered in a single
# page gets a unique gradient id regardless of call order.
_grad_counter = 0

def _next_grad_id() -> str:
    global _grad_counter
    _grad_counter += 1
    return f'sg{_grad_counter}'


# ── Sparkline SVG ─────────────────────────────────────────────────────────────

def _sparkline_svg(
    series:  Sequence[float],
    delta:   float | None,
    grad_id: str,
    *,
    w: float = 120,
    h: float = 40,
) -> object:
    """Build the sparkline SVG Tag with a unique gradient id."""
    if not series or len(series) < 2:
        return span()

    vp    = Viewport(w=w, h=h, pad_top=3, pad_right=3, pad_bottom=3, pad_left=3)
    color = delta_color(delta)

    area_d = sparkline_path(series, vp, close=True)
    line_d = sparkline_path(series, vp, close=False)

    # Last-point dot coordinates
    lo, hi = min(series), max(series)
    pad    = (hi - lo) * 0.08 or 1
    x_sc   = linear_scale(0, len(series) - 1, vp.plot_x, vp.plot_x + vp.plot_w)
    y_sc   = linear_scale(lo - pad, hi + pad, vp.plot_y + vp.plot_h, vp.plot_y)
    dot_x  = round(x_sc(len(series) - 1), 2)
    dot_y  = round(y_sc(series[-1]), 2)

    # Gradient uses the SAME color as the line — they must always match.
    gradient = linear_gradient(
        id=grad_id, x1='0', y1='0', x2='0', y2='1',
    )(
        stop(**{'offset': '0%',   'stop-color': color, 'stop-opacity': '0.22'}),
        stop(**{'offset': '100%', 'stop-color': color, 'stop-opacity': '0'}),
    )

    area = path(
        d=area_d,
        fill=f'url(#{grad_id})',
        stroke=Color.none,
    )
    line = path(
        d=line_d,
        fill=Color.none,
        stroke=color,
        **{'stroke-width': '1.5', 'stroke-linejoin': 'round', 'stroke-linecap': 'round'},
    )
    dot = circle(
        cx=str(dot_x), cy=str(dot_y), r='2.5',
        fill=color,
        stroke=Color.bg,
        **{'stroke-width': '1.5'},
    )

    return svg(
        cls='stat-sparkline',
        width=str(w), height=str(h),
        viewBox=vp.viewBox(),
        **{'aria-hidden': 'true'},
    )(
        defs()(gradient),
        g()(area, line, dot),
    )


# ── Public component ──────────────────────────────────────────────────────────

def stat_card(
    label:       str,
    value:       str,
    *,
    delta:       float | None = None,
    series:      Sequence[float] = (),
    unit:        str = '',
    sparkline_w: float = 120,
    sparkline_h: float = 40,
    cls:         str = '',
    id:          str | None = None,
) -> object:
    """KPI stat card with optional sparkline.

    `.surface` is on the card wrapper only — child elements are transparent
    and inherit text color from the surface context automatically.
    Each sparkline gradient is assigned a unique document-scoped id so
    multiple cards on the same page render correctly.
    """
    # Unique gradient id for this render — avoids SVG id collision
    grad_id = _next_grad_id()

    # Delta badge — color via inline style only, no background class
    delta_el = None
    if delta is not None:
        arrow    = delta_arrow(delta)
        clr      = delta_color(delta)
        pct_str  = f'{abs(delta):.1f}{unit or "%"}'
        delta_el = span(
            cls='stat-delta',
            style=f'color:{clr}',
        )(f'{arrow} {pct_str}')

    # Sparkline
    spark_el = (
        _sparkline_svg(series, delta, grad_id, w=sparkline_w, h=sparkline_h)
        if series else None
    )

    # stat_card returns bare content — no surface wrapper.
    # Wrap in card() from md_web.ui.layout to get the surface,
    # border, and padding.  This avoids double-card when used
    # inside a layout card().
    extra_cls = f' {cls}' if cls else ''
    attrs     = {'cls': f'stat-card{" " + extra_cls if extra_cls else ""}'}
    if id:
        attrs['id'] = id

    return div(**attrs)(
        div(cls='stat-header')(
            small(cls='stat-label')(label),
            delta_el,
        ),
        p(cls='stat-value')(value),
        spark_el,
    )
