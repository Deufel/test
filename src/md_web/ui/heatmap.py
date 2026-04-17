"""
md_web.ui.heatmap
=================
A GitHub-style 52-week × 7-day activity heatmap.

Color is driven entirely through the design system's --color API.

  no activity    →  surface mode (no --color set)
                    — covers both None (no data) and 0 (tracked zero).
                    Visually identical; both mean "nothing happened here."

  activity > 0   →  linear map of value → --color: 0..1, with percentile
                    trimming at both ends. By default, values at/below
                    the 5th percentile clamp to 0 and values at/above
                    the 95th clamp to 1; the middle 90% gets the full
                    scale to spread across.

Why trim percentiles instead of using min/max? A few unusually small
values (a weekend in a weekday-business, a dead holiday) drag the scale
floor down and crush every typical day into the top sliver of the color
range. A single huge value (Black Friday, a launch day) does the mirror
damage from the top. Trimming both ends gives the middle 90% of data
the full contrast range, which is usually what you actually want to see.

Pass outlier_percentiles=(0, 100) to disable trimming and use pure
min/max linear. Pass (5, 100) to trim only the low end.

Zeros are excluded from the percentile computation and rendered as
no-activity. This matches how humans read calendars: a day with zero
activity is not "the dimmest active day," it's the absence of activity.

Depends on the design system: core.css (--color, --bg, --border, --type,
--contrast) and app.css (.stack composition class, small element defaults).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, Optional

from md_web.html import mk_tag
from .base import svg, g, rect, div, Color

foreign_object = mk_tag('foreignObject')
small          = mk_tag('small')

# ── Layout constants ──────────────────────────────────────────────────────────
CELL    = 11
GAP     = 2
STEP    = CELL + GAP
DOW_W   = 26
MONTH_H = 18

MONTHS  = ['Jan','Feb','Mar','Apr','May','Jun',
           'Jul','Aug','Sep','Oct','Nov','Dec']


# ── foreignObject label factory ───────────────────────────────────────────────

def _fo_label(
    x: float, y: float, w: float, h: float,
    text: str,
    *,
    align_x: str,   # 'start' | 'center' | 'end'
    align_y: str,   # 'start' | 'center' | 'end'
    pad: str = '',
) -> object:
    style = (
        f'--type: -2; --contrast: 0.5; '
        f'display: flex; '
        f'align-items: {align_y}; justify-content: {align_x}; '
        f'height: 100%; white-space: nowrap'
    )
    if pad:
        style += f'; {pad}'
    return foreign_object(
        x=str(x), y=str(y), width=str(w), height=str(h),
    )(div(xmlns='http://www.w3.org/1999/xhtml', style=style)(text))


# ── Grid geometry ─────────────────────────────────────────────────────────────

def _grid_dates(weeks: int, end: date) -> tuple[date, list[date]]:
    days_to_sunday = (6 - end.weekday()) % 7
    last_sunday    = end + timedelta(days=days_to_sunday)
    first_monday   = last_sunday - timedelta(days=weeks * 7 - 1)
    return first_monday, [first_monday + timedelta(days=i) for i in range(weeks * 7)]


def _month_label_positions(dates: list[date]) -> list[tuple[float, str]]:
    labels    = []
    last_week = -99
    for i, d in enumerate(dates):
        week_idx = i // 7
        if d.day <= 7 and week_idx - last_week >= 3:
            labels.append((DOW_W + week_idx * STEP, MONTHS[d.month - 1]))
            last_week = week_idx
    return labels


# ── Legend ────────────────────────────────────────────────────────────────────
# Continuous scale shown as ~5 sample stops. Not bins — just signposts
# along the gradient so the user can read the mapping.

def _legend_swatches(left: float, y: float) -> list:
    """'Less ■ ■ ■ ■ ■ More' — five sample points along the 0..1 scale."""
    items = []
    n_stops = 5

    items.append(_fo_label(
        left, y, 26, CELL + 2, 'Less',
        align_x='start', align_y='center',
    ))

    swatch_x = left + 28
    for i in range(n_stops):
        t  = i / (n_stops - 1)          # 0, 0.25, 0.5, 0.75, 1
        sx = swatch_x + i * (CELL + 3)
        items.append(rect(
            x=str(sx), y=str(y),
            width=str(CELL), height=str(CELL),
            rx='2',
            fill=Color.bg,
            style=f'--color: {t:.2f}',
        ))

    more_x = swatch_x + n_stops * (CELL + 3) + 2
    items.append(_fo_label(
        more_x, y, 26, CELL + 2, 'More',
        align_x='start', align_y='center',
    ))

    return items


# ── SVG builder ───────────────────────────────────────────────────────────────

def _heatmap_svg(
    data:         Dict[str, Optional[float]],
    weeks:        int,
    end:          date,
    color_hue:    Optional[float],
    lo_pct:       float,
    hi_pct:       float,
    show_dow:     bool,
    show_months:  bool,
    show_legend:  bool,
) -> object:

    _, dates = _grid_dates(weeks, end)

    # Percentile-trimmed linear mapping.
    # A few tiny values (weekend orders of a few hundred, a dead Tuesday
    # during a bank holiday) will otherwise drag the min down and squash
    # every normal day into the top of the scale. A single huge day
    # (Black Friday) does the mirror damage from the top. Trimming to
    # a percentile band gives most cells the full 0..1 range to spread
    # across; outliers on either end clamp.
    values = [v for v in (data.get(d.isoformat()) for d in dates)
              if v is not None and v > 0]
    if values:
        s = sorted(values)
        lo_idx = int(len(s) * lo_pct / 100)
        hi_idx = min(len(s) - 1, int(len(s) * hi_pct / 100))
        v_lo = s[lo_idx]
        v_hi = s[hi_idx]
        # Degenerate: if trimming collapses the range, fall back to full min/max
        if v_hi <= v_lo:
            v_lo, v_hi = s[0], s[-1]
    else:
        v_lo = v_hi = 0.0

    grid_w   = weeks * STEP - GAP
    grid_h   = 7 * STEP - GAP
    left     = DOW_W if show_dow     else 4
    top      = MONTH_H if show_months else 4
    legend_h = (MONTH_H + 4) if show_legend else 0
    total_w  = left + grid_w + 4
    total_h  = top  + grid_h + legend_h + 4

    # ── Cells ────────────────────────────────────────────────────────────
    # Two cases:
    #   - no activity (None or <= 0)  → surface mode
    #   - activity (val > 0)          → linear map val → [v_lo, v_hi] → [0, 1]
    cells = []
    for i, d in enumerate(dates):
        week_idx = i // 7
        dow      = i  % 7
        cx       = left + week_idx * STEP
        cy       = top  + dow      * STEP
        val      = data.get(d.isoformat())

        if val is None or val <= 0:
            cells.append(rect(
                x=str(cx), y=str(cy),
                width=str(CELL), height=str(CELL),
                rx='2', fill=Color.bg,
            ))
        else:
            if v_hi == v_lo:
                t = 0.5
            else:
                t = max(0.0, min(1.0, (val - v_lo) / (v_hi - v_lo)))
            cells.append(rect(
                x=str(cx), y=str(cy),
                width=str(CELL), height=str(CELL),
                rx='2',
                fill=Color.bg,
                style=f'--color: {t:.3f}',
            ))

    # ── Day-of-week labels ───────────────────────────────────────────────
    dow_labels = []
    if show_dow:
        fo_w = left - 2
        fo_h = CELL + 2
        for dow_idx, name in [(0, 'Mon'), (2, 'Wed'), (4, 'Fri')]:
            fy = top + dow_idx * STEP
            dow_labels.append(_fo_label(
                0, fy, fo_w, fo_h, name,
                align_x='end', align_y='center',
                pad='padding-right: 3px',
            ))

    # ── Month labels ─────────────────────────────────────────────────────
    month_labels = []
    if show_months:
        for x, name in _month_label_positions(dates):
            month_labels.append(_fo_label(
                x, 0, STEP * 3, MONTH_H, name,
                align_x='start', align_y='end',
                pad='padding-bottom: 2px',
            ))

    # ── Legend ───────────────────────────────────────────────────────────
    legend_els = []
    if show_legend:
        legend_y = top + grid_h + 8
        legend_els = _legend_swatches(left, legend_y)

    # ── Assembly ─────────────────────────────────────────────────────────
    root_style = f'--hue: {color_hue}' if color_hue is not None else None

    return svg(
        viewBox=f'0 0 {total_w} {total_h}',
        width=str(total_w),
        height=str(total_h),
        style=root_style,
        **{'aria-label': 'activity heatmap'},
    )(
        g()(*month_labels),
        g()(*dow_labels),
        g()(*cells),
        g()(*legend_els),
    )


# ── Public component ──────────────────────────────────────────────────────────

def activity_heatmap(
    data:                Dict[str, Optional[float]],
    *,
    label:               str = '',
    weeks:               int = 52,
    end:                 Optional[date] = None,
    color_hue:           Optional[float] = 142,
    outlier_percentiles: tuple[float, float] = (5, 95),
    show_dow:            bool = True,
    show_months:         bool = True,
    show_legend:         bool = True,
    cls:                 str = '',
    id:                  Optional[str] = None,
) -> object:
    """GitHub-style 52-week activity heatmap.

    color_hue
        Numeric — pin the hue at the SVG root.
        None    — inherit hue from surrounding context (.suc/.inf/.wrn/.dgr
                  or any ancestor setting --hue / --hue-shift).

    outlier_percentiles
        (lo, hi) percentile band used for the color scale endpoints.
        Default (5, 95): values at/below the 5th percentile clamp to
        --color: 0; values at/above the 95th clamp to --color: 1; the
        middle 90% get the full range to spread across. Use (0, 100)
        for pure min/max linear with no trimming.

    Wrap the result in layout.card() for a surface/border/padding wrapper.
    """
    end = end or date.today()
    lo_pct, hi_pct = outlier_percentiles

    hmap_svg = _heatmap_svg(
        data=data, weeks=weeks, end=end,
        color_hue=color_hue,
        lo_pct=lo_pct, hi_pct=hi_pct,
        show_dow=show_dow, show_months=show_months, show_legend=show_legend,
    )

    wrapper_cls = f'stack{" " + cls if cls else ""}'
    attrs = {'cls': wrapper_cls}
    if id:
        attrs['id'] = id

    return div(**attrs)(
        hmap_svg,
        small()(label) if label else None,
    )
