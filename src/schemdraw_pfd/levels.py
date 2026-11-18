"""Automatic levels: the height tier of every item whose level is not given.

Only pumps have a fixed place: they stand on the ground (level 0).  Every
other item left without a level is placed on whatever level makes the
drawing best -- the pipes shortest, with the fewest bends and crossings.  No
item's role or the fluid in its lines is assumed.

The search starts with pumps on the ground and everything else one level up
(level 0 on a sheet without pumps).  A move shifts a level up or down: one
item, an item with its satellites (flags and instruments joined to it alone),
or the two items at the ends of a line -- the corresponding items of repeated
units (parallel trains) always together, so the copies stay alike (see
:func:`moves`).  Every move is tried (a cheap layout and routing of the
sheet, see ``Flowsheet._level_cost``) in batches; the best one of the first
batch that lowers the cost is taken, and the search stops when no move lowers
it.  Moves are tried in the natural order of the ids and a tie goes to the
first, so the result does not depend on the order of the lists.

Levels given by the user are kept as they are.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from .netlist import Connection, Equipment

PUMP = "pump"
MAX_ROUNDS = 100  # moves taken at most (each one lowers the cost)
BATCH = 16  # moves tried at once (see search_levels)

Levels = dict[str, int]


def _natural(text: str) -> tuple:
    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", text))


def start_levels(eqs: Sequence[Equipment]) -> Levels:
    """Where the search starts: given levels kept, pumps on the ground, the
    rest one level up (on the ground when the sheet has no pumps)."""
    up = int(any(e.type == PUMP for e in eqs))
    return {
        e.id: e.level if e.level is not None else 0 if e.type == PUMP else up
        for e in eqs
    }


def moves(
    eqs: Sequence[Equipment],
    conns: Sequence[Connection],
    together: Sequence[Sequence[str]] = (),
) -> list[list[str]]:
    """The sets of items one move of the search shifts a level up or down.

    Every item whose level was left out (pumps aside) moves on its own;
    with its satellites (the items joined by lines to it alone: a flag, an
    instrument), so a branch can move off without leaving them behind; and
    with the item at the other end of each of its lines.  ``together``: the
    corresponding items of repeated units, which always move together.
    """
    free = {e.id for e in eqs if e.level is None and e.type != PUMP}
    ids = {e.id for e in eqs}
    group: dict[str, list[str]] = {}
    for g in together:
        members = [i for i in g if i in free]
        for i in members:
            group[i] = members
    partners: dict[str, set[str]] = {i: set() for i in ids}
    for c in conns:
        (a, _), (b, _) = c.ends
        if a in ids and b in ids and a != b:
            partners[a].add(b)
            partners[b].add(a)

    def close(items: list[str]) -> tuple[str, ...]:
        out = {j for i in items for j in group.get(i, [i])}
        return tuple(sorted(out & free, key=_natural))

    sets: dict[tuple[str, ...], None] = {}
    for i in sorted(free, key=_natural):
        sets[close([i])] = None
        sats = [j for j in free if partners[j] == {i}]
        if sats:
            sets[close([i, *sats])] = None
        for o in sorted(partners[i] & free, key=_natural):
            sets[close([i, o])] = None
    return [list(m) for m in sets if m]


def search_levels(
    start: Levels,
    sets: Sequence[Sequence[str]],
    evaluate: Callable[[list[Levels]], list[int | None]],
    rounds: int = MAX_ROUNDS,
) -> Levels:
    """Descent over the levels of one sheet from ``start``.

    The moves (each set of :func:`moves` a level down, then up) are tried in
    batches of ``BATCH``: the best one of the first batch that lowers the
    cost is taken, and the next round starts from it.  ``evaluate`` returns
    the cost of each levels it gets (None: it does not route); a batch goes
    to it in one call, so it can work them out in parallel.  The batch size
    is fixed, so the result does not depend on how many processes do the
    work.
    """
    current = dict(start)
    (cost,) = evaluate([current])
    for _ in range(rounds):
        top = max(current.values()) + 1
        tasks: list[Levels] = []
        for move in sets:
            for step in (-1, 1):
                shifted = {i: current[i] + step for i in move}
                if all(0 <= v <= top for v in shifted.values()):
                    tasks.append({**current, **shifted})
        found = None
        for k in range(0, len(tasks), BATCH):
            batch = tasks[k : k + BATCH]
            for lv, c in zip(batch, evaluate(batch)):
                if c is not None and (cost is None or c < cost):
                    if found is None or c < found[0]:
                        found = (c, lv)
            if found is not None:
                break
        if found is None:
            break
        cost, current = found
    return current
