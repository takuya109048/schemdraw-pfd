"""Visual settings: line kinds and the sheet theme.

Kept free of any drawing backend so the model can validate line kinds and a
flowsheet can carry its own line kinds in JSON.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any


@dataclass(frozen=True)
class LineStyle:
    """How one kind of line (``pipe``, ``signal`` ...) is drawn.

    ``process``: a process line.  Crossings are drawn explicitly on it (a
    gap in the vertical line) and vessels with nozzles get a flange tick where
    it meets them.
    ``main``: the line belongs to the main flow, so ``layout="flow"`` orders
    the equipment by it.  Utility lines (cooling water, steam ...) are process
    lines that are not ``main``: their equipment (valves, flags) is placed
    beside the item they serve instead.
    """

    lw: float = 1.1
    ls: str = "-"
    arrow: str | None = None  # e.g. "->" for a flow arrow at the line end
    process: bool = False
    main: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LineStyle:
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown line style keys {sorted(unknown)}")
        return cls(**d)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_LINE_STYLES: dict[str, LineStyle] = {
    "pipe": LineStyle(1.1, "-", "->", process=True),  # process line + flow arrow
    "utility": LineStyle(0.9, "-", "->", process=True, main=False),  # CW, steam ...
    "signal": LineStyle(0.8, "--"),  # instrument signal (dashed)
    "instrument": LineStyle(0.8, "-"),  # thin tapping line to an instrument
}

JP_FONTS = ("Yu Gothic", "Meiryo", "MS Gothic", "Noto Sans CJK JP", "IPAexGothic")


@dataclass(frozen=True)
class Theme:
    """Sheet-wide look (everything except the per-kind line styles)."""

    fontsize: float = 16
    fonts: tuple[str, ...] = JP_FONTS  # first installed one is used
    equipment_lw: float = 1.3
    crossing_gap: float = 0.3  # half-width of the break at a crossing (grid cells)
    background: str = "white"
    margin: float = 2.0  # cells between the drawing and the frame
    frame_lw: float = 2.0
    tag_underline: bool = True
    nozzle_ticks: bool = True
    spec_line_height: float = 1.45  # spec-row line pitch, in font heights


DEFAULT_THEME = Theme()
