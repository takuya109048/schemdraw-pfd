"""Automatic levels: the height tier of every item whose level is not given.

A level is the tier an item stands on (0: the ground).  When the user leaves
it out it follows from the lines alone, as on a plant:

* **Pumps stand on the ground**: level 0.
* **A pump's suction comes from above**: whatever feeds a pump stands at
  least one level higher (a drum, a column bottom, a cooler before it).
* **Liquid falls**: a line from a vessel's bottom nozzle into another item's
  top nozzle (a condenser draining into its reflux drum) needs the source
  wholly above the destination.
* **Vapour rises**: a line out of a vessel's top nozzle (overhead vapour to a
  condenser, flash gas to a compressor) goes to an item above it.
* **A bottom nozzle is fed from below**: a line from a side nozzle into a
  vessel's bottom nozzle (a feed into a desalter) comes up from a lower
  level.
* **Columns stand on the ground**: a column (``column``) stands from level 0
  up through the level above the pumps (``span`` 2; 1 on a sheet without
  pumps), so its pumps stand beside its bottom, the items on the structure
  beside its upper part, and its overhead above its top.

Every other item stands as low as these allow: on the ground when the sheet
has no pumps, otherwise one level up (on the structure, beside the pumps
below).  Lines marked or found as recycles do not lift anything: they run
back against the flow.  A rule that would contradict the ones already taken
(a loop of rising lines) is dropped.

Items with no height of their own follow what they serve: an off-sheet flag
on a main-flow line stands by its item a level above it when it meets a top
nozzle (or a header's branch), a level below when it meets a bottom nozzle;
one on utility lines only stands on its item's level.  Flags that run a
main pipe (lines to several items) each keep a level of their own, a supply
above a collecting one; instruments, standalone valves and tees stand on
the level of what they are joined to.

Levels given by the user are kept as they are, and the rules then work around
them.  The result never depends on the order of the lists: rules are applied
in a fixed order of the ids.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from .elements import Geometry
from .netlist import Connection, Equipment, split_ref

PUMP = "pump"
TOWER = "column"
# items that take the level of what they are joined to
FOLLOWERS = {"instrument", "valve", "tee"}


def _natural(text: str) -> tuple:
    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", text))


def _homes(eqs: list[Equipment], conns: list[Connection], geos: dict[str, Geometry]):
    """Inline item id -> the equipment id it stands next to (its anchor)."""
    known = {e.id for e in eqs}
    out: dict[str, str] = {}
    for c in conns:
        (s, _), (d, _) = c.ends
        d_off = d in geos and geos[d].off_sheet
        home = s if c.anchor == "src" or (c.anchor == "auto" and d_off) else d
        for item in c.inline:
            if item.id and item.id not in known:
                out[item.id] = home
    return out


def _solve(
    bodies: list[str],
    fixed: dict[str, int],
    low: dict[str, int],
    rules: list[tuple[str, str, int]],
) -> dict[str, int] | None:
    """Lowest levels (at least ``low``) with ``lv[hi] >= lv[lo] + k`` for
    every rule; None when the rules contradict each other or a fixed level."""
    lv = {b: fixed.get(b, low[b]) for b in bodies}
    for _ in range(len(bodies) + 1):
        changed = False
        for hi, lo, k in rules:
            if lv[hi] < lv[lo] + k:
                if hi in fixed:
                    return None
                lv[hi] = lv[lo] + k
                changed = True
        if not changed:
            return lv
    return None


def auto_levels(
    eqs: list[Equipment],
    conns: list[Connection],
    geos: dict[str, Geometry],
    process_kinds: set[str],
    main_kinds: set[str],
    given: Callable[[Equipment], int | None] = lambda e: e.level,
) -> tuple[dict[str, int], dict[str, int]]:
    """Level of every item of ``eqs``, and the span of every column whose
    level is worked out (see the module docstring)."""
    by_id = {e.id: e for e in eqs}
    homes = _homes(eqs, conns, geos)

    def owner(ref: str) -> tuple[str | None, str | None]:
        """(equipment id, port side) of a line end; the side is None for an
        end on an inline item (it stands by its anchor)."""
        eq, port = split_ref(ref)
        if eq in by_id:
            port_ = geos[eq].ports.get(port)
            return eq, port_.side if port_ else None
        seen = set()
        while eq in homes and eq not in seen:
            seen.add(eq)
            eq = homes[eq]
            if eq in by_id:
                return eq, None
        return None, None

    def kind(e: Equipment) -> str:
        if geos[e.id].off_sheet:
            return "flag"
        if e.type == PUMP:
            return "pump"
        if e.type in FOLLOWERS:
            return "follow"
        return "body"

    kinds = {e.id: kind(e) for e in eqs}
    fixed = {e.id: lv for e in eqs if (lv := given(e)) is not None}
    for e in eqs:
        if kinds[e.id] == "pump":
            fixed.setdefault(e.id, 0)
    bodies = sorted(
        (i for i, k in kinds.items() if k in ("body", "pump") or i in fixed),
        key=_natural,
    )
    body = set(bodies)
    base = 1 if any(k == "pump" for k in kinds.values()) else 0
    # a column stands on the ground through the pumps' level, its upper part
    # beside the items on the structure (it stretches no level band)
    towers = {
        e.id: base + 1
        for e in eqs
        if e.type == TOWER and e.id not in fixed and e.span == 1
    }
    span = {e.id: towers.get(e.id, max(e.span, 1)) for e in eqs}
    low = {b: 0 if b in towers else base for b in bodies}

    lines = sorted(
        (c for c in conns if c.kind in process_kinds),
        key=lambda c: (_natural(c.src), _natural(c.dst)),
    )
    suction, falls, rises, enters = [], [], [], []
    for c in lines:
        (s, s_side), (d, d_side) = owner(c.src), owner(c.dst)
        if s is None or d is None or s == d or s not in body or d not in body:
            continue
        if kinds[d] == "pump" and kinds[s] != "pump":
            suction.append((s, d, 1 if span[s] == 1 else 0))
        if c.recycle:
            continue
        if d_side == "S" and s_side != "N" and geos[d].nozzles and span[s] == 1:
            # into a bottom nozzle from the side: it comes from below
            enters.append((d, s, 1))
        if not geos[s].nozzles or kinds[s] == "pump":
            continue
        if s_side == "S" and d_side == "N":
            falls.append((s, d, span[d]))
        elif s_side == "N" and kinds[d] != "pump":
            # the destination's top band above the source's
            rises.append((d, s, span[s] - span[d] + 1))

    rules: list[tuple[str, str, int]] = []
    lv = _solve(bodies, fixed, low, rules)
    assert lv is not None  # no rules yet: always solvable
    for rule in suction + falls + rises + enters:
        trial = _solve(bodies, fixed, low, rules + [rule])
        if trial is not None:
            rules.append(rule)
            lv = trial

    # flags, then instruments / valves / tees: by what they are joined to
    out = dict(lv)
    pending = sorted((i for i in kinds if i not in out), key=_natural)
    flags = [i for i in pending if kinds[i] == "flag"]
    rest = [i for i in pending if kinds[i] != "flag"]
    # per item: (partner, the partner's port side, a main-flow line?)
    ends: dict[str, list[tuple[str, str | None, bool]]] = {}
    for c in sorted(conns, key=lambda c: (_natural(c.src), _natural(c.dst))):
        (pa, sa), (pb, sb) = owner(c.src), owner(c.dst)
        if pa is None or pb is None:
            continue
        (ea, _), (eb, _) = split_ref(c.src), split_ref(c.dst)
        main = c.kind in main_kinds
        if ea in by_id:
            ends.setdefault(ea, []).append((pb, sb, main))
        if eb in by_id:
            ends.setdefault(eb, []).append((pa, sa, main))

    for f in flags:
        levels = []
        for p, side, main in ends.get(f, []):
            if p in lv:
                if not main or kinds[p] == "pump":  # utility; a pump outlet
                    levels.append(lv[p])
                elif side == "N":
                    levels.append(lv[p] + span[p])
                elif side == "S":
                    levels.append(max(lv[p] - 1, 0))
                else:
                    levels.append(lv[p])
        if levels:
            out[f] = min(levels)
    # a flag with lines to several items runs a main pipe along its level:
    # two such pipes on one level would run into each other, so each keeps a
    # level of its own -- a supply main above a collecting one
    feeds = {f for f in flags for c in conns if split_ref(c.src)[0] == f}
    mains = sorted(
        (f for f in flags if f in out and len(ends.get(f, [])) > 1),
        key=lambda f: (f in feeds, _natural(f)),
    )
    taken: set[int] = set()
    for f in mains:
        while out[f] in taken:
            out[f] += 1
        taken.add(out[f])
    changed = True
    while changed:
        changed = False
        for i in rest + flags:
            if i in out:
                continue
            levels = [out[p] for p, _, _ in ends.get(i, []) if p in out]
            if levels:
                out[i] = min(levels)
                changed = True
    for i in pending:
        out.setdefault(i, base)
    return out, towers
