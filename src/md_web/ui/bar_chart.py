"""
md_web.ui.bar_chart
===================
Horizontal and vertical bar chart using the unified SVG + HTML tag system.

- SVG owns geometry: bar rects, axis lines, tick marks
- HTML (via foreignObject) owns all text: axis labels, bar labels, title
- Color flows from the design system via CSS custom properties

Series format
-------------
A series is a list of (label, value) tuples::

    data = [
        ('Food & Bev',  847_000),
        ('Clothing',    312_000),
        ('Electronics', 589_000),
    ]

Multiple series (grouped bars) are supported::

    series = [
        ('2023', [('Q1', 210), ('Q2', 240), ('Q3', 195), ('Q4', 310)]),
        ('2024', [('Q1', 225), ('Q2', 268), ('Q3', 220), ('Q4', 340)]),
    ]

Usage
-----
    from md_web.ui.bar_chart import bar_chart

    bar_chart(
        data=[('Jan', 420), ('Feb', 380), ('Mar', 510)],
        title='Monthly Revenue',
        orientation='vertical',
        color_hue=220,
    )
"""
from __future__ import annotations

import math
from typing import Callable, Optional, Sequence

from md_web.html import mk_tag
from .base import (
    Viewport,
    svg, g, rect, line, path,
    div, span,
    Color, Font,
    linear_scale, nice_ticks, fmt_value,
)

foreign_object = mk_tag('foreignObject')
small          = mk_tag('small')

# ── Types ─────────────────────────────────────────────────────────────────────

Series = list[tuple[str, float]]          # [(label, value), ...]
MultiSeries = list[tuple[str, Series]]    # [(series_name, series), ...]


# ── Color palette ─────────────────────────────────────────────────────────────
# Default multi-series palette — OKLCH, perceptually distinct hues,
# same lightness and chroma so they feel like a coherent family.

DEFAULT_PALETTE = [
    'oklch(62% 0.18 220)',   # blue
    'oklch(62% 0.18 142)',   # green
    'oklch(62% 0.18 25)',    # orange
    'oklch(62% 0.18 280)',   # purple
    'oklch(62% 0.18 60)',    # yellow
    'oklch(62% 0.18 340)',   # pink
]


# ── Layout helpers ────────────────────────────────────────────────────────────

# foreignObject label: full-width HTML div at SVG coordinates
def _fo_label(
    x: float, y: float, w: float, h: float,
    text: str, cls: str,
) -> object:
    return foreign_object(
        x=f'{x:.1f}', y=f'{y:.1f}',
        width=f'{w:.1f}', height=f'{h:.1f}',
    )(
        div(xmlns='http://www.w3.org/1999/xhtml', cls=cls)(text)
    )


# ── Vertical bar chart ────────────────────────────────────────────────────────

def _vertical_bars(
    flat:       Series,
    vp:         Viewport,
    y_scale:    Callable,
    y_ticks:    list[float],
    bar_color:  str,
    value_fmt:  Callable,
    show_values: bool,
) -> tuple[list, list, list]:
    """Return (bars, x_labels, value_labels) Tag lists for vertical orientation."""
    n        = len(flat)
    bar_w    = vp.plot_w / n * 0.62
    gap_w    = vp.plot_w / n * 0.38
    x0       = vp.plot_x + gap_w / 2 + (vp.plot_w / n - bar_w) / 2

    bars, x_labels, val_labels = [], [], []

    baseline = y_scale(0)

    for i, (label, value) in enumerate(flat):
        bar_x  = vp.plot_x + i * (vp.plot_w / n) + (vp.plot_w / n - bar_w) / 2
        bar_y  = y_scale(max(value, 0))
        bar_h  = abs(baseline - bar_y)

        bars.append(rect(
            x=f'{bar_x:.2f}', y=f'{bar_y:.2f}',
            width=f'{bar_w:.2f}', height=f'{max(bar_h, 1):.2f}',
            rx='2', fill=bar_color,
        ))

        # X-axis label via foreignObject
        label_y = vp.plot_y + vp.plot_h + 4
        label_h = vp.pad_bottom - 6
        x_labels.append(_fo_label(
            bar_x - 4, label_y,
            bar_w + 8, label_h,
            label, 'bc-xlabel',
        ))

        # Value label above bar
        if show_values:
            val_y = bar_y - 14
            val_labels.append(_fo_label(
                bar_x - 4, val_y,
                bar_w + 8, 14,
                value_fmt(value), 'bc-vallabel',
            ))

    return bars, x_labels, val_labels


# ── Horizontal bar chart ──────────────────────────────────────────────────────

