from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class Channel:
    center_freq_hz: float
    slot_width_hz: float
    # S2-2: None means "use the adapter's tx_power_dbm default"; a float is a
    # literal launch power. Using 0.0 as the sentinel made a genuine 0 dBm (1 mW,
    # a common coherent launch) inexpressible.
    power_dbm: Optional[float]
    mode_id: str
    # S2-4: per-channel spectral shape. None means "use build_si_for_loading's
    # scalar fallback". Populate baud_rate_hz from the channel's TransceiverMode
    # so a loading state mixing formats at different symbol rates computes NLI
    # with each carrier's own shape rather than one broadcast baud.
    baud_rate_hz: Optional[float] = None
    roll_off: Optional[float] = None

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
