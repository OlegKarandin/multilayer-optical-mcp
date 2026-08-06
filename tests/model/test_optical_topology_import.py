import os
import subprocess
import sys
from pathlib import Path

from multilayer_optical_mcp.model.optical_topology_import import (
    optical_model_from_abstract_graph,
    split_link_into_spans,
)


def test_split_short_link_single_span():
    assert split_link_into_spans(37.0) == [37.0]


def test_split_balances_near_target():
    spans = split_link_into_spans(278.0, target_span_km=80.0)
    assert len(spans) == 4
    assert all(abs(s - 69.5) < 0.01 for s in spans)


def test_split_sum_is_exact():
    for length in (144.0, 208.0, 316.0, 353.0):
        spans = split_link_into_spans(length)
        assert abs(sum(spans) - length) < 1e-6


def test_split_respects_min_span():
    spans = split_link_into_spans(30.0, target_span_km=80.0, min_span_km=20.0)
    assert spans == [30.0]  # cannot subdivide below min_span_km


def _reg():
    from multilayer_optical_mcp.model.assets import TransceiverMode
    from multilayer_optical_mcp.model.modes import ModeRegistry

    return ModeRegistry([
        TransceiverMode(id="400G@7.1dB", bitrate_gbps=400.0, required_gsnr_db=7.1,
                        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9),
    ])


def _tiny_graph():
    return {
        "nodes": [{"id": 0}, {"id": 1}],
        "edges": [{
            "src": 0, "dst": 1, "length_km": 160.0, "num_spans": 2,
            "span_lengths_km": [80.0, 80.0], "fiber_type": "SSMF",
            "amplifier_nf_db": [5.5, 5.5],
        }],
    }


# ------------------------------------------------------- optical_model_from_abstract_graph


def test_optical_model_from_abstract_graph_matches_optical_half_of_full_build():
    """optical_model_from_abstract_graph must build the exact same optical layer
    as model_from_abstract_graph (same populate_optical call), just on a bare
    OpticalNetworkModel with no routers attached."""
    from multilayer_optical_mcp.model.network import NetworkModel
    from multilayer_optical_mcp.model.optical_network import OpticalNetworkModel
    from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph

    graph = _tiny_graph()
    optical = optical_model_from_abstract_graph(graph, modes=_reg())
    full = model_from_abstract_graph(graph, modes=_reg())

    assert type(optical) is OpticalNetworkModel
    assert not isinstance(optical, NetworkModel)
    assert not hasattr(optical, "_routers")

    assert set(optical._roadms) == set(full._roadms)
    assert set(optical._transceivers) == set(full._transceivers)
    assert {r.id for r in optical.list_oms()} == {r.id for r in full.list_oms()}
    assert optical.get_fiber("fiber_0_1_0").length_km == full.get_fiber(
        "fiber_0_1_0"
    ).length_km


def test_optical_model_from_abstract_graph_imports_without_ip_layer():
    """The real proof of the split: a fresh subprocess that imports only
    optical_topology_import and calls optical_model_from_abstract_graph must
    never pull in ip_assets or network — this module has no IP-layer import
    anywhere, module scope or lazy. In-process this is meaningless (pytest has
    already imported everything)."""
    code = (
        "import multilayer_optical_mcp.model.optical_topology_import as oti;"
        "import sys;"
        "bad=[m for m in sys.modules "
        "if m.endswith(('.ip_assets','.network','.ip_routing'))];"
        "assert not bad, bad;"
        "from multilayer_optical_mcp.model.modes import ModeRegistry;"
        "from multilayer_optical_mcp.model.assets import TransceiverMode;"
        "reg = ModeRegistry([TransceiverMode(id='400G@7.1dB', bitrate_gbps=400.0, "
        "required_gsnr_db=7.1, symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)]);"
        "graph = {'nodes': [{'id': 0}, {'id': 1}], 'edges': [{'src': 0, 'dst': 1, "
        "'length_km': 160.0, 'num_spans': 2, 'span_lengths_km': [80.0, 80.0], "
        "'fiber_type': 'SSMF', 'amplifier_nf_db': [5.5, 5.5]}]};"
        "n = oti.optical_model_from_abstract_graph(graph, modes=reg);"
        "assert len(n.list_oms()) == 2;"
        "bad2=[m for m in sys.modules "
        "if m.endswith(('.ip_assets','.network','.ip_routing'))];"
        "assert not bad2, bad2"
    )
    import multilayer_optical_mcp
    src_root = str(Path(multilayer_optical_mcp.__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [src_root] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
