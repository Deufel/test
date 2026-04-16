"""
md_web.ui.base
==============
Shared foundations for all UI components:

  - Linear / log scale mappers  (value → SVG coordinate)
  - SVG viewport helpers        (padding box, axis ticks)
  - CSS design-token constants  (maps to the ui.css @property names)
  - Tag aliases for every SVG element we use

Nothing in this file produces a visible component on its own.
Everything here is imported by the individual component modules.

Design rule
-----------
All geometry is computed in plain numbers.  Strings are assembled only
at the Tag constructor boundary so the Tag system handles escaping.
"""
from __future__ import annotations
from md_web.html import mk_tag
from dataclasses import dataclass, field
from typing import Sequence


# ── SVG tag factories ─────────────────────────────────────────────────────────
# Defined once here; imported by every component module.

svg      = mk_tag('svg')
g        = mk_tag('g')
rect     = mk_tag('rect')
circle   = mk_tag('circle')
line     = mk_tag('line')
path     = mk_tag('path')
polyline = mk_tag('polyline')
polygon  = mk_tag('polygon')
text     = mk_tag('text')
tspan    = mk_tag('tspan')
defs     = mk_tag('defs')
clip_path = mk_tag('clipPath')
linear_gradient = mk_tag('linearGradient')
stop     = mk_tag('stop')
title    = mk_tag('title')   # SVG tooltip

# HTML chrome tags used in component wrappers
div   = mk_tag('div')
span  = mk_tag('span')


# ── CSS design-token helpers ──────────────────────────────────────────────────
# These map to the @property names in ui.css.
# Use them as attribute values so component code reads semantically.

class Color:
    """CSS custom property references for the design system color tokens.

    Usage::
        rect(fill=Color.surface, stroke=Color.border)
        text(fill=Color.fg)
    """
    # Surface backgrounds — inherit from nearest .surface ancestor
    bg        = 'var(--_bg)'
    border    = 'var(--border)'
    Border    = 'var(--Border)'       # stronger border variant

    # Semantic fill shorthands that work on SVG elements
    fg        = 'currentColor'        # inherits text color = auto-contrast
    none      = 'none'

    # Named accent classes — set cls= on the parent element
    # so the cascade sets --_bg correctly for children
    PRI   = 'pri'     # primary accent hue
    SEC   = 'sec'     # secondary surface
    SURF  = 'surface' # neutral surface (nests automatically)


class Font:
    """CSS font-family variable references."""
    heading = 'var(--font-heading)'
    body    = 'var(--font-body)'
    mono    = 'var(--font-mono)'


# ── Viewport / padding box ────────────────────────────────────────────────────

@dataclass
class Viewport:
    """Defines the SVG coordinate space and inner plot area.

    Attributes
    ----------
    w, h        : total SVG width/height in SVG user units
    pad_top     : space above the plot area (room for title, labels)
    pad_right   : space right of the plot area
    pad_bottom  : space below the plot area (room for x-axis labels)
    pad_left    : space left of the plot area (room for y-axis labels)

    Derived
    -------
    plot_x, plot_y  : top-left corner of the inner plot area
    plot_w, plot_h  : dimensions of the inner plot area
    """
    w:          float = 320
    h:          float = 80
    pad_top:    float = 4
    pad_right:  float = 4
    pad_bottom: float = 4
    pad_left:   float = 4

    @property
    def plot_x(self) -> float: return self.pad_left

    @property
    def plot_y(self) -> float: return self.pad_top

    @property
    def plot_w(self) -> float: return self.w - self.pad_left - self.pad_right

    @property
    def plot_h(self) -> float: return self.h - self.pad_top  - self.pad_bottom

    def viewBox(self) -> str:
        return f'0 0 {self.w} {self.h}'


# ── Scale functions ───────────────────────────────────────────────────────────

