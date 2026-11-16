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


def _sign(v: int) -> int:
    return (v > 0) - (v < 0)


def expand(
    equipment: list[Equipment],
    connections: list[Connection],
    library: ElementLibrary,
    describe: Callable[[Equipment, Callable[[], Geometry]], Geometry] | None = None,
) -> Expanded:
    """``describe(eq, build)`` may wrap geometry building (e.g. error context)."""
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
    flat = _auto_tees(eqs, flat, geo, pending, headers)
    return Expanded(eqs, flat, {e.id: geo(e) for e in eqs}, pending, headers)


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
def _auto_tees(
    eqs: list[Equipment],
    conns: list[Connection],
    geo: Callable[[Equipment], Geometry],
    pending: list[PendingTee],
    headers: list[list[str]],
) -> list[Connection]:
    """Every port used by several lines gets a main pipe (header).

    The main pipe leaves the port and runs straight on; the lines cut into it
    one after another, each at a tap of its own (a generated tee with three
    arms: from the port, on along the main pipe, out to the line).  The line
    that best continues straight on ends the main pipe; the others tap in
    ordered by how far along the flow their other end lies, the nearest
    closest to the port, so lines do not cross.  There is no limit on the
    number of lines.
    """
    uses: dict[tuple[str, str], list[tuple[int, int]]] = {}
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
        base_id = f"{GENERATED_PREFIX}T_{owner_id}_{pname}"
        tee_ids = [base_id] + [f"{base_id}_{k}" for k in range(2, len(us))]
        for t in tee_ids:
            if t in by_id:
                raise ValueError(f"duplicate id {t!r}")

        side = ports[pname].side
        level = owner.level
        settle = False
        if side in "EW":
            after = side == "E"
        else:
            others = [by_id.get(conns[ci].ends[1 - end][0]) for ci, end in us]
            right = sum(o is not None and order[o.id] > order[owner_id] for o in others)
            after = 2 * right >= len(others)
            if side == "S" and owner.level > 0:
                level -= 1  # hang the main pipe in the band below
            elif side == "N" and owner.level < top:
                level += 1  # or stand it in the band above
            else:
                settle = True
        trunk = "w" if after else "e"
        straight = "e" if after else "w"
        # on a tie, a main pipe lifted above an N port turns down, below an S up
        across = "sn" if side == "N" else "ns"

        def along(u: tuple[int, int]) -> int:
            """How far the line's other end lies along the main pipe."""
            o = by_id.get(conns[u[0]].ends[1 - u[1]][0])
            if o is None:
                return 0
            return (order[o.id] - order[owner_id]) * (1 if after else -1)

        def dy(u: tuple[int, int]) -> int:
            o = by_id.get(conns[u[0]].ends[1 - u[1]][0])
            return 0 if o is None else _sign(o.level - level)

        # the line that carries on straight ends the main pipe
        last = max(
            us, key=lambda u: (along(u) > 0 and dy(u) == 0, along(u) > 0, along(u))
        )
        taps = sorted((u for u in us if u is not last), key=along)

        at = order[owner_id] + (1 if after else 0)
        chain = [
            Equipment(
                t,
                "tee",
                level,
                {
                    "arms": trunk
                    + straight
                    + ({1: "n", -1: "s"}.get(dy(u)) or across[0])
                },
            )
            for t, u in zip(tee_ids, taps)
        ]
        eqs[at:at] = chain if after else chain[::-1]
        headers.append([t.id for t in chain])
        for tee in chain:
            by_id[tee.id] = tee
            if settle:
                pending.append(PendingTee(tee.id, owner_id, pname, side))

        first = us[0][0]
        kind = conns[first].kind
        split = roles == {0}
        prev = f"{owner_id}.{pname}"
        for tee in chain:
            here = f"{tee.id}.{trunk}"
            trunks.setdefault(first, []).append(
                Connection(prev, here, kind) if split else Connection(here, prev, kind)
            )
            prev = f"{tee.id}.{straight}"
        refs = {u: f"{t.id}.{t.params['arms'][2]}" for t, u in zip(chain, taps)}
        refs[last] = prev
        for ci, end in us:
            c = conns[ci]
            ref = refs[(ci, end)]
            conns[ci] = replace(c, src=ref) if end == 0 else replace(c, dst=ref)

    out: list[Connection] = []
    for ci, c in enumerate(conns):
        out += trunks.get(ci, [])
        out.append(c)
    return out
