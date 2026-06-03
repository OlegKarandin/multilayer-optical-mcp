from __future__ import annotations
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class Channel:
    center_freq_hz: float
    slot_width_hz: float
    power_dbm: float
    mode_id: str

    @property
    def low_hz(self) -> float:
        return self.center_freq_hz - self.slot_width_hz / 2

    @property
    def high_hz(self) -> float:
        return self.center_freq_hz + self.slot_width_hz / 2


@dataclass(frozen=True)
class LoadingState:
    channels: Tuple[Channel, ...] = ()

    @classmethod
    def empty(cls) -> "LoadingState":
        return cls(channels=())

    def union(self, other: "LoadingState") -> "LoadingState":
        for a in self.channels:
            for b in other.channels:
                if a.low_hz < b.high_hz and b.low_hz < a.high_hz:
                    raise ValueError(
                        f"spectrum clash: {a.center_freq_hz:.3e} vs {b.center_freq_hz:.3e}"
                    )
        return LoadingState(channels=self.channels + other.channels)