def _horizontal_bars(
    flat:       Series,
    vp:         Viewport,
    x_scale:    Callable,
    bar_color:  str,
    value_fmt:  Callable,
    show_values: bool,
) -> tuple[list, list, list]:
    """Return (bars, y_labels, value_labels) for horizontal orientation."""
    n      = len(flat)
    bar_h  = vp.plot_h / n * 0.60
    step   = vp.plot_h / n

    bars, y_labels, val_labels = [], [], []

    for i, (label, value) in enumerate(flat):
        bar_y  = vp.plot_y + i * step + (step - bar_h) / 2
        bar_x  = vp.plot_x
        bar_w  = x_scale(max(value, 0)) - vp.plot_x

        bars.append(rect(
            x=f'{bar_x:.2f}', y=f'{bar_y:.2f}',
            width=f'{max(bar_w, 1):.2f}', height=f'{bar_h:.2f}',
            rx='2', fill=bar_color,
        ))

        # Y-axis label (left of bar)
        y_labels.append(_fo_label(
            0, bar_y,
            vp.plot_x - 4, bar_h,
            label, 'bc-ylabel',
        ))

        # Value label at end of bar
        if show_values:
            val_x = vp.plot_x + bar_w + 4
            val_labels.append(_fo_label(
                val_x, bar_y,
                vp.pad_right - 6, bar_h,
                value_fmt(value), 'bc-vallabel--h',
            ))

    return bars, y_labels, val_labels


# ── Axis builders ─────────────────────────────────────────────────────────────

def _y_axis(vp: Viewport, y_scale, ticks, value_fmt) -> list:
    """Horizontal gridlines + y-axis tick labels for vertical charts."""
    els = []
    for tick in ticks:
        y = y_scale(tick)
        # Gridline
        els.append(line(
            x1=f'{vp.plot_x:.1f}',  y1=f'{y:.1f}',
            x2=f'{vp.plot_x + vp.plot_w:.1f}', y2=f'{y:.1f}',
            stroke=Color.border, **{'stroke-width': '0.5'},
        ))
        # Tick label via foreignObject
        els.append(_fo_label(
            0, y - 8,
            vp.plot_x - 4, 16,
            value_fmt(tick), 'bc-ytick',
        ))
    return els


def _x_axis_line(vp: Viewport) -> object:
    """The baseline axis line."""
    return line(
        x1=f'{vp.plot_x:.1f}',
        y1=f'{vp.plot_y + vp.plot_h:.1f}',
        x2=f'{vp.plot_x + vp.plot_w:.1f}',
        y2=f'{vp.plot_y + vp.plot_h:.1f}',
        stroke=Color.border, **{'stroke-width': '1'},
    )


def _x_axis_gridlines(vp: Viewport, x_scale, ticks, value_fmt) -> list:
    """Vertical gridlines + x-axis tick labels for horizontal charts."""
    els = []
    for tick in ticks:
        x = x_scale(tick)
        els.append(line(
            x1=f'{x:.1f}', y1=f'{vp.plot_y:.1f}',
            x2=f'{x:.1f}', y2=f'{vp.plot_y + vp.plot_h:.1f}',
            stroke=Color.border, **{'stroke-width': '0.5'},
        ))
        els.append(_fo_label(
            x - 16, vp.plot_y + vp.plot_h + 2,
            32, vp.pad_bottom - 4,
            value_fmt(tick), 'bc-xtick',
        ))
    return els


# ── Public component ──────────────────────────────────────────────────────────

