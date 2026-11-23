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

from .air_cooler import AirCoolerSpec
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
from .blower import BlowerSpec
from .centrifuge import CentrifugeSpec
from .column import ColumnSpec
from .compressor import CompressorSpec
from .connector import ConnectorSpec
from .custom import CustomSpec
from .cyclone import CycloneSpec
from .ejector import EjectorSpec
from .filter_housing import FilterHousingSpec
from .furnace import FurnaceSpec
from .header import HeaderSpec
from .heat_exchanger import HeatExchangerSpec
from .horizontal_drum import HorizontalDrumSpec
from .instrument import InstrumentSpec
from .orifice import OrificeSpec
from .plate_heat_exchanger import PlateHeatExchangerSpec
from .pump import PumpSpec
from .reactor import ReactorSpec
from .safety_valve import SafetyValveSpec
from .static_mixer import StaticMixerSpec
from .steam_trap import SteamTrapSpec
from .strainer import StrainerSpec
from .tank import TankSpec
from .tee import TeeSpec
from .valve import ValveSpec
from .vertical_drum import VerticalDrumSpec

STANDARD_ELEMENTS = (
    PumpSpec,
    CompressorSpec,
    BlowerSpec,
    CentrifugeSpec,
    ValveSpec,
    SafetyValveSpec,
    StrainerSpec,
    SteamTrapSpec,
    OrificeSpec,
    StaticMixerSpec,
    EjectorSpec,
    VerticalDrumSpec,
    ColumnSpec,
    ReactorSpec,
    TankSpec,
    HorizontalDrumSpec,
    CycloneSpec,
    HeatExchangerSpec,
    PlateHeatExchangerSpec,
    AirCoolerSpec,
    FurnaceSpec,
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
    "AirCoolerSpec",
    "BlowerSpec",
    "CentrifugeSpec",
    "ColumnSpec",
    "CompressorSpec",
    "ConnectorSpec",
    "CustomSpec",
    "CycloneSpec",
    "EjectorSpec",
    "ElementLibrary",
    "ElementSpec",
    "FilterHousingSpec",
    "FurnaceSpec",
    "Geometry",
    "HeaderSpec",
    "HeatExchangerSpec",
    "HorizontalDrumSpec",
    "InstrumentSpec",
    "OrificeSpec",
    "PlateHeatExchangerSpec",
    "PumpSpec",
    "ReactorSpec",
    "SafetyValveSpec",
    "StaticMixerSpec",
    "SteamTrapSpec",
    "StrainerSpec",
    "TankSpec",
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
