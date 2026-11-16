"""Grid placement: left-to-right order, levels (Y) and compaction.

Rules
-----
* Y: level 0 is the bottom tier.  Each level is a horizontal band as tall as its
  tallest member; bands are separated by ``gap_y`` cells.
* X: every element owns a column span, strictly in the user's order, so elements
  never overlap vertically and the order can never be swapped.
* Off-sheet connector flags are the one exception: they are pulled to the left or
  right edge of the sheet, where flags of the same edge share one column and are
  stacked vertically.
* Density: the gap in front of an element is ``gap_x`` cells, never less than
  :func:`min_gap_x` (0 when the neighbours cannot touch because they sit at
  different heights, otherwise 1) and never less than what a connection between
  already placed elements needs as a pipe corridor (:func:`_link_min_x`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .elements import Geometry


@dataclass
class Item:
    """One element to place (input of :func:`place`)."""

    id: str
    type: str
    level: int
    geo: Geometry
    elevation: int | None = None  # cells above the band floor; None = automatic
    edge: str | None = None  # "left" / "right" for off-sheet connector flags


@dataclass
class Placed:
    id: str
    type: str
    level: int
    geo: Geometry
    x: int  # bottom-left corner, grid units
    y: int

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.x + self.geo.width, self.y + self.geo.height


@dataclass(frozen=True)
class Link:
    """A connection between item ``a`` (earlier in order) and ``b`` (later)."""

    a: int
    a_side: str
    a_off: int
    b: int
    b_side: str
    b_off: int


def _link_min_x(link: Link, ax: int, a_geo: Geometry, ay: int, by: int) -> int | None:
    """Smallest x of ``b`` that lets the pipe run without doubling back.

    Only forward cases are constrained (``a`` is to the left of ``b``): ``a`` leaves
    through its east face or a vertical face, ``b`` is entered through its west face
    or a vertical face.  Cases that cannot be satisfied (e.g. entering ``b`` from the
    east) are left to the router, which reports them.
    """
    if link.a_side == "E":
        right = ax + a_geo.width
        if link.b_side == "W":
            # facing ports on one row join directly across a single cell;
            # otherwise the pipe jogs: out one cell, up/down, then one cell in
            return right + (1 if ay + link.a_off == by + link.b_off else 3)
        if link.b_side in "NS":
            return right + 1 - link.b_off
    elif link.a_side in "NS" and link.b_side == "W":
        return ax + link.a_off + 2  # bend, then one straight cell into the face
    return None


def _bands(items: Sequence[Item], gap_y: Sequence[int]) -> tuple[list[int], list[int]]:
    top = max(it.level for it in items)
    height = [0] * (top + 1)
    for it in items:
        if it.level < 0 or (it.elevation or 0) < 0:
            raise ValueError("level and elevation must be >= 0")
        height[it.level] = max(height[it.level], it.geo.height + (it.elevation or 0))
    base = [0] * (top + 1)
    for lv in range(1, top + 1):
        base[lv] = base[lv - 1] + height[lv - 1] + gap_y[lv]
    return height, base


def place_y(
    items: Sequence[Item],
    gap_y: Sequence[int],
    links: Sequence[Link] = (),
    anchor_free: bool = False,
) -> list[int]:
    """Bottom y of every item.

    Inside its band an element sits ``elevation`` cells above the floor.  With
    automatic elevation it is lifted so that a W/E port lines up with the facing
    port it is connected to; heights spread outwards from elements whose height is
    already settled, so a whole train of valves and instruments ends up on one
    straight line.  Flags sharing an edge column are pushed apart if they collide.

    Order: first heights spread from the equipment (flags may follow it), then
    the flags are stacked, then whatever is still free lines up with the flags
    where they finally are.

    ``anchor_free``: a chain of items that reaches no settled item would stay
    unaligned; instead its tallest item is settled where it stands (on the
    band floor) and the rest of the chain lines up with it.
    """
    height, base = _bands(items, gap_y)
    ys = [base[it.level] + (it.elevation or 0) for it in items]
    slack = [height[it.level] - it.geo.height for it in items]
    settled = {
        j for j, it in enumerate(items) if it.elevation is not None or not slack[j]
    }

    def spread(from_flags: bool) -> None:
        changed = True
        while changed:
            changed = False
            for lk in links:
                if (lk.a_side, lk.b_side) != ("E", "W"):
                    continue
                for j, o, mine, theirs in (
                    (lk.a, lk.b, lk.a_off, lk.b_off),
                    (lk.b, lk.a, lk.b_off, lk.a_off),
                ):
                    if j in settled or o not in settled:
                        continue
                    if items[o].edge and not from_flags:
                        continue
                    if items[j].level != items[o].level:
                        continue
                    dy = ys[o] + theirs - (base[items[j].level] + mine)
                    if 0 <= dy <= slack[j]:
                        ys[j] = base[items[j].level] + dy
                        settled.add(j)
                        changed = True

    spread(from_flags=False)
    while anchor_free:
        linked = {
            j
            for lk in links
            if (lk.a_side, lk.b_side) == ("E", "W")
            and items[lk.a].level == items[lk.b].level
            for j in (lk.a, lk.b)
            if j not in settled and not items[j].edge
        }
        if not linked:
            break
        settled.add(max(linked, key=lambda j: (items[j].geo.height, -j)))
        spread(from_flags=False)
    for edge in ("left", "right"):
        stack = sorted(
            (j for j, it in enumerate(items) if it.edge == edge), key=ys.__getitem__
        )
        for below, j in zip(stack, stack[1:]):
            ys[j] = max(ys[j], ys[below] + items[below].geo.height + 1)
        settled.update(stack)
    spread(from_flags=True)
    return ys


def min_gap_x(items: Sequence[Item], ys: Sequence[int]) -> list[int]:
    """Smallest allowed gap in front of each item.

    Neighbouring columns may touch (gap 0) when the two elements are at least one
    cell apart vertically, so they can never run into each other; otherwise one
    cell is kept.  Edge flags always keep one cell to the equipment area.
    """
    gaps = [1] * len(items)
    prev = None
    for j, it in enumerate(items):
        if it.edge:
            continue
        if prev is not None:
            lo, hi = sorted((prev, j), key=ys.__getitem__)
            apart = ys[hi] - (ys[lo] + items[lo].geo.height)
            gaps[j] = 0 if apart >= 1 else 1
        prev = j
    return gaps


@dataclass(frozen=True)
class FlowLink:
    """Main-flow arrow ``u -> v``: v's port may not lie left of u's port.

    ``u_dx`` / ``v_dx``: x of the port relative to the item's left edge.
    """

    u: int
    u_dx: int
    v: int
    v_dx: int


def port_dx(geo: Geometry, side: str, offset: int) -> int:
    """X of a port relative to the element's left edge."""
    return {"W": 0, "E": geo.width}.get(side, offset)


