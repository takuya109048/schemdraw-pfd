"""Draw a built diagram with SchemDraw in the style of a Japanese P&ID sheet.

* thin black lines, underlined equipment tags, flange ticks at vessel nozzles;
* each line kind is drawn with its :class:`~.style.LineStyle` (by default process
  pipes are solid with a flow arrow, instrument signals are dashed, instrument
  tapping lines are thin and plain);
* equipment specifications are listed above the diagram (below it for rotating
  machinery) and the sheet gets a border frame;
* where two lines cross, the vertical process line is broken (a gap) as if it
  passed behind the horizontal one; lines never loop over each other.

Everything sheet-wide (font, line widths, margins ...) comes from a
:class:`~.style.Theme`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import matplotlib
import schemdraw
from matplotlib import font_manager
from schemdraw import elements as elm
from schemdraw.segments import (
    Segment,
    SegmentArc,
    SegmentCircle,
    SegmentPoly,
    SegmentText,
)

from .expand import GENERATED_PREFIX
from .geometry import Polyline, find_crossings  # noqa: F401 (re-exported)
from .layout import Placed
from .model import Diagram
from .style import DEFAULT_THEME, LineStyle, Theme

Point = tuple[float, float]
PT_PER_UNIT = 36.0  # schemdraw default: 0.5 inch per drawing unit


class _Shapes(elm.Element):
    """An Element whose segments are given in absolute drawing coordinates."""

    def __init__(self, segments, **kw):
        super().__init__(**kw)
        self.segments.extend(segments)


def _font(candidates: tuple[str, ...]) -> str | None:
    """The first installed font of ``candidates`` (e.g. one for Japanese)."""
    available = {f.name for f in font_manager.fontManager.ttflist}
    return next((f for f in candidates if f in available), None)


def _line_segments(
    pts: list[Point],
    marks: dict[int, list[float]],
    style: LineStyle,
    r: float,
) -> list:
    """Split a route at its crossing marks into line pieces (a gap at each)."""
    lw = style.lw
    ls: Any = style.ls  # schemdraw types it as a Literal of the matplotlib styles
    segs: list = []
    run: list[Point] = [pts[0]]
    for si, (p, q) in enumerate(zip(pts, pts[1:])):
        horizontal = p[1] == q[1]
        axis = 0 if horizontal else 1
        sign = 1 if q[axis] > p[axis] else -1
        for m in sorted(marks.get(si, []), reverse=sign < 0):
            before = (m - sign * r, p[1]) if horizontal else (p[0], m - sign * r)
            after = (m + sign * r, p[1]) if horizontal else (p[0], m + sign * r)
            run.append(before)
            segs.append(Segment(run, lw=lw, ls=ls))
            run = [after]
        run.append(q)
    arrow: dict[str, Any] = (
        {"arrow": style.arrow, "arrowwidth": 0.2, "arrowlength": 0.3}
        if style.arrow
        else {}
    )
    segs.append(Segment(run, lw=lw, ls=ls, **arrow))
    return segs


def _shape_segments(p: Placed) -> list:
    segs: list = []
    for s in p.geo.translated(p.x, p.y):
        if isinstance(s, Polyline):
            if s.fill:
                segs.append(SegmentPoly(list(s.points), closed=s.closed, fill=s.fill))
            else:
                pts = list(s.points) + ([s.points[0]] if s.closed else [])
                segs.append(Segment(pts))
        elif s.fill and s.full and s.rx == s.ry:
            segs.append(SegmentCircle((s.cx, s.cy), s.rx, fill=s.fill))
        else:
            segs.append(
                SegmentArc(
                    (s.cx, s.cy), 2 * s.rx, 2 * s.ry, theta1=s.theta1, theta2=s.theta2
                )
            )
    return segs


Pipe = tuple[tuple[float, float], tuple[float, float]]  # one straight piece


def _tag_segments(
    p: Placed, theme: Theme, busy: list[float], pipes: Sequence[Pipe] = ()
) -> list:
    """Equipment tag (underlined by default) placed according to ``label_pos``.

    ``busy``: heights at which lines meet the west face; a tag placed on the left
    is moved up or down until it is clear of them.  ``pipes``: straight pieces
    of the routed lines; when one runs through the tag at its usual place, the
    tag moves to the left, above, below or right of the element, wherever it
    is clear -- above or below slid half a cell or a cell sideways if need be.
    """
    pos = p.geo.label_pos
    if pos == "none":
        return []
    x0, y0, x1, y1 = p.bbox
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    em = theme.fontsize / PT_PER_UNIT  # font size in drawing units
    width = 0.62 * em * len(p.id)

    def spot(where: str) -> tuple[float, float]:
        if where == "above":
            return cx, y1 + 0.5 + em / 2
        if where == "below":
            return cx, y0 - 0.5 - em / 2
        if where == "left":
            offsets = sorted(range(-(y1 - y0) // 2 + 1, (y1 - y0) // 2), key=abs)
            y = next(
                (cy + o for o in offsets if all(abs(cy + o - b) >= 1 for b in busy)),
                cy,
            )
            return x0 - 0.5 - width / 2, y
        if where == "right":
            return x1 + 0.5 + width / 2, cy
        return cx, cy

    def clear(at: tuple[float, float]) -> bool:
        tx, ty = at
        bx0, bx1 = tx - width / 2 - 0.1, tx + width / 2 + 0.1
        by0, by1 = ty - 0.75 * em, ty + em / 2
        return not any(
            min(a[0], b[0]) <= bx1
            and max(a[0], b[0]) >= bx0
            and min(a[1], b[1]) <= by1
            and max(a[1], b[1]) >= by0
            for a, b in pipes
        )

    x, y = spot(pos)
    if pos != "center" and not clear((x, y)):
        # the other sides, then above / below slid sideways off a riser
        spots = [spot(o) for o in ("left", "above", "below", "right") if o != pos]
        for where in ("above", "below"):
            sx, sy = spot(where)
            spots += [(sx + dx, sy) for dx in (-0.5, 0.5, -1.0, 1.0)]
        x, y = next((s for s in spots if clear(s)), (x, y))
    segs: list = [
        SegmentText((x, y), p.id, align=("center", "center"), fontsize=theme.fontsize)
    ]
    if theme.tag_underline:
        under = y - 0.62 * em
        segs.append(Segment([(x - width / 2, under), (x + width / 2, under)], lw=0.8))
    return segs


def _text_width(text: str, em: float) -> float:
    """Rough width of a text block: full-width (CJK) characters take one em,
    others a bit over half."""
    return max(
        sum(1.0 if ord(ch) > 0x2E7F else 0.6 for ch in line) * em
        for line in text.split("\n")
    )


def _spread(entries: list[tuple[float, str]], em: float) -> list[tuple[float, str]]:
    """Spec texts of one row, each centred over its item where possible, moved
    right just enough not to overlap the previous one (items on different
    levels may share X)."""
    out: list[tuple[float, str]] = []
    right = None
    for cx, text in sorted(entries, key=lambda e: e[0]):
        half = _text_width(text, em) / 2
        if right is not None and cx - half < right + em:
            cx = right + em + half
        out.append((cx, text))
        right = cx + half
    return out


def _nozzle_tick(a: Point, b: Point) -> Segment:
    """Short flange line across a pipe, just outside the equipment outline."""
    dx = (b[0] > a[0]) - (b[0] < a[0])
    dy = (b[1] > a[1]) - (b[1] < a[1])
    cx, cy = a[0] + 0.45 * dx, a[1] + 0.45 * dy
    return Segment([(cx - 0.3 * dy, cy - 0.3 * dx), (cx + 0.3 * dy, cy + 0.3 * dx)])


def draw(
    diagram: Diagram,
    path: str | Path,
    label: bool = True,
    fontsize: float | None = None,
    frame: bool = True,
    theme: Theme | None = None,
) -> None:
    """Draw ``diagram`` to ``path`` (format from the suffix: svg / png / pdf ...).

    ``fontsize`` overrides ``theme.fontsize``.
    """
    theme = theme or DEFAULT_THEME
    if fontsize is not None:
        theme = replace(theme, fontsize=fontsize)
    styles = diagram.line_styles
    missing = {c.kind for c, _ in diagram.routes} - set(styles)
    if missing:
        raise ValueError(f"no line style for kinds {sorted(missing)}")

    matplotlib.use("Agg")  # file output only; never open a GUI window
    schemdraw.use("matplotlib")
    d = schemdraw.Drawing(show=False)
    font = _font(theme.fonts)
    extra: dict[str, Any] = {"font": font} if font else {}
    d.config(
        unit=1,
        fontsize=theme.fontsize,
        lw=theme.equipment_lw,
        bgcolor=theme.background,  # opaque sheet, not a transparent background
        **extra,
    )
    by_id = {p.id: p for p in diagram.placed}
    west: dict[str, list[float]] = {}  # id -> heights of lines on the west face
    for c, r in diagram.routes:
        for (eq, _), pt in zip(c.ends, (r.points[0], r.points[-1])):
            if pt[0] <= by_id[eq].x:
                west.setdefault(eq, []).append(pt[1])
    pipes: list[Pipe] = [
        (a, b) for _, r in diagram.routes for a, b in zip(r.points, r.points[1:])
    ]

    # equipment ---------------------------------------------------------------
    for p in diagram.placed:
        segs = _shape_segments(p)
        for x, y, text, scale in p.geo.texts:
            if text:
                segs.append(
                    SegmentText(
                        (p.x + x, p.y + y),
                        text,
                        align=("center", "center"),
                        fontsize=theme.fontsize * scale,
                    )
                )
        if label and not p.id.startswith(GENERATED_PREFIX):
            segs += _tag_segments(p, theme, west.get(p.id, []), pipes)
        d.add(_Shapes(segs).at((0, 0)))

    # lines -------------------------------------------------------------------
    conns = [c for c, _ in diagram.routes]
    paths = [r.points for _, r in diagram.routes]
    marks: list[dict[int, list[float]]] = [{} for _ in paths]
    for _, _, vr, vs, (_, y) in find_crossings(paths):
        if styles[conns[vr].kind].process:  # the vertical line passes behind
            marks[vr].setdefault(vs, []).append(y)
    for c, pts, m in zip(conns, paths, marks):
        style = styles[c.kind]
        segs = _line_segments(pts, m, style, theme.crossing_gap)
        if style.process and theme.nozzle_ticks:
            (src, _), (dst, _) = c.ends
            if by_id[src].geo.nozzles:
                segs.append(_nozzle_tick(pts[0], pts[1]))
            if by_id[dst].geo.nozzles:
                segs.append(_nozzle_tick(pts[-1], pts[-2]))
        d.add(_Shapes(segs).at((0, 0)))

    # sheet: specification rows and border --------------------------------------
    xs = [v for p in diagram.placed for v in (p.bbox[0], p.bbox[2])]
    ys = [v for p in diagram.placed for v in (p.bbox[1], p.bbox[3])]
    xs += [x for pts in paths for x, _ in pts]
    ys += [y for pts in paths for _, y in pts]
    mg = theme.margin
    x0, x1, y0, y1 = min(xs) - mg, max(xs) + mg, min(ys) - mg, max(ys) + mg
    em = theme.fontsize / PT_PER_UNIT
    sheet: list = []
    rows = {False: 0, True: 0}  # rotating? -> number of text lines needed
    specs: dict[bool, list[tuple[float, str]]] = {False: [], True: []}
    for p in diagram.placed:
        desc = diagram.descs.get(p.id)
        if not (label and desc):
            continue
        text = f"{p.id}\n{desc}"
        rows[p.geo.rotating] = max(rows[p.geo.rotating], text.count("\n") + 1)
        specs[p.geo.rotating].append(((p.bbox[0] + p.bbox[2]) / 2, text))
    for rotating, entries in specs.items():
        y, va = (y0, "top") if rotating else (y1, "bottom")
        for cx, text in _spread(entries, em):
            x0, x1 = (
                min(x0, cx - _text_width(text, em) / 2 - 1),
                max(x1, cx + _text_width(text, em) / 2 + 1),
            )
            sheet.append(
                SegmentText(
                    (cx, y), text, align=("center", va), fontsize=theme.fontsize
                )
            )
    pitch = theme.spec_line_height * em
    y0 -= rows[True] * pitch + (1 if rows[True] else 0)
    y1 += rows[False] * pitch + (1 if rows[False] else 0)
    if frame:
        sheet.append(
            Segment(
                [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], lw=theme.frame_lw
            )
        )
    if sheet:
        d.add(_Shapes(sheet).at((0, 0)))

    d.save(str(path), transparent=False)
