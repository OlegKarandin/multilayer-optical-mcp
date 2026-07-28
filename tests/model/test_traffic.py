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


# --------------------------------------------------------------- pair_density

def test_pair_density_same_seed_identical():
    n = _star_model()
    a = generate_demands(n, seed=0, scale=3000.0, pair_density=0.3)
    b = generate_demands(n, seed=0, scale=3000.0, pair_density=0.3)
    assert a == b


def test_pair_density_different_seeds_reproducible_variety():
    n = _star_model()
    a = generate_demands(n, seed=0, scale=3000.0, pair_density=0.3)
    b = generate_demands(n, seed=1, scale=3000.0, pair_density=0.3)
    assert a != b
    assert a == generate_demands(n, seed=0, scale=3000.0, pair_density=0.3)


def test_pair_density_weighted_inclusion_favors_hub_pairs():
    """At low density, hub-incident pairs (higher gravity weight) should survive
    the per-pair Bernoulli filter more often than a peripheral leaf-leaf pair,
    across many seeds. mass_jitter=0.0 holds weights fixed so only the Bernoulli
    draw varies between seeds."""
    n = _star_model()
    hub_survivals = 0
    periph_survivals = 0
    n_seeds = 50
    for s in range(n_seeds):
        agg = _pair_gbps(generate_demands(
            n, seed=s, scale=3000.0, pair_density=0.15, mass_jitter=0.0))
        if agg.get(frozenset(("H", "A")), 0.0) > 0.0:
            hub_survivals += 1
        if agg.get(frozenset(("A", "B")), 0.0) > 0.0:
            periph_survivals += 1
    assert hub_survivals > periph_survivals


def test_pair_density_preserves_total_offered_volume():
    """total_w is renormalized over surviving pairs only, so total offered
    volume across all demands stays close to `scale` regardless of density,
    rather than shrinking as pairs drop. Each surviving pair rounds its own
    share to the nearest unit_gbps independently, so the aggregate tolerance
    must scale with how many distinct pairs survived (worst case: each off by
    up to half a unit), not a flat one-unit bound."""
    n = _star_model()
    scale = 3000.0
    unit_gbps = 100.0
    for density in (0.2, 0.5, 1.0):
        for s in range(5):
            demands = generate_demands(
                n, seed=s, scale=scale, pair_density=density, unit_gbps=unit_gbps)
            total = sum(d["demand_gbps"] for d in demands)
            if total == 0.0:
                continue      # degenerate all-dropped draw; covered separately
            n_pairs = len({(d["src"], d["dst"]) for d in demands})
            assert abs(total - scale) <= unit_gbps * 0.5 * n_pairs


def test_pair_density_degenerate_all_dropped_returns_empty_list():
    """A vanishingly small pair_density must, for at least one seed, drop every
    pair and return [] rather than raising."""
    n = _star_model()
    results = [generate_demands(n, seed=s, scale=3000.0, pair_density=1e-6)
              for s in range(20)]
    assert any(r == [] for r in results)


# ------------------------------------------------- protection_constraints

def test_protection_constraints_attach_only_to_protected_demands():
    """Regression for the audit's Important finding: generate_demands must be
    able to request a non-default basis/level for every protected demand it
    emits -- otherwise every synthesized operating network is stuck at
    physical/link disjointness with no way to ask for srlg/risk_group."""
    n = _star_model()
    constraints = {"basis": "risk_group", "level": "link"}
    demands = generate_demands(n, seed=0, scale=3000.0, protected_fraction=0.5,
                               protection_constraints=constraints)
    assert demands   # sanity: the fixture/scale actually produced demands
    for d in demands:
        if d["protected"]:
            assert d.get("constraints") == constraints
        else:
            assert "constraints" not in d


def test_protection_constraints_default_none_preserves_old_shape():
    """Backward-compat guard: omitting protection_constraints must reproduce
    today's exact demand shape (no 'constraints' key at all)."""
    n = _star_model()
    demands = generate_demands(n, seed=0, scale=3000.0, protected_fraction=0.5)
    assert demands
    assert all("constraints" not in d for d in demands)
