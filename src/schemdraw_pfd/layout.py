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
    span: int = 1  # levels a tall item stands through (it sets no band height)


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


FLANGED_RUN = 2  # cells of a straight line between two flanged nozzles


@dataclass(frozen=True)
class Link:
    """A connection between item ``a`` (earlier in order) and ``b`` (later).

    ``flanged``: a process line between two vessel nozzles, drawn with a
    flange tick at each end.  Joined straight across, the ports stand at
    least ``FLANGED_RUN`` cells apart, so the two ticks and the flow arrow
    do not run into each other."""

    a: int
    a_side: str
    a_off: int
    b: int
    b_side: str
    b_off: int
    flanged: bool = False


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
            # facing ports on one row join directly across a single cell
            # (two between flanged nozzles); otherwise the pipe jogs: out one
            # cell, up/down, then one cell in
            if ay + link.a_off != by + link.b_off:
                return right + 3
            return right + (FLANGED_RUN if link.flanged else 1)
        if link.b_side in "NS":
            # out of the face one cell, then the turn: the nozzle stands at
            # least two cells clear of the face
            return right + 2 - link.b_off
    elif link.a_side in "NS" and link.b_side == "W":
        return ax + link.a_off + 2  # bend, then one straight cell into the face
    elif link.a_side in "NS" and link.b_side in "NS" and link.a_side != link.b_side:
        return ax + link.a_off - link.b_off  # facing nozzles: straight above
    return None


def _bands(items: Sequence[Item], gap_y: Sequence[int]) -> tuple[list[int], list[int]]:
    top = max(it.level for it in items)
    height = [0] * (top + 1)
    tall: list[Item] = []
    for it in items:
        if it.level < 0 or (it.elevation or 0) < 0:
            raise ValueError("level and elevation must be >= 0")
        if it.span > 1:
            tall.append(it)  # stands through several bands, stretches none
        else:
            height[it.level] = max(
                height[it.level], it.geo.height + (it.elevation or 0)
            )
    for it in tall:  # a band with nothing but tall items still needs a height
        if not height[it.level]:
            height[it.level] = 1
    base = [0] * (top + 1)
    for lv in range(1, top + 1):
        base[lv] = base[lv - 1] + height[lv - 1] + gap_y[lv]
        for it in tall:  # the first level above a tall item clears its top
            if it.level + it.span == lv:
                head = base[it.level] + (it.elevation or 0) + it.geo.height
                base[lv] = max(base[lv], head + gap_y[lv])
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
    slack = [max(height[it.level] - it.geo.height, 0) for it in items]
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
                    lj, lo = items[j].level, items[o].level
                    if lj != lo and items[o].span < 2:
                        continue  # only a tall item reaches into other bands
                    dy = ys[o] + theirs - (base[lj] + mine)
                    limit = slack[j]
                    if lj == lo or lj == lo + items[o].span - 1:
                        # beside a partner on its own level, or in the top
                        # band a tall partner stands through: free up to the
                        # partner's top (nothing else is that high there)
                        head = ys[o] + items[o].geo.height
                        limit = max(limit, head - items[j].geo.height - base[lj])
                    if 0 <= dy <= limit:
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
            if j not in settled and not items[j].edge and slack[j]
        }
        if not linked:
            break
        settled.add(max(linked, key=lambda j: (items[j].geo.height, -j)))
        spread(from_flags=False)
    # flags on one height stack so that their lines nest.  A line reaching
    # its item from below (a pump's outlet, a side nozzle) turns off the
    # flag's row at the item and runs down: on each edge the line from the
    # nearest item takes the lowest flag, lines from further in go over it.
    # A line that reaches its item's downward port (a tap on a main pipe
    # above, a vessel's bottom) runs up from the flag's row instead: those
    # flags stack above the others, the line from the furthest item lowest,
    # so it passes under the nearer lines' risers.  Flags with no line on top.
    partner: dict[int, int] = {}
    up: set[int] = set()  # flags whose line runs up to a downward port
    for lk in links:
        for j, o, side in ((lk.a, lk.b, lk.b_side), (lk.b, lk.a, lk.a_side)):
            if j not in partner:
                partner[j] = o
                if side == "S":
                    up.add(j)
    for edge in ("left", "right"):
        sign = 1 if edge == "left" else -1

        def nest(j: int) -> tuple:
            near = sign * partner.get(j, 0)
            return (ys[j], j not in partner, j in up, -near if j in up else near, j)

        stack = sorted((j for j, it in enumerate(items) if it.edge == edge), key=nest)
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
            gaps[j] = 0 if _apart(ys, items, prev, j) >= 1 else 1
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


