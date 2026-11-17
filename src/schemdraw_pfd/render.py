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

What is drawn is first collected as a :class:`Scene` -- equipment outlines,
texts and strokes in drawing units -- which :func:`draw` hands to SchemDraw
and :mod:`.xlsx` turns into a workbook, so both outputs share every coordinate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
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
from schemdraw.types import Halign, Valign

from .expand import GENERATED_PREFIX
from .geometry import Polyline, Shape, find_crossings  # noqa: F401 (re-exported)
from .layout import Placed
from .model import Diagram
from .style import DEFAULT_THEME, LineStyle, Theme

Point = tuple[float, float]
PT_PER_UNIT = 36.0  # schemdraw default: 0.5 inch per drawing unit


# -- scene ---------------------------------------------------------------------
@dataclass(frozen=True)
class Text:
    """A text block at ``(x, y)``; ``ha`` / ``va`` say which point of it."""

    x: float
    y: float
    text: str
    fontsize: float
    ha: Halign = "center"
    va: Valign = "center"


@dataclass(frozen=True)
class Stroke:
    """A polyline drawn with ``style`` (SchemDraw keyword arguments: ``lw``,
    ``ls``, ``arrow`` ...; no ``lw`` means the equipment line width)."""

    points: tuple[Point, ...]
    style: dict[str, Any] = field(default_factory=dict)


Item = Shape | Text | Stroke  # a Shape: equipment outline in sheet coordinates


@dataclass
class Group:
    """What belongs together: one piece of equipment (outline, texts, tag),
    one line (its pieces between crossing gaps and its nozzle ticks), or the
    sheet (specification rows and frame)."""

    kind: str  # "equipment" | "line" | "sheet"
    id: str
    items: list[Item]
    placed: Placed | None = None  # equipment only


@dataclass
class Scene:
    groups: list[Group]
    theme: Theme
    font: str | None  # the installed font the texts are drawn with
    bounds: tuple[float, float, float, float]  # sheet (frame) x0, y0, x1, y1


class _Shapes(elm.Element):
    """An Element whose segments are given in absolute drawing coordinates."""

    def __init__(self, segments, **kw):
        super().__init__(**kw)
        self.segments.extend(segments)


def _font(candidates: tuple[str, ...]) -> str | None:
    """The first installed font of ``candidates`` (e.g. one for Japanese)."""
    available = {f.name for f in font_manager.fontManager.ttflist}
    return next((f for f in candidates if f in available), None)


def _line_strokes(
    pts: list[Point],
    marks: dict[int, list[float]],
    style: LineStyle,
    r: float,
) -> list[Stroke]:
    """Split a route at its crossing marks into line pieces (a gap at each);
    the last piece carries the arrow."""
    plain: dict[str, Any] = {"lw": style.lw, "ls": style.ls}
    strokes: list[Stroke] = []
    run: list[Point] = [pts[0]]
    for si, (p, q) in enumerate(zip(pts, pts[1:])):
        horizontal = p[1] == q[1]
        axis = 0 if horizontal else 1
        sign = 1 if q[axis] > p[axis] else -1
        for m in sorted(marks.get(si, []), reverse=sign < 0):
            before = (m - sign * r, p[1]) if horizontal else (p[0], m - sign * r)
            after = (m + sign * r, p[1]) if horizontal else (p[0], m + sign * r)
            run.append(before)
            strokes.append(Stroke(tuple(run), plain))
            run = [after]
        run.append(q)
    arrow: dict[str, Any] = (
        {"arrow": style.arrow, "arrowwidth": 0.2, "arrowlength": 0.3}
        if style.arrow
        else {}
    )
    strokes.append(Stroke(tuple(run), {**plain, **arrow}))
    return strokes


def _line_segments(
    pts: list[Point],
    marks: dict[int, list[float]],
    style: LineStyle,
    r: float,
) -> list:
    """:func:`_line_strokes` as SchemDraw segments."""
    return [_segment(s) for s in _line_strokes(pts, marks, style, r)]


Pipe = tuple[tuple[float, float], tuple[float, float]]  # one straight piece


def _tag_items(
    p: Placed, theme: Theme, busy: list[float], pipes: Sequence[Pipe] = ()
) -> list[Item]:
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
    items: list[Item] = [Text(x, y, p.id, theme.fontsize)]
    if theme.tag_underline:
        under = y - 0.62 * em
        items.append(
            Stroke(((x - width / 2, under), (x + width / 2, under)), {"lw": 0.8})
        )
    return items


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


def _nozzle_tick(a: Point, b: Point) -> Stroke:
    """Short flange line across a pipe, just outside the equipment outline."""
    dx = (b[0] > a[0]) - (b[0] < a[0])
    dy = (b[1] > a[1]) - (b[1] < a[1])
    cx, cy = a[0] + 0.45 * dx, a[1] + 0.45 * dy
    return Stroke(((cx - 0.3 * dy, cy - 0.3 * dx), (cx + 0.3 * dy, cy + 0.3 * dx)))


