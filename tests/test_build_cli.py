"""The offline operating-network builder CLI.

build_operating_network is monkeypatched throughout: this file tests the CLI's
wiring and its gating, not the packer (which tests/model/test_scenario.py
covers) and not GNPy (which would make these tests minutes long).
"""
import json
import sys
from pathlib import Path

import pytest

from multilayer_optical_mcp import build_cli
from multilayer_optical_mcp.model.modes import load_modulation_formats
from multilayer_optical_mcp.model.scenario import ScenarioReport, ScenarioResult
from multilayer_optical_mcp.model.solvers import SolverStatus
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph
from multilayer_optical_mcp.state_file import load_model_from_state_file
from multilayer_optical_mcp.topology_loader import MOD_FORMATS_YAML

TOPOLOGY = {
    "graph": {
        "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        "edges": [
            {"src": "a", "dst": "b", "length_km": 80.0},
            {"src": "b", "dst": "c", "length_km": 80.0},
            {"src": "c", "dst": "a", "length_km": 80.0},
        ],
    },
}


def _report(**over):
    base = dict(status=SolverStatus.SOLUTION, achieved_mean_util=0.4,
                achieved_max_util=0.6, n_demands=4, total_offered_gbps=400.0,
                transponders_used=4, unplaced_count=0, scale=800.0, limit="none",
                unplaced_reasons={})
    base.update(over)
    return ScenarioReport(**base)


def _patch_build(monkeypatch, report):
    """Stand in for the real (minutes-to-hours, GNPy-driven) build."""
    modes = load_modulation_formats(MOD_FORMATS_YAML)
    model = model_from_abstract_graph(TOPOLOGY["graph"], modes=modes)

    def fake_build(_model, **_kw):
        return ScenarioResult(model, [], report, None)

    monkeypatch.setattr(build_cli, "build_operating_network", fake_build)


@pytest.fixture
def topo(tmp_path: Path) -> Path:
    p = tmp_path / "topo.json"
    p.write_text(json.dumps(TOPOLOGY), encoding="utf-8")
    return p


def _run(monkeypatch, topo: Path, out: Path, *extra):
    monkeypatch.setattr(sys, "argv", ["multilayer-optical-mcp-build",
                                      "--topology", str(topo), "--out", str(out),
                                      *extra])
    build_cli.main()


def test_build_writes_a_state_file_the_server_can_load(monkeypatch, topo, tmp_path):
    _patch_build(monkeypatch, _report())
    out = tmp_path / "state.json"
    _run(monkeypatch, topo, out)

    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["format_version"] == 1
    assert doc["meta"]["topology_fingerprint"].startswith("sha256:")
    assert doc["meta"]["params"]["seed"] == 0
    assert doc["meta"]["report"]["limit"] == "none"
    # The whole point: the artifact round-trips back through the server's loader.
    modes = load_modulation_formats(MOD_FORMATS_YAML)
    assert load_model_from_state_file(topo, out, modes=modes) is not None


def test_build_refuses_to_write_when_no_demands_were_generated(monkeypatch, topo, tmp_path, capsys):
    # The silent-failure mode: gravity demands quantize to zero on a large
    # sparse topology, and the build "succeeds" with an empty network.
    _patch_build(monkeypatch, _report(n_demands=0))
    out = tmp_path / "state.json"
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, topo, out)
    assert exc.value.code != 0
    assert not out.exists()
    err = capsys.readouterr().err
    assert "--pair-density" in err and "--unit-gbps" in err


def test_build_refuses_to_write_on_no_solution(monkeypatch, topo, tmp_path):
    _patch_build(monkeypatch, _report(status=SolverStatus.NO_SOLUTION))
    out = tmp_path / "state.json"
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, topo, out)
    assert exc.value.code != 0
    assert not out.exists()


def test_build_warns_but_still_writes_when_some_demands_are_unplaced(
        monkeypatch, topo, tmp_path, capsys):
    # A partially-placed operating network is legitimate -- warn, don't fail.
    _patch_build(monkeypatch, _report(
        status=SolverStatus.PARTIAL, unplaced_count=8, limit="no_disjoint_pair",
        unplaced_reasons={"no disjoint feasible pair": 8}))
    out = tmp_path / "state.json"
    _run(monkeypatch, topo, out)
    assert out.exists()
    err = capsys.readouterr().err
    assert "no disjoint feasible pair" in err
    assert "8" in err


def test_build_records_the_parameters_it_was_given(monkeypatch, topo, tmp_path):
    _patch_build(monkeypatch, _report())
    out = tmp_path / "state.json"
    _run(monkeypatch, topo, out, "--seed", "7", "--pair-density", "0.02")
    params = json.loads(out.read_text(encoding="utf-8"))["meta"]["params"]
    assert params["seed"] == 7
    assert params["pair_density"] == 0.02
