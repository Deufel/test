"""
md_web.ui.scatter_plot
======================
Scatter plot with optional size encoding (bubble scatter) and
reference lines.

- SVG owns geometry: point circles, axis lines, reference lines
- HTML (via foreignObject) owns all text: axis labels, point labels,
  axis titles, legend
- Points can encode a third dimension via radius (bubble mode)
- Color per-point or per-series from design system palette

Data format
-----------
Single series — list of ScatterPoint or dicts::

    data = [
        ScatterPoint(x=18.4, y=847, label='Food & Bev', size=847),
        ScatterPoint(x=3.2,  y=312, label='Clothing'),
        {'x': 5.1, 'y': 589, 'label': 'Electronics', 'size': 589},
    ]

Multi-series — list of (name, [points]) tuples::

    data = [
        ('2022', [ScatterPoint(18.4, 847), ScatterPoint(3.2, 312)]),
        ('2023', [ScatterPoint(19.1, 891), ScatterPoint(3.4, 328)]),
    ]

Usage
-----
    from md_web.ui.scatter_plot import scatter_plot, ScatterPoint

    scatter_plot(
        data=[
            ScatterPoint(18.4, 847, 'Food & Bev',   size=847),
            ScatterPoint( 3.2, 312, 'Clothing',      size=312),
            ScatterPoint( 5.1, 589, 'Electronics',   size=589),
        ],
        x_label='E-Commerce Share %',
        y_label='Sales $B',
        x_fmt=lambda v: f'{v:.1f}%',
        y_fmt=lambda v: f'${v:.0f}B',
    )
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional

from md_web.html import mk_tag
from .base import (
    Viewport,
    svg, g, rect, circle, line, path,
    div, span,
    Color, Font,
    linear_scale, nice_ticks, fmt_value,
)

foreign_object = mk_tag('foreignObject')
small          = mk_tag('small')

# ── Palette ───────────────────────────────────────────────────────────────────

PALETTE = [
    'oklch(65% 0.18 220)',
    'oklch(65% 0.18 142)',
    'oklch(65% 0.18 25)',
    'oklch(65% 0.18 280)',
    'oklch(65% 0.18 60)',
    'oklch(65% 0.18 340)',
]

# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class ScatterPoint:
    x:      float
    y:      float
    label:  str   = ''
    size:   float = 0.0    # 0 = use default radius; > 0 = size-encoded
    color:  str   = ''     # optional per-point color override


def _normalise(item) -> ScatterPoint:
    if isinstance(item, ScatterPoint):
        return item
    return ScatterPoint(
        x     = item['x'],
        y     = item['y'],
        label = item.get('label', ''),
        size  = item.get('size', 0.0),
        color = item.get('color', ''),
    )


# ── foreignObject label helper ────────────────────────────────────────────────

def _fo(x, y, w, h, text, cls):
    return foreign_object(
        x=f'{x:.1f}', y=f'{y:.1f}',
        width=f'{w:.1f}', height=f'{h:.1f}',
    )(div(xmlns='http://www.w3.org/1999/xhtml', cls=cls)(text))


# ── Axis builders ─────────────────────────────────────────────────────────────

def _y_gridlines(vp, y_scale, ticks, y_fmt):
    els = []
    for tick in ticks:
        y = y_scale(tick)
        els.append(line(
            x1=f'{vp.plot_x:.1f}', y1=f'{y:.1f}',
            x2=f'{vp.plot_x + vp.plot_w:.1f}', y2=f'{y:.1f}',
            stroke=Color.border, **{'stroke-width': '0.5'},
        ))
        els.append(_fo(0, y - 8, vp.plot_x - 4, 16, y_fmt(tick), 'sp-ytick'))
    return els


def _x_gridlines(vp, x_scale, ticks, x_fmt):
    els = []
    for tick in ticks:
        x = x_scale(tick)
        els.append(line(
            x1=f'{x:.1f}', y1=f'{vp.plot_y:.1f}',
            x2=f'{x:.1f}', y2=f'{vp.plot_y + vp.plot_h:.1f}',
            stroke=Color.border, **{'stroke-width': '0.5'},
        ))
        els.append(_fo(x - 20, vp.plot_y + vp.plot_h + 3, 40, 14,
                       x_fmt(tick), 'sp-xtick'))
    return els


def _axis_lines(vp):
    return [
        line(  # x-axis baseline
            x1=f'{vp.plot_x:.1f}', y1=f'{vp.plot_y + vp.plot_h:.1f}',
            x2=f'{vp.plot_x + vp.plot_w:.1f}', y2=f'{vp.plot_y + vp.plot_h:.1f}',
            stroke=Color.border, **{'stroke-width': '1'},
        ),
        line(  # y-axis
            x1=f'{vp.plot_x:.1f}', y1=f'{vp.plot_y:.1f}',
            x2=f'{vp.plot_x:.1f}', y2=f'{vp.plot_y + vp.plot_h:.1f}',
            stroke=Color.border, **{'stroke-width': '1'},
        ),
    ]


def _axis_titles(vp, x_label, y_label):
    """Axis title labels."""
    els = []
    if x_label:
        cx = vp.plot_x + vp.plot_w / 2
        els.append(_fo(cx - 60, vp.h - 14, 120, 14, x_label, 'sp-axis-title'))
    if y_label:
        # Rotated y-axis title — use a transform on the foreignObject
        # SVG transform on foreignObject is not universally supported,
        # so we place it vertically as a short label on the left
        cy = vp.plot_y + vp.plot_h / 2
        els.append(_fo(0, cy - 30, 14, 60, y_label, 'sp-axis-title-y'))
    return els


# ── Reference lines ───────────────────────────────────────────────────────────

def _ref_line(vp, x_scale, y_scale, axis, value, color, label=''):
    """Draw a reference line at x=value or y=value."""
    els = []
    if axis == 'y':
        y = y_scale(value)
        els.append(line(
            x1=f'{vp.plot_x:.1f}', y1=f'{y:.1f}',
            x2=f'{vp.plot_x + vp.plot_w:.1f}', y2=f'{y:.1f}',
            stroke=color, **{'stroke-width': '1', 'stroke-dasharray': '4 3'},
        ))
        if label:
            els.append(_fo(vp.plot_x + vp.plot_w + 2, y - 7, 50, 14,
                           label, 'sp-refline-label'))
    else:
        x = x_scale(value)
        els.append(line(
            x1=f'{x:.1f}', y1=f'{vp.plot_y:.1f}',
            x2=f'{x:.1f}', y2=f'{vp.plot_y + vp.plot_h:.1f}',
            stroke=color, **{'stroke-width': '1', 'stroke-dasharray': '4 3'},
        ))
        if label:
            els.append(_fo(x + 2, vp.plot_y + 2, 50, 14,
                           label, 'sp-refline-label'))
    return els


# ── Legend ────────────────────────────────────────────────────────────────────

def _legend(series_names, colors, vp):
    els = []
    x   = vp.plot_x
    y   = vp.plot_y + vp.plot_h + vp.pad_bottom - 14
    for name, color in zip(series_names, colors):
        els.append(circle(
            cx=f'{x + 5:.1f}', cy=f'{y + 6:.1f}', r='4',
            fill=color, **{'fill-opacity': '0.8'},
        ))
        lw = max(60, len(name) * 7 + 4)
        els.append(_fo(x + 14, y, lw, 14, name, 'sp-legend'))
        x += lw + 20
    return els


# ── SVG builder ───────────────────────────────────────────────────────────────

def _scatter_svg(
    multi:       list[tuple[str, list[ScatterPoint]]],
    colors:      list[str],
    vp:          Viewport,
    x_fmt:       Callable,
    y_fmt:       Callable,
    x_label:     str,
    y_label:     str,
    show_labels: bool,
    show_grid:   bool,
    show_legend: bool,
    ref_lines:   list,
    max_radius:  float,
) -> object:

    # Gather all coords for scales
    all_x = [p.x for _, pts in multi for p in pts]
    all_y = [p.y for _, pts in multi for p in pts]
    all_s = [p.size for _, pts in multi for p in pts if p.size > 0]

    if not all_x:
        return svg(cls='scatter-plot-svg', viewBox=vp.viewBox(),
                   width=str(vp.w), height=str(vp.h))()

    x_ticks = nice_ticks(min(all_x), max(all_x), 6)
    y_ticks = nice_ticks(min(all_y), max(all_y), 5)

    x_scale = linear_scale(x_ticks[0], x_ticks[-1], vp.plot_x, vp.plot_x + vp.plot_w)
    y_scale = linear_scale(y_ticks[0], y_ticks[-1], vp.plot_y + vp.plot_h, vp.plot_y)

    s_max = max(all_s) if all_s else 1.0

    # ── Grid & axes ───────────────────────────────────────────────────────
    grid_y = _y_gridlines(vp, y_scale, y_ticks, y_fmt) if show_grid else []
    grid_x = _x_gridlines(vp, x_scale, x_ticks, x_fmt) if show_grid else []
    axes   = _axis_lines(vp)
    titles = _axis_titles(vp, x_label, y_label)

    # ── Reference lines ───────────────────────────────────────────────────
    ref_els = []
    for rl in ref_lines:
        ref_els.extend(_ref_line(vp, x_scale, y_scale, **rl))

    # ── Points ────────────────────────────────────────────────────────────
    # Draw all series, smallest points first to avoid occlusion
    all_pts_colored = []
    for si, (sname, pts) in enumerate(multi):
        color = colors[si]
        for p in pts:
            r = (math.sqrt(p.size / s_max) * max_radius) if p.size > 0 else 5.0
            r = max(r, 3.0)
            col = p.color or color
            all_pts_colored.append((r, p, col))

    all_pts_colored.sort(key=lambda t: t[0], reverse=True)  # large first (behind)

    points_els = []
    label_els  = []

    for r, p, col in all_pts_colored:
        px = x_scale(p.x)
        py = y_scale(p.y)

        points_els.append(circle(
            cx=f'{px:.2f}', cy=f'{py:.2f}', r=f'{r:.1f}',
            fill=col,
            **{'fill-opacity': '0.75', 'stroke': col,
               'stroke-width': '1', 'stroke-opacity': '0.4'},
        ))

        if show_labels and p.label:
            lw = max(50, len(p.label) * 6 + 4)
            label_els.append(_fo(
                px - lw/2, py + r + 2, lw, 13,
                p.label, 'sp-point-label',
            ))

    # ── Legend ────────────────────────────────────────────────────────────
    legend_els = []
    if show_legend and len(multi) > 1:
        legend_els = _legend([n for n,_ in multi], colors, vp)

    return svg(
        cls='scatter-plot-svg',
        viewBox=vp.viewBox(),
        width=str(vp.w), height=str(vp.h),
        **{'aria-label': 'scatter plot'},
    )(
        g(cls='sp-grid-y')(*grid_y),
        g(cls='sp-grid-x')(*grid_x),
        g(cls='sp-axes')(*axes),
        g(cls='sp-reflines')(*ref_els),
        g(cls='sp-points')(*points_els),
        g(cls='sp-labels')(*label_els),
        g(cls='sp-titles')(*titles),
        g(cls='sp-legend')(*legend_els),
    )


# ── Public component ──────────────────────────────────────────────────────────

def scatter_plot(
    data,
    *,
    title:       str   = '',
    x_label:     str   = '',
    y_label:     str   = '',
    x_fmt:       Callable[[float], str] = lambda v: fmt_value(v),
    y_fmt:       Callable[[float], str] = lambda v: fmt_value(v),
    show_labels: bool  = True,
    show_grid:   bool  = True,
    show_legend: bool  = True,
    ref_lines:   list  = None,
    color_hue:   float = 220,
    color_chroma: float = 0.18,
    palette:     list[str] | None = None,
    max_radius:  float = 16.0,
    w:           float = 480,
    h:           float = 280,
    cls:         str   = '',
    id:          str | None = None,
) -> object:
    """Scatter plot with optional size encoding and reference lines.

    Parameters
    ----------
    data        : [ScatterPoint|dict, ...] single series
                  [(name, [ScatterPoint|dict, ...]), ...] multi-series
    title       : chart title
    x_label     : x-axis title
    y_label     : y-axis title
    x_fmt       : formats x-axis tick labels
    y_fmt       : formats y-axis tick labels
    show_labels : show point labels below each dot
    show_grid   : show axis gridlines
    show_legend : show series legend (multi-series only)
    ref_lines   : list of dicts — each: {axis:'x'|'y', value:float,
                  color:str, label:str}
    color_hue   : OKLCH hue for single-series points
    color_chroma: OKLCH chroma
    palette     : explicit color list for multi-series
    max_radius  : max bubble radius when size encoding is used
    w, h        : SVG viewport dimensions
    cls, id     : HTML wrapper attributes

    Example
    -------
        scatter_plot(
            data=[
                ScatterPoint(18.4, 847, 'Food & Bev',  size=847),
                ScatterPoint( 3.2, 312, 'Clothing',    size=312),
                ScatterPoint( 5.1, 589, 'Electronics', size=589),
            ],
            x_label='E-Commerce Share %',
            y_label='Sales $B',
            x_fmt=lambda v: f'{v:.1f}%',
            y_fmt=lambda v: f'${v:.0f}B',
        )
    """
    ref_lines = ref_lines or []

    # Normalise to multi-series
    if data and not isinstance(data[0], tuple):
        multi    = [('', [_normalise(p) for p in data])]
        n_series = 1
    elif data and isinstance(data[0], tuple) and isinstance(data[0][1], list):
        multi    = [(name, [_normalise(p) for p in pts]) for name, pts in data]
        n_series = len(multi)
    else:
        multi = []; n_series = 1

    colors = (palette or PALETTE)[:n_series]
    if n_series == 1:
        colors = [f'oklch(65% {color_chroma:.3f} {color_hue})']

    legend_h = 20 if (show_legend and n_series > 1) else 0

    vp = Viewport(
        w=w, h=h,
        pad_top=12, pad_right=16,
        pad_bottom=28 + legend_h,
        pad_left=52,
    )

    chart_svg = _scatter_svg(
        multi, colors, vp, x_fmt, y_fmt,
        x_label, y_label,
        show_labels, show_grid, show_legend,
        ref_lines, max_radius,
    )
    title_el = small(cls='sp-title')(title) if title else None

    attrs = {'cls': f'scatter-plot{" " + cls if cls else ""}'}
    if id:
        attrs['id'] = id

    return div(**attrs)(title_el, chart_svg)
