# multilayer-optical-mcp — Phase 3: Read Tools + Risk Groups

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the **State (read-only)** + **Risk Groups** tool groups from CLAUDE.md as the next slice on top of the Phase 1–2 foundation. Land the **`get_exposure`** primitive that catches "design-time SRLG-disjoint but now correlated under a freshly injected risk group" — the scenario-1 catch CLAUDE.md treats as the central thesis of dynamic risk groups.

**Architecture:** Two new model-side modules and a server-tool extension. No new GNPy, no solvers, no IP routing, no validate/commit. All read tools and the risk-group surface return **structured dicts** (CLAUDE.md design rule: "all tool results are structured, never prose"). Mutating risk-group ops are runtime (no plan/validate gate — risk groups are an in-memory partition, not a network change).

**Tech Stack:** unchanged — Python 3.11+, FastMCP, pytest. No new third-party deps.

---

## Context

Phases 1–2 (committed at `75d8326`) built the multilayer state engine, snapshot/branch lifecycle, and the load-bearing GNPy adapter with arbitrary-loading + per-direction QoT. The model already carries `Service`, `SRLG`, `RiskGroup` dataclasses and basic `add_*` / `list_*` accessors on `NetworkModel`, but none of the read/exposure surface is wired to MCP yet, and several adjacent contract questions are unresolved.

This plan resolves three contract questions and exposes the read + risk-group tools.

**1. `Service.working_path` / `protection_path` contents = IP link ids.** Today the field is `Tuple[str, ...]` with no declared content. Services are an IP-layer concept; CLAUDE.md says services have "working/protection paths and grooming" over lightpaths via IP links. Settling this now is required because `get_exposure` must walk service → IP links → lightpaths → OMS → fiber/amp/roadm uids to compute the asset set a service touches. The alternative (raw lightpath ids) skips the IP layer entirely and conflicts with later `simulate_ip_routing` / `reroute_service`. We add an invariant on `add_service`: every id in `working_path` and `protection_path` must be a registered IP link.