Riser = tuple[int, int, int, int]  # port dx, y from, y to, partner item


def _risers(
    items: Sequence[Item], ys: Sequence[int], links: Sequence[Link]
) -> dict[int, list[Riser]]:
    """Vertical pipe runs that must stay free, per item.

    A line leaving a top (bottom) nozzle runs straight up (down) at the
    nozzle's x: as far as its partner when that lies in this direction,
    otherwise two cells (room to turn).  Items on other heights may share X
    with the item, but not stand in this run.
    """
    out: dict[int, list[Riser]] = {}
    for lk in links:
        for i, side, off, o in (
            (lk.a, lk.a_side, lk.a_off, lk.b),
            (lk.b, lk.b_side, lk.b_off, lk.a),
        ):
            if side not in "NS":
                continue
            top = ys[i] + items[i].geo.height
            o_lo, o_hi = ys[o], ys[o] + items[o].geo.height
            if side == "N":
                span = (top, max(top + 2, o_hi if o_lo >= top else 0))
            else:
                span = (min(ys[i] - 2, o_lo if o_hi <= ys[i] else ys[i]), ys[i])
            out.setdefault(i, []).append((off, span[0], span[1], o))
    return out


def _clear_risers(
    x: int,
    j: int,
    items: Sequence[Item],
    xs: dict[int, int],
    ys: Sequence[int],
    done: Sequence[int],
    risers: dict[int, list[Riser]],
) -> int:
    """Smallest x' >= x at which item ``j`` neither stands in the vertical pipe
    run of a placed item nor has one of its own runs through a placed item."""
    w, y0, y1 = items[j].geo.width, ys[j], ys[j] + items[j].geo.height
    moved = True
    while moved:
        moved = False
        for i in done:
            for dx, lo, hi, partner in risers.get(i, ()):  # j in i's run?
                c = xs[i] + dx
                if partner != j and lo <= y1 and y0 <= hi and x <= c <= x + w:
                    x, moved = c + 1, True
            i0, i1 = ys[i], ys[i] + items[i].geo.height
            for dx, lo, hi, partner in risers.get(j, ()):  # i in j's run?
                right = xs[i] + items[i].geo.width
                if partner != i and lo <= i1 and i0 <= hi and xs[i] <= x + dx <= right:
                    x, moved = right + 1 - dx, True
    return x


