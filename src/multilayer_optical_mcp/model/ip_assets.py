from __future__ import annotations
from dataclasses import dataclass
from typing import Tuple


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