**2. `RiskGroup.asset_ids` are *not* validated against the model.** CLAUDE.md frames risk groups as "abstract asset partitions" that "arrive as asset lists." A downstream disaster app may inject ids whose meaning lives in its own asset registry (a duct id, a regional pole, a building) that the optical model never sees. Validating against the model would make this server refuse legitimate inputs. So `define_risk_group` is permissive — store the asset list verbatim. `get_exposure` simply intersects the stored asset_ids with whatever set the service resolves to; unknown ids miss silently. (SRLGs differ: they're static design-time data, owned by this server, and may be validated when ingest tooling lands later. Out of scope here.)

**3. Read-tool return shape = structured dicts.** Tools return JSON-serializable dicts at the MCP boundary, not domain dataclasses. Serialization lives in a new pure module `model/views.py` so the server stays a thin shim and serializers are independently testable.

CLAUDE.md design rules carried in unchanged:
- Read tools never mutate state.
- No event/geo/weather logic — risk groups arrive as asset lists, never as geometry.
- `get_exposure` is the load-bearing primitive (working **AND** protection both intersect = latent correlation; either alone = partial exposure).

---

## File Structure

```
multilayer-optical-mcp/
├── src/multilayer_optical_mcp/
│   ├── model/
│   │   ├── network.py                          # +invariant on add_service; +getters
│   │   ├── exposure.py        [NEW]            # service→asset-set + compute_exposure
│   │   └── views.py           [NEW]            # pure dict serializers
│   └── server.py                               # +9 read/risk tools
└── tests/
    ├── model/
    │   ├── test_network_service_invariant.py  [NEW]
    │   ├── test_exposure.py                   [NEW]
    │   └── test_views.py                       [NEW]
    └── test_server_phase3.py                   [NEW]
```

**Module boundaries:**
- `model/exposure.py` — depends on `model/network.py` only. Pure functions, no I/O.
- `model/views.py` — depends on `model/network.py` only. Pure serializers, no I/O.
- `server.py` — wires the new tools onto the existing `SnapshotStore.current()` and the registries already created in `build_app()`.

---

## Task 0: Service `working_path` / `protection_path` invariant

**Files:**
- Modify: `src/multilayer_optical_mcp/model/network.py`
- Create: `tests/model/test_network_service_invariant.py`

Settle the contract: every id in `Service.working_path` and `Service.protection_path` must be a registered IP link. Reject otherwise. This is the precondition for the asset-set resolver to be well-defined.

- [ ] **Step 1: Write failing test**

`tests/model/test_network_service_invariant.py`:
```python
import pytest
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, Lightpath, Router, IPLink, Service,
    TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel


def _seed_with_one_iplink() -> NetworkModel:
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f1", a_end="amp1", z_end="amp2",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp2", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(id="oms1", src_node_id="A", dst_node_id="B",
                  elements=("amp1", "f1", "amp2")))
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms1",),
                              mode_id="100G-QPSK", center_freq_hz=193.4e12))
    n.add_router(Router(id="R1", site="A"))
    n.add_router(Router(id="R2", site="B"))
    n.add_ip_link(IPLink(id="ip1", a_router="R1", z_router="R2",
                         lightpath_id="lp1"))
    return n


def test_service_rejects_unknown_working_iplink():
    n = _seed_with_one_iplink()
    with pytest.raises(ValueError, match="unknown IP link"):
        n.add_service(Service(id="svc1", src_router="R1", dst_router="R2",
                              demand_gbps=10.0,
                              working_path=("ip-missing",)))


def test_service_rejects_unknown_protection_iplink():
    n = _seed_with_one_iplink()
    with pytest.raises(ValueError, match="unknown IP link"):
        n.add_service(Service(id="svc1", src_router="R1", dst_router="R2",
                              demand_gbps=10.0,
                              working_path=("ip1",),
                              protection_path=("ip-missing",)))


def test_service_accepts_empty_paths_for_unrouted_demand():
    """A demand that hasn't been routed yet (no working/protection assigned)
    is legal — services exist before routing solves run."""
    n = _seed_with_one_iplink()
    n.add_service(Service(id="svc1", src_router="R1", dst_router="R2",
                          demand_gbps=10.0))


def test_service_accepts_valid_paths():
    n = _seed_with_one_iplink()
    n.add_service(Service(id="svc1", src_router="R1", dst_router="R2",
                          demand_gbps=10.0,
                          working_path=("ip1",),
                          protection_path=("ip1",)))
```

- [ ] **Step 2: Run test — expect FAIL** (current `add_service` is a pure setter).

- [ ] **Step 3: Add invariant + getter to `network.py`**

Replace the existing `add_service`:
```python
    def add_service(self, s: Service) -> None:
        for ip in s.working_path:
            if ip not in self._ip_links:
                raise ValueError(f"Service {s.id!r}: unknown IP link {ip!r} in working_path")
        for ip in s.protection_path:
            if ip not in self._ip_links:
                raise ValueError(f"Service {s.id!r}: unknown IP link {ip!r} in protection_path")
        self._services[s.id] = s

    def get_service(self, sid: str) -> Service:
        return self._services[sid]
```

- [ ] **Step 4: Run tests — expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/network.py tests/model/test_network_service_invariant.py
git commit -m "feat(model): Service.working_path/protection_path must reference registered IP links"
```

---

## Task 1: Registry getters (SRLG / RiskGroup)

**Files:**
- Modify: `src/multilayer_optical_mcp/model/network.py`
- Tests: extend `tests/model/test_network_service_invariant.py`

Existing model has `list_srlgs()` and `list_risk_groups()` but no by-id `get_*` or a permissive `define_risk_group`. Add them.

- [ ] **Step 1: Write failing test (append)**

```python
from multilayer_optical_mcp.model.assets import SRLG, RiskGroup


def test_srlg_get_and_members():
    n = _seed_with_one_iplink()
    n.add_srlg(SRLG(id="srlg-aerial-A", asset_ids=("f1", "amp1")))
    g = n.get_srlg("srlg-aerial-A")
    assert g.asset_ids == ("f1", "amp1")
    assert n.get_srlg_members("srlg-aerial-A") == ("f1", "amp1")


def test_define_risk_group_is_permissive_about_asset_ids():
    """Risk groups are abstract partitions; ids need not be in the model."""
    n = _seed_with_one_iplink()
    rg = n.define_risk_group(
        rg_id="rg-storm-1",
        asset_ids=("f1", "duct-7", "pole-13"),  # last two unknown
        metadata={"source": "operator-injected"},
    )
    assert rg.id == "rg-storm-1"
    assert n.get_risk_group("rg-storm-1").asset_ids == ("f1", "duct-7", "pole-13")


def test_define_risk_group_rejects_duplicate_id():
    n = _seed_with_one_iplink()
    n.define_risk_group(rg_id="rg1", asset_ids=("f1",))
    with pytest.raises(ValueError, match="risk group .* already exists"):
        n.define_risk_group(rg_id="rg1", asset_ids=("amp1",))
```

- [ ] **Step 2: Run tests — expect FAIL.**

- [ ] **Step 3: Add getters + `define_risk_group` to `network.py`**

Append:
```python
    def get_service(self, sid: str) -> Service:  # if not already added in Task 0
        return self._services[sid]

    def get_srlg(self, gid: str) -> SRLG:
        return self._srlgs[gid]

    def get_srlg_members(self, gid: str) -> Tuple[str, ...]:
        return self._srlgs[gid].asset_ids

    def get_risk_group(self, gid: str) -> RiskGroup:
        return self._risk_groups[gid]

    def define_risk_group(
        self,
        rg_id: str,
        asset_ids: Tuple[str, ...],
        metadata: dict | None = None,
    ) -> RiskGroup:
        """Permissive runtime risk-group constructor. asset_ids are NOT
        validated against the model — risk groups are abstract partitions
        and a downstream app may reference assets this server does not own.
        Reject only on duplicate id."""
        if rg_id in self._risk_groups:
            raise ValueError(f"risk group {rg_id!r} already exists")
        rg = RiskGroup(id=rg_id, asset_ids=tuple(asset_ids),
                       metadata=dict(metadata or {}))
        self._risk_groups[rg_id] = rg
        return rg
```

- [ ] **Step 4: Run tests — expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/network.py tests/model/test_network_service_invariant.py
git commit -m "feat(model): SRLG/RiskGroup getters + permissive define_risk_group"
```

---

## Task 2: Asset-set resolution + `compute_exposure`

**Files:**
- Create: `src/multilayer_optical_mcp/model/exposure.py`
- Create: `tests/model/test_exposure.py`

`compute_exposure(model, service_id, risk_group_id)` resolves the service's asset footprint (working *and* protection independently) and intersects with the risk group's `asset_ids`. The asset footprint walks service → IP link → lightpath → OMS → fiber/amp/roadm uids; the footprint also *includes* the ids of the intermediate IP links, lightpaths, and OMS themselves, so a risk group expressed at any abstraction layer matches.

Return type is a frozen dataclass with both intersections and the "both intersect" flag — the latent-correlation signal.

- [ ] **Step 1: Write failing test**

`tests/model/test_exposure.py`:
```python
import pytest
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, Lightpath, Router, IPLink, Service,
    RiskGroup, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.exposure import (
    ExposureResult, compute_exposure, service_asset_set,
)


def _model_two_paths() -> NetworkModel:
    """Build a model with two disjoint IP link options (ip1 over oms1,
    ip2 over oms2). Service rides ip1 (working) and ip2 (protection)."""
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    for amp_id in ("ampA1", "ampA2", "ampB1", "ampB2"):
        n.add_amplifier(Amplifier(id=amp_id, type_variety="advanced_toy",
                                  gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fiber-north", a_end="ampA1", z_end="ampA2",
                      length_km=80.0, type_variety="SSMF"))
    n.add_fiber(Fiber(id="fiber-south", a_end="ampB1", z_end="ampB2",
                      length_km=80.0, type_variety="SSMF"))
    n.add_oms(OMS(id="oms-north", src_node_id="A", dst_node_id="B",
                  elements=("ampA1", "fiber-north", "ampA2")))
    n.add_oms(OMS(id="oms-south", src_node_id="A", dst_node_id="B",
                  elements=("ampB1", "fiber-south", "ampB2")))
    n.add_lightpath(Lightpath(id="lp-north", oms_sequence=("oms-north",),
                              mode_id="100G-QPSK", center_freq_hz=193.4e12))
    n.add_lightpath(Lightpath(id="lp-south", oms_sequence=("oms-south",),
                              mode_id="100G-QPSK", center_freq_hz=193.4e12))
    n.add_router(Router(id="R1", site="A"))
    n.add_router(Router(id="R2", site="B"))
    n.add_ip_link(IPLink(id="ip1", a_router="R1", z_router="R2",
                         lightpath_id="lp-north"))
    n.add_ip_link(IPLink(id="ip2", a_router="R1", z_router="R2",
                         lightpath_id="lp-south"))
    n.add_service(Service(id="svc1", src_router="R1", dst_router="R2",
                          demand_gbps=10.0,
                          working_path=("ip1",),
                          protection_path=("ip2",)))
    return n


def test_asset_set_includes_all_layers():
    n = _model_two_paths()
    # Working path = ip1 over lp-north over oms-north over {ampA1, fiber-north, ampA2}.
    assets = service_asset_set(n, "svc1", which="working")
    assert "ip1" in assets
    assert "lp-north" in assets
    assert "oms-north" in assets
    assert "fiber-north" in assets
    assert "ampA1" in assets and "ampA2" in assets
    # Should NOT include south path assets.
    assert "fiber-south" not in assets


def test_exposure_neither_path_intersects():
    n = _model_two_paths()
    n.define_risk_group(rg_id="rg-unrelated", asset_ids=("fiber-east",))
    res = compute_exposure(n, "svc1", "rg-unrelated")
    assert isinstance(res, ExposureResult)
    assert res.working_intersects is False
    assert res.protection_intersects is False
    assert res.both_intersect is False
    assert res.working_intersection == ()
    assert res.protection_intersection == ()


def test_exposure_working_only_intersects():
    n = _model_two_paths()
    n.define_risk_group(rg_id="rg-north-aerial", asset_ids=("fiber-north",))
    res = compute_exposure(n, "svc1", "rg-north-aerial")
    assert res.working_intersects is True
    assert res.protection_intersects is False
    assert res.both_intersect is False
    assert res.working_intersection == ("fiber-north",)


def test_exposure_both_paths_intersect_is_the_latent_correlation_case():
    """Scenario 1: working and protection were SRLG-disjoint at design time,
    but a newly injected risk group spans them both (e.g. a storm cone over
    both aerial spans). This is the load-bearing signal."""
    n = _model_two_paths()
    n.define_risk_group(
        rg_id="rg-storm-cone",
        asset_ids=("fiber-north", "fiber-south"),
        metadata={"injected_by": "operator"},
    )
    res = compute_exposure(n, "svc1", "rg-storm-cone")
    assert res.both_intersect is True
    assert set(res.working_intersection) == {"fiber-north"}
    assert set(res.protection_intersection) == {"fiber-south"}


def test_exposure_unknown_assets_in_risk_group_miss_silently():
    """Permissive contract: risk-group asset_ids may reference ids this
    server doesn't own. They never match anything; no error."""
    n = _model_two_paths()
    n.define_risk_group(rg_id="rg-opaque", asset_ids=("duct-7", "pole-13"))
    res = compute_exposure(n, "svc1", "rg-opaque")
    assert res.both_intersect is False


def test_exposure_unknown_service_raises():
    n = _model_two_paths()
    with pytest.raises(KeyError):
        compute_exposure(n, "svc-missing", "any")


def test_exposure_unknown_risk_group_raises():
    n = _model_two_paths()
    with pytest.raises(KeyError):
        compute_exposure(n, "svc1", "rg-missing")


def test_exposure_service_with_no_protection_path():
    """Unrouted protection — protection_intersects is False regardless."""
    n = _model_two_paths()
    # Replace the service with one that has no protection assigned.
    n._services["svc1"] = Service(id="svc1", src_router="R1", dst_router="R2",
                                  demand_gbps=10.0, working_path=("ip1",))
    n.define_risk_group(rg_id="rg-north", asset_ids=("fiber-north",))
    res = compute_exposure(n, "svc1", "rg-north")
    assert res.working_intersects is True
    assert res.protection_intersects is False
    assert res.both_intersect is False
```

- [ ] **Step 2: Run tests — expect FAIL (module missing).**

- [ ] **Step 3: Implement `exposure.py`**

`src/multilayer_optical_mcp/model/exposure.py`:
```python
from __future__ import annotations
from dataclasses import dataclass
from typing import FrozenSet, Tuple
from .network import NetworkModel


@dataclass(frozen=True)
class ExposureResult:
    """Result of intersecting a service's asset footprint with a risk group.

    `both_intersect` is the load-bearing signal: working AND protection both
    touch the group, so the pair that was disjoint at design time is now
    correlated under this partition (CLAUDE.md scenario 1).
    """
    service_id: str
    risk_group_id: str
    working_intersects: bool
    protection_intersects: bool
    both_intersect: bool
    working_intersection: Tuple[str, ...]
    protection_intersection: Tuple[str, ...]


def _path_asset_set(model: NetworkModel, ip_link_ids: Tuple[str, ...]) -> FrozenSet[str]:
    """Expand IP-link-id sequence to the full multi-layer asset set:
    {ip_link_ids} ∪ {lightpath_ids} ∪ {oms_ids} ∪ {fiber/amp/roadm uids}.

    Risk groups may be expressed at any of these layers (fiber-level for a
    storm hitting a span; oms-level for a cable cut; lightpath-level for a
    transponder failure). Including every layer in the asset set makes
    intersection layer-agnostic.
    """
    assets: set[str] = set()
    for ip_id in ip_link_ids:
        assets.add(ip_id)
        link = model.get_ip_link(ip_id)
        lp = model.get_lightpath(link.lightpath_id)
        assets.add(lp.id)
        for oms_id in lp.oms_sequence:
            assets.add(oms_id)
            oms = model.get_oms(oms_id)
            assets.update(oms.elements)
    return frozenset(assets)


def service_asset_set(
    model: NetworkModel, service_id: str, *, which: str,
) -> FrozenSet[str]:
    """Return the full asset footprint of a service's `working` or
    `protection` path. Raises KeyError on unknown service."""
    svc = model.get_service(service_id)
    if which == "working":
        return _path_asset_set(model, svc.working_path)
    if which == "protection":
        return _path_asset_set(model, svc.protection_path)
    raise ValueError(f"which must be 'working' or 'protection', got {which!r}")


def compute_exposure(
    model: NetworkModel, service_id: str, risk_group_id: str,
) -> ExposureResult:
    """Intersect a service's working+protection asset footprints with a
    risk group. Unknown asset ids in the risk group miss silently."""
    rg = model.get_risk_group(risk_group_id)
    rg_assets = frozenset(rg.asset_ids)
    working = service_asset_set(model, service_id, which="working")
    protection = service_asset_set(model, service_id, which="protection")
    w_hit = tuple(sorted(working & rg_assets))
    p_hit = tuple(sorted(protection & rg_assets))
    return ExposureResult(
        service_id=service_id,
        risk_group_id=risk_group_id,
        working_intersects=bool(w_hit),
        protection_intersects=bool(p_hit),
        both_intersect=bool(w_hit) and bool(p_hit),
        working_intersection=w_hit,
        protection_intersection=p_hit,
    )
```

- [ ] **Step 4: Run tests — expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/exposure.py tests/model/test_exposure.py
git commit -m "feat(model): service asset-set resolver + compute_exposure (latent-correlation detector)"
```

---

## Task 3: Dict serializers (`model/views.py`)

**Files:**
- Create: `src/multilayer_optical_mcp/model/views.py`
- Create: `tests/model/test_views.py`

Pure serializers from domain dataclasses to JSON-ready dicts. Keeps `server.py` thin and lets us assert tool result shapes without touching FastMCP.

Functions:
- `topology_dict(model, layer)` — `layer ∈ {"optical", "ip", "both"}`
- `lightpaths_dict(model)` — list of lightpaths with mode, path, QoT-if-known
- `services_dict(model)` — list of services with paths and grooming-map (`{lightpath_id: [service_ids]}`)
- `traffic_matrix_dict(model)` — `{src_router: {dst_router: aggregated_demand_gbps}}` (aggregated across services)
- `srlgs_dict(model)` — list of SRLGs
- `risk_groups_dict(model)` — list of risk groups with metadata

- [ ] **Step 1: Write failing test**

`tests/model/test_views.py`:
```python
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, Lightpath, Router, IPLink, Service,
    SRLG, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.views import (
    topology_dict, lightpaths_dict, services_dict,
    traffic_matrix_dict, srlgs_dict, risk_groups_dict,
)


def _seed():
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f1", a_end="amp1", z_end="amp2",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp2", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(id="oms1", src_node_id="A", dst_node_id="B",
                  elements=("amp1", "f1", "amp2")))
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms1",),
                              mode_id="100G-QPSK", center_freq_hz=193.4e12))
    n.add_router(Router(id="R1", site="A"))
    n.add_router(Router(id="R2", site="B"))
    n.add_ip_link(IPLink(id="ip1", a_router="R1", z_router="R2",
                         lightpath_id="lp1"))
    n.add_service(Service(id="svc1", src_router="R1", dst_router="R2",
                          demand_gbps=10.0, working_path=("ip1",)))
    n.add_service(Service(id="svc2", src_router="R1", dst_router="R2",
                          demand_gbps=15.0, working_path=("ip1",)))
    n.add_srlg(SRLG(id="srlg-pole-A", asset_ids=("f1", "amp1")))
    n.define_risk_group(rg_id="rg-storm", asset_ids=("f1",),
                        metadata={"source": "operator"})
    return n


