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
    walk = _branch_order(n, _through_taps(forward, taps), process)
    keys: dict[int, list[int]] = {i: [k] for k, i in enumerate(walk)}
    _place_taps(keys, taps, conns, index, eqs, owners)
    _attach(keys, attach, eqs, geos)
    for i in range(n):  # unconnected items go last, as declared
        keys.setdefault(i, [n + i])
    width = max(len(k) for k in keys.values())
    order = sorted(range(n), key=lambda i: keys[i] + [0] * (width - len(keys[i])))
    return FlowPlan(order, back, [by_ci[ci] for ci, _, _ in forward])


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
) -> None:
    """Order each tap right after the item on its branch (the item it is
    joined to other than the main pipe's own port); a tap with no such item
    (the riser of a main pipe running both ways) goes after the owner."""
    partner: dict[int, int] = {}
    for c in conns:
        (a, _), (b, _) = c.ends
        for t, o in ((a, b), (b, a)):
            if t in owners and o not in owners and o in index and o != owners[t]:
                partner[index[t]] = index[o]
    for t in sorted(taps):
        p = partner.get(t, index.get(owners[eqs[t].id], -1))
        if p in keys:
            keys[t] = keys[p] + [1, t]


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
    n: int, forward: list[tuple[int, int, int]], process: set[int]
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
      last, so an item's own branch stays beside it.
    """
    succ: dict[int, list[int]] = {i: [] for i in range(n)}
    pred: dict[int, list[int]] = {i: [] for i in range(n)}
    for _, u, v in forward:
        succ[u].append(v)
        pred[v].append(u)
    tail: dict[int, int] = {}  # longest way on from an item

    def length(u: int) -> int:
        if u not in tail:
            tail[u] = 0  # (the arrows are loop-free; this only stops recursion)
            tail[u] = max((1 + length(v) for v in succ[u]), default=0)
        return tail[u]

    order: list[int] = []
    seen: set[int] = set()

    def visit(u: int) -> None:
        if u in seen:
            return
        seen.add(u)
        for p in pred[u]:
            visit(p)
        order.append(u)
        # on a tie a merge point (fed by other items too) waits for the
        # item's own branches
        for v in sorted(succ[u], key=lambda v: (length(v), len(pred[v]) > 1, v)):
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
