"""Command line: ``uv run pfd spec.json -o out.svg``."""

from __future__ import annotations

import argparse
import inspect
import sys

from .model import Flowsheet


def _summary(obj: object) -> str:
    doc = inspect.getdoc(obj) or ""
    return doc.splitlines()[0] if doc else ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="pfd", description="Generate a PFD from a JSON spec"
    )
    ap.add_argument("spec", nargs="?", help="JSON flowsheet specification")
    ap.add_argument("-o", "--output", default="pfd.svg", help="svg/png/pdf output path")
    ap.add_argument("--no-labels", action="store_true")
    ap.add_argument("--no-frame", action="store_true", help="omit the sheet border")
    ap.add_argument(
        "--fontsize", type=float, help="tag/spec font size (points, default 20)"
    )
    ap.add_argument(
        "--layout",
        choices=("order", "flow"),
        help="override the spec's layout: declared order, or from the flow direction",
    )
    ap.add_argument(
        "--list-elements",
        action="store_true",
        help="list element types (including the spec's custom elements) and exit",
    )
    ap.add_argument(
        "--describe", metavar="TYPE", help="show the documentation of an element type"
    )
    args = ap.parse_args(argv)

    fs = Flowsheet.from_json(args.spec) if args.spec else Flowsheet()
    if args.list_elements:
        for name in fs.library.names():
            print(f"{name:18} {_summary(fs.library.get(name))}")
        return 0
    if args.describe:
        spec = fs.library.get(args.describe)
        print(inspect.getdoc(spec) or "(no documentation)")
        print("\ncommon params: rotation (0/90/180/270), mirror (true/false)")
        print(f"default ports: {sorted(fs.library.build(args.describe).ports)}")
        return 0
    if not args.spec:
        ap.error("the spec argument is required")
    if args.layout:
        fs.layout = args.layout

    diagram = fs.render(
        args.output,
        label=not args.no_labels,
        frame=not args.no_frame,
        fontsize=args.fontsize,
    )
    for w in diagram.warnings:
        print(f"warning: {w}", file=sys.stderr)
    bends = ", ".join(f"{c.src}->{c.dst}: {r.bends}" for c, r in diagram.routes)
    print(f"wrote {args.output} (bends: {bends})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