def test_topology_optical_only():
    n = _seed()
    t = topology_dict(n, layer="optical")
    assert "fibers" in t and "amplifiers" in t and "oms" in t
    assert "ip_links" not in t and "routers" not in t
    assert any(f["id"] == "f1" for f in t["fibers"])


def test_topology_ip_only():
    n = _seed()
    t = topology_dict(n, layer="ip")
    assert "ip_links" in t and "routers" in t
    assert "fibers" not in t


def test_topology_both_layers():
    n = _seed()
    t = topology_dict(n, layer="both")
    assert "fibers" in t and "ip_links" in t


def test_lightpaths_dict_carries_mode_and_path_and_qot_when_available():
    n = _seed()
    n.set_qot_state("lp1", QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=8.0,
                                    limiting_element_id="amp1"))
    lps = lightpaths_dict(n)
    assert len(lps) == 1
    entry = lps[0]
    assert entry["id"] == "lp1"
    assert entry["mode_id"] == "100G-QPSK"
    assert entry["oms_sequence"] == ["oms1"]
    assert entry["qot"]["margin_db"] == 8.0
    assert entry["qot"]["limiting_element_id"] == "amp1"


def test_lightpaths_dict_qot_is_none_when_not_observed():
    n = _seed()
    lps = lightpaths_dict(n)
    assert lps[0]["qot"] is None


