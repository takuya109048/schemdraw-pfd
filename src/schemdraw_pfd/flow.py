"""Left-to-right order from the direction of flow (``layout="flow"``).

The user gives every item a level (its height on the plant) and draws the
lines; the order along X is derived from the process lines:

1. **Process graph**: items are nodes, process lines (line kinds with
   ``process=True``, e.g. ``pipe``) are "left of" arrows.  An arrow points the
   way the flow goes -- the main flow runs left to right -- unless the ports
   say otherwise: a line that leaves a west port or enters an east port runs
   leftwards (a supply coming in from the right, a mirrored valve).
2. **Recycles**: lines marked ``recycle`` and every arrow that closes a loop
   (found by a depth-first walk from the feeds, in declaration order) run
   against the main flow.  They do not take part in ordering.
3. **Ranks**: the remaining arrows form a DAG; each item gets its longest
   distance from a feed.  Items are ordered by rank, ties by declaration.
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


def plan_flow(
    eqs: list[Equipment],
    conns: list[Connection],
    geos: dict[str, Geometry],
    process_kinds: set[str],
) -> FlowPlan:
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

    rank = _longest_path(n, forward)
    process = {i for _, u, v in edges for i in (u, v)}
    keys: dict[int, list[int]] = {i: [rank[i], i] for i in process}
    _attach(keys, attach, eqs, geos)
    for i in range(n):  # unconnected items keep their declared neighbourhood
        keys.setdefault(i, [0, i])
    width = max(len(k) for k in keys.values())
    order = sorted(range(n), key=lambda i: keys[i] + [0] * (width - len(keys[i])))
    return FlowPlan(order, back, [by_ci[ci] for ci, _, _ in forward])


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


def _longest_path(n: int, forward: list[tuple[int, int, int]]) -> list[int]:
    """Rank = longest distance from a start, then every item is pulled right
    to just before its first successor (as late as possible).  So a valve on
    a feed line sits beside the unit it feeds, and a valve on a recycle line
    beside the item it serves, instead of next to the far-left feed."""
    rank = [0] * n
    indeg = [0] * n
    succ: dict[int, list[int]] = {i: [] for i in range(n)}
    for _, u, v in forward:
        succ[u].append(v)
        indeg[v] += 1
    starts = [i for i in range(n) if indeg[i] == 0]
    topo: list[int] = []
    ready = list(starts)
    left = list(indeg)
    while ready:
        u = ready.pop(0)
        topo.append(u)
        for v in succ[u]:
            rank[v] = max(rank[v], rank[u] + 1)
            left[v] -= 1
            if left[v] == 0:
                ready.append(v)
    for u in reversed(topo):
        if succ[u]:
            rank[u] = max(rank[u], min(rank[v] for v in succ[u]) - 1)
    return rank


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
