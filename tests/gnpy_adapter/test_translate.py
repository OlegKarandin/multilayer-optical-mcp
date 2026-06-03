from pathlib import Path
from gnpy.tools.json_io import load_equipment, load_network


REPO_ROOT = Path(__file__).resolve().parents[2]
EQPT = REPO_ROOT / "eqpt" / "eqpt_config.json"
TOPO = REPO_ROOT / "topologies" / "toy_2span.json"


def test_toy_topology_loads_with_advanced_amp_model():
    eqpt = load_equipment(EQPT)
    network = load_network(TOPO, eqpt)
    from gnpy.core.elements import Edfa
    amps = [n for n in network.nodes if isinstance(n, Edfa)]
    assert amps
    for amp in amps:
        assert amp.params.type_variety != "variable_gain"