def test_services_dict_carries_grooming_map():
    n = _seed()
    s = services_dict(n)
    assert "services" in s and "grooming_map" in s
    # Both services ride lp1 — grooming_map should group them.
    assert sorted(s["grooming_map"]["lp1"]) == ["svc1", "svc2"]


def test_traffic_matrix_aggregates_across_services():
    n = _seed()
    tm = traffic_matrix_dict(n)
    assert tm["R1"]["R2"] == 25.0  # 10 + 15


def test_srlgs_dict():
    n = _seed()
    s = srlgs_dict(n)
    assert s[0]["id"] == "srlg-pole-A"
    assert s[0]["asset_ids"] == ["f1", "amp1"]


def test_risk_groups_dict_carries_metadata():
    n = _seed()
    r = risk_groups_dict(n)
    assert r[0]["id"] == "rg-storm"
    assert r[0]["metadata"]["source"] == "operator"
```

- [ ] **Step 2: Run tests — expect FAIL (module missing).**

- [ ] **Step 3: Implement `views.py`**

`src/multilayer_optical_mcp/model/views.py`:
```python
from __future__ import annotations
from collections import defaultdict
from typing import Any, Dict, List
from .network import NetworkModel


def _fiber(f) -> dict:
    return {"id": f.id, "a_end": f.a_end, "z_end": f.z_end,
            "length_km": f.length_km, "type_variety": f.type_variety}