def linear_scale(
    domain_min: float,
    domain_max: float,
    range_min:  float,
    range_max:  float,
):
    """Return a function that maps domain → range linearly.

    Usage::
        x_scale = linear_scale(0, 100, vp.plot_x, vp.plot_x + vp.plot_w)
        svg_x = x_scale(value)
    """
    span = domain_max - domain_min
    if span == 0:
        return lambda v: (range_min + range_max) / 2
    def scale(v: float) -> float:
        return range_min + (v - domain_min) / span * (range_max - range_min)
    return scale


def nice_ticks(
    lo: float,
    hi: float,
    target_count: int = 5,
) -> list[float]:
    """Generate 'nice' round tick values between lo and hi.

    Produces human-friendly axis labels (multiples of 1, 2, 5, 10 …)
    with approximately target_count ticks.
    """
    if lo == hi:
        return [lo]
    import math
    span      = hi - lo
    rough     = span / max(target_count - 1, 1)
    magnitude = 10 ** math.floor(math.log10(rough))
    for step in (1, 2, 2.5, 5, 10):
        nice_step = step * magnitude
        if span / nice_step <= target_count + 1:
            break
    start = math.floor(lo / nice_step) * nice_step
    ticks = []
    v = start
    while v <= hi + nice_step * 0.001:
        ticks.append(round(v, 10))
        v += nice_step
    return ticks


def fmt_value(v: float, unit: str = '') -> str:
    """Format a numeric value for display on an axis or label.

    Automatically abbreviates large numbers (K, M, B).
    """
    abs_v = abs(v)
    if abs_v >= 1_000_000_000:
        s = f'{v / 1_000_000_000:.3g}B'
    elif abs_v >= 1_000_000:
        s = f'{v / 1_000_000:.3g}M'
    elif abs_v >= 1_000:
        s = f'{v / 1_000:.3g}K'
    elif abs_v >= 10:
        s = f'{v:.3g}'
    elif abs_v >= 0.1:
        s = f'{v:.2g}'
    else:
        s = f'{v:.1g}'
    return f'{s}{unit}' if unit else s


# ── Sparkline path builder ────────────────────────────────────────────────────

def sparkline_path(
    series:    Sequence[float],
    vp:        Viewport,
    *,
    close:     bool = False,   # close to bottom for area fill
) -> str:
    """Build an SVG path `d` string for a sparkline from a data series.

    Parameters
    ----------
    series  : sequence of numeric values
    vp      : Viewport defining the plot area
    close   : if True, closes path to bottom-left/right for area fill

    Returns the `d` attribute string — pass as path(d=...).
    """
    if not series or len(series) < 2:
        return ''

    lo  = min(series)
    hi  = max(series)
    # Give a little padding so the line doesn't clip the stroke at extremes
    pad = (hi - lo) * 0.08 or 1

    x_scale = linear_scale(0, len(series) - 1, vp.plot_x, vp.plot_x + vp.plot_w)
    y_scale = linear_scale(lo - pad, hi + pad, vp.plot_y + vp.plot_h, vp.plot_y)

    pts = [(x_scale(i), y_scale(v)) for i, v in enumerate(series)]

    d = f'M {pts[0][0]:.2f},{pts[0][1]:.2f}'
    for x, y in pts[1:]:
        d += f' L {x:.2f},{y:.2f}'

    if close:
        bx, by = vp.plot_x + vp.plot_w, vp.plot_y + vp.plot_h
        ax, ay = vp.plot_x, vp.plot_y + vp.plot_h
        d += f' L {bx:.2f},{by:.2f} L {ax:.2f},{ay:.2f} Z'

    return d


# ── Delta helpers ─────────────────────────────────────────────────────────────

def delta_color(delta: float | None) -> str:
    """Return an inline CSS color string for a positive/negative delta."""
    if delta is None:     return Color.fg
    if delta > 0:         return 'oklch(62% 0.18 145)'   # green
    if delta < 0:         return 'oklch(62% 0.20 25)'    # red
    return                       'oklch(62% 0.04 220)'   # neutral


def delta_arrow(delta: float | None) -> str:
    """Return ▲ / ▼ / — for a delta value."""
    if delta is None: return ''
    if delta > 0:     return '▲'
    if delta < 0:     return '▼'
    return '—'
