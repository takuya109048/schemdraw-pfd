"""Repeated units (同じ構成の繰り返し): draw equal things the same way.

Parallel trains -- tank, pump, header, filter, three times over -- read at a
glance when every copy looks alike: the same spacing inside each copy and the
same pipe shapes.  This module finds such copies in the expanded netlist so
that layout and routing can keep them alike.

A *unit* is a connected group of equipment once the shared items are taken
out: off-sheet flags and the taps of generated main pipes (they join the units,
they belong to none).  Two units are *equivalent* when, item by item in the
left-to-right order, they have the same type, parameters and height, the same
lines between them (same ports, kinds and recycle flags) and the same lines
out to shared items.  Item ``k`` of one unit then corresponds to item ``k`` of
the other, and so do their lines.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .expand import TAP_PREFIX, Expanded


@dataclass
class Symmetry:
    """Equivalence classes found by :func:`find_units`.

    ``item_class[i]``: class of ``net.equipment[i]``; equal for corresponding
    items of equivalent units, unique otherwise.  ``line_class[ci]``: class of
    ``net.connections[ci]``, likewise.  ``units``: the units that have an
    equivalent (item indices, left to right), grouped by class.
    """

    item_class: list[int]
    line_class: list[int]
    shared: list[bool] = field(default_factory=list)  # flags and generated taps
    units: list[list[list[int]]] = field(default_factory=list)

    @property
    def repeated(self) -> bool:
        return bool(self.units)


def _shared(net: Expanded, i: int) -> bool:
    e = net.equipment[i]
    return e.id.startswith(TAP_PREFIX) or net.geos[e.id].off_sheet


def find_units(net: Expanded) -> Symmetry:
    n = len(net.equipment)
    index = {e.id: i for i, e in enumerate(net.equipment)}
    ends = [tuple(index[eq] for eq, _ in c.ends) for c in net.connections]

    parent = list(range(n))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in ends:
        if not (_shared(net, a) or _shared(net, b)):
            parent[root(a)] = root(b)
    comps: dict[int, list[int]] = {}
    for i in range(n):
        if not _shared(net, i):
            comps.setdefault(root(i), []).append(i)

    lines_of: dict[int, list[int]] = {}  # component root -> its lines
    for ci, (a, b) in enumerate(ends):
        for i in (a, b):
            if not _shared(net, i):
                lines_of.setdefault(root(i), []).append(ci)
                break

    def line_key(ci: int, local: dict[int, int]) -> tuple:
        c = net.connections[ci]
        (_, pa), (_, pb) = c.ends
        a, b = ends[ci]
        # a line out to a shared item: only the unit's end is compared
        return (local.get(a, -1), pa, local.get(b, -1), pb, c.kind, c.recycle)

    groups: dict[tuple, list[tuple[list[int], dict[tuple, int]]]] = {}
    for r, members in comps.items():
        if len(members) < 2:
            continue  # a lone item is no train
        local = {i: k for k, i in enumerate(members)}
        items = tuple(
            (
                e.type,
                json.dumps(e.params, sort_keys=True, default=str),
                e.level,
                e.elevation,
                e.span,
            )
            for e in (net.equipment[i] for i in members)
        )
        keyed = {line_key(ci, local): ci for ci in lines_of.get(r, [])}
        if len(keyed) != len(lines_of.get(r, [])):
            continue  # two lines alike inside one unit: no clear mapping
        groups.setdefault((items, tuple(sorted(keyed))), []).append((members, keyed))

    item_class = list(range(n))
    line_class = list(range(len(net.connections)))
    units: list[list[list[int]]] = []
    for copies in groups.values():
        if len(copies) < 2:
            continue
        first_items, first_lines = copies[0]
        for members, keyed in copies[1:]:
            for i, j in zip(first_items, members):
                item_class[j] = item_class[i]
            for key, ci in keyed.items():
                line_class[ci] = line_class[first_lines[key]]
        units.append([m for m, _ in copies])
    shared = [_shared(net, i) for i in range(n)]
    return Symmetry(item_class, line_class, shared, units)


def gap_groups(sym: Symmetry) -> list[list[int]]:
    """Item indices whose gaps (cells in front of the item) move together.

    Two items share a gap when they correspond *and* so do the items just in
    front of them: the gap between two items of a unit, or the gap in front
    of every unit that follows an equivalent one.  The first unit's gap to
    whatever precedes it stays free.  Shared items (flags, taps) in between
    are looked through: they stand wherever the units put them.
    """
    groups: dict[tuple[int, int], list[int]] = {}
    before = -1  # class of the last item that is not shared
    for i, c in enumerate(sym.item_class):
        groups.setdefault((c, before), []).append(i)
        if not sym.shared[i]:
            before = c
    return sorted(groups.values())
