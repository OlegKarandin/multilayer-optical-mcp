import json
from pathlib import Path

import pytest

from multilayer_optical_mcp.model.modes import load_modulation_formats
from multilayer_optical_mcp.server import MOD_FORMATS_YAML
from multilayer_optical_mcp.topology_loader import load_model_from_topology_file

TOPOLOGY = {
    "graph": {
        "nodes": [{"id": "a"}, {"id": "b"}],
        "edges": [{"src": "a", "dst": "b", "length_km": 120.0}],
    },
    "srlgs": [
        {"id": "srlg_ab_site", "asset_ids": ["roadm_a", "roadm_b"]},
    ],
}


@pytest.fixture
def topology_file(tmp_path: Path) -> Path:
    p = tmp_path / "toy.json"
    p.write_text(json.dumps(TOPOLOGY))
    return p


def test_load_model_from_topology_file_builds_optical_layer(topology_file: Path):
    modes = load_modulation_formats(MOD_FORMATS_YAML)
    model = load_model_from_topology_file(topology_file, modes=modes)
    oms_ids = {o.id for o in model.list_oms()}
    assert "oms_a_b" in oms_ids
    assert "oms_b_a" in oms_ids
    assert model.get_fiber("fiber_a_b_0") is not None


def test_load_model_from_topology_file_seeds_srlgs(topology_file: Path):
    modes = load_modulation_formats(MOD_FORMATS_YAML)
    model = load_model_from_topology_file(topology_file, modes=modes)
    srlg = model.get_srlg("srlg_ab_site")
    assert srlg.asset_ids == ("roadm_a", "roadm_b")


def test_load_model_from_topology_file_srlgs_default_to_empty(tmp_path: Path):
    p = tmp_path / "no_srlgs.json"
    p.write_text(json.dumps({"graph": TOPOLOGY["graph"]}))
    modes = load_modulation_formats(MOD_FORMATS_YAML)
    model = load_model_from_topology_file(p, modes=modes)
    assert model.list_srlgs() == ()


def test_load_model_from_topology_file_tolerates_utf8_bom(tmp_path: Path):
    # Windows tools (PowerShell Set-Content -Encoding utf8, Notepad's "UTF-8"
    # save) commonly prepend a BOM; the loader must not choke on one.
    p = tmp_path / "toy_bom.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps(TOPOLOGY).encode("utf-8"))
    modes = load_modulation_formats(MOD_FORMATS_YAML)
    model = load_model_from_topology_file(p, modes=modes)
    assert model.get_fiber("fiber_a_b_0") is not None
