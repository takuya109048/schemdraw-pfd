"""Column / tower (塔)."""

from __future__ import annotations

from .vertical_drum import VerticalDrumSpec


class ColumnSpec(VerticalDrumSpec):
    """Tall tower with packed beds (defaults: 4 wide, 20 tall, 2 beds).

    Same parameters and ports as :class:`VerticalDrumSpec`; the tag is placed
    beside the tower instead of inside it.
    """

    name = "column"
    defaults = {"width": 4, "height": 20, "beds": 2}
    label_pos = "left"
