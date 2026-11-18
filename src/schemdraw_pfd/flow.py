"""Left-to-right order from the direction of flow (``layout="flow"``).

Every item has a level (its height on the plant: given, or worked out from
the lines by :mod:`.levels`) and the user draws the lines; the order along X
is derived from the process lines:

1. **Process graph**: items are nodes, process lines (line kinds with
   ``process=True``, e.g. ``pipe``) are "left of" arrows.  An arrow points the
   way the flow goes -- the main flow runs left to right -- unless the ports
   say otherwise: a line that leaves a west port or enters an east port runs
   leftwards (a supply coming in from the right, a mirrored valve).
2. **Recycles**: lines marked ``recycle`` and every arrow that closes a loop
   (found by a depth-first walk from the feeds, in declaration order) run
   against the main flow.  They do not take part in ordering.
3. **Order**: the remaining arrows form a DAG, walked depth first from the
   feed of the longest path: branch by branch, the main flow last (see
   :func:`_branch_order`).
4. **Attachments**: items joined only by non-process lines (instruments,
   controllers) are placed right next to the item they are attached to, on
   the side their line leaves from.

The forward arrows are also returned: the layout keeps each destination port
at or to the right of its source port (the flow never runs left), while items
on different levels may share X (one above the other).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .elements import Geometry
from .netlist import Connection, Equipment


@dataclass(frozen=True)
class Arrow:
    """``a`` lies left of ``b``; the line joins ``a.a_port`` and ``b.b_port``."""

    ci: int
    a: int
    a_port: str
    b: int
    b_port: str


@dataclass
class FlowPlan:
    order: list[int]  # equipment indices, left to right
    back: set[int] = field(default_factory=set)  # recycle connection indices
    forward: list[Arrow] = field(default_factory=list)
    main: list[int] = field(default_factory=list)  # the main flow, feed first


def plan_flow(
    eqs: list[Equipment],
    conns: list[Connection],
    geos: dict[str, Geometry],
    process_kinds: set[str],
    owners: dict[str, str] | None = None,
) -> FlowPlan:
    """``owners``: the taps of generated main pipes (tee id -> the item whose
    shared port the main pipe leaves).  Taps are no equipment: the order is
    found as if each main pipe were plain lines from its port to the items
    tapped off it, then every tap goes right after the item it serves -- so a
    tap can stand straight above (or beside) that item's nozzle, and parallel
    trains taken off one main pipe keep their declared order."""
    n = len(eqs)
    index = {e.id: i for i, e in enumerate(eqs)}
    arrows: list[Arrow] = []
    attach: list[tuple[int, int, str, int, str]] = []  # ci, a, a_port, b, b_port
    for ci, c in enumerate(conns):
        (s, sp), (d, dp) = c.ends
        if s not in index or d not in index:
            continue  # ends on an inline item that is not expanded here
        u, v = index[s], index[d]
        if u == v or sp not in geos[s].ports or dp not in geos[d].ports:
            continue
        if c.kind not in process_kinds:
            attach.append((ci, u, sp, v, dp))
            continue
        leftwards = _runs_left(geos[s].ports[sp].side, geos[d].ports[dp].side)
        arrows.append(Arrow(ci, v, dp, u, sp) if leftwards else Arrow(ci, u, sp, v, dp))
    edges = [(a.ci, a.a, a.b) for a in arrows]

    by_ci = {a.ci: a for a in arrows}
    back = {ci for ci, _, _ in edges if conns[ci].recycle}
    out: dict[int, list[tuple[int, int]]] = {i: [] for i in range(n)}
    indeg = [0] * n
    for ci, u, v in edges:
        if ci not in back:
            out[u].append((ci, v))
            indeg[v] += 1
    _close_loops(n, out, indeg, back)
    forward = [(ci, u, v) for ci, u, v in edges if ci not in back]

    owners = owners or {}
    taps = {index[t] for t in owners if t in index}
    process = {i for _, u, v in edges for i in (u, v)} - taps
    # how high on its vessel each line leaves (top nozzle highest)
    rise: dict[int, int] = {}
    for a in arrows:
        g = geos[eqs[a.a].id]
        if not g.nozzles:
            continue  # tees, pumps ...: their outlets are no draw heights
        port = g.ports[a.a_port]
        rise[a.ci] = {"N": 1 << 20, "S": -(1 << 20)}.get(port.side, port.offset)
    through = _through_taps(forward, taps)
    walk = _branch_order(n, through, process, rise)
    keys: dict[int, list[int]] = {i: [k] for k, i in enumerate(walk)}
    _place_taps(keys, taps, conns, index, eqs, owners, geos)
    _attach(keys, attach, eqs, geos)
    for i in range(n):  # unconnected items go last, as declared
        keys.setdefault(i, [n + i])
    width = max(len(k) for k in keys.values())
    order = sorted(range(n), key=lambda i: keys[i] + [0] * (width - len(keys[i])))
    main = _main_path(n, through, process, rise)
    return FlowPlan(order, back, [by_ci[ci] for ci, _, _ in forward], main)


def _main_path(
    n: int,
    forward: list[tuple[int, int, int]],
    process: set[int],
    rise: dict[int, int],
) -> list[int]:
    """The main flow: from the feed with the longest way on, always on to
    the item with the longest way on (between equals the one leaving the
    lowest nozzle -- a column's bottoms), as :func:`_branch_order` puts last."""
    succ: dict[int, list[tuple[int, int]]] = {i: [] for i in range(n)}
    pred = [0] * n
    for ci, u, v in forward:
        succ[u].append((v, rise.get(ci, 0)))
        pred[v] += 1
    tail: dict[int, int] = {}

    def length(u: int) -> int:
        if u not in tail:
            tail[u] = 0
            tail[u] = max((1 + length(v) for v, _ in succ[u]), default=0)
        return tail[u]

    feeds = sorted((i for i in process if not pred[i]), key=lambda i: (-length(i), i))
    path: list[int] = []
    u = feeds[0] if feeds else -1
    while u >= 0 and u not in path:
        path.append(u)
        nxt = max(succ[u], key=lambda e: (length(e[0]), -e[1], -e[0]), default=None)
        u = nxt[0] if nxt else -1
    return path


def split_rows(
    plan: FlowPlan,
    majors: set[int],
    edge: set[int],
    min_head: int = 3,
) -> list[list[int]]:
    """Fold a long flow into rows, one major unit (a column) per row.

    Only a main flow that runs through two or more major units is folded:
    it is cut where it leaves a major unit for the next (right after the
    unit and its own branches -- the next row starts with what feeds the
    next unit), and before the first unit when at least ``min_head`` items
    other than edge flags (``edge``) come before it (a feed preheat train
    gets a row of its own).  Rows are runs of ``plan.order``.
    """
    order = plan.order
    pos = {i: k for k, i in enumerate(order)}
    on_main = [i for i in plan.main if i in majors]
    if len(on_main) < 2:
        return [list(order)]
    cuts: set[int] = set()
    first = plan.main.index(on_main[0])
    if first > 0:
        at = pos[plan.main[first - 1]] + 1
        if sum(1 for i in order[:at] if i not in edge) >= min_head:
            cuts.add(at)
    for m in on_main[:-1]:
        k = plan.main.index(m)
        if k + 1 < len(plan.main):
            cuts.add(pos[plan.main[k + 1]])
    bounds = [0, *sorted(c for c in cuts if 0 < c < len(order)), len(order)]
    return [order[a:b] for a, b in zip(bounds, bounds[1:]) if b > a]


def _through_taps(
    forward: list[tuple[int, int, int]], taps: set[int]
) -> list[tuple[int, int, int]]:
    """The arrows with every run of taps taken out: ``u -> tap -> ... -> w``
    becomes ``u -> w`` for each item ``w`` the run reaches."""
    if not taps:
        return forward
    succ: dict[int, list[int]] = {}
    for _, u, v in forward:
        succ.setdefault(u, []).append(v)
    out = []
    for ci, u, v in forward:
        if u in taps:
            continue
        stack, seen = [v], set()
        while stack:
            w = stack.pop()
            if w in seen:
                continue
            seen.add(w)
            if w in taps:
                stack += succ.get(w, [])
            else:
                out.append((ci, u, w))
    return out


def _place_taps(
    keys: dict[int, list[int]],
    taps: set[int],
    conns: list[Connection],
    index: dict[str, int],
    eqs: list[Equipment],
    owners: dict[str, str],
    geos: dict[str, Geometry],
) -> None:
    """Order each tap right after the item on its branch (the item it is
    joined to other than the main pipe's own port) -- or right in front of
    it when the branch reaches the item's west face, so the line drops (or
    rises) beside the item straight into the nozzle; a tap with no such item
    (the riser of a main pipe running both ways) goes after the owner."""
    partner: dict[int, tuple[int, int]] = {}
    for c in conns:
        for (t, _), (o, op) in (c.ends, c.ends[::-1]):
            if t in owners and o not in owners and o in index and o != owners[t]:
                side = geos[o].ports[op].side if op in geos[o].ports else ""
                partner[index[t]] = (index[o], -1 if side == "W" else 1)
    for t in sorted(taps):
        p, where = partner.get(t, (index.get(owners[eqs[t].id], -1), 1))
        if p in keys:
            keys[t] = keys[p] + [where, t]


def _runs_left(out_side: str, in_side: str) -> bool:
    """Does a line leaving a port on ``out_side`` and entering one on
    ``in_side`` run leftwards?  The source port decides, then the
    destination port; between top/bottom ports the flow runs to the right."""
    if out_side in "WE":
        return out_side == "W"
    if in_side in "WE":
        return in_side == "E"
    return False


def _close_loops(
    n: int, out: dict[int, list[tuple[int, int]]], indeg: list[int], back: set[int]
) -> None:
    """Mark every arrow that closes a loop as a recycle (depth-first walk
    starting from the feeds, i.e. items nothing flows into)."""
    color = [0] * n  # 0 new, 1 on the current path, 2 done
    roots = [i for i in range(n) if indeg[i] == 0] + list(range(n))
    for root in roots:
        if color[root]:
            continue
        color[root] = 1
        stack = [(root, iter(out[root]))]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                color[node] = 2
                stack.pop()
                continue
            ci, v = nxt
            if color[v] == 1:
                back.add(ci)
            elif color[v] == 0:
                color[v] = 1
                stack.append((v, iter(out[v])))
    for u in out:  # drop the recycles from the graph
        out[u] = [(ci, v) for ci, v in out[u] if ci not in back]


def _branch_order(
    n: int,
    forward: list[tuple[int, int, int]],
    process: set[int],
    rise: dict[int, int] | None = None,
) -> list[int]:
    """Left-to-right order that keeps every branch together.

    A depth-first walk of the forward arrows from the first feed:

    * an item comes after everything that flows into it (pulled in right
      before it when not placed yet -- so a valve on a second feed or on a
      recycle line sits beside the unit it serves, not at the far left);
    * after an item its branches follow one by one, short ones first and the
      longest (the main flow) last.  A product train (stripper, pump, cooler)
      thus stays between its column and the next main unit instead of being
      mixed with other trains.  Between branches of equal length, one that
      leads to a merge point (an item other lines flow into as well) comes
      last, so an item's own branch stays beside it;
    * a merge point fed by branches the walk reaches anyway (parallel trains
      off one feed, merging into one product) waits for the last of them, so
      each train stays together.
    """
    succ: dict[int, list[int]] = {i: [] for i in range(n)}
    pred: dict[int, list[int]] = {i: [] for i in range(n)}
    height: dict[tuple[int, int], int] = {}  # (u, v) -> how high it leaves u
    for ci, u, v in forward:
        succ[u].append(v)
        pred[v].append(u)
        h = (rise or {}).get(ci, 0)
        height[(u, v)] = max(height.get((u, v), h), h)
    tail: dict[int, int] = {}  # longest way on from an item

    def length(u: int) -> int:
        if u not in tail:
            tail[u] = 0  # (the arrows are loop-free; this only stops recursion)
            tail[u] = max((1 + length(v) for v in succ[u]), default=0)
        return tail[u]

    up: dict[int, set[int]] = {}  # everything that flows into an item

    def ancestors(u: int) -> set[int]:
        if u not in up:
            up[u] = set()  # (loop-free; this only stops recursion)
            up[u] = set(pred[u]).union(*(ancestors(p) for p in pred[u]))
        return up[u]

    order: list[int] = []
    seen: set[int] = set()

    def waits(v: int) -> bool:
        """A merge point waits for the other branches flowing into it that
        the walk will reach on its own (parallel trains off one feed): it
        is placed after the last of them, instead of pulling them in --
        and with them the other trains -- in front of this branch."""
        return any(
            p not in seen and ancestors(p) & seen for p in pred[v] if len(pred[v]) > 1
        )

    def visit(u: int) -> None:
        if u in seen:
            return
        seen.add(u)
        for p in pred[u]:
            visit(p)
        order.append(u)
        # on a tie a merge point (fed by other items too) waits for the
        # item's own branches
        # the main flow: the longest way on (between equals the one from
        # the lowest nozzle -- a column's bottoms)
        # (none when several are alike: parallel trains keep their order)
        ranks = sorted(((length(v), -height[(u, v)]), v) for v in succ[u])
        main = -1
        if len(ranks) > 1 and ranks[-1][0] != ranks[-2][0]:
            main = ranks[-1][1]

        def branch(v: int) -> tuple:
            # the main flow last; the others from the highest nozzle down (a
            # column's overhead beside it, then its side draws top to
            # bottom), then short ones first
            return v == main, height[(u, v)], length(v), len(pred[v]) > 1, v

        for v in sorted(succ[u], key=branch):
            if not waits(v):
                visit(v)

    feeds = sorted((i for i in process if not pred[i]), key=lambda i: (-length(i), i))
    for i in feeds[:1] + sorted(process):  # main feed first, then leftovers
        visit(i)
    return order


def _attach(
    keys: dict[int, list[int]],
    attach: list[tuple[int, int, str, int, str]],
    eqs: list[Equipment],
    geos: dict[str, Geometry],
) -> None:
    """Order items joined only by instrument / signal lines next to their
    partner: before it when the line leaves the partner westwards (or, from a
    top/bottom port, reaches the item's east face), otherwise after it."""
    pending = list(attach)
    while pending:
        left = []
        for ci, a, ap, b, bp in pending:
            for anchor, aport, item, iport in ((a, ap, b, bp), (b, bp, a, ap)):
                if anchor in keys and item not in keys:
                    side = geos[eqs[anchor].id].ports[aport].side
                    own = geos[eqs[item].id].ports[iport].side
                    before = side == "W" if side in "WE" else own == "E"
                    keys[item] = keys[anchor] + [-1 if before else 1, item]
                    break
            else:
                if not (a in keys and b in keys):
                    left.append((ci, a, ap, b, bp))
        if len(left) == len(pending):
            break  # the rest hangs off nothing that is placed
        pending = left
