"""Element (equipment) definitions: one module per element.

Symbols follow common Japanese P&ID practice (see README for the reference):
small bow-tie valves, pumps on a pedestal, capsule-shaped vessels, round-headed
exchangers, instrument bubbles and off-sheet connector flags.  Sizes are in grid
cells: valves and pumps are the smallest symbols (2 cells wide), vessels and
exchangers are several times larger.

Adding an element: create ``elements/<name>.py`` with an :class:`ElementSpec`
subclass, then add it to ``STANDARD_ELEMENTS`` below.  Users of the library can
instead register it in a flowsheet's own :class:`ElementLibrary`
(``Flowsheet.library.register``), in the global default library
(:func:`register_element`), or describe it as data with :class:`CustomSpec`.

Every element accepts the common parameters ``rotation`` (0/90/180/270,
counter-clockwise) and ``mirror`` (left-right flip, applied before rotating).
"""

from .base import (
    DEFAULT_LIBRARY,
    ElementLibrary,
    ElementSpec,
    Geometry,
    active_library,
    element_names,
    get_element,
    mirror_x,
    parse_port,
    register_element,
    rotate_ccw,
    transform,
)
from .column import ColumnSpec
from .connector import ConnectorSpec
from .custom import CustomSpec
from .filter_housing import FilterHousingSpec
from .header import HeaderSpec
from .heat_exchanger import HeatExchangerSpec
from .horizontal_drum import HorizontalDrumSpec
from .instrument import InstrumentSpec
from .pump import PumpSpec
from .tee import TeeSpec
from .valve import ValveSpec
from .vertical_drum import VerticalDrumSpec

STANDARD_ELEMENTS = (
    PumpSpec,
    ValveSpec,
    VerticalDrumSpec,
    ColumnSpec,
    HorizontalDrumSpec,
    HeatExchangerSpec,
    FilterHousingSpec,
    HeaderSpec,
    TeeSpec,
    ConnectorSpec,
    InstrumentSpec,
)
for _spec in STANDARD_ELEMENTS:
    DEFAULT_LIBRARY.register(_spec())

__all__ = [
    "DEFAULT_LIBRARY",
    "STANDARD_ELEMENTS",
    "ColumnSpec",
    "ConnectorSpec",
    "CustomSpec",
    "ElementLibrary",
    "ElementSpec",
    "FilterHousingSpec",
    "Geometry",
    "HeaderSpec",
    "HeatExchangerSpec",
    "HorizontalDrumSpec",
    "InstrumentSpec",
    "PumpSpec",
    "TeeSpec",
    "ValveSpec",
    "VerticalDrumSpec",
    "active_library",
    "element_names",
    "get_element",
    "mirror_x",
    "parse_port",
    "register_element",
    "rotate_ccw",
    "transform",
]
