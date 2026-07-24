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


def test_yaml_loader_reads_per_format_symbol_rate(tmp_path):
    """S2-4: a format that declares its own symbol_rate_gbaud overrides the
    file-level default; formats without one inherit the default."""
    yaml_text = (
        "channel_spacing_ghz: 100\n"
        "symbol_rate_gbaud: 87.5\n"
        "formats:\n"
        "  - bitrate_gbps: 400\n"
        "    snr_threshold_db: 7.1\n"
        "  - bitrate_gbps: 1200\n"
        "    snr_threshold_db: 20.0\n"
        "    symbol_rate_gbaud: 140.0\n"
    )
    p = tmp_path / "mf.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    by_bitrate = {m.bitrate_gbps: m for m in load_modulation_formats(p).list()}
    assert by_bitrate[400.0].symbol_rate_baud == 87.5e9   # inherits default
    assert by_bitrate[1200.0].symbol_rate_baud == 140.0e9  # per-format override


def test_yaml_loader_defaults_roll_off_when_absent():
    """modulation_formats.yaml has no roll_off key anywhere; every mode must
    still get the 0.15 scalar that used to be hardcoded in the adapter."""
    reg = load_modulation_formats(MOD_FORMATS_YAML)
    for m in reg.list():
        assert m.roll_off == 0.15


def test_yaml_loader_reads_per_format_roll_off(tmp_path):
    """Mirrors S2-4's symbol_rate_gbaud pattern: a format that declares its own
    roll_off overrides the file-level default; formats without one inherit it."""
    yaml_text = (
        "channel_spacing_ghz: 100\n"
        "symbol_rate_gbaud: 87.5\n"
        "roll_off: 0.15\n"
        "formats:\n"
        "  - bitrate_gbps: 400\n"
        "    snr_threshold_db: 7.1\n"
        "  - bitrate_gbps: 1200\n"
        "    snr_threshold_db: 20.0\n"
        "    roll_off: 0.3\n"
    )
    p = tmp_path / "mf.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    by_bitrate = {m.bitrate_gbps: m for m in load_modulation_formats(p).list()}
    assert by_bitrate[400.0].roll_off == 0.15    # inherits file-level default
    assert by_bitrate[1200.0].roll_off == 0.3    # per-format override