def _amp(a) -> dict:
    return {"id": a.id, "type_variety": a.type_variety,
            "gain_db": a.gain_db, "nf_db": a.nf_db, "tilt_db": a.tilt_db}


def _oms(o) -> dict:
    return {"id": o.id, "src_node_id": o.src_node_id,
            "dst_node_id": o.dst_node_id, "elements": list(o.elements)}


def _router(r) -> dict:
    return {"id": r.id, "site": r.site}


def _ip_link(link) -> dict:
    return {"id": link.id, "a_router": link.a_router,
            "z_router": link.z_router, "lightpath_id": link.lightpath_id}


def topology_dict(model: NetworkModel, *, layer: str) -> Dict[str, Any]:
    """Layered topology view. `layer` ∈ {'optical', 'ip', 'both'}."""
    if layer not in {"optical", "ip", "both"}:
        raise ValueError(f"layer must be 'optical', 'ip', or 'both', got {layer!r}")
    out: Dict[str, Any] = {}
    if layer in {"optical", "both"}:
        out["fiber_types"] = [{"type_variety": ft.type_variety,
                               "loss_coef_db_per_km": ft.loss_coef_db_per_km}
                              for ft in model.list_fiber_types()]
        out["fibers"] = [_fiber(f) for f in model._fibers.values()]
        out["amplifiers"] = [_amp(a) for a in model._amplifiers.values()]
        out["oms"] = [_oms(o) for o in model.list_oms()]
    if layer in {"ip", "both"}:
        out["routers"] = [_router(r) for r in model._routers.values()]
        out["ip_links"] = [_ip_link(l) for l in model.list_ip_links()]
    return out


