"""
md_web.ui.bubble_map
====================
A choropleth-style US map with data bubbles overlaid at lat/lon positions.

The base map uses pre-projected Albers Equal-Area Conic SVG paths for all
49 continental US states.  Bubbles are positioned by projecting (lat, lon)
through the same Albers transform.

Bubble encoding
---------------
  Position  : Albers projection of (lat, lon) → SVG (x, y)
  Size      : sqrt(value / max_value) * max_radius  — area encodes value
  Color     : single hue at fixed chroma, varying lightness by value tier
              OR per-point color overrides
  Label     : city/region name via foreignObject (HTML, inherits CSS)

Data format
-----------
    data = [
        BubblePoint(lat=40.71, lon=-74.01, value=1_240_000, label='New York'),
        BubblePoint(lat=34.05, lon=-118.24, value=890_000,  label='Los Angeles'),
        ...
    ]

    # Or plain dicts:
    data = [
        {'lat': 40.71, 'lon': -74.01, 'value': 1_240_000, 'label': 'New York'},
    ]

Usage
-----
    from md_web.ui.bubble_map import bubble_map, BubblePoint

    bubble_map(
        data=[
            BubblePoint(40.71, -74.01, 1_240_000, 'New York'),
            BubblePoint(34.05, -118.24,  890_000, 'Los Angeles'),
        ],
        title='Revenue by Market',
        color_hue=220,
        value_fmt=lambda v: f'${v/1e6:.1f}M',
    )
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from md_web.html import mk_tag
from .base import (
    svg, g, rect, circle, path, line,
    div, span,
    Color, Font, fmt_value,
)
from .map_data import STATE_PATHS, W as MAP_W, H as MAP_H
from .map_data import _MIN_X, _MAX_X, _MIN_Y, _MAX_Y

foreign_object = mk_tag('foreignObject')
small          = mk_tag('small')

# ── Projection ────────────────────────────────────────────────────────────────

def project(lat: float, lon: float) -> tuple[float, float]:
    """Project (lat, lon) to SVG (x, y) using Albers Equal-Area Conic.

    Matches the projection used to generate STATE_PATHS.
    """
    phi1 = math.radians(29.5); phi2 = math.radians(45.5)
    phi0 = math.radians(37.5); lam0 = math.radians(-96.0)
    phi  = math.radians(lat);  lam  = math.radians(lon)
    n    = (math.sin(phi1) + math.sin(phi2)) / 2
    C    = math.cos(phi1)**2 + 2*n*math.sin(phi1)
    rho0 = math.sqrt(C - 2*n*math.sin(phi0)) / n
    rho  = math.sqrt(C - 2*n*math.sin(phi)) / n
    theta = n * (lam - lam0)
    raw_x =  rho * math.sin(theta)
    raw_y = -(rho0 - rho * math.cos(theta))
    sx = (raw_x - _MIN_X) / (_MAX_X - _MIN_X) * MAP_W * 0.92 + MAP_W * 0.04
    sy = (raw_y - _MIN_Y) / (_MAX_Y - _MIN_Y) * MAP_H * 0.92 + MAP_H * 0.04
    return round(sx, 1), round(sy, 1)


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class BubblePoint:
    lat:   float
    lon:   float
    value: float
    label: str          = ''
    color: str          = ''   # optional per-point color override


def _normalise(item) -> BubblePoint:
    """Accept dict or BubblePoint."""
    if isinstance(item, BubblePoint):
        return item
    return BubblePoint(
        lat   = item['lat'],
        lon   = item['lon'],
        value = item['value'],
        label = item.get('label', ''),
        color = item.get('color', ''),
    )


# ── Color helpers ─────────────────────────────────────────────────────────────

def _bubble_color(value: float, vmax: float, hue: float, chroma: float) -> str:
    """OKLCH fill for a bubble — brighter = larger value."""
    if vmax <= 0:
        return f'oklch(55% {chroma:.3f} {hue})'
    t = math.sqrt(max(0.0, min(1.0, value / vmax)))
    # Lightness range 40–75%: dim for small, bright for large
    l = 40 + t * 35
    return f'oklch({l:.1f}% {chroma:.3f} {hue})'


# ── Legend ────────────────────────────────────────────────────────────────────

def _legend(
    vmax:       float,
    max_r:      float,
    hue:        float,
    chroma:     float,
    value_fmt:  Callable,
    x:          float,
    y:          float,
) -> list:
    """Size legend: three reference circles (25%, 50%, 100% of max value)."""
    els = []
    steps = [0.25, 0.5, 1.0]
    cx = x + max_r
    baseline = y + max_r * 2 + 4

    for t in steps:
        r   = math.sqrt(t) * max_r
        cy  = baseline - r
        col = _bubble_color(t * vmax, vmax, hue, chroma)
        els.append(circle(
            cx=f'{cx:.1f}', cy=f'{cy:.1f}', r=f'{r:.1f}',
            fill=col, **{'fill-opacity': '0.7',
                         'stroke': col, 'stroke-width': '1'},
        ))
        # Value label to the right
        lx = cx + max_r + 6
        ly = cy
        els.append(
            foreign_object(
                x=f'{lx:.1f}', y=f'{ly - 7:.1f}',
                width='80', height='14',
            )(div(
                xmlns='http://www.w3.org/1999/xhtml',
                cls='bm-legend-label',
            )(value_fmt(t * vmax)))
        )
        cx += max_r * 2 + 90

    return els


# ── SVG builder ───────────────────────────────────────────────────────────────

def _map_svg(
    points:     list[BubblePoint],
    color_hue:  float,
    color_chroma: float,
    max_radius: float,
    value_fmt:  Callable,
    show_labels: bool,
    show_legend: bool,
    state_fill:  str,
    state_stroke: str,
) -> object:

    vmax = max((p.value for p in points), default=1.0)

    # ── State paths ───────────────────────────────────────────────────────
    state_paths = [
        path(
            d=d,
            fill=state_fill,
            stroke=state_stroke,
            **{'stroke-width': '0.5'},
        )
        for d in STATE_PATHS.values()
    ]

    # ── Bubbles — sorted small-to-large so large don't occlude small ──────
    sorted_pts = sorted(points, key=lambda p: p.value)
    bubbles    = []
    labels     = []

    for pt in sorted_pts:
        # Skip points outside continental US bounds
        if pt.lon < -130 or pt.lon > -65 or pt.lat < 24 or pt.lat > 50:
            continue

        bx, by = project(pt.lat, pt.lon)
        r  = math.sqrt(pt.value / vmax) * max_radius
        r  = max(r, 3.0)   # minimum visible radius
        col = pt.color or _bubble_color(pt.value, vmax, color_hue, color_chroma)

        bubbles.append(circle(
            cx=f'{bx:.1f}', cy=f'{by:.1f}', r=f'{r:.1f}',
            fill=col,
            **{
                'fill-opacity': '0.75',
                'stroke':       col,
                'stroke-width': '1.5',
                'stroke-opacity': '0.5',
            },
        ))

        # Label below bubble if requested
        if show_labels and pt.label:
            lw = max(60, len(pt.label) * 7)
            labels.append(
                foreign_object(
                    x=f'{bx - lw/2:.1f}',
                    y=f'{by + r + 2:.1f}',
                    width=str(lw),
                    height='14',
                )(div(
                    xmlns='http://www.w3.org/1999/xhtml',
                    cls='bm-bubble-label',
                )(pt.label))
            )

    # ── Legend ────────────────────────────────────────────────────────────
    legend_h  = (max_radius * 2 + 28) if show_legend else 0
    total_h   = MAP_H + legend_h

    legend_els = []
    if show_legend:
        legend_els = _legend(
            vmax, max_radius, color_hue, color_chroma, value_fmt,
            x=MAP_W * 0.05, y=MAP_H + 4,
        )

    return svg(
        cls='bubble-map-svg',
        viewBox=f'0 0 {MAP_W} {total_h}',
        width=str(MAP_W),
        height=str(total_h),
        **{'aria-label': 'US bubble map'},
    )(
        g(cls='bm-states')(*state_paths),
        g(cls='bm-bubbles')(*bubbles),
        g(cls='bm-labels')(*labels),
        g(cls='bm-legend')(*legend_els),
    )


# ── Public component ──────────────────────────────────────────────────────────

def bubble_map(
    data:           list,
    *,
    title:          str   = '',
    color_hue:      float = 220,
    color_chroma:   float = 0.18,
    max_radius:     float = 40.0,
    value_fmt:      Callable[[float], str] = lambda v: fmt_value(v),
    show_labels:    bool  = True,
    show_legend:    bool  = True,
    state_fill:     str   = '',   # '' = use CSS var
    state_stroke:   str   = '',   # '' = use CSS var
    cls:            str   = '',
    id:             Optional[str] = None,
) -> object:
    """US bubble map with Albers projection.

    Parameters
    ----------
    data         : list of BubblePoint or dicts with lat/lon/value/label keys
    title        : optional chart title
    color_hue    : OKLCH hue for bubbles (220=blue, 142=green, 25=orange)
    color_chroma : OKLCH chroma for bubbles
    max_radius   : SVG units for the largest bubble (default 40)
    value_fmt    : formats values for legend labels
    show_labels  : show city/region name below each bubble
    show_legend  : show size reference legend below the map
    state_fill   : SVG fill for state polygons (default: CSS var)
    state_stroke : SVG stroke for state borders (default: CSS var)
    cls, id      : HTML attributes on the wrapper

    Example
    -------
        from md_web.ui.bubble_map import bubble_map, BubblePoint

        bubble_map(
            data=[
                BubblePoint(40.71, -74.01, 1_240_000, 'New York'),
                BubblePoint(34.05, -118.24,  890_000, 'Los Angeles'),
                BubblePoint(41.88,  -87.63,  670_000, 'Chicago'),
            ],
            title='Revenue by Market',
            color_hue=220,
            value_fmt=lambda v: f'${v/1e3:.0f}K',
        )
    """
    points = [_normalise(p) for p in data]

    # Default state colors from CSS design system
    s_fill   = state_fill   or Color.border
    s_stroke = state_stroke or 'var(--Border)'

    map_svg = _map_svg(
        points=points,
        color_hue=color_hue,
        color_chroma=color_chroma,
        max_radius=max_radius,
        value_fmt=value_fmt,
        show_labels=show_labels,
        show_legend=show_legend,
        state_fill=s_fill,
        state_stroke=s_stroke,
    )

    title_el = small(cls='bm-title')(title) if title else None

    attrs = {'cls': f'bubble-map{" " + cls if cls else ""}'}
    if id:
        attrs['id'] = id

    return div(**attrs)(title_el, map_svg)