def _pull_sources(
    items: Sequence[Item],
    xs: dict[int, int],
    ys: Sequence[int],
    links: Sequence[Link],
    flow: Sequence[FlowLink],
    overlap: Sequence[bool],
    gap_x: Sequence[int],
    risers: dict[int, list[Riser]],
    pull: set[int] | None = None,
) -> None:
    """Move items that only feed items to their right up to them.

    ``pull``: items also moved up to the items on their right that they are
    joined to, whatever else they are joined to on their left (an item on a
    recycle, kept out of the in-order placement: it stands just in front of
    the line it is fed from, not far back by the item it returns to).

    Packed from the left, an item with nothing in front of it on its own
    height (a steam flag feeding a column, a supply flag above a heat
    exchanger) would slide far left and its line would cross the sheet.  An
    item whose lines all run to items on its right is instead placed as far
    right as it can stand: just in front of what it feeds, still clear of
    the items on its height and of the vertical pipe runs, and with every
    line still able to run without doubling back.  Rightmost first, so a
    chain of such items closes up behind its head.
    """
    n = len(items)
    fed: dict[int, list[Link]] = {}
    sources = []
    for j in range(n):
        if items[j].edge or not overlap[j] or j not in xs:
            continue
        if items[j].id.startswith("_T_"):
            continue  # a main pipe's tap stands where its main pipe needs it
        mine = [lk for lk in links if j in (lk.a, lk.b)]
        ahead = [lk for lk in mine if lk.a == j]
        if any(lk.b not in xs for lk in ahead):
            continue  # feeds an edge flag: it is not placed yet
        if ahead and (len(ahead) == len(mine) or j in (pull or ())):
            fed[j] = ahead
            sources.append(j)
    for j in sorted(sources, reverse=True):
        w = items[j].geo.width
        g = max(gap_x[j], 1)
        others = [i for i in xs if i != j and not items[i].edge]
        near = [i for i in others if _apart(ys, items, i, j) < 1]

        def fits(x: int) -> bool:
            for i in near:
                if not (x + w + g <= xs[i] or xs[i] + items[i].geo.width + g <= x):
                    return False
            for lk in fed[j]:
                low = _link_min_x(lk, x, items[j].geo, ys[j], ys[lk.b])
                if low is not None and xs[lk.b] < low:
                    return False
            for f in flow:
                if f.u == j and xs[f.v] + f.v_dx < x + f.u_dx:
                    return False
            return _clear_risers(x, j, items, xs, ys, others, risers) == x

        x = min(xs[lk.b] for lk in fed[j]) - w - g
        while x > xs[j] and not fits(x):
            x -= 1
        if x > xs[j]:
            xs[j] = x


def place(
    items: Sequence[Item],
    gap_x: Sequence[int],
    gap_y: Sequence[int],
    links: Sequence[Link] = (),
    overlap: Sequence[bool] | None = None,
    flow: Sequence[FlowLink] = (),
    anchor_free: bool = False,
    in_order: Sequence[bool] | None = None,
) -> list[Placed]:
    """Place ``items`` (given in left-to-right order); see the module docstring.

    ``in_order[i]`` (with ``overlap``): item ``i``'s left edge is never left
    of the item before it -- items still stack across levels, but a branch's upper
    items (a side stripper, a reflux drum) cannot run ahead of the pumps and
    coolers below them that the lane on their level has pushed right.

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
    risers = _risers(items, ys, links) if overlap and any(overlap) else {}
    for j in main:
        if overlap and overlap[j]:
            ends = [
                xs[i] + items[i].geo.width for i in done if _apart(ys, items, i, j) < 1
            ]
            if left:
                ends.append(edge_w)
            x = max(ends) + max(gap_x[j], 1) if ends else 0  # they could touch
            if in_order and in_order[j] and done:
                x = max(x, xs[done[-1]])  # never left of the item before
            x = _clear_risers(max(x, link_floor(j)), j, items, xs, ys, done, risers)
        else:
            x = 0 if cursor is None else cursor + gap_x[j]
        xs[j] = max(x, link_floor(j))
        cursor = max(cursor or 0, xs[j] + items[j].geo.width)
        done.append(j)
    if overlap and any(overlap):
        pull = {j for j in xs if in_order and not in_order[j]}
        _pull_sources(items, xs, ys, links, flow, overlap, gap_x, risers, pull)
    if right:  # west faces of the right flags line up
        x = (cursor or 0) + max(gap_x[j] for j in right)
        x = max([x] + [link_floor(j) for j in right])
        for j in right:
            xs[j] = x
    return [
        Placed(it.id, it.type, it.level, it.geo, xs[j], ys[j])
        for j, it in enumerate(items)
    ]