def lightpaths_dict(model: NetworkModel) -> List[dict]:
    out: List[dict] = []
    for lp in model.list_lightpaths():
        try:
            qs = model.get_qot_state(lp.id)
            qot = {"gsnr_db": qs.gsnr_db, "osnr_db": qs.osnr_db,
                   "margin_db": qs.margin_db,
                   "mode_feasible": qs.mode_feasible,
                   "limiting_element_id": qs.limiting_element_id}
        except LookupError:
            qot = None
        out.append({
            "id": lp.id,
            "oms_sequence": list(lp.oms_sequence),
            "mode_id": lp.mode_id,
            "center_freq_hz": lp.center_freq_hz,
            "qot": qot,
        })
    return out


def services_dict(model: NetworkModel) -> Dict[str, Any]:
    services = []
    grooming: Dict[str, List[str]] = defaultdict(list)
    for svc in model.list_services():
        services.append({
            "id": svc.id,
            "src_router": svc.src_router, "dst_router": svc.dst_router,
            "demand_gbps": svc.demand_gbps,
            "working_path": list(svc.working_path),
            "protection_path": list(svc.protection_path),
        })
        for ip_id in svc.working_path:
            lp_id = model.get_ip_link(ip_id).lightpath_id
            grooming[lp_id].append(svc.id)
    return {"services": services, "grooming_map": dict(grooming)}


def traffic_matrix_dict(model: NetworkModel) -> Dict[str, Dict[str, float]]:
    tm: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for svc in model.list_services():
        tm[svc.src_router][svc.dst_router] += svc.demand_gbps
    return {src: dict(dsts) for src, dsts in tm.items()}


def srlgs_dict(model: NetworkModel) -> List[dict]:
    return [{"id": g.id, "asset_ids": list(g.asset_ids)}
            for g in model.list_srlgs()]


def risk_groups_dict(model: NetworkModel) -> List[dict]:
    return [{"id": g.id, "asset_ids": list(g.asset_ids),
             "metadata": dict(g.metadata)}
            for g in model.list_risk_groups()]
```

- [ ] **Step 4: Run tests — expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/views.py tests/model/test_views.py
git commit -m "feat(model): pure dict serializers for read-tool surface"
```

---

## Task 4: MCP server tools (read + risk groups)

**Files:**
- Modify: `src/multilayer_optical_mcp/server.py`
- Create: `tests/test_server_phase3.py`

Add nine tools onto the existing `build_app()`. All read tools delegate to `views.py`; risk-group ops mutate `snapshots.current()` (not gated — risk groups are an in-memory partition, not a network change).

New tools:
- `get_topology(layer)`
- `get_lightpaths()`
- `get_services()`
- `get_traffic_matrix()`
- `list_srlgs()` / `get_srlg_members(srlg_id)`
- `define_risk_group(rg_id, asset_ids, metadata)`
- `list_risk_groups()` / `get_risk_group(rg_id)`
- `get_exposure(service_id, risk_group_id)`

- [ ] **Step 1: Write failing test**

