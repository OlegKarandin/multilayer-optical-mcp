from pathlib import Path
import pytest
from multilayer_optical_mcp.model.modes import ModeRegistry, load_modulation_formats
from multilayer_optical_mcp.model.assets import TransceiverMode


REPO_ROOT = Path(__file__).resolve().parents[2]
MOD_FORMATS_YAML = REPO_ROOT / "modulation_formats.yaml"


def test_registry_lookup_and_list():
    a = TransceiverMode(id="A", bitrate_gbps=100.0, required_gsnr_db=12.0,
                        symbol_rate_baud=32e9, channel_spacing_hz=50e9)
    b = TransceiverMode(id="B", bitrate_gbps=200.0, required_gsnr_db=18.5,
                        symbol_rate_baud=32e9, channel_spacing_hz=50e9)
    reg = ModeRegistry([a, b])
    assert reg.get("A") is a
    assert reg.list() == (a, b)
    with pytest.raises(KeyError):
        reg.get("nope")


def test_yaml_loader_constructs_all_eleven_modes():
    reg = load_modulation_formats(MOD_FORMATS_YAML)
    modes = reg.list()
    assert len(modes) == 11
    bitrates = sorted(m.bitrate_gbps for m in modes)
    assert bitrates[0] == 300.0
    assert bitrates[-1] == 800.0


def test_yaml_loader_populates_global_baud_and_spacing_on_every_mode():
    reg = load_modulation_formats(MOD_FORMATS_YAML)
    for m in reg.list():
        assert m.symbol_rate_baud == 87.5e9
        assert m.channel_spacing_hz == 100e9


def test_yaml_loader_snr_threshold_matches_file():
    reg = load_modulation_formats(MOD_FORMATS_YAML)
    by_bitrate = {m.bitrate_gbps: m for m in reg.list()}
    assert by_bitrate[300.0].required_gsnr_db == 4.8
    assert by_bitrate[400.0].required_gsnr_db == 7.1
    assert by_bitrate[800.0].required_gsnr_db == 15.1