def _apart(ys: Sequence[int], items: Sequence[Item], i: int, j: int) -> int:
    """Vertical clearance between two items (negative when they overlap)."""
    lo, hi = sorted((i, j), key=ys.__getitem__)
    return ys[hi] - (ys[lo] + items[lo].geo.height)


def place(
    items: Sequence[Item],
    gap_x: Sequence[int],
    gap_y: Sequence[int],
    links: Sequence[Link] = (),
    overlap: Sequence[bool] | None = None,
    flow: Sequence[FlowLink] = (),
    anchor_free: bool = False,
) -> list[Placed]:
    """Place ``items`` (given in left-to-right order); see the module docstring.

    ``gap_x[i]``: cells in front of item ``i``; ``gap_y[level]``: cells below the
    band of ``level``.

    ``overlap[i]``: item ``i`` may share X with earlier items that are at least
    one cell above or below it; it only keeps clear (by ``gap_x[i]``) of the
    earlier items it would otherwise touch.  Without it, every item has a
    column of its own.  ``flow``: main-flow arrows that may never point left.
    """
    if not items:
        return []
    ys = place_y(items, gap_y, links, anchor_free)
    xs: dict[int, int] = {}  # item index -> x, filled in placement order

    def link_floor(j: int) -> int:
        lows = [
            _link_min_x(lk, xs[lk.a], items[lk.a].geo, ys[lk.a], ys[j])
            for lk in links
            if lk.b == j and lk.a in xs
        ]
        lows += [xs[f.u] + f.u_dx - f.v_dx for f in flow if f.v == j and f.u in xs]
        return max((v for v in lows if v is not None), default=0)

    left = [j for j, it in enumerate(items) if it.edge == "left"]
    right = [j for j, it in enumerate(items) if it.edge == "right"]
    main = [j for j, it in enumerate(items) if not it.edge]

    edge_w = max((items[j].geo.width for j in left), default=0)
    for j in left:  # inner (east) faces of the left flags line up
        xs[j] = edge_w - items[j].geo.width
    cursor = edge_w if left else None  # right edge of the previous column
    done: list[int] = []
    for j in main:
        if overlap and overlap[j]:
            ends = [
                xs[i] + items[i].geo.width for i in done if _apart(ys, items, i, j) < 1
            ]
            if left:
                ends.append(edge_w)
            x = max(ends) + max(gap_x[j], 1) if ends else 0  # they could touch
        else:
            x = 0 if cursor is None else cursor + gap_x[j]
        xs[j] = max(x, link_floor(j))
        cursor = max(cursor or 0, xs[j] + items[j].geo.width)
        done.append(j)
    if right:  # west faces of the right flags line up
        x = (cursor or 0) + max(gap_x[j] for j in right)
        x = max([x] + [link_floor(j) for j in right])
        for j in right:
            xs[j] = x
    return [
        Placed(it.id, it.type, it.level, it.geo, xs[j], ys[j])
        for j, it in enumerate(items)
    ]