def bar_chart(
    data:          Series | MultiSeries,
    *,
    title:         str  = '',
    orientation:   str  = 'vertical',    # 'vertical' | 'horizontal'
    color_hue:     float = 220,
    color_chroma:  float = 0.18,
    palette:       Optional[list[str]] = None,
    value_fmt:     Callable[[float], str] = lambda v: fmt_value(v),
    show_values:   bool  = False,
    show_grid:     bool  = True,
    w:             float = 480,
    h:             float = 220,
    cls:           str   = '',
    id:            Optional[str] = None,
) -> object:
    """Bar chart — vertical or horizontal.

    Parameters
    ----------
    data        : [(label, value), ...] for single series
                  [(series_name, [(label, value), ...]), ...] for multi
    title       : optional chart title (rendered as HTML above SVG)
    orientation : 'vertical' (default) or 'horizontal'
    color_hue   : OKLCH hue for single-series bars
    color_chroma: OKLCH chroma for single-series bars
    palette     : list of color strings for multi-series (overrides hue)
    value_fmt   : formats axis tick and value labels
    show_values : show value labels on/above each bar
    show_grid   : show axis gridlines
    w, h        : SVG viewport dimensions
    cls, id     : HTML attributes on the wrapper
    """
    # ── Normalise data to MultiSeries ─────────────────────────────────────
    if data and isinstance(data[0], tuple) and isinstance(data[0][1], (int, float)):
        # Single series: [(label, value), ...]
        multi    = [('', list(data))]
        n_series = 1
    else:
        # Multi series: [(name, [(label, value), ...]), ...]
        multi    = [(name, list(s)) for name, s in data]
        n_series = len(multi)

    colors = (palette or DEFAULT_PALETTE)[:n_series]
    if n_series == 1:
        colors = [f'oklch(62% {color_chroma:.3f} {color_hue})']

    # All values flattened for scale computation
    all_values = [v for _, series in multi for _, v in series]
    vmin  = min(0.0, min(all_values)) if all_values else 0.0
    vmax  = max(all_values)            if all_values else 1.0

    # ── Viewport ──────────────────────────────────────────────────────────
    is_h = orientation == 'horizontal'

    if is_h:
        vp = Viewport(
            w=w, h=h,
            pad_top=4, pad_right=60,
            pad_bottom=24, pad_left=80,
        )
    else:
        vp = Viewport(
            w=w, h=h,
            pad_top=20, pad_right=8,
            pad_bottom=32, pad_left=52,
        )

    # ── Scales ────────────────────────────────────────────────────────────
    if is_h:
        x_scale = linear_scale(vmin, vmax, vp.plot_x, vp.plot_x + vp.plot_w)
        x_ticks = nice_ticks(vmin, vmax, 5)
        axis_els = _x_axis_gridlines(vp, x_scale, x_ticks, value_fmt) if show_grid else []
    else:
        y_scale  = linear_scale(vmin, vmax, vp.plot_y + vp.plot_h, vp.plot_y)
        y_ticks  = nice_ticks(vmin, vmax, 5)
        axis_els = _y_axis(vp, y_scale, y_ticks, value_fmt) if show_grid else []

    # ── Bars ──────────────────────────────────────────────────────────────
    all_bars, all_xlabels, all_vallabels = [], [], []

    if n_series == 1:
        flat = multi[0][1]
        color = colors[0]
        if is_h:
            bars, ylabels, vallabels = _horizontal_bars(
                flat, vp, x_scale, color, value_fmt, show_values)
            all_bars     += bars
            all_xlabels  += ylabels
            all_vallabels += vallabels
        else:
            bars, xlabels, vallabels = _vertical_bars(
                flat, vp, y_scale, y_ticks, color, value_fmt, show_values)
            all_bars      += bars
            all_xlabels   += xlabels
            all_vallabels += vallabels
    else:
        # Grouped bars — each group of series bars shares the same x slot
        n_cats    = len(multi[0][1])
        slot_w    = vp.plot_w / n_cats
        bar_w     = slot_w * 0.7 / n_series

        for si, (sname, series) in enumerate(multi):
            color = colors[si]
            for ci, (label, value) in enumerate(series):
                slot_x  = vp.plot_x + ci * slot_w
                bar_x   = slot_x + slot_w * 0.15 + si * bar_w
                bar_top = y_scale(max(value, 0))
                bar_h   = abs(y_scale(0) - bar_top)
                all_bars.append(rect(
                    x=f'{bar_x:.2f}', y=f'{bar_top:.2f}',
                    width=f'{bar_w:.2f}', height=f'{max(bar_h, 1):.2f}',
                    rx='2', fill=color,
                ))
                # Category label only on first series
                if si == 0:
                    all_xlabels.append(_fo_label(
                        slot_x, vp.plot_y + vp.plot_h + 4,
                        slot_w, vp.pad_bottom - 6,
                        label, 'bc-xlabel',
                    ))

    # ── SVG assembly ──────────────────────────────────────────────────────
    chart_svg = svg(
        cls=f'bar-chart-svg{" " + cls if cls else ""}',
        viewBox=vp.viewBox(),
        width=str(w), height=str(h),
        **{'aria-label': title or 'bar chart'},
    )(
        g(cls='bc-axis')(*axis_els),
        _x_axis_line(vp) if not is_h else None,
        g(cls='bc-bars')(*all_bars),
        g(cls='bc-xlabels')(*all_xlabels),
        g(cls='bc-vallabels')(*all_vallabels),
    )

    # ── Title ─────────────────────────────────────────────────────────────
    title_el = small(cls='bc-title')(title) if title else None

    attrs = {'cls': 'bar-chart'}
    if id:
        attrs['id'] = id

    return div(**attrs)(title_el, chart_svg)
