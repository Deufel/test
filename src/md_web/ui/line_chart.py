"""
md_web.ui.line_chart
====================
Multi-series time-series line chart with smooth curves and optional area fill.

- SVG owns geometry: paths, area fills, axis lines, tick marks, dots
- HTML (via foreignObject) owns all text: axis labels, series legend, title
- Smooth curves via cubic Bézier control points — no jagged L-segments
- Area fill uses a linearGradient per series fading to transparent

Series formats
--------------
Single series — list of (label, value) tuples::

    data = [('Jan', 420), ('Feb', 380), ('Mar', 510), ('Apr', 490)]

Multi-series — list of (name, [(label, value), ...]) tuples::

    data = [
        ('Food & Bev', [('2020', 780), ('2021', 820), ('2022', 847)]),
        ('E-Commerce', [('2020',  90), ('2021', 145), ('2022', 203)]),
        ('Clothing',   [('2020', 290), ('2021', 265), ('2022', 312)]),
    ]

Usage
-----
    from md_web.ui.line_chart import line_chart

    line_chart(
        data=[('Jan', 420), ('Feb', 380), ('Mar', 510)],
        title='Monthly Revenue',
        y_fmt=lambda v: f'${v:.0f}B',
        area=True,
    )
"""
from __future__ import annotations

import math
from typing import Callable, Optional

from md_web.html import mk_tag
from .base import (
    Viewport,
    svg, g, rect, circle, line, path, defs, linear_gradient, stop,
    div, span,
    Color, Font,
    linear_scale, nice_ticks, fmt_value,
)

foreign_object = mk_tag('foreignObject')
small          = mk_tag('small')

# ── Palette ───────────────────────────────────────────────────────────────────

PALETTE = [
    'oklch(65% 0.18 220)',   # blue
    'oklch(65% 0.18 142)',   # green
    'oklch(65% 0.18 25)',    # orange
    'oklch(65% 0.18 280)',   # purple
    'oklch(65% 0.18 60)',    # yellow
    'oklch(65% 0.18 340)',   # pink
]

# ── Path builders ────────────────────────────────────────────────────────────

def _linear_path(pts: list[tuple[float, float]], close_y: float | None = None) -> str:
    """Straight line segments between data points (L commands).
    
    Each point is connected exactly — no interpolation, no smoothing.
    Use when data density is high or when step changes matter.
    """
    if len(pts) < 2:
        return ''
    d = f'M {pts[0][0]:.2f},{pts[0][1]:.2f}'
    for x, y in pts[1:]:
        d += f' L {x:.2f},{y:.2f}'
    if close_y is not None:
        d += f' L {pts[-1][0]:.2f},{close_y:.2f} L {pts[0][0]:.2f},{close_y:.2f} Z'
    return d


def _smooth_path(pts: list[tuple[float, float]], close_y: float | None = None) -> str:
    """Smooth cubic Bézier path using correct Catmull-Rom → Bézier conversion.

    For segment from p1 to p2:
      cp1 = p1 + (p2 - p0) / 6   ← tangent at p1 scaled by h/3
      cp2 = p2 - (p3 - p1) / 6   ← tangent at p2 scaled by h/3

    This is the exact uniform-parameterization Catmull-Rom formula.
    The curve passes through every data point and tangents are
    C1-continuous across all segment boundaries — no kinks.

    The /6 factor (not /3) comes from the Bézier-to-Catmull-Rom
    equivalence: the bezier control point distance is 1/3 of the
    chord, and the Catmull-Rom tangent is the full chord / 2, so
    the product is chord / 6.
    """
    if len(pts) < 2:
        return ''

    n = len(pts)
    d = f'M {pts[0][0]:.2f},{pts[0][1]:.2f}'

    for i in range(1, n):
        p0 = pts[max(0, i-2)]
        p1 = pts[i-1]
        p2 = pts[i]
        p3 = pts[min(n-1, i+1)]

        # Control point leaving p1
        cp1x = p1[0] + (p2[0] - p0[0]) / 6
        cp1y = p1[1] + (p2[1] - p0[1]) / 6

        # Control point arriving at p2
        cp2x = p2[0] - (p3[0] - p1[0]) / 6
        cp2y = p2[1] - (p3[1] - p1[1]) / 6

        d += (f' C {cp1x:.2f},{cp1y:.2f}'
              f' {cp2x:.2f},{cp2y:.2f}'
              f' {p2[0]:.2f},{p2[1]:.2f}')

    if close_y is not None:
        d += f' L {pts[-1][0]:.2f},{close_y:.2f} L {pts[0][0]:.2f},{close_y:.2f} Z'

    return d


# ── foreignObject label helper ────────────────────────────────────────────────

def _fo(x: float, y: float, w: float, h: float, text: str, cls: str) -> object:
    return foreign_object(
        x=f'{x:.1f}', y=f'{y:.1f}',
        width=f'{w:.1f}', height=f'{h:.1f}',
    )(div(xmlns='http://www.w3.org/1999/xhtml', cls=cls)(text))