def scene(
    diagram: Diagram,
    label: bool = True,
    fontsize: float | None = None,
    frame: bool = True,
    theme: Theme | None = None,
) -> Scene:
    """Everything :func:`draw` puts on the sheet, in drawing order."""
    theme = theme or DEFAULT_THEME
    if fontsize is not None:
        theme = replace(theme, fontsize=fontsize)
    styles = diagram.line_styles
    missing = {c.kind for c, _ in diagram.routes} - set(styles)
    if missing:
        raise ValueError(f"no line style for kinds {sorted(missing)}")
    groups: list[Group] = []
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
        items: list[Item] = list(p.geo.translated(p.x, p.y))
        for x, y, text, scale in p.geo.texts:
            if text:
                items.append(Text(p.x + x, p.y + y, text, theme.fontsize * scale))
        if label and not p.id.startswith(GENERATED_PREFIX):
            items += _tag_items(p, theme, west.get(p.id, []), pipes)
        groups.append(Group("equipment", p.id, items, p))

    # lines -------------------------------------------------------------------
    conns = [c for c, _ in diagram.routes]
    paths = [r.points for _, r in diagram.routes]
    marks: list[dict[int, list[float]]] = [{} for _ in paths]
    for _, _, vr, vs, (_, y) in find_crossings(paths):
        if styles[conns[vr].kind].process:  # the vertical line passes behind
            marks[vr].setdefault(vs, []).append(y)
    for c, pts, m in zip(conns, paths, marks):
        style = styles[c.kind]
        line: list[Item] = list(_line_strokes(pts, m, style, theme.crossing_gap))
        if style.process and theme.nozzle_ticks:
            (src, _), (dst, _) = c.ends
            if by_id[src].geo.nozzles:
                line.append(_nozzle_tick(pts[0], pts[1]))
            if by_id[dst].geo.nozzles:
                line.append(_nozzle_tick(pts[-1], pts[-2]))
        groups.append(Group("line", f"{c.src} -> {c.dst}", line))

    # sheet: specification rows and border --------------------------------------
    xs = [v for p in diagram.placed for v in (p.bbox[0], p.bbox[2])]
    ys = [v for p in diagram.placed for v in (p.bbox[1], p.bbox[3])]
    xs += [x for pts in paths for x, _ in pts]
    ys += [y for pts in paths for _, y in pts]
    mg = theme.margin
    x0, x1, y0, y1 = min(xs) - mg, max(xs) + mg, min(ys) - mg, max(ys) + mg
    em = theme.fontsize / PT_PER_UNIT
    sheet: list[Item] = []
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
        va: Valign = "top" if rotating else "bottom"
        y = y0 if rotating else y1
        for cx, text in _spread(entries, em):
            x0, x1 = (
                min(x0, cx - _text_width(text, em) / 2 - 1),
                max(x1, cx + _text_width(text, em) / 2 + 1),
            )
            sheet.append(Text(cx, y, text, theme.fontsize, "center", va))
    pitch = theme.spec_line_height * em
    y0 -= rows[True] * pitch + (1 if rows[True] else 0)
    y1 += rows[False] * pitch + (1 if rows[False] else 0)
    if frame:
        corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0))
        sheet.append(Stroke(corners, {"lw": theme.frame_lw}))
    if sheet:
        groups.append(Group("sheet", "sheet", sheet))
    return Scene(groups, theme, _font(theme.fonts), (x0, y0, x1, y1))


# -- SchemDraw -----------------------------------------------------------------
def _segment(item: Item) -> Any:
    """The SchemDraw segment of one scene item."""
    if isinstance(item, Text):
        return SegmentText(
            (item.x, item.y),
            item.text,
            align=(item.ha, item.va),
            fontsize=item.fontsize,
        )
    if isinstance(item, Stroke):
        return Segment(list(item.points), **item.style)
    if isinstance(item, Polyline):
        if item.fill:
            return SegmentPoly(list(item.points), closed=item.closed, fill=item.fill)
        pts = list(item.points) + ([item.points[0]] if item.closed else [])
        return Segment(pts)
    if item.fill and item.full and item.rx == item.ry:
        return SegmentCircle((item.cx, item.cy), item.rx, fill=item.fill)
    return SegmentArc(
        (item.cx, item.cy),
        2 * item.rx,
        2 * item.ry,
        theta1=item.theta1,
        theta2=item.theta2,
    )


def configure(d: schemdraw.Drawing, sc: Scene) -> None:
    """Drawing defaults shared by every output (font, sizes, line width)."""
    extra: dict[str, Any] = {"font": sc.font} if sc.font else {}
    d.config(
        unit=1,
        fontsize=sc.theme.fontsize,
        lw=sc.theme.equipment_lw,
        bgcolor=sc.theme.background,  # opaque sheet, not a transparent background
        **extra,
    )


def draw(
    diagram: Diagram,
    path: str | Path,
    label: bool = True,
    fontsize: float | None = None,
    frame: bool = True,
    theme: Theme | None = None,
) -> None:
    """Draw ``diagram`` to ``path`` (format from the suffix: svg / png / pdf ...,
    or xlsx -- see :mod:`.xlsx`).

    ``fontsize`` overrides ``theme.fontsize``.
    """
    sc = scene(diagram, label, fontsize, frame, theme)
    if Path(path).suffix.lower() == ".xlsx":
        from .xlsx import write_xlsx

        write_xlsx(sc, path)
        return
    matplotlib.use("Agg")  # file output only; never open a GUI window
    schemdraw.use("matplotlib")
    d = schemdraw.Drawing(show=False)
    configure(d, sc)
    for g in sc.groups:
        d.add(_Shapes([_segment(i) for i in g.items]).at((0, 0)))
    d.save(str(path), transparent=False)
