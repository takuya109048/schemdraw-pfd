"""Header (ヘッダー): a main pipe with upward branches."""

from __future__ import annotations

from ..geometry import Polyline, Port, Shape, outline_distance
from .base import ElementSpec, Geometry, active_library


class HeaderSpec(ElementSpec):
    """Horizontal main pipe with branches that all rise in +Y.

    params:
      branches: number of branch pipes (default 3)
      pitch: base (equal) grid spacing between branches (default 2)
      branch_elements: ``{index: item}`` where ``item`` is an element type name,
          ``{"type": ..., "params": {...}}``, or a list of those.  The elements are
          inserted on the branch line, stacked from the main pipe towards the branch
          end (the last one is the "branch destination").

    Branch spacing is equal by default; wherever inserted elements would collide,
    the spacing (and so the main pipe length) grows by exactly the width they
    occupy plus one cell of clearance.

    Ports: ``main_w`` / ``main_e`` (ends of the main pipe), ``b<i>`` (top of branch
    ``i``) and ``m<i>`` (underside of the main pipe below branch ``i``).  Lines
    on plain branches (no inserted elements) may be swapped to avoid crossings.
    """

    name = "header"

    def build(self, params):
        n = int(params.get("branches", 3))
        if n < 1:
            raise ValueError("header needs at least one branch")
        pitch = int(params.get("pitch", 2))
        stacks = self._stacks(params.get("branch_elements") or {}, n)

        half = [
            max((g.width // 2 for g in stacks.get(i, [])), default=0) for i in range(n)
        ]
        xs = [max(1, half[0])]
        for i in range(1, n):
            xs.append(xs[-1] + max(pitch, half[i - 1] + half[i] + 1))
        width = xs[-1] + max(1, half[-1])
        top = max([2] + [1 + sum(g.height + 1 for g in s) for s in stacks.values()])

        shapes: list[Shape] = [Polyline(((0, 0), (width, 0)))]
        ports = {"main_w": Port("W", 0), "main_e": Port("E", 0)}
        for i, x in enumerate(xs):
            ports[f"b{i}"] = Port("N", x)
            ports[f"m{i}"] = Port("S", x)
            y = 0.0  # end of the pipe drawn so far on this branch
            base = 1
            for g in stacks.get(i, []):
                lo = next(p for p in g.ports.values() if p.side == "S")
                hi = next(p for p in g.ports.values() if p.side == "N")
                shapes += g.translated(x - lo.offset, base)
                below = outline_distance(g.shapes, (lo.offset, -1.0), (0, 1))
                above = outline_distance(g.shapes, (hi.offset, g.height + 1.0), (0, -1))
                y_lo = base + below - 1.0
                y_hi = base + g.height + 1.0 - above
                shapes.append(Polyline(((x, y), (x, y_lo))))
                y = y_hi
                base += g.height + 1
            shapes.append(Polyline(((x, y), (x, top))))
        # plain branches (no inserted elements) serve alike
        plain = [i for i in range(n) if not stacks.get(i)]
        groups = [[f"b{i}" for i in plain], [f"m{i}" for i in range(n)]]
        return Geometry(width, top, shapes, ports, label_pos="none", swappable=groups)

    @staticmethod
    def _stacks(raw: dict, n: int) -> dict[int, list[Geometry]]:
        """Geometries of the elements inserted on each branch, bottom to top."""
        stacks: dict[int, list[Geometry]] = {}
        for key, value in raw.items():
            idx = int(key)
            if not 0 <= idx < n:
                raise ValueError(f"branch index {idx} out of range 0..{n - 1}")
            for item in value if isinstance(value, list) else [value]:
                tname, tparams = (
                    (item, {})
                    if isinstance(item, str)
                    else (item["type"], item.get("params", {}))
                )
                geo = active_library().build(
                    tname, {"orientation": "vertical", **tparams}
                )
                south = [p for p in geo.ports.values() if p.side == "S"]
                north = [p for p in geo.ports.values() if p.side == "N"]
                if not south or not north or south[0].offset != north[0].offset:
                    raise ValueError(
                        f"{tname!r} needs aligned S and N ports to sit on a branch"
                    )
                if geo.width != 2 * south[0].offset:
                    raise ValueError(f"{tname!r} must be centred on its S/N ports")
                stacks.setdefault(idx, []).append(geo)
        return stacks
