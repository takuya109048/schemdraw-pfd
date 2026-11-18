"""Shared helpers: example sheets are built once per test session."""

from functools import cache

from schemdraw_pfd import Diagram, Flowsheet


@cache
def built(name: str) -> Diagram:
    """The example ``examples/<name>.json`` laid out and routed (cached: a
    build of the large examples takes tens of seconds)."""
    return Flowsheet.from_json(f"examples/{name}.json").build()