# ── Gradient builder ──────────────────────────────────────────────────────────

_grad_counter = 0

def _area_gradient(grad_id: str, color: str) -> object:
    return linear_gradient(
        id=grad_id, x1='0', y1='0', x2='0', y2='1',
    )(
        stop(**{'offset': '0%',   'stop-color': color, 'stop-opacity': '0.12'}),
        stop(**{'offset': '100%', 'stop-color': color, 'stop-opacity': '0'}),
    )


# ── Axis helpers ──────────────────────────────────────────────────────────────

def _y_gridlines(vp: Viewport, y_scale, ticks, y_fmt) -> list:
    els = []
    for tick in ticks:
        y = y_scale(tick)
        els.append(line(
            x1=f'{vp.plot_x:.1f}',              y1=f'{y:.1f}',
            x2=f'{vp.plot_x + vp.plot_w:.1f}',  y2=f'{y:.1f}',
            stroke=Color.border, **{'stroke-width': '0.5'},
        ))
        els.append(_fo(
            0, y - 8, vp.plot_x - 4, 16,
            y_fmt(tick), 'lc-ytick',
        ))
    return els


def _x_labels(vp: Viewport, x_scale, labels, n_total) -> list:
    """Render x-axis category labels, skipping some if too dense."""
    els   = []
    every = max(1, n_total // 10)   # show at most ~10 labels
    slot  = vp.plot_w / max(n_total - 1, 1)
    for i, label in enumerate(labels):
        if i % every != 0 and i != n_total - 1:
            continue
        x = x_scale(i)
        els.append(_fo(
            x - slot/2, vp.plot_y + vp.plot_h + 4,
            slot, vp.pad_bottom - 6,
            label, 'lc-xlabel',
        ))
    return els


def _x_axis_line(vp: Viewport) -> object:
    return line(
        x1=f'{vp.plot_x:.1f}',
        y1=f'{vp.plot_y + vp.plot_h:.1f}',
        x2=f'{vp.plot_x + vp.plot_w:.1f}',
        y2=f'{vp.plot_y + vp.plot_h:.1f}',
        stroke=Color.border, **{'stroke-width': '1'},
    )


# ── Legend ────────────────────────────────────────────────────────────────────

def _legend(series_names: list[str], colors: list[str], vp: Viewport) -> list:
    """Horizontal legend below the plot area."""
    els = []
    x   = vp.plot_x
    y   = vp.plot_y + vp.plot_h + vp.pad_bottom - 14
    for name, color in zip(series_names, colors):
        # Color swatch line
        els.append(line(
            x1=f'{x:.1f}',    y1=f'{y + 6:.1f}',
            x2=f'{x+16:.1f}', y2=f'{y + 6:.1f}',
            stroke=color, **{'stroke-width': '2.5', 'stroke-linecap': 'round'},
        ))
        label_w = max(60, len(name) * 7 + 4)
        els.append(_fo(x + 20, y, label_w, 14, name, 'lc-legend'))
        x += label_w + 28
    return els


# ── SVG builder ───────────────────────────────────────────────────────────────

def _line_chart_svg(
    multi:         list[tuple[str, list[tuple[str, float]]]],
    colors:        list[str],
    vp:            Viewport,
    y_fmt:         Callable,
    area:          bool,
    show_dots:     bool,
    show_grid:     bool,
    show_legend:   bool,
    interpolation: str,     # 'smooth' | 'linear'
) -> object:
    global _grad_counter

    # Gather all values for y scale
    all_vals = [v for _, series in multi for _, v in series]
    all_labels = [lbl for lbl, v in multi[0][1]] if multi else []
    n      = len(all_labels)
    v_data_min = min(all_vals) if all_vals else 0.0
    v_data_max = max(all_vals) if all_vals else 1.0

    # Y-axis: start at 0 only when data naturally includes or approaches 0.
    # If all values are far above zero (>20% of range), start from data min
    # with a small padding so the chart uses its full vertical space.
    v_span = v_data_max - v_data_min
    if v_data_min > v_span * 0.25:
        # Data doesn't approach zero — use padded data range
        pad  = v_span * 0.08
        vmin = v_data_min - pad
        vmax = v_data_max + pad
    else:
        vmin = min(0.0, v_data_min)
        vmax = v_data_max

    y_ticks  = nice_ticks(vmin, vmax, 5)
    y_scale  = linear_scale(vmin, vmax, vp.plot_y + vp.plot_h, vp.plot_y)
    x_scale  = linear_scale(0, max(n - 1, 1), vp.plot_x, vp.plot_x + vp.plot_w)

    # ── Axes ──────────────────────────────────────────────────────────────
    grid_els   = _y_gridlines(vp, y_scale, y_ticks, y_fmt) if show_grid else []
    xlabel_els = _x_labels(vp, x_scale, all_labels, n)
    axis_line  = _x_axis_line(vp)

    # ── Series ────────────────────────────────────────────────────────────
    grad_defs = []
    areas     = []
    lines_    = []
    dots      = []

    baseline_y = y_scale(max(vmin, 0))

    for si, (sname, series) in enumerate(multi):
        color = colors[si]
        pts   = [(x_scale(i), y_scale(v)) for i, (_, v) in enumerate(series)]

        # Select path builder once per series
        _path_fn = _smooth_path if interpolation == 'smooth' else _linear_path

        # Area fill
        if area:
            _grad_counter += 1
            gid = f'lcg{_grad_counter}'
            grad_defs.append(_area_gradient(gid, color))
            area_d = _path_fn(pts, close_y=baseline_y)
            areas.append(path(
                d=area_d,
                fill=f'url(#{gid})',
                stroke=Color.none,
            ))

        # Line
        line_d = _path_fn(pts)
        lines_.append(path(
            d=line_d,
            fill=Color.none,
            stroke=color,
            **{'stroke-width': '2', 'stroke-linejoin': 'round',
               'stroke-linecap': 'round'},
        ))

        # End dot
        if show_dots and pts:
            lx, ly = pts[-1]
            dots.append(circle(
                cx=f'{lx:.2f}', cy=f'{ly:.2f}', r='3',
                fill=color, stroke=Color.bg, **{'stroke-width': '1.5'},
            ))

    # ── Legend ────────────────────────────────────────────────────────────
    legend_els = []
    if show_legend and len(multi) > 1:
        names = [name for name, _ in multi]
        legend_els = _legend(names, colors, vp)

    return svg(
        cls='line-chart-svg',
        viewBox=vp.viewBox(),
        width=str(vp.w), height=str(vp.h),
        **{'aria-label': 'line chart'},
    )(
        defs()(*grad_defs),
        g(cls='lc-grid')(*grid_els),
        axis_line,
        g(cls='lc-areas')(*areas),
        g(cls='lc-lines')(*lines_),
        g(cls='lc-dots')(*dots),
        g(cls='lc-xlabels')(*xlabel_els),
        g(cls='lc-legend')(*legend_els),
    )


# ── Public component ──────────────────────────────────────────────────────────

def line_chart(
    data,
    *,
    title:         str   = '',
    y_fmt:         Callable[[float], str] = lambda v: fmt_value(v),
    area:          bool  = False,
    show_dots:     bool  = True,
    show_grid:     bool  = True,
    show_legend:   bool  = True,
    interpolation: str   = 'smooth',   # 'smooth' | 'linear'
    color_hue:     float = 220,
    color_chroma:  float = 0.18,
    palette:       list[str] | None = None,
    w:             float = 480,
    h:             float = 220,
    cls:           str   = '',
    id:            str | None = None,
) -> object:
    """Multi-series line chart with smooth cubic Bézier curves.

    Parameters
    ----------
    data        : [(label, value), ...] single series
                  [(name, [(label, value), ...]), ...] multi-series
    title       : optional chart title shown above SVG
    y_fmt         : formats y-axis tick labels and tooltips
    area          : fill area under each line with gradient
    show_dots     : show endpoint dot on each series
    show_grid     : show horizontal gridlines
    show_legend   : show series legend (only for multi-series)
    interpolation : 'smooth' — Catmull-Rom cubic Bézier curves (default)
                    'linear' — straight line segments between points
    color_hue     : OKLCH hue for single-series line
    color_chroma: OKLCH chroma
    palette     : explicit color list for multi-series
    w, h        : SVG viewport dimensions
    cls, id     : HTML wrapper attributes

    Example
    -------
        line_chart(
            data=[
                ('Food & Bev', [('2018',780),('2019',800),('2020',812),('2021',847)]),
                ('E-Commerce', [('2018', 60),('2019', 80),('2020',120),('2021',203)]),
            ],
            title='Retail Sales by Category',
            y_fmt=lambda v: f'${v:.0f}B',
            area=True,
        )
    """
    # Normalise to multi-series
    if data and isinstance(data[0], tuple) and isinstance(data[0][1], (int, float)):
        multi    = [('', list(data))]
        n_series = 1
    else:
        multi    = [(name, list(s)) for name, s in data]
        n_series = len(multi)

    colors = (palette or PALETTE)[:n_series]
    if n_series == 1:
        colors = [f'oklch(65% {color_chroma:.3f} {color_hue})']

    # Legend needs extra bottom padding for multi-series
    legend_h = 22 if (show_legend and n_series > 1) else 0

    vp = Viewport(
        w=w, h=h,
        pad_top=12, pad_right=16,
        pad_bottom=32 + legend_h,
        pad_left=52,
    )

    chart_svg  = _line_chart_svg(multi, colors, vp, y_fmt, area,
                                  show_dots, show_grid, show_legend,
                                  interpolation)
    title_el   = small(cls='lc-title')(title) if title else None

    attrs = {'cls': f'line-chart{" " + cls if cls else ""}'}
    if id:
        attrs['id'] = id

    return div(**attrs)(title_el, chart_svg)
