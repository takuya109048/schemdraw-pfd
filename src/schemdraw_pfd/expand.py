"""Turn the user's description into the flat netlist that is laid out.

Two conveniences are resolved here, before layout:

* **inline items** (valves, meters ... written on a line instead of in the
  equipment order) become equipment placed next to one end of their line, and
  the line is split into a chain through them;
* **shared ports**: a port used by several lines gets a main pipe (header)
  that runs straight on from the port; the lines cut into it one after
  another, each at a tap of its own (a generated tee), never all at one point.
  Any number of lines may share a port.

Generated ids start with ``_`` (``_L<line>_<k>`` for inline items without an
id, ``_T_<id>_<port>``, ``_T_<id>_<port>_2`` ... for the taps); they are never
drawn as tags.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

from .elements import ElementLibrary, Geometry
from .netlist import Connection, Equipment

GENERATED_PREFIX = "_"
TAP_PREFIX = f"{GENERATED_PREFIX}T_"  # taps (tees) of generated main pipes


@dataclass
class PendingTee:
    """A tee on the level of an element with a vertical (N/S) shared port.

    Its height must follow the port, which is only known once the level band
    is laid out (see ``Flowsheet._settle_tees``).
    """

    tee: str
    owner: str
    port: str
    side: str  # "N" or "S"


@dataclass
class Expanded:
    equipment: list[Equipment]  # left-to-right order, generated items included
    connections: list[Connection]  # flat: no inline items, no shared ports
    geos: dict[str, Geometry]
    pending: list[PendingTee] = field(default_factory=list)
    headers: list[list[str]] = field(default_factory=list)  # taps of one main pipe
    # taps of main pipes whose lines are alike (parallel trains) -> the item
    # whose shared port the main pipe leaves
    owners: dict[str, str] = field(default_factory=dict)


def _sign(v: int) -> int:
    return (v > 0) - (v < 0)


def expand(
    equipment: list[Equipment],
    connections: list[Connection],
    library: ElementLibrary,
    describe: Callable[[Equipment, Callable[[], Geometry]], Geometry] | None = None,
    edges: dict[str, str] | None = None,
) -> Expanded:
    """``describe(eq, build)`` may wrap geometry building (e.g. error context).

    ``edges``: off-sheet flags that will stand on a sheet edge (``"left"`` /
    ``"right"``, flow layout) -- for the order of taps on a main pipe they
    lie beyond everything on that side, wherever they were declared."""
    eqs = list(equipment)
    cache: dict[str, Geometry] = {}

    def geo(e: Equipment) -> Geometry:
        if e.id not in cache:
            build = lambda: e.geometry(library)  # noqa: E731
            cache[e.id] = describe(e, build) if describe else build()
        return cache[e.id]

    flat = _inline(eqs, connections, geo)
    pending: list[PendingTee] = []
    headers: list[list[str]] = []
    owners: dict[str, str] = {}
    flat = _auto_tees(eqs, flat, geo, pending, headers, owners, edges or {})
    return Expanded(eqs, flat, {e.id: geo(e) for e in eqs}, pending, headers, owners)


# -- inline items ----------------------------------------------------------------
def _inline(
    eqs: list[Equipment],
    connections: list[Connection],
    geo: Callable[[Equipment], Geometry],
) -> list[Connection]:
    by_id = {e.id: e for e in eqs}
    results: list[list[Connection] | None] = [
        None if c.inline else [c] for c in connections
    ]
    todo = [ci for ci, r in enumerate(results) if r is None]
    while todo:  # a line may end on an inline item of a later line
        ready = [ci for ci in todo if all(e in by_id for e, _ in connections[ci].ends)]
        if not ready:
            c = connections[todo[0]]
            missing = [e for e, _ in c.ends if e not in by_id]
            raise KeyError(f"{c.src} -> {c.dst}: unknown equipment {missing[0]!r}")
        for ci in ready:
            results[ci] = _expand_line(ci, connections[ci], eqs, by_id, geo)
            todo.remove(ci)
    return [c for r in results if r is not None for c in r]


def _expand_line(
    ci: int,
    c: Connection,
    eqs: list[Equipment],
    by_id: dict[str, Equipment],
    geo: Callable[[Equipment], Geometry],
) -> list[Connection]:
    order = {e.id: i for i, e in enumerate(eqs)}
    (s, _), (d, _) = c.ends
    forward = order[s] <= order[d]
    anchor = c.anchor
    if anchor == "auto":  # next to the destination, unless it is an edge flag
        off = geo(by_id[d]).off_sheet and not geo(by_id[s]).off_sheet
        anchor = "src" if off else "dst"
    a = by_id[d if anchor == "dst" else s]
    # on a line out to an off-sheet flag the items sit on the flag's level
    home = by_id[d] if geo(by_id[d]).off_sheet else a

    chain: list[Equipment] = []
    for k, item in enumerate(c.inline):
        params = dict(item.params)
        if not forward:
            params.setdefault("mirror", True)  # in/out face the way the line runs
        level = home.level if item.level is None else item.level
        eq = Equipment(
            item.id or f"{GENERATED_PREFIX}L{ci}_{k}",
            item.type,
            level,
            params,
            {},
            item.desc,
            item.elevation,
        )
        if eq.id in by_id:
            raise ValueError(f"duplicate id {eq.id!r}")
        ports = geo(eq).ports
        for p in (item.port_in, item.port_out):
            if p not in ports:
                raise KeyError(
                    f"inline {item.type!r} ({eq.id}) has no port {p!r}; "
                    f"known: {sorted(ports)} (set 'in' / 'out')"
                )
        by_id[eq.id] = eq
        chain.append(eq)

    i = order[a.id]
    at = i if (anchor == "dst") == forward else i + 1
    eqs[at:at] = chain if forward else chain[::-1]
    refs = [c.src]
    for eq, item in zip(chain, c.inline):
        refs += [f"{eq.id}.{item.port_in}", f"{eq.id}.{item.port_out}"]
    refs.append(c.dst)
    pairs = range(0, len(refs), 2)
    # on a recycle line the items follow the source; only the last leg runs back
    return [
        Connection(refs[j], refs[j + 1], c.kind, recycle=c.recycle and j == pairs[-1])
        for j in pairs
    ]


# -- shared ports -> main pipe (header) with taps ----------------------------------
Use = tuple[int, int]  # (connection index, end: 0 = src, 1 = dst)


def _auto_tees(
    eqs: list[Equipment],
    conns: list[Connection],
    geo: Callable[[Equipment], Geometry],
    pending: list[PendingTee],
    headers: list[list[str]],
    owners: dict[str, str],
    edges: dict[str, str],
) -> list[Connection]:
    """Every port used by several lines gets a main pipe (header).

    The main pipe leaves the port and runs straight on; the lines cut into it
    one after another, each at a tap of its own (a generated tee with three
    arms: from the port, on along the main pipe, out to the line).  The line
    that best continues straight on ends the main pipe; the others tap in
    ordered by how far along the flow their other end lies, the nearest
    closest to the port, so lines do not cross.  A top / bottom port whose
    lines go both ways rises (drops) into the middle of the main pipe, which
    then runs both ways.  There is no limit on the number of lines.
    """
    uses: dict[tuple[str, str], list[Use]] = {}
    for ci, c in enumerate(conns):
        for end, ref in enumerate(c.ends):
            uses.setdefault(ref, []).append((ci, end))
    by_id = {e.id: e for e in eqs}
    top = max((e.level for e in eqs), default=0)
    conns = list(conns)
    trunks: dict[int, list[Connection]] = {}

    for (owner_id, pname), us in uses.items():
        if len(us) < 2 or owner_id not in by_id:
            continue  # unknown ids / ports are reported by validation
        owner = by_id[owner_id]
        ports = geo(owner).ports
        if pname not in ports:
            continue
        where = f"port {owner_id}.{pname}"
        roles = {end for _, end in us}
        if len(roles) > 1:
            raise ValueError(f"{where} is both the start and the end of lines")
        order = {e.id: i for i, e in enumerate(eqs)}
        base_id = f"{TAP_PREFIX}{owner_id}_{pname}"
        ids = [base_id] + [f"{base_id}_{k}" for k in range(2, len(us) + 1)]
        for t in ids:
            if t in by_id:
                raise ValueError(f"duplicate id {t!r}")
        tee_ids = iter(ids)

        def other(u: Use) -> Equipment | None:
            return by_id.get(conns[u[0]].ends[1 - u[1]][0])

        def rel(u: Use) -> int:
            """Where the line's other end lies in the order (+: right)."""
            o = other(u)
            if o is None:
                return 0
            edge = {"left": -1, "right": 1}.get(edges.get(o.id, ""))
            if edge and not edges.get(owner_id):
                return edge * len(eqs)  # beyond everything on that side
            return order[o.id] - order[owner_id]

        side = ports[pname].side
        level = owner.level
        settle = False
        if side in "NS":
            # hang the main pipe in the band below an S port, or stand it in
            # the band above an N port -- unless every line leads the other
            # way (edge flags aside, their height is free): a pump's outlet
            # sending reflux back down to its column and product on to a
            # flag keeps the main pipe on its own band, just above the port
            ends = [by_id.get(conns[ci].ends[1 - e][0]) for ci, e in us]
            levels = [o.level for o in ends if o and not geo(o).off_sheet]
            if side == "S":
                back = bool(levels) and min(levels) > owner.level
            else:
                back = bool(levels) and max(levels) < owner.level
            if side == "S" and owner.level > 0 and not back:
                level -= 1
            elif side == "N" and owner.level < top and not back:
                level += 1
            else:
                settle = True
        # on a tie, a main pipe lifted above an N port turns down, below an S up
        across = "s" if side == "N" else "n"

        def dy(u: Use) -> int:
            o = other(u)
            return 0 if o is None else _sign(o.level - level)

        def faces(u: Use) -> str:
            """The side the line's other port faces ("" if unknown)."""
            o = other(u)
            if o is None:
                return ""
            port = geo(o).ports.get(conns[u[0]].ends[1 - u[1]][1])
            return "" if port is None else port.side

        def height(e: Equipment, side: str, offset: int) -> int:
            """Rough height of a port above its band floor (before layout)."""
            g = geo(e)
            at = {"N": g.height, "S": 0}.get(side, offset)
            return (e.elevation or 0) + at

        # the main pipe's rough height on its band; None: free to follow
        # the lines (it leaves an off-sheet flag, whose height is free)
        pipe_h: int | None = None
        if level == owner.level and not geo(owner).off_sheet:
            pipe_h = height(owner, side, ports[pname].offset)
            pipe_h += {"N": 2, "S": -2}.get(side, 0)

        def arm_of(u: Use) -> str:
            """The tap's arm the line enters by: from the band above or
            below; on the same band from the side its port faces when the
            port lies beyond the main pipe that way (a pump's outlet rises
            into the tap from below, a vessel's bottom drops in from above),
            else across from the shared port."""
            if dy(u):
                return "n" if dy(u) > 0 else "s"
            f = faces(u)
            o = other(u)
            if f not in ("N", "S") or o is None:
                return across
            if pipe_h is not None:
                port = geo(o).ports[conns[u[0]].ends[1 - u[1]][1]]
                h = height(o, f, port.offset)
                if (h < pipe_h) if f == "N" else (h > pipe_h):
                    return "s" if f == "N" else "n"
                return across
            return "s" if f == "N" else "n"

        def ahead(u: Use, sign: int) -> bool:
            """The line's other end lies ahead on the main pipe's own band."""
            return sign * rel(u) > 0 and dy(u) == 0

        def level_run(u: Use, sign: int) -> bool:
            """The line runs straight on from the end of the main pipe (its
            port faces along it, not up or down)."""
            return ahead(u, sign) and faces(u) not in ("N", "S")

        kind = conns[us[0][0]].kind

        def far_end(u: Use) -> tuple:
            o = other(u)
            port = conns[u[0]].ends[1 - u[1]][1]
            return (None,) if o is None else (o.type, port, o.level, o.elevation)

        alike = len({far_end(u) for u in us}) == 1
        split = roles == {0}
        refs: dict[Use, str] = {}
        made = len(trunks.get(us[0][0], []))  # main-pipe pieces made before

        def join(a: str, b: str) -> None:
            """Pipe from ``a`` (nearer the shared port) to ``b``."""
            c = Connection(a, b, kind) if split else Connection(b, a, kind)
            trunks.setdefault(us[0][0], []).append(c)

        def run(start: str, way: str, lines: list[Use]) -> list[Equipment]:
            """Main pipe from ``start`` running ``way`` ("e"/"w"); the lines
            cut into it in order.  Returns its taps, nearest first."""
            sign = 1 if way == "e" else -1
            back = "w" if way == "e" else "e"
            last = max(
                lines,
                key=lambda u: (ahead(u, sign), sign * rel(u) > 0, sign * rel(u)),
            )
            # alike lines (repeated units) that do not run straight on all tap
            # in the same way -- the last tap is an elbow -- instead of one of
            # them entering the end of the main pipe from the side
            straight = not (alike and not level_run(last, sign))
            taps = sorted(
                (u for u in lines if u is not last or not straight),
                key=lambda u: sign * rel(u),
            )
            chain: list[Equipment] = []
            prev = start
            for k, u in enumerate(taps):
                arm = arm_of(u)
                end = k == len(taps) - 1 and not straight
                arms = back + arm if end else back + way + arm
                tee = Equipment(next(tee_ids), "tee", level, {"arms": arms})
                join(prev, f"{tee.id}.{back}")
                refs[u] = f"{tee.id}.{arm}"
                prev = f"{tee.id}.{way}"
                chain.append(tee)
            if straight:
                refs[last] = prev
            return chain

        port_ref = f"{owner_id}.{pname}"
        west = [u for u in us if rel(u) < 0]
        east = [u for u in us if rel(u) >= 0]
        at = order[owner_id] + 1
        if side in "NS" and west and east:
            # the riser enters the middle of the main pipe, which runs both ways
            riser = "s" if side == "N" else "n"
            mid = Equipment(next(tee_ids), "tee", level, {"arms": riser + "we"})
            join(port_ref, f"{mid.id}.{riser}")
            w_chain = run(f"{mid.id}.w", "w", west)
            e_chain = run(f"{mid.id}.e", "e", east)
            placed = w_chain[::-1] + [mid] + e_chain
        else:
            if side in "EW":
                way = side.lower()
            else:
                way = "e" if 2 * len(east) >= len(us) else "w"
            chain = run(port_ref, way, us)
            placed = chain if way == "e" else chain[::-1]
            if way == "w":
                at -= 1  # in front of the port's element
        eqs[at:at] = placed
        headers.append([t.id for t in placed])
        if alike:
            owners.update((t.id, owner_id) for t in placed)
        for tee in placed:
            by_id[tee.id] = tee
            if settle:
                pending.append(PendingTee(tee.id, owner_id, pname, side))
        # only recycles merge into a top / bottom port: the lines flow on into
        # the main pipe, which is what runs back (a side port keeps its main
        # pipe right in front of it, so there the lines keep the flag)
        back = side in "NS" and not split and all(conns[ci].recycle for ci, _ in us)
        for ci, end in us:
            c = conns[ci]
            ref = refs[(ci, end)]
            c = replace(c, src=ref) if end == 0 else replace(c, dst=ref)
            conns[ci] = replace(c, recycle=False) if back else c
        if back:
            pieces = trunks[us[0][0]]
            pieces[made:] = [replace(c, recycle=True) for c in pieces[made:]]

    out: list[Connection] = []
    for ci, c in enumerate(conns):
        out += trunks.get(ci, [])
        out.append(c)
    return out
