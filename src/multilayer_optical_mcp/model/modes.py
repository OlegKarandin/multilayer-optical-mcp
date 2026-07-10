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
    # S2-4: symbol rate is read per-format so future multi-baud formats (1.2T at
    # 140 Gbaud alongside 400G at 87.5) each carry their own spectral shape. The
    # file-level symbol_rate_gbaud remains the default for formats that omit it.
    default_baud_gbaud = raw.get("symbol_rate_gbaud")
    modes = []
    for f in raw["formats"]:
        bitrate = float(f["bitrate_gbps"])
        threshold = float(f["snr_threshold_db"])
        baud_gbaud = f.get("symbol_rate_gbaud", default_baud_gbaud)
        if baud_gbaud is None:
            raise ValueError(
                f"format {bitrate:g}G has no symbol_rate_gbaud and the file "
                f"declares no default symbol_rate_gbaud"
            )
        modes.append(TransceiverMode(
            id=f"{int(bitrate)}G@{threshold}dB",
            bitrate_gbps=bitrate,
            required_gsnr_db=threshold,
            symbol_rate_baud=float(baud_gbaud) * 1e9,
            channel_spacing_hz=spacing_hz,
        ))
    return ModeRegistry(modes)
