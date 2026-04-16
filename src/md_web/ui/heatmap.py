"""
md_web.ui.heatmap
=================
A GitHub-style 52-week × 7-day activity heatmap.

Each cell represents one calendar day.  Value intensity is encoded
as one of 5 discrete color steps — not a continuous ramp — which is
easier to read at a glance and prints cleanly.

Color system
------------
Rather than hardcoding lightness values that break in light/dark mode,
cell colors are expressed as OKLCH steps at fixed chroma and hue,
with lightness chosen to sit clearly above the card surface in both
themes.  The empty/no-data distinction uses the surface border token
so it adapts automatically.

  None    → var(--border)              no data — pipeline gap / weekend
  step 0  → lowest lightness           data exists, near-zero value
  step 1  → …
  step 2  → …
  step 3  → …
  step 4  → highest lightness          maximum value

No hover-reveal / tooltip
--------------------------
Value is communicated entirely through color.  Hiding data behind
hover is bad visualization practice: it fails on print, fails on
touch, and creates an invisible information layer.  The legend below
the grid provides the color→value mapping.

Usage
-----
    from md_web.ui.heatmap import activity_heatmap

    activity_heatmap(
        data=daily_revenue,        # {ISO-date-str: float | None}
        label='Daily Revenue',
        weeks=52,
        color_hue=142,             # 142=green  220=blue  25=orange  280=purple
        value_fmt=lambda v: f'${v:,.0f}',
    )
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Callable, Dict, Optional

from md_web.html import mk_tag
from .base import (
    svg, g, rect,
    div, span,
    Color, Font, fmt_value,
)

# foreignObject bridges SVG coordinate space → HTML namespace
# HTML children inherit the full CSS cascade (fonts, color, design tokens)
foreign_object = mk_tag('foreignObject')

small = mk_tag('small')

# ── Layout constants ──────────────────────────────────────────────────────────

CELL    = 11          # cell size in SVG user units
GAP     = 2           # gap between cells
STEP    = CELL + GAP  # 13 units per cell + gap
DOW_W   = 26          # left margin for Mon/Wed/Fri labels
MONTH_H = 18          # top margin for month abbreviations

MONTHS  = ['Jan','Feb','Mar','Apr','May','Jun',
           'Jul','Aug','Sep','Oct','Nov','Dec']

# ── Discrete color steps ──────────────────────────────────────────────────────
# Five steps from dim → bright, expressed as (lightness%, chroma_multiplier).
# Lightness values are chosen to be clearly readable on a dark surface
# AND a light surface — tested against both themes.
#
# The design system's surface background in dark mode is ~oklch(20-22% …),
# in light mode ~oklch(82-84% …).  We need steps that contrast with both.
#
# Solution: use a fixed set of lightness values that work in dark mode,
# and invert them in light mode via the CSS `prefers-color-scheme` cascade.
# The Python side emits two parallel sets; CSS applies the right one.
# Simpler: emit CSS custom properties for the 5 steps and reference them —
# but that requires the CSS to know the hue.
#
# Practical solution for a Python-rendered SVG: emit the hue as a data
# attribute on the SVG element and resolve the colors in CSS via @property.
# That's complex.  Instead, accept that users pick dark/light at call time
# OR we emit both and let CSS media query switch.  Too complex for now.
#
# Real solution we use: 5 OKLCH steps with chroma=color_chroma and the
# caller's hue.  Lightness is chosen so steps are perceptually distinct
# in BOTH themes by staying in the middle lightness band (35–72%).
# The no-data cell uses var(--border) which is CSS-variable aware.

# Five discrete lightness steps at fixed chroma.
# Chroma is constant so all steps read as the same hue at different
# intensities — the eye correctly decodes "more" vs "less" of one thing.
# Lightness range 38–82% gives clear separation on both dark (~20% bg)
# and light (~84% bg) surfaces.  Step 0 starts at 38% — far enough above
# a dark surface to be clearly visible without looking washed out.
STEPS = [38, 50, 62, 72, 82]   # lightness% only — chroma is caller-supplied
N_STEPS = len(STEPS)


def _quantize(value: float, vmax: float) -> int:
    """Map value to a step index 0–4.

    Uses a square-root scale so mid-range values spread across the middle
    steps rather than piling up at the top.  Any positive value maps to
    at least step 0 (never invisible).
    """
    if vmax <= 0 or value <= 0:
        return -1    # sentinel: zero activity (not missing)
    t = math.sqrt(max(0.0, min(1.0, value / vmax)))
    return min(N_STEPS - 1, int(t * N_STEPS))


def _step_color(step: int, hue: float, chroma: float) -> str:
    """Return the OKLCH fill string for a given step index.
    
    Chroma is fixed across all steps — same hue, different brightness only.
    """
    return f'oklch({STEPS[step]}% {chroma:.3f} {hue})'


# ── Grid geometry ─────────────────────────────────────────────────────────────

def _grid_dates(weeks: int, end: date) -> tuple[date, list[date]]:
    """Return (grid_start, all_dates) for the heatmap.

    The grid always ends on the Sunday of the week containing `end`
    so today is always visible in the rightmost columns.
    """
    days_to_sunday = (6 - end.weekday()) % 7
    last_sunday    = end + timedelta(days=days_to_sunday)
    first_monday   = last_sunday - timedelta(days=weeks * 7 - 1)
    return first_monday, [first_monday + timedelta(days=i) for i in range(weeks * 7)]


def _month_label_positions(dates: list[date]) -> list[tuple[float, str]]:
    """Return [(x, abbrev)] for month labels, spaced at least 3 weeks apart."""
    labels    = []
    last_week = -99
    for i, d in enumerate(dates):
        week_idx = i // 7
        if d.day <= 7 and week_idx - last_week >= 3:
            labels.append((DOW_W + week_idx * STEP, MONTHS[d.month - 1]))
            last_week = week_idx
    return labels


# ── Legend builder ────────────────────────────────────────────────────────────

def _legend_svg(
    vmax:       float,
    hue:        float,
    chroma:     float,
    value_fmt:  Callable[[float], str],
    left:       float,
    total_w:    float,
    y:          float,
) -> list:
    """Build legend elements: 'Less ■■■■■ More' with value annotations."""
    items = []

    # "Less" label
    items.append(
        foreign_object(x=str(left), y=str(y), width='26', height=str(CELL + 2))(
            div(xmlns='http://www.w3.org/1999/xhtml', cls='hm-legend-label')('Less')
        )
    )

    # 6 swatches: no-data + 5 steps
    swatch_x = left + 28
    swatches = [
        (Color.border, 'no data'),
    ] + [
        (_step_color(s, hue, chroma), value_fmt(vmax * (s + 1) / N_STEPS))
        for s in range(N_STEPS)
    ]

    for s_idx, (fill, _) in enumerate(swatches):
        sx = swatch_x + s_idx * (CELL + 3)
        items.append(rect(
            x=str(sx), y=str(y),
            width=str(CELL), height=str(CELL),
            rx='2', fill=fill,
        ))

    # "More" label
    more_x = swatch_x + len(swatches) * (CELL + 3) + 2
    items.append(
        foreign_object(x=str(more_x), y=str(y), width='26', height=str(CELL + 2))(
            div(xmlns='http://www.w3.org/1999/xhtml', cls='hm-legend-label')('More')
        )
    )

    return items


# ── SVG builder ───────────────────────────────────────────────────────────────

def _heatmap_svg(
    data:         Dict[str, Optional[float]],
    weeks:        int,
    end:          date,
    color_hue:    float,
    color_chroma: float,
    value_fmt:    Callable[[float], str],
    show_dow:     bool,
    show_months:  bool,
    show_legend:  bool,
) -> object:

    _, dates = _grid_dates(weeks, end)

    # Scale max from the visible window only
    values = [v for v in (data.get(d.isoformat()) for d in dates)
              if v is not None and v > 0]
    vmax   = max(values) if values else 1.0

    # Dimensions
    grid_w  = weeks * STEP - GAP
    grid_h  = 7 * STEP - GAP
    left    = DOW_W if show_dow     else 4
    top     = MONTH_H if show_months else 4
    legend_h = (MONTH_H + 4) if show_legend else 0
    total_w  = left + grid_w + 4
    total_h  = top  + grid_h + legend_h + 4

    # ── Cells ──────────────────────────────────────────────────────────────
    cells = []
    for i, d in enumerate(dates):
        week_idx = i // 7
        dow      = i  % 7
        cx       = left + week_idx * STEP
        cy       = top  + dow      * STEP
        val      = data.get(d.isoformat())

        if val is None:
            fill = Color.border        # no data — CSS surface border token
        else:
            step = _quantize(val, vmax)
            if step < 0:
                # Zero activity: use step 0 at reduced chroma so it reads
                # as "empty but tracked" — same hue family, clearly dim
                fill = f'oklch({STEPS[0]}% {color_chroma * 0.35:.3f} {color_hue})'
            else:
                fill = _step_color(step, color_hue, color_chroma)

        cells.append(rect(
            x=str(cx), y=str(cy),
            width=str(CELL), height=str(CELL),
            rx='2', fill=fill,
        ))

    # ── Day-of-week labels — HTML via foreignObject ───────────────────────
    # foreignObject places an HTML span at SVG coordinates.
    # The span inherits font-family, color, and font-size from the CSS
    # cascade — no hardcoded font attributes needed.
    dow_labels = []
    if show_dow:
        fo_w = left - 2     # width of the foreignObject box
        fo_h = CELL + 2     # height matches cell height
        for dow_idx, name in [(0, 'Mon'), (2, 'Wed'), (4, 'Fri')]:
            fy = top + dow_idx * STEP
            dow_labels.append(
                foreign_object(
                    x='0', y=f'{fy:.1f}',
                    width=str(fo_w), height=str(fo_h),
                )(
                    div(
                        xmlns='http://www.w3.org/1999/xhtml',
                        cls='hm-dow-label',
                    )(name)
                )
            )

    # ── Month labels — HTML via foreignObject ─────────────────────────────
    month_labels = []
    if show_months:
        fo_h = MONTH_H
        for x, name in _month_label_positions(dates):
            month_labels.append(
                foreign_object(
                    x=str(x), y='0',
                    width=str(STEP * 3), height=str(fo_h),
                )(
                    div(
                        xmlns='http://www.w3.org/1999/xhtml',
                        cls='hm-month-label',
                    )(name)
                )
            )

    # ── Legend ─────────────────────────────────────────────────────────────
    legend_els = []
    if show_legend:
        legend_y = top + grid_h + 8
        legend_els = _legend_svg(vmax, color_hue, color_chroma,
                                  value_fmt, left, total_w, legend_y)

    return svg(
        cls='activity-heatmap',
        viewBox=f'0 0 {total_w} {total_h}',
        width=str(total_w),
        height=str(total_h),
        **{'aria-label': 'activity heatmap'},
    )(
        g(cls='hm-months')(*month_labels),
        g(cls='hm-dow')(*dow_labels),
        g(cls='hm-cells')(*cells),
        g(cls='hm-legend')(*legend_els),
    )


# ── Public component ──────────────────────────────────────────────────────────

def activity_heatmap(
    data:          Dict[str, Optional[float]],
    *,
    label:         str = '',
    weeks:         int = 52,
    end:           Optional[date] = None,
    color_hue:     float = 142,
    color_chroma:  float = 0.18,
    value_fmt:     Callable[[float], str] = lambda v: fmt_value(v),
    show_dow:      bool = True,
    show_months:   bool = True,
    show_legend:   bool = True,
    cls:           str = '',
    id:            Optional[str] = None,
) -> object:
    """GitHub-style 52-week activity heatmap.

    Parameters
    ----------
    data          : {ISO-date-string: value | None} — missing key = no data
    label         : optional caption below the heatmap
    weeks         : columns of weeks to show (default 52)
    end           : rightmost date (default today)
    color_hue     : OKLCH hue  142=green  220=blue  25=orange  280=purple
    color_chroma  : OKLCH chroma — saturation of the color ramp
    value_fmt     : formats a value for the legend scale
    show_dow      : Mon / Wed / Fri labels on the left
    show_months   : month abbreviations above the grid
    show_legend   : Less ■■■■■ More scale below the grid
    cls, id       : HTML attributes on the wrapper div
    """
    end = end or date.today()

    hmap_svg = _heatmap_svg(
        data=data, weeks=weeks, end=end,
        color_hue=color_hue, color_chroma=color_chroma,
        value_fmt=value_fmt,
        show_dow=show_dow, show_months=show_months, show_legend=show_legend,
    )

    # activity_heatmap returns bare content — no surface wrapper.
    # Wrap in card() from md_web.ui.layout for surface/border/padding.
    extra_cls = f' {cls}' if cls else ''
    attrs     = {'cls': f'activity-heatmap-wrap{" " + extra_cls if extra_cls else ""}'}
    if id:
        attrs['id'] = id

    return div(**attrs)(
        hmap_svg,
        small(cls='heatmap-label')(label) if label else None,
    )
