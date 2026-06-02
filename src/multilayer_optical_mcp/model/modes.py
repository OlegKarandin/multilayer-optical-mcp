from __future__ import annotations
from pathlib import Path
from typing import Iterable, Tuple
import yaml
from .assets import TransceiverMode


class ModeRegistry:
    def __init__(self, modes: Iterable[TransceiverMode]) -> None:
        self._by_id = {m.id: m for m in modes}

    def get(self, mode_id: str) -> TransceiverMode:
        return self._by_id[mode_id]

    def list(self) -> Tuple[TransceiverMode, ...]:
        return tuple(self._by_id.values())


def load_modulation_formats(yaml_path: Path) -> ModeRegistry:
    raw = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8"))
    spacing_hz = float(raw["channel_spacing_ghz"]) * 1e9
    baud = float(raw["symbol_rate_gbaud"]) * 1e9
    modes = []
    for f in raw["formats"]:
        bitrate = float(f["bitrate_gbps"])
        threshold = float(f["snr_threshold_db"])
        modes.append(TransceiverMode(
            id=f"{int(bitrate)}G@{threshold}dB",
            bitrate_gbps=bitrate,
            required_gsnr_db=threshold,
            symbol_rate_baud=baud,
            channel_spacing_hz=spacing_hz,
        ))
    return ModeRegistry(modes)
