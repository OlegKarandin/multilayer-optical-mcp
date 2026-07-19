# multilayer-optical-mcp — Phase 6a: Topology synthesis (abstract graph → model → GNPy)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Use superpowers:test-driven-development for every function. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the **NetworkModel the single source of truth for the optical layer** and let the server ingest an *abstract* topology graph (e.g. `topologies/german_17.json` — bare nodes + edges with lengths and per-span NF). Build a pipeline `abstract graph → NetworkModel → GNPy network`, replace the hand-written-JSON `load_toy` path with a synthesizer that builds the GNPy network **from model objects**, and prove the synthesizer reproduces the pinned step-2 ground-truth GSNR before anything is built on it.

**Architecture:** Two pure transforms plus a rewire. (1) An **importer** maps an abstract graph to model objects (`Router`/`ROADM`/`Transceiver` per node, a fiber+amplifier chain + `OMS` per edge, spans balanced by `split_link_into_spans`). (2) A **synthesizer** in the GNPy adapter serializes the model to a GNPy-native element/connection dict + an equipment dict (one Edfa `type_variety` per distinct NF so each amp's GNPy NF == `Amplifier.nf_db`) and feeds it to GNPy's own `network_from_json`/`build_network`. (3) `compute_qot` is rewired to build the GNPy network from the model instead of reading a topology file, and the hard-coded `"trx A"` launch transceiver is replaced by a generic per-direction lookup. The existing `load_toy` JSON files survive only as ground-truth oracles for the bridge test.

**Tech Stack:** Python 3.11+, gnpy==2.11.1 (pinned), NetworkX ≥3.2, pytest, FastMCP. No new deps.

---

## Context (what already exists — do not rebuild)

Steps 1–5 are committed. Specifically relevant to 6a:

- **GNPy is loaded from a hand-written JSON file today.** `gnpy_adapter/translate.py:load_toy()` calls gnpy's `load_equipment(DEFAULT_EQPT)` + `load_network(DEFAULT_TOPO, eqpt)`. `DEFAULT_TOPO = topologies/toy_2span.json`, `DEFAULT_EQPT = eqpt/eqpt_config.json`. `compute_qot` (adapter.py:152) calls `load_toy` on every evaluation and never reads the model's `Amplifier`/`Fiber` objects. **This is the dual-source-of-truth that 6a removes.**
- **`compute_qot` bakes in one literal:** `if "trx A" in by_uid: si = by_uid["trx A"](si)` (adapter.py:196). Both toy topologies happen to name their launch transceiver `"trx A"`, so it works for them. 6a replaces this with a generic lookup.
- **`toy_2route.json` already proves multi-topology works** through the same `compute_qot` via the `topo_path=` seam (`tests/gnpy_adapter/test_toy_2route.py`): a routed OMS path on a 2-route graph returns finite GSNR and the longer south route shows lower GSNR. 6a must keep these tests green.
- **The model already has every asset dataclass the importer needs** (`model/assets.py`): `OpticalNode`, `FiberType`, `Fiber(id,a_end,z_end,length_km,type_variety)`, `Amplifier(id,type_variety,gain_db,nf_db,tilt_db)`, `ROADM(id,target_pch_out_db)`, `Transceiver(id,site,supported_mode_ids)`, `OMS(id,src_node_id,dst_node_id,elements)`, `Router(id,site)`. And `NetworkModel` (model/network.py) has `register_fiber_type`, `add_optical_node`, `add_fiber` (validates fiber type exists), `add_amplifier`, `add_roadm`, `add_transceiver`, `add_oms` (validates every element id is a known fiber/amp/roadm), `add_router`.
- **OMS element convention (must be preserved).** `OMS.elements` is an ordered tuple of element ids that the adapter treats as GNPy uids (`translate.resolve_oms_path_to_uids` flattens the OMS sequence into this uid list; `compute_qot` walks them in order). In `toy_2route.json` the OMS for an edge is `(ROADM_src, booster, fiber_0, amp_0, fiber_1, amp_1, …)` — it **starts at the source ROADM**, then a booster, then alternating `(fiber, post-span amp)` pairs, ending on the last span's amp (the "preamp"); it does **not** include the destination ROADM. The importer MUST produce this exact shape so the synthesizer and the existing ROADM-at-position-0 handling in `compute_qot` keep working.
- **`compute_qot` already handles ROADMs generically** (adapter.py:211-234): it resolves ROADM degree/from_degree from the path order and the GNPy network's predecessors, so a synthesized network with correctly-wired ROADMs needs no special-casing.
- **gnpy version is pinned** in `requirements.txt` and `pyproject.toml` at `gnpy==2.11.1`. Ground-truth numbers are valid only against this version.

---

## Decisions settled (call out at spec review if you disagree)

1. **Model is the single source of truth; GNPy is rebuilt from the model each evaluation.** No topology file is read at QoT time once 6a lands. `toy_2span.json` / `toy_2route.json` / `eqpt_config.json` remain in the tree **only** as oracles for the bridge ground-truth test (Task 7) and to keep the legacy `test_toy_2route.py` green during the transition.
2. **Reuse GNPy's own loaders on a synthesized dict — do not hand-instantiate GNPy element objects.** The synthesizer builds a plain `{"elements":[…], "connections":[…]}` dict (identical in shape to `toy_2span.json`) and calls gnpy's `network_from_json(dict, equipment)` then `build_network(...)`. This reuses the exact proven code path `load_toy` uses (it differs only in *where the dict comes from*: an in-memory build vs `json.load`), which is what makes the bridge test (Task 7) able to match ground truth. Rejected: constructing `gnpy.core.elements.Edfa/Fiber` objects directly — it bypasses GNPy's `build_network` design step and is the most likely way to silently diverge from ground truth.
3. **Per-amp NF is honoured by synthesizing one Edfa `type_variety` per distinct NF value.** GNPy stores NF in equipment type-varieties, not per element. To make each amp's GNPy NF equal `Amplifier.nf_db`, the synthesizer emits, for each distinct `nf_db` value `X`, an `advanced_model` Edfa type-variety named `adv_nf_{X}` whose advanced config carries `nf_fit_coeff=[0,0,0,X]` (a flat-NF polynomial, exactly like the existing `advanced_toy_config.json` which encodes 5.5), and points each amp element at the matching type-variety. This satisfies the CLAUDE.md requirement ("use the advanced model … NF from config") and makes NF genuinely model-sourced. **Task 5 is the gate that confirms this mechanism reproduces ground truth on gnpy 2.11.1.**
4. **Booster convention.** An abstract edge supplies `num_spans` fibers and `num_spans` post-span amplifier NFs. The synthesized OMS adds **one booster Edfa** right after the source ROADM (gain set so the booster lifts the ROADM's `target_pch_out_db` to the per-span launch power; NF = a module-level `DEFAULT_BOOSTER_NF_DB = 5.5`), followed by `(fiber_i, amp_i)` for each span where `amp_i` compensates span `i`'s loss and takes its NF from `amplifier_nf_db[i]`. This mirrors the existing toys (booster + ILA + preamp).
5. **The importer builds the optical layer + one Router per node only.** It does **not** auto-create lightpaths or IP links — a lightpath is a *routed* path produced by the solvers/provisioning, not a topology primitive. 6a is about getting QoT-correct optical synthesis; the IP layer stays empty until something routes onto it.
6. **Abstract-graph schema** (matches `german_17.json`): top-level `{"nodes":[{"id":<int|str>}…], "edges":[{"src","dst","length_km","num_spans","span_lengths_km":[…],"fiber_type","amplifier_nf_db":[…]}…]}`. `span_lengths_km` is honoured when present and length-consistent; otherwise the importer derives spans with `split_link_into_spans(length_km)`. Edges are treated as **bidirectional** optical adjacencies but synthesized as **two directed OMS** (src→dst and dst→src) so per-direction QoT and the existing forward/backward propagation both work.

---

## File structure

- **Create `src/multilayer_optical_mcp/model/topology_import.py`** — the abstract-graph→model importer and the `split_link_into_spans` span planner. One responsibility: *turn an abstract optical graph into a consistent NetworkModel optical layer.* Pure (no GNPy import).
- **Create `src/multilayer_optical_mcp/gnpy_adapter/synthesize.py`** — the model→GNPy-dict serializer (`model_to_gnpy_topology`, `model_to_gnpy_equipment`) and `build_gnpy_network(model)`. One responsibility: *render the model as a GNPy network + equipment.* The only new place that imports GNPy.
- **Modify `src/multilayer_optical_mcp/gnpy_adapter/adapter.py`** — replace the `load_toy(...)` call in `compute_qot` with `build_gnpy_network(model)`; replace the `"trx A"` literal with `_find_launch_transceiver(network, path_uids)`.
- **Modify `src/multilayer_optical_mcp/gnpy_adapter/translate.py`** — keep `load_toy` (oracle for the bridge test); no behavioural change.
- **Create `tests/model/test_topology_import.py`** — `split_link_into_spans`, importer on a tiny 2-node graph, importer on `german_17.json` (structural counts).
- **Create `tests/gnpy_adapter/test_synthesize.py`** — synthesized dict structure + per-NF equipment type-varieties.
- **Create `tests/gnpy_adapter/test_ground_truth_bridge.py`** — the gate: synthesized-from-model toy GSNR matches `load_toy` GSNR within tolerance, and a `german_17` routed path returns finite GSNR with longer-path-lower-GSNR.

---

# Part A — span planning + importer (pure, no GNPy)

## Task 1: `split_link_into_spans`

**Files:**
- Create: `src/multilayer_optical_mcp/model/topology_import.py`
- Test: `tests/model/test_topology_import.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/model/test_topology_import.py
import math
import json
from pathlib import Path

import pytest

from multilayer_optical_mcp.model.topology_import import split_link_into_spans


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
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/model/test_topology_import.py -v`
Expected: FAIL with `ImportError`/`ModuleNotFoundError`.

- [ ] **Step 3: Implement `split_link_into_spans`**

```python
# src/multilayer_optical_mcp/model/topology_import.py
from __future__ import annotations

import math
from typing import List


def split_link_into_spans(
    length_km: float,
    target_span_km: float = 80.0,
    min_span_km: float = 20.0,
) -> List[float]:
    """Split a link into balanced spans near *target_span_km*.

    1. n_min = ceil(length/100), n_max = ceil(length/40), clamped to >= 1.
    2. For each n in [n_min, n_max], span_len = length/n; skip if < min_span_km.
    3. Pick n minimising |span_len - target_span_km|.
    4. Return n equal spans, last adjusted so the sum equals length exactly.
    """
    n_min = max(1, math.ceil(length_km / 100.0))
    n_max = max(n_min, math.ceil(length_km / 40.0))

    best_n = None
    best_dev = float("inf")
    for n in range(n_min, n_max + 1):
        span_len = length_km / n
        if span_len < min_span_km:
            continue
        dev = abs(span_len - target_span_km)
        if dev < best_dev:
            best_dev = dev
            best_n = n
    if best_n is None:
        best_n = 1

    base_len = round(length_km / best_n, 2)
    spans = [base_len] * best_n
    spans[-1] = round(length_km - base_len * (best_n - 1), 2)
    return spans
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/model/test_topology_import.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/topology_import.py tests/model/test_topology_import.py
git commit -m "feat(topology): split_link_into_spans balanced span planner"
```

---

## Task 2: Abstract-graph importer → NetworkModel

**Files:**
- Modify: `src/multilayer_optical_mcp/model/topology_import.py`
- Test: `tests/model/test_topology_import.py`

The importer produces, per node `N`: an `OpticalNode(id=f"roadm_{N}", kind="roadm")`, a `ROADM(id=f"roadm_{N}")`, a `Transceiver(id=f"trx_{N}", site=str(N))`, and a `Router(id=f"router_{N}", site=str(N))`. Per directed edge `src→dst` with spans `[L0,L1,…]` and NFs `[nf0,nf1,…]`: a booster `amp_{src}_{dst}_booster`, then for each span `i` a `Fiber(id=f"fiber_{src}_{dst}_{i}")` and an `Amplifier(id=f"amp_{src}_{dst}_{i}", nf_db=nf_i)`, and an `OMS(id=f"oms_{src}_{dst}", src_node_id=str(src), dst_node_id=str(dst), elements=(roadm_src, booster, fiber_0, amp_0, fiber_1, amp_1, …))`. Both directions are emitted.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/model/test_topology_import.py
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.assets import TransceiverMode

REPO_ROOT = Path(__file__).resolve().parents[2]
GERMAN_17 = REPO_ROOT / "topologies" / "german_17.json"


def _reg():
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


def test_import_tiny_graph_builds_optical_layer():
    n = model_from_abstract_graph(_tiny_graph(), modes=_reg())
    # one ROADM, transceiver, router per node
    assert set(n._roadms) == {"roadm_0", "roadm_1"}
    assert set(n._transceivers) == {"trx_0", "trx_1"}
    assert {r.id for r in n.list_oms()} == {"oms_0_1", "oms_1_0"}  # both directions
    oms = n.get_oms("oms_0_1")
    assert oms.src_node_id == "0" and oms.dst_node_id == "1"
    # ROADM first, booster second, then (fiber, amp) per span
    assert oms.elements[0] == "roadm_0"
    assert oms.elements[1] == "amp_0_1_booster"
    assert oms.elements[2] == "fiber_0_1_0"
    assert oms.elements[3] == "amp_0_1_0"
    assert oms.elements[4] == "fiber_0_1_1"
    assert oms.elements[5] == "amp_0_1_1"
    # fibers carry the per-span lengths
    assert n.get_fiber("fiber_0_1_0").length_km == 80.0
    # amps carry per-span NF
    assert n.get_amplifier("amp_0_1_0").nf_db == 5.5


def test_import_german_17_structural_counts():
    graph = json.loads(GERMAN_17.read_text())
    n = model_from_abstract_graph(graph, modes=_reg())
    assert len(n.list_oms()) == 2 * len(graph["edges"])  # both directions
    # total fibers = 2 * sum(num_spans)
    total_spans = sum(e["num_spans"] for e in graph["edges"])
    assert len(n._fibers) == 2 * total_spans
    # router per node
    assert len(n._routers) == len(graph["nodes"])
```

> Note: the `:=` line above is a placeholder-free smoke assertion; replace with `assert n.list_oms()` if you prefer. Keep it simple — the real assertions follow.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/model/test_topology_import.py::test_import_tiny_graph_builds_optical_layer -v`
Expected: FAIL (`model_from_abstract_graph` undefined).

- [ ] **Step 3: Implement the importer**

```python
# append to src/multilayer_optical_mcp/model/topology_import.py
from typing import Any, Dict, List, Optional, Tuple

from .assets import (
    Amplifier, Fiber, FiberType, OMS, OpticalNode, ROADM, Router, Transceiver,
)
from .modes import ModeRegistry
from .network import NetworkModel

DEFAULT_AMP_NF_DB = 5.5
DEFAULT_AMP_GAIN_DB = 20.0
SSMF_LOSS_COEF_DB_PER_KM = 0.2


def _edge_spans(edge: Dict[str, Any]) -> List[float]:
    """Return per-span lengths: honour span_lengths_km if consistent, else derive."""
    spans = edge.get("span_lengths_km")
    if spans and abs(sum(spans) - edge["length_km"]) < 1.0:
        return [float(s) for s in spans]
    return split_link_into_spans(float(edge["length_km"]))


def model_from_abstract_graph(
    graph: Dict[str, Any],
    *,
    modes: ModeRegistry,
    fiber_loss_coef_db_per_km: float = SSMF_LOSS_COEF_DB_PER_KM,
) -> NetworkModel:
    """Build a NetworkModel optical layer from an abstract node/edge graph.

    See the abstract-graph schema decision in the phase-6a plan. Emits one
    ROADM/Transceiver/Router per node and two directed OMS per edge.
    """
    n = NetworkModel(modes=modes)
    n.register_fiber_type(
        FiberType(type_variety="SSMF", loss_coef_db_per_km=fiber_loss_coef_db_per_km)
    )

    for node in graph["nodes"]:
        nid = str(node["id"])
        n.add_optical_node(OpticalNode(id=f"roadm_{nid}", kind="roadm"))
        n.add_roadm(ROADM(id=f"roadm_{nid}"))
        n.add_transceiver(Transceiver(id=f"trx_{nid}", site=nid))
        n.add_router(Router(id=f"router_{nid}", site=nid))

    for edge in graph["edges"]:
        spans = _edge_spans(edge)
        nfs = edge.get("amplifier_nf_db") or [DEFAULT_AMP_NF_DB] * len(spans)
        for src, dst in ((edge["src"], edge["dst"]), (edge["dst"], edge["src"])):
            _add_directed_oms(n, str(src), str(dst), spans, nfs,
                              fiber_type=edge.get("fiber_type", "SSMF"))
    return n


def _add_directed_oms(
    n: NetworkModel, src: str, dst: str,
    spans: List[float], nfs: List[float], *, fiber_type: str,
) -> None:
    booster_id = f"amp_{src}_{dst}_booster"
    n.add_amplifier(Amplifier(id=booster_id, type_variety="advanced_toy",
                              gain_db=DEFAULT_AMP_GAIN_DB, nf_db=DEFAULT_AMP_NF_DB))
    elements: List[str] = [f"roadm_{src}", booster_id]
    for i, span_km in enumerate(spans):
        fid = f"fiber_{src}_{dst}_{i}"
        aid = f"amp_{src}_{dst}_{i}"
        n.add_fiber(Fiber(id=fid, a_end=f"roadm_{src}" if i == 0 else f"amp_{src}_{dst}_{i-1}",
                          z_end=aid, length_km=float(span_km), type_variety=fiber_type))
        nf_i = float(nfs[i]) if i < len(nfs) else DEFAULT_AMP_NF_DB
        # gain compensates this span's loss; NF is model-sourced.
        n.add_amplifier(Amplifier(id=aid, type_variety="advanced_toy",
                                  gain_db=fiber_loss_db(span_km, n, fiber_type),
                                  nf_db=nf_i))
        elements.extend([fid, aid])
    n.add_oms(OMS(id=f"oms_{src}_{dst}", src_node_id=src, dst_node_id=dst,
                  elements=tuple(elements)))


def fiber_loss_db(length_km: float, n: NetworkModel, fiber_type: str) -> float:
    return length_km * n.get_fiber_type(fiber_type).loss_coef_db_per_km
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/model/test_topology_import.py -v`
Expected: PASS. Fix the smoke assertion in the test if it reads awkwardly (`assert n.list_oms()`).

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/topology_import.py tests/model/test_topology_import.py
git commit -m "feat(topology): abstract-graph importer builds optical layer from nodes/edges"
```

---

# Part B — model → GNPy synthesizer

## Task 3: `model_to_gnpy_topology` + `model_to_gnpy_equipment`

**Files:**
- Create: `src/multilayer_optical_mcp/gnpy_adapter/synthesize.py`
- Test: `tests/gnpy_adapter/test_synthesize.py`

The serializer walks the model and produces a GNPy-native dict. Element rules: each `ROADM` → `{"uid","type":"Roadm"}`; each `Transceiver` → `{"uid","type":"Transceiver"}`; each `Amplifier` → `{"uid","type":"Edfa","type_variety": nf_type_variety(nf_db),"operational":{"gain_target":gain_db,"tilt_target":0}}`; each `Fiber` → `{"uid","type":"Fiber","type_variety": fiber_type,"params":{"length":length_km,"length_units":"km","loss_coef":loss_coef,"att_in":0,"con_in":0,"con_out":0}}`. Connections come from the OMS element order plus `trx_N → roadm_N` and `<last OMS amp> → roadm_dst`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/gnpy_adapter/test_synthesize.py
from multilayer_optical_mcp.gnpy_adapter.synthesize import (
    model_to_gnpy_topology, model_to_gnpy_equipment, nf_type_variety,
)
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.assets import TransceiverMode


def _reg():
    return ModeRegistry([TransceiverMode(id="400G@7.1dB", bitrate_gbps=400.0,
        required_gsnr_db=7.1, symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)])


def _tiny_model():
    return model_from_abstract_graph({
        "nodes": [{"id": 0}, {"id": 1}],
        "edges": [{"src": 0, "dst": 1, "length_km": 160.0, "num_spans": 2,
                   "span_lengths_km": [80.0, 80.0], "fiber_type": "SSMF",
                   "amplifier_nf_db": [5.5, 7.5]}],
    }, modes=_reg())


def test_topology_has_all_element_types():
    topo = model_to_gnpy_topology(_tiny_model())
    types = {e["type"] for e in topo["elements"]}
    assert {"Roadm", "Transceiver", "Edfa", "Fiber"} <= types
    # connections wire trx -> roadm
    pairs = {(c["from_node"], c["to_node"]) for c in topo["connections"]}
    assert ("trx_0", "roadm_0") in pairs


def test_distinct_nf_gets_distinct_type_variety():
    eqpt = model_to_gnpy_equipment(_tiny_model())
    edfa_varieties = {e["type_variety"] for e in eqpt["Edfa"]}
    # NF 5.5 and 7.5 both present as distinct advanced-model varieties
    assert nf_type_variety(5.5) in edfa_varieties
    assert nf_type_variety(7.5) in edfa_varieties


def test_nf_type_variety_carries_flat_nf_polynomial():
    eqpt = model_to_gnpy_equipment(_tiny_model())
    by_name = {e["type_variety"]: e for e in eqpt["Edfa"]}
    adv = by_name[nf_type_variety(7.5)]
    assert adv["type_def"] == "advanced_model"
    assert adv["advanced_config_from_json"]["nf_fit_coeff"][-1] == 7.5
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/gnpy_adapter/test_synthesize.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement the serializer**

```python
# src/multilayer_optical_mcp/gnpy_adapter/synthesize.py
"""model -> GNPy network synthesis. The only new component that imports GNPy.

Renders a NetworkModel optical layer as a GNPy-native {elements, connections}
dict plus an equipment dict, then reuses gnpy's own network_from_json /
build_network so the synthesized network is byte-for-byte the same code path
load_toy uses (only the dict source differs). NF is model-sourced: each distinct
Amplifier.nf_db becomes its own advanced_model Edfa type_variety.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..model.network import NetworkModel

ROADM_TARGET_PCH_OUT_DB = -20.0
ROADM_ADD_DROP_OSNR = 33.0


def nf_type_variety(nf_db: float) -> str:
    """Stable name for the advanced-model Edfa type_variety carrying flat NF=nf_db."""
    return f"adv_nf_{nf_db:g}"


def model_to_gnpy_equipment(model: NetworkModel) -> Dict[str, Any]:
    """Build the GNPy equipment dict: SI, Span, Roadm, Fiber, Transceiver, and one
    advanced_model Edfa type_variety per distinct amplifier NF in the model."""
    nfs = sorted({amp.nf_db for amp in model._amplifiers.values()})
    edfa = [
        {
            "type_variety": nf_type_variety(nf),
            "type_def": "advanced_model",
            "gain_flatmax": 25,
            "gain_min": 0,
            "p_max": 23,
            "advanced_config_from_json": {
                "nf_fit_coeff": [0.0, 0.0, 0.0, float(nf)],
                "f_min": 191.275e12,
                "f_max": 196.125e12,
                "nf_ripple": [0.0],
                "dgt": [1.0],
                "gain_ripple": [0.0],
            },
            "out_voa_auto": False,
            "allowed_for_design": True,
        }
        for nf in nfs
    ]
    return {
        "Edfa": edfa,
        "Fiber": [{"type_variety": "SSMF", "dispersion": 1.67e-05,
                   "effective_area": 83e-12, "pmd_coef": 1.265e-15}],
        "Span": [{"power_mode": True, "delta_power_range_db": [0, 0, 0.5],
                  "max_fiber_lineic_loss_for_raman": 0.25, "target_extended_gain": 2.5,
                  "max_length": 150, "length_units": "km", "max_loss": 28,
                  "padding": 10, "EOL": 0, "con_in": 0, "con_out": 0}],
        "Roadm": [{"target_pch_out_db": ROADM_TARGET_PCH_OUT_DB,
                   "add_drop_osnr": ROADM_ADD_DROP_OSNR, "pmd": 0, "pdl": 0,
                   "restrictions": {"preamp_variety_list": [], "booster_variety_list": []}}],
        "SI": [{"f_min": 191.3e12, "baud_rate": 87.5e9, "f_max": 196.1e12,
                "spacing": 100e9, "power_dbm": 0, "power_range_db": [0, 0, 1],
                "roll_off": 0.15, "tx_osnr": 40, "sys_margins": 2}],
        "Transceiver": [{"type_variety": "vendor-A",
                         "frequency": {"min": 191.35e12, "max": 196.1e12}, "mode": []}],
    }


def model_to_gnpy_topology(model: NetworkModel) -> Dict[str, Any]:
    """Build the GNPy {elements, connections} dict from the model."""
    elements: List[Dict[str, Any]] = []
    for r in model._roadms.values():
        elements.append({"uid": r.id, "type": "Roadm"})
    for t in model._transceivers.values():
        elements.append({"uid": t.id, "type": "Transceiver"})
    for a in model._amplifiers.values():
        elements.append({"uid": a.id, "type": "Edfa",
                         "type_variety": nf_type_variety(a.nf_db),
                         "operational": {"gain_target": a.gain_db, "tilt_target": 0}})
    for f in model._fibers.values():
        loss = model.get_fiber_type(f.type_variety).loss_coef_db_per_km
        elements.append({"uid": f.id, "type": "Fiber", "type_variety": f.type_variety,
                         "params": {"length": f.length_km, "length_units": "km",
                                    "loss_coef": loss, "att_in": 0,
                                    "con_in": 0, "con_out": 0}})

    connections: List[Dict[str, str]] = []
    seen = set()

    def connect(a: str, b: str) -> None:
        if (a, b) not in seen:
            seen.add((a, b))
            connections.append({"from_node": a, "to_node": b})

    # Transceiver -> ROADM at the same node (trx_N -> roadm_N).
    for t in model._transceivers.values():
        connect(t.id, f"roadm_{t.site}")
        connect(f"roadm_{t.site}", t.id)

    # Walk each OMS element chain in order, then wire its last amp -> dst ROADM.
    for oms in model.list_oms():
        chain = list(oms.elements)
        for a, b in zip(chain, chain[1:]):
            connect(a, b)
        connect(chain[-1], f"roadm_{oms.dst_node_id}")

    return {"elements": elements, "connections": connections}
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/gnpy_adapter/test_synthesize.py -v`
Expected: PASS (3 tests).

> **If `test_nf_type_variety_carries_flat_nf_polynomial` reveals gnpy 2.11.1 will not accept an inline `advanced_config_from_json` dict** (it may require a file path), fall back to writing each distinct NF config to a temp file under a per-build temp dir and pass its path. Decide this in Task 5 against real propagation; keep the dict form here since it makes the unit test hermetic.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/gnpy_adapter/synthesize.py tests/gnpy_adapter/test_synthesize.py
git commit -m "feat(adapter): serialize model to GNPy topology+equipment dicts (per-NF type varieties)"
```

---

## Task 4: `build_gnpy_network(model)` via gnpy's own loaders

**Files:**
- Modify: `src/multilayer_optical_mcp/gnpy_adapter/synthesize.py`
- Test: `tests/gnpy_adapter/test_synthesize.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/gnpy_adapter/test_synthesize.py
def test_build_gnpy_network_returns_network_with_named_nodes():
    from multilayer_optical_mcp.gnpy_adapter.synthesize import build_gnpy_network
    eqpt, network = build_gnpy_network(_tiny_model())
    uids = {n.uid for n in network.nodes}
    assert "roadm_0" in uids and "roadm_1" in uids
    assert "trx_0" in uids and "trx_1" in uids
    assert "fiber_0_1_0" in uids
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/gnpy_adapter/test_synthesize.py::test_build_gnpy_network_returns_network_with_named_nodes -v`
Expected: FAIL (`build_gnpy_network` undefined).

- [ ] **Step 3: Implement `build_gnpy_network`**

```python
# append to src/multilayer_optical_mcp/gnpy_adapter/synthesize.py
def build_gnpy_network(model: NetworkModel):
    """Return (equipment, network) built from the model, ready to propagate.

    Reuses gnpy's network_from_json + build_network — the same code path load_toy
    uses — so synthesized results match a hand-written topology of the same shape.
    """
    from gnpy.tools.json_io import network_from_json
    from gnpy.core.equipment import trx_mode_params  # noqa: F401  (import sanity)

    equipment = _equipment_from_dict(model_to_gnpy_equipment(model))
    network = network_from_json(model_to_gnpy_topology(model), equipment)

    from gnpy.core.network import build_network
    build_network(network, equipment, pref_ch_db=0.0, pref_total_db=0.0)
    return equipment, network


def _equipment_from_dict(eqpt_dict: Dict[str, Any]):
    """Turn the equipment dict into gnpy Equipment objects.

    gnpy 2.11.1's public loader reads from a file; _equipment_from_json takes the
    already-parsed dict. If the internal name changes, the fallback writes the dict
    to a temp file and calls load_equipment.
    """
    try:
        from gnpy.tools.json_io import _equipment_from_json
        return _equipment_from_json(eqpt_dict, filename=None)
    except Exception:
        import json
        import tempfile
        from pathlib import Path
        from gnpy.tools.json_io import load_equipment
        tmp = Path(tempfile.mkdtemp()) / "eqpt.json"
        tmp.write_text(json.dumps(eqpt_dict))
        return load_equipment(tmp)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/gnpy_adapter/test_synthesize.py -v`
Expected: PASS. **If the `_equipment_from_json` signature differs in 2.11.1**, adjust the call (it may be `_equipment_from_json(json_data, filename)` positional) — the temp-file fallback guarantees a working path regardless.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/gnpy_adapter/synthesize.py tests/gnpy_adapter/test_synthesize.py
git commit -m "feat(adapter): build_gnpy_network(model) via gnpy network_from_json+build_network"
```

---

# Part C — rewire compute_qot + prove ground truth

## Task 5: Rewire `compute_qot` to build from the model + generic launch transceiver

**Files:**
- Modify: `src/multilayer_optical_mcp/gnpy_adapter/adapter.py`
- Test: existing `tests/gnpy_adapter/test_compute_qot.py`, `test_per_direction.py`, `test_toy_2route.py` (must stay green)

The model passed to `compute_qot` is the source of the GNPy network now. `oms_sequence` still selects the path; the network is `build_gnpy_network(model)`.

- [ ] **Step 1: Replace the network source.** In `compute_qot` (adapter.py ~152), replace:

```python
    eqpt, network = load_toy(
        eqpt_path=eqpt_path or DEFAULT_EQPT,
        topo_path=topo_path or DEFAULT_TOPO,
    )
    from gnpy.core.network import build_network as _build_network
    _build_network(network, eqpt, pref_ch_db=0.0, pref_total_db=0.0)
```

with:

```python
    from .synthesize import build_gnpy_network
    if topo_path is not None or eqpt_path is not None:
        # Legacy file path retained only for the bridge test / legacy 2route test.
        eqpt, network = load_toy(eqpt_path=eqpt_path or DEFAULT_EQPT,
                                 topo_path=topo_path or DEFAULT_TOPO)
        from gnpy.core.network import build_network as _build_network
        _build_network(network, eqpt, pref_ch_db=0.0, pref_total_db=0.0)
    else:
        eqpt, network = build_gnpy_network(model)
```

> Keeping the `topo_path`/`eqpt_path` branch means `test_toy_2route.py` (which passes `topo_path=`) keeps exercising the legacy oracle while the default path now synthesizes from the model. Both must agree at Task 7.

- [ ] **Step 2: Replace the `"trx A"` literal.** Replace adapter.py:194-197:

```python
    if "trx A" in by_uid:
        si = by_uid["trx A"](si)
```

with a generic per-direction lookup:

```python
    launch_trx = _find_launch_transceiver(network, uids_list, by_uid)
    if launch_trx is not None:
        si = launch_trx(si)
```

and add the helper near the top of adapter.py:

```python
def _find_launch_transceiver(network, path_uids, by_uid):
    """Return the GNPy Transceiver feeding the first path element, or None.

    Generic replacement for the hard-coded 'trx A': the launch transceiver is a
    Transceiver predecessor of path_uids[0] in the GNPy graph (e.g. trx_N -> roadm_N).
    """
    from gnpy.core.elements import Transceiver as _GnpyTrx
    first = by_uid.get(path_uids[0])
    if first is None:
        return None
    for pred in network.predecessors(first):
        if isinstance(pred, _GnpyTrx):
            return pred
    return None
```

- [ ] **Step 3: Run the existing adapter tests**

Run: `pytest tests/gnpy_adapter/ -v`
Expected: `test_toy_2route.py` PASS (uses `topo_path=`, legacy path). The single-channel/per-direction tests that call `compute_qot` **without** `topo_path` now synthesize from the model they construct — they should still PASS because those tests build a model whose OMS elements are the same uids. **If a test built a model whose element ids do not match GNPy uids** (e.g. the `tests/model/test_ip_routing.py:_two_link_model` style with ids like `a1`/`fAB`), that test was never calling real GNPy (it sets QoT directly) and is unaffected. Investigate any failure with superpowers:systematic-debugging — do not paper over a GSNR change.

- [ ] **Step 4: Commit**

```bash
git add src/multilayer_optical_mcp/gnpy_adapter/adapter.py
git commit -m "feat(adapter): compute_qot builds GNPy from model; generic launch-transceiver lookup"
```

---

## Task 6: Bridge ground-truth test — synthesized toy == load_toy

**Files:**
- Create: `tests/gnpy_adapter/test_ground_truth_bridge.py`

This is the gate for the entire synthesis approach. Build the model equivalent of `toy_2span.json` (single OMS: ROADM A, booster, fiber 80 km, ILA amp, fiber 80 km, preamp; all NF 5.5) and assert the synthesized GSNR matches the legacy `load_toy` GSNR.

- [ ] **Step 1: Write the test**

```python
# tests/gnpy_adapter/test_ground_truth_bridge.py
"""Gate: a model synthesized to match toy_2span must reproduce load_toy GSNR."""
from pathlib import Path

from multilayer_optical_mcp.gnpy_adapter.adapter import compute_qot
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.model.assets import (
    Amplifier, Direction, Fiber, FiberType, OMS, ROADM, Transceiver, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot_results import QoTResultStore

REPO_ROOT = Path(__file__).resolve().parents[2]
TOY = REPO_ROOT / "topologies" / "toy_2span.json"
MODE = "400G@7.1dB"
TOL_DB = 0.25  # GSNR agreement tolerance between synthesized and file-loaded toy


def _mode():
    return TransceiverMode(id=MODE, bitrate_gbps=400.0, required_gsnr_db=7.1,
                           symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)


def _toy_model_matching_2span() -> NetworkModel:
    """Model whose synthesized GNPy network mirrors toy_2span.json."""
    n = NetworkModel(modes=ModeRegistry([_mode()]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_roadm(ROADM(id="roadm_A"))
    n.add_roadm(ROADM(id="roadm_Z"))
    n.add_transceiver(Transceiver(id="trx_A", site="A"))
    n.add_transceiver(Transceiver(id="trx_Z", site="Z"))
    n.add_amplifier(Amplifier(id="amp_A_Z_booster", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fiber_A_Z_0", a_end="roadm_A", z_end="amp_A_Z_0",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp_A_Z_0", type_variety="advanced_toy",
                              gain_db=16.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fiber_A_Z_1", a_end="amp_A_Z_0", z_end="amp_A_Z_1",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp_A_Z_1", type_variety="advanced_toy",
                              gain_db=16.0, nf_db=5.5))
    n.add_oms(OMS(id="oms_A_Z", src_node_id="A", dst_node_id="Z", elements=(
        "roadm_A", "amp_A_Z_booster", "fiber_A_Z_0", "amp_A_Z_0",
        "fiber_A_Z_1", "amp_A_Z_1")))
    return n


def _gsnr_synthesized(model) -> float:
    store = QoTResultStore()
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, MODE),))
    state, _ = compute_qot(model=model, store=store, oms_sequence=("oms_A_Z",),
                           direction=Direction.FORWARD, mode_id=MODE, loading=loading)
    return state.gsnr_db


def _gsnr_legacy(model) -> float:
    """Use the legacy file path on the SAME probe via the existing toy_2span OMS."""
    # The legacy toy OMS elements are the gnpy uids in toy_2span.json.
    from multilayer_optical_mcp.model.assets import OMS as _OMS
    model.add_oms(_OMS(id="oms_legacy", src_node_id="A", dst_node_id="Z", elements=(
        "ROADM A", "booster A", "east fiber A to ILA", "east edfa in ILA",
        "east fiber ILA to Z", "east edfa at Z")))
    store = QoTResultStore()
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, MODE),))
    state, _ = compute_qot(model=model, store=store, oms_sequence=("oms_legacy",),
                           direction=Direction.FORWARD, mode_id=MODE, loading=loading,
                           topo_path=TOY)
    return state.gsnr_db


def test_synthesized_toy_matches_file_loaded_toy():
    model = _toy_model_matching_2span()
    g_syn = _gsnr_synthesized(model)
    g_leg = _gsnr_legacy(model)
    assert abs(g_syn - g_leg) < TOL_DB, (
        f"synthesized GSNR {g_syn:.3f} dB diverges from file-loaded {g_leg:.3f} dB"
    )
```

- [ ] **Step 2: Run the gate**

Run: `pytest tests/gnpy_adapter/test_ground_truth_bridge.py -v`
Expected: PASS within `TOL_DB`.

> **If it fails:** the divergence is the signal to resolve before going further. Likely causes, in order: (a) booster/ILA `gain_target` differs from what `build_network` derives for the file topology — align `gain_db` in the model so the synthesized booster gain equals the file's (read the file's effective gains by printing `el.operational.gain_target` for both networks); (b) the inline `advanced_config_from_json` dict was not honoured and NF defaulted — switch to the temp-file fallback from Task 3; (c) ROADM `add_drop_osnr`/`target_pch_out_db` mismatch. Use superpowers:systematic-debugging; do **not** widen `TOL_DB` to force a pass.

- [ ] **Step 3: Commit**

```bash
git add tests/gnpy_adapter/test_ground_truth_bridge.py
git commit -m "test(adapter): bridge ground-truth — synthesized toy matches file-loaded GSNR"
```

---

## Task 7: `german_17` end-to-end QoT ground-truth

**Files:**
- Modify: `tests/gnpy_adapter/test_ground_truth_bridge.py`

- [ ] **Step 1: Write the test**

```python
# append to tests/gnpy_adapter/test_ground_truth_bridge.py
import json
import math
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph

GERMAN_17 = REPO_ROOT / "topologies" / "german_17.json"


def test_german_17_routed_path_finite_and_monotone():
    graph = json.loads(GERMAN_17.read_text())
    model = model_from_abstract_graph(graph, modes=ModeRegistry([_mode()]))
    store = QoTResultStore()
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, MODE),))

    def gsnr(oms_id: str) -> float:
        st, _ = compute_qot(model=model, store=store, oms_sequence=(oms_id,),
                            direction=Direction.FORWARD, mode_id=MODE, loading=loading)
        return st.gsnr_db

    # edge 2-3 is 37 km (1 span); edge 9-10 is 353 km (4 spans). Longer -> lower GSNR.
    g_short = gsnr("oms_2_3")
    g_long = gsnr("oms_9_10")
    assert math.isfinite(g_short) and math.isfinite(g_long)
    assert g_long < g_short
```

- [ ] **Step 2: Run**

Run: `pytest tests/gnpy_adapter/test_ground_truth_bridge.py::test_german_17_routed_path_finite_and_monotone -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/gnpy_adapter/test_ground_truth_bridge.py
git commit -m "test(adapter): german_17 imports and propagates; longer path has lower GSNR"
```

---

## Task 8: Full regression sweep

- [ ] **Step 1: Run the whole suite**

Run: `pytest -q`
Expected: all green. Pay special attention to `tests/gnpy_adapter/` and any test that called `compute_qot` without `topo_path`.

- [ ] **Step 2: If anything regressed**, debug with superpowers:systematic-debugging. A GSNR shift on a previously-passing test means the synthesized network differs from the file network for that topology — fix the synthesizer, not the assertion.

- [ ] **Step 3: Commit any fixes, then finalize**

```bash
git add -A && git commit -m "test: phase-6a regression sweep green"
```

---

## Self-review checklist (run before handoff)

- [ ] **Spec coverage:** abstract→model importer (Task 2), `split_link_into_spans` (Task 1), model→GNPy synthesizer with model-sourced NF (Tasks 3-4), `compute_qot` rebuilt from model + generic launch transceiver (Task 5), ground-truth gate (Task 6), german_17 proof (Task 7). All present.
- [ ] **`"trx A"` literal removed** and replaced by `_find_launch_transceiver` (Task 5).
- [ ] **Single source of truth:** no QoT-time read of a topology file on the default path; NF flows from `Amplifier.nf_db` (Tasks 3, 6).
- [ ] **Type consistency:** `nf_type_variety`, `model_to_gnpy_topology`, `model_to_gnpy_equipment`, `build_gnpy_network`, `model_from_abstract_graph`, `split_link_into_spans`, `_find_launch_transceiver` — names identical across tasks.
- [ ] **Open GNPy-API risks are gated by tests, not assertions:** inline-vs-file advanced config (Task 3/4 fallback), `_equipment_from_json` signature (Task 4 fallback), synthesized-gain alignment (Task 6 debug note).

---

## What 6a deliberately leaves for 6b

- No `inject_degradation` / `inject_failure` / `whatif_margin_threshold_sweep` yet. 6a only makes NF/loss model-sourced so 6b can perturb them on a branch and re-synthesize.
- No new server tool for topology import is required by 6b; if you want to expose ingestion, add `import_topology(graph_json)` to `server.py` mirroring the snapshot pattern — but it is optional and not on the CLAUDE.md tool surface.