`tests/test_server_phase3.py`:
```python
from multilayer_optical_mcp.server import build_app
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, Lightpath, Router, IPLink, Service,
    SRLG,
)


def _seed_app():
    """Build an app and populate its current snapshot with a 2-path scenario."""
    app = build_app()
    # Reach into the server's model (tests are allowed; CLAUDE.md design rule
    # is about MCP boundaries, not test internals).
    model = app._snapshots.current()  # type: ignore[attr-defined]
    model.register_fiber_type(FiberType("SSMF", 0.2))
    for amp_id in ("ampA1", "ampA2", "ampB1", "ampB2"):
        model.add_amplifier(Amplifier(id=amp_id, type_variety="advanced_toy",
                                      gain_db=20.0, nf_db=5.5))
    model.add_fiber(Fiber("fiber-north", "ampA1", "ampA2", 80.0, "SSMF"))
    model.add_fiber(Fiber("fiber-south", "ampB1", "ampB2", 80.0, "SSMF"))
    model.add_oms(OMS("oms-north", "A", "B", ("ampA1", "fiber-north", "ampA2")))
    model.add_oms(OMS("oms-south", "A", "B", ("ampB1", "fiber-south", "ampB2")))
    model.add_lightpath(Lightpath("lp-north", ("oms-north",), "400G@7.1dB", 193.4e12))
    model.add_lightpath(Lightpath("lp-south", ("oms-south",), "400G@7.1dB", 193.4e12))
    model.add_router(Router("R1", "A")); model.add_router(Router("R2", "B"))
    model.add_ip_link(IPLink("ip1", "R1", "R2", "lp-north"))
    model.add_ip_link(IPLink("ip2", "R1", "R2", "lp-south"))
    model.add_service(Service("svc1", "R1", "R2", 10.0,
                              working_path=("ip1",), protection_path=("ip2",)))
    model.add_srlg(SRLG("srlg-pole-A", ("fiber-north",)))
    return app


def test_server_registers_phase_3_tools():
    app = build_app()
    names = {t.name for t in app.list_tools_sync()}
    expected = {
        "get_topology", "get_lightpaths", "get_services", "get_traffic_matrix",
        "list_srlgs", "get_srlg_members",
        "define_risk_group", "list_risk_groups", "get_risk_group",
        "get_exposure",
    }
    assert expected.issubset(names)


def test_get_topology_layer_optical_excludes_ip():
    app = _seed_app()
    out = app.call_tool_sync("get_topology", {"layer": "optical"})
    assert "fibers" in out and "ip_links" not in out


def test_get_lightpaths_and_get_services():
    app = _seed_app()
    lps = app.call_tool_sync("get_lightpaths", {})
    assert {lp["id"] for lp in lps} == {"lp-north", "lp-south"}
    svcs = app.call_tool_sync("get_services", {})
    assert svcs["services"][0]["id"] == "svc1"
    assert svcs["grooming_map"]["lp-north"] == ["svc1"]


def test_get_traffic_matrix():
    app = _seed_app()
    tm = app.call_tool_sync("get_traffic_matrix", {})
    assert tm["R1"]["R2"] == 10.0


def test_list_srlgs_and_get_srlg_members():
    app = _seed_app()
    s = app.call_tool_sync("list_srlgs", {})
    assert s[0]["id"] == "srlg-pole-A"
    members = app.call_tool_sync("get_srlg_members", {"srlg_id": "srlg-pole-A"})
    assert members == ["fiber-north"]


def test_define_and_get_risk_group():
    app = _seed_app()
    out = app.call_tool_sync("define_risk_group", {
        "rg_id": "rg-storm",
        "asset_ids": ["fiber-north", "fiber-south"],
        "metadata": {"source": "operator"},
    })
    assert out["id"] == "rg-storm"
    fetched = app.call_tool_sync("get_risk_group", {"rg_id": "rg-storm"})
    assert sorted(fetched["asset_ids"]) == ["fiber-north", "fiber-south"]
    assert fetched["metadata"]["source"] == "operator"


def test_get_exposure_both_paths_intersect_after_storm_injection():
    """The headline scenario: design-time-disjoint pair, freshly-injected
    risk group spans both, exposure flags it."""
    app = _seed_app()
    app.call_tool_sync("define_risk_group", {
        "rg_id": "rg-storm-cone",
        "asset_ids": ["fiber-north", "fiber-south"],
        "metadata": {},
    })
    exp = app.call_tool_sync("get_exposure", {
        "service_id": "svc1", "risk_group_id": "rg-storm-cone",
    })
    assert exp["both_intersect"] is True
    assert exp["working_intersects"] is True
    assert exp["protection_intersects"] is True
```

(`list_tools_sync` / `call_tool_sync` — match the existing Phase-2 test_server.py style. If the installed FastMCP exposes a different in-memory client API, use the equivalent and update both server tests in the same commit.)

- [ ] **Step 2: Run tests — expect FAIL.**

- [ ] **Step 3: Extend `server.py`**

Inside `build_app()`, after the existing tools and before `return app`, append:

```python
    from .model.views import (
        topology_dict, lightpaths_dict, services_dict,
        traffic_matrix_dict, srlgs_dict, risk_groups_dict,
    )
    from .model.exposure import compute_exposure

    # Expose the SnapshotStore on the app so tests can reach the current model.
    app._snapshots = snapshots  # type: ignore[attr-defined]

    @app.tool()
    def get_topology(layer: str = "both") -> dict:
        return topology_dict(snapshots.current(), layer=layer)

    @app.tool()
    def get_lightpaths() -> list[dict]:
        return lightpaths_dict(snapshots.current())

    @app.tool()
    def get_services() -> dict:
        return services_dict(snapshots.current())

    @app.tool()
    def get_traffic_matrix() -> dict:
        return traffic_matrix_dict(snapshots.current())

    @app.tool()
    def list_srlgs() -> list[dict]:
        return srlgs_dict(snapshots.current())

    @app.tool()
    def get_srlg_members(srlg_id: str) -> list[str]:
        return list(snapshots.current().get_srlg_members(srlg_id))

    @app.tool()
    def define_risk_group(
        rg_id: str, asset_ids: list[str], metadata: dict | None = None,
    ) -> dict:
        rg = snapshots.current().define_risk_group(
            rg_id=rg_id, asset_ids=tuple(asset_ids), metadata=metadata or {},
        )
        return {"id": rg.id, "asset_ids": list(rg.asset_ids),
                "metadata": dict(rg.metadata)}

    @app.tool()
    def list_risk_groups() -> list[dict]:
        return risk_groups_dict(snapshots.current())

    @app.tool()
    def get_risk_group(rg_id: str) -> dict:
        rg = snapshots.current().get_risk_group(rg_id)
        return {"id": rg.id, "asset_ids": list(rg.asset_ids),
                "metadata": dict(rg.metadata)}

    @app.tool()
    def get_exposure(service_id: str, risk_group_id: str) -> dict:
        res = compute_exposure(snapshots.current(), service_id, risk_group_id)
        return {
            "service_id": res.service_id,
            "risk_group_id": res.risk_group_id,
            "working_intersects": res.working_intersects,
            "protection_intersects": res.protection_intersects,
            "both_intersect": res.both_intersect,
            "working_intersection": list(res.working_intersection),
            "protection_intersection": list(res.protection_intersection),
        }
```

