"""Component A — the pure gravity demand generator (model/traffic.py).

No solver, no GNPy: generate_demands is a deterministic function of (model, seed,
params). A star topology (hub H, leaves A/B/C) makes the gravity math checkable by
hand: mass = node degree, so hub-incident pairs dominate leaf-leaf pairs.
"""
import json
from collections import Counter
from pathlib import Path

from multilayer_optical_mcp.model.assets import (
    ROADM, FiberType, Fiber, OMS, Router,
)
from multilayer_optical_mcp.model.modes import ModeRegistry, load_modulation_formats
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph
from multilayer_optical_mcp.model.traffic import generate_demands

_REPO = Path(__file__).resolve().parents[2]


def _modes() -> ModeRegistry:
    from multilayer_optical_mcp.model.assets import TransceiverMode
    return ModeRegistry([
        TransceiverMode(id="400G", bitrate_gbps=400.0, required_gsnr_db=7.1,
                        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9),
    ])


def _star_model(leaf_len_km: float = 100.0) -> NetworkModel:
    """Hub H joined to leaves A, B, C by equal-length edges. Undirected degrees:
    H=3, leaves=1. Distances: H-leaf = leaf_len, leaf-leaf = 2*leaf_len."""
    n = NetworkModel(modes=_modes())
    n.register_fiber_type(FiberType("SSMF", 0.2))
    for node in ("H", "A", "B", "C"):
        n.add_roadm(ROADM(id=f"roadm_{node}"))
        n.add_router(Router(id=f"router_{node}", site=node))
    for leaf in ("A", "B", "C"):
        fid = f"fiber_H_{leaf}"
        n.add_fiber(Fiber(fid, f"roadm_H", f"roadm_{leaf}", leaf_len_km, "SSMF"))
        n.add_oms(OMS(f"oms_H_{leaf}", "H", leaf, (f"roadm_H", fid)))
    return n


def _pair_gbps(demands):
    """Aggregate offered gbps per unordered node pair."""
    agg = Counter()
    for d in demands:
        agg[frozenset((d["src"], d["dst"]))] += d["demand_gbps"]
    return agg


# ------------------------------------------------------------------- determinism

def test_same_seed_and_params_identical():
    n = _star_model()
    a = generate_demands(n, seed=0, scale=3000.0)
    b = generate_demands(n, seed=0, scale=3000.0)
    assert a == b


def test_seed_only_enters_through_jitter():
    """With jitter disabled, output is independent of seed (seed's ONLY effect is
    deterministic mass jitter)."""
    n = _star_model()
    a = generate_demands(n, seed=0, scale=3000.0, mass_jitter=0.0)
    b = generate_demands(n, seed=7, scale=3000.0, mass_jitter=0.0)
    assert a == b


def test_different_seeds_give_reproducible_variety():
    n = _star_model()
    a = generate_demands(n, seed=0, scale=3000.0, mass_jitter=0.5)
    b = generate_demands(n, seed=1, scale=3000.0, mass_jitter=0.5)
    assert a != b                       # variety
    assert a == generate_demands(n, seed=0, scale=3000.0, mass_jitter=0.5)  # reproducible


# ----------------------------------------------------------------- gravity shape

def test_hub_pair_outweighs_peripheral_pair():
    n = _star_model()
    agg = _pair_gbps(generate_demands(n, seed=0, scale=3000.0, mass_jitter=0.0))
    hub_pair = agg[frozenset(("H", "A"))]        # mass 3*1, dist 100
    periph_pair = agg[frozenset(("A", "B"))]     # mass 1*1, dist 200
    assert hub_pair > periph_pair


# ------------------------------------------------------------------ quantization

def test_every_demand_is_a_unit_multiple_within_line_rate():
    n = _star_model()
    ds = generate_demands(n, seed=0, scale=3000.0, unit_gbps=100.0)
    assert ds
    for d in ds:
        assert d["demand_gbps"] % 100.0 == 0.0
        assert 0 < d["demand_gbps"] <= 800.0


# -------------------------------------------------------------------- protection

def test_protected_fraction_count_and_hub_priority():
    n = _star_model()
    frac = 0.3
    ds = generate_demands(n, seed=0, scale=3000.0, protected_fraction=frac,
                          mass_jitter=0.0)
    protected = [d for d in ds if d["protected"]]
    assert len(protected) == round(frac * len(ds))
    # hub-incident pairs carry the most gravity, so protection lands on them first
    assert all("H" in (d["src"], d["dst"]) for d in protected)


# ------------------------------------------------------- frozen german_17 fixture

def test_generate_demands_reproduces_frozen_german_17_fixture():
    """The seeded generator reproduces the checked-in operating-scenario demand set
    byte-for-byte — the durable, GNPy-free replacement for hand-written demands."""
    fix = json.loads(
        (_REPO / "tests/fixtures/german_17_demands_seed0.json").read_text(encoding="utf-8"))
    graph = json.loads(
        (_REPO / "topologies/german_17.json").read_text(encoding="utf-8"))
    modes = load_modulation_formats(_REPO / "modulation_formats.yaml")
    model = model_from_abstract_graph(graph, modes=modes)

    got = generate_demands(model, seed=fix["seed"], scale=fix["scale"])
    assert got == fix["demands"]
