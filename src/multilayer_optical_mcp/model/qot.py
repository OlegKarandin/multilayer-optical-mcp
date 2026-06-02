from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class QoTState:
    gsnr_db: float
    osnr_db: float
    margin_db: float
    limiting_element_id: Optional[str] = None

    @property
    def mode_feasible(self) -> bool:
        return self.margin_db >= 0


@dataclass(frozen=True)
class ElementSnapshot:
    element_id: str
    gsnr_db_after: float
    osnr_db_after: float
    gsnr_delta_db: float
    ase_contribution_db: float
    nli_contribution_db: float


@dataclass(frozen=True)
class QoTBreakdown:
    snapshots: Tuple[ElementSnapshot, ...]
    limiting_element_id: Optional[str]
