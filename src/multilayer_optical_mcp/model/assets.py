from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Tuple


class Direction(str, Enum):
    FORWARD = "forward"
    BACKWARD = "backward"


@dataclass(frozen=True)
class FiberType:
    type_variety: str
    loss_coef_db_per_km: float
    dispersion: float = 1.67e-05    # s/m/m
    effective_area: float = 83e-12  # m^2 (SSMF reference; GNPy derives gamma from it)
    pmd_coef: float = 1.265e-15     # s/sqrt(m)


@dataclass(frozen=True)
class Fiber:
    id: str
    a_end: str
    z_end: str
    length_km: float
    type_variety: str
    extra_loss_db: float = 0.0


@dataclass(frozen=True)
class Amplifier:
    id: str
    type_variety: str
    gain_db: float
    nf_db: float
    tilt_db: float = 0.0


@dataclass(frozen=True)
class ROADM:
    id: str
    target_pch_out_db: float = -20.0


@dataclass(frozen=True)
class Transceiver:
    id: str
    site: str
    supported_mode_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class TransceiverMode:
    id: str
    bitrate_gbps: float
    required_gsnr_db: float
    symbol_rate_baud: float
    channel_spacing_hz: float


@dataclass(frozen=True)
class OMS:
    id: str
    src_node_id: str
    dst_node_id: str
    elements: Tuple[str, ...]


@dataclass(frozen=True)
class Lightpath:
    id: str
    oms_sequence: Tuple[str, ...]
    mode_id: str
    center_freq_hz: float


@dataclass(frozen=True)
class Router:
    id: str
    site: str


@dataclass(frozen=True)
class IPLink:
    id: str
    a_router: str
    z_router: str
    lightpath_id: str


@dataclass(frozen=True)
class Service:
    id: str
    src_router: str
    dst_router: str
    demand_gbps: float
    working_path: Tuple[str, ...] = ()
    protection_path: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SRLG:
    id: str
    asset_ids: Tuple[str, ...]


@dataclass(frozen=True)
class RiskGroup:
    id: str
    asset_ids: Tuple[str, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # S1-1: frozen=True only blocks rebinding the attribute, not mutating a
        # dict stored in it. Wrap a *copy* of the incoming mapping in a read-only
        # MappingProxyType so the frozen risk group is genuinely immutable and the
        # caller's original dict is not a live backdoor.
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata))
        )