- [ ] **Step 4: Run tests — expect PASS.**

- [ ] **Step 5: Smoke-run**

`multilayer-optical-mcp --help` still returns FastMCP help text.

- [ ] **Step 6: Commit**

```bash
git add src/multilayer_optical_mcp/server.py tests/test_server_phase3.py
git commit -m "feat(server): phase 3 read-tools + risk-group surface incl. get_exposure"
```

---

## Critical files referenced

Existing (read for context before editing):
- `src/multilayer_optical_mcp/model/network.py` — extend with `get_service`, `get_srlg`, `get_srlg_members`, `get_risk_group`, `define_risk_group`, and the `add_service` invariant.
- `src/multilayer_optical_mcp/model/assets.py` — `Service`, `SRLG`, `RiskGroup` already exist; no changes.
- `src/multilayer_optical_mcp/server.py` — extend `build_app()`.

New:
- `src/multilayer_optical_mcp/model/exposure.py`
- `src/multilayer_optical_mcp/model/views.py`

Existing utilities reused:
- `NetworkModel.get_ip_link`, `get_lightpath`, `get_oms`, `list_*` — already there.
- `QoTState` already serializes naturally via attribute access in `views.lightpaths_dict`.
- `SnapshotStore.current()` already returns the live model; no new snapshot wiring.

---

## Verification

1. **Full suite green:** `pytest -v`. All Phase 1–2 tests stay green; 4 new test files pass.
2. **Service invariant holds:** `add_service` rejects unknown IP-link ids in either path.
3. **Risk groups are permissive:** `define_risk_group(rg_id, ["unknown-1", "unknown-2"])` succeeds; `get_exposure` against it returns no intersection.
4. **The headline scenario passes end-to-end:**
   - Seed a 2-path model (working over `fiber-north`, protection over `fiber-south`).
   - Confirm SRLG-disjointness at design time (no shared SRLG members).
   - Inject `rg-storm-cone` with `asset_ids=["fiber-north", "fiber-south"]`.
   - `get_exposure("svc1", "rg-storm-cone")` returns `both_intersect=True` with the per-path intersection lists populated.
5. **MCP smoke:** launch `multilayer-optical-mcp`, connect via `npx @modelcontextprotocol/inspector multilayer-optical-mcp`. Call `get_topology`, `define_risk_group`, `get_exposure` and verify the typed JSON.
6. **No mutation in read tools:** confirm `get_topology` / `get_lightpaths` / `get_services` / `get_traffic_matrix` / `list_srlgs` / `get_srlg_members` / `list_risk_groups` / `get_risk_group` / `get_exposure` do not change snapshot ids (snapshot the model id before and after; equal). Only `define_risk_group` mutates.

**Deferred to follow-up plans (consistent with phases-1-2 deferral list):**
- Solvers (`compute_paths`, `compute_disjoint_paths` with `basis ∈ {physical, srlg, risk_group, union}`, `check_disjointness` audit, `solve_rsa`, `solve_allocation`).
- IP-over-optical simulation (`simulate_ip_routing`, `get_grooming_map` derivation beyond the basic view, `get_affected_services`, `reroute_service`).
- What-if (`whatif_margin_threshold_sweep`, `inject_degradation`, `inject_failure`).
- Per-channel loading attribution.
- Validate / commit / reconcile.
- Multi-OMS topologies beyond toy 2-span.
- Model-driven GNPy translation.

---

## Risks / open

- **`Service.working_path` semantics may need to evolve.** Today's choice (IP link id sequence) assumes a single IP path per service per role. Multi-path / ECMP scenarios would need a richer representation. Acceptable for Phase 3 since no solver yet consumes it; revisit when `reroute_service` and `simulate_ip_routing` land.
- **Grooming map is *derived from* `working_path` here.** Phase 5 may switch to an explicit grooming registry once solvers can place demands. The view-layer derivation will keep working as a fallback.
- **`app._snapshots` exposed on the app for test access** is a deliberate small leak. Cleaner would be a `build_app() -> (app, model_handle)` factory, but that changes the Phase-2 signature. Defer the refactor unless a second consumer needs it.
