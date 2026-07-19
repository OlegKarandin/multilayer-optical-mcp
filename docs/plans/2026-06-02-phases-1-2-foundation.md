# multilayer-optical-mcp — Phases 1–2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the in-memory multilayer network model + snapshot/branch engine (Phase 1) and the GNPy adapter with arbitrary-loading and per-direction QoT contracts (Phase 2), with a two-tier QoT result contract (compact summary + cached per-element breakdown), exposed through a thin FastMCP shell.

**Architecture:** Pure-Python library under `src/multilayer_optical_mcp/` with two packages — `model/` (typed assets, multilayer state, observed QoT, COW snapshots, QoT result cache) and `gnpy_adapter/` (loading state, OMS→GNPy translator, `compute_qot`). `server.py` exposes a subset of CLAUDE.md's tool surface via FastMCP. No solvers, no IP routing, no validate/commit, no risk groups — those are later plans.

**Tech Stack:** Python 3.11+, FastMCP (`mcp[cli]>=1.0`), `gnpy` (pinned), PyYAML, NetworkX, pytest.

---

## Context

CLAUDE.md describes a greenfield MCP server whose load-bearing contract is a GNPy adapter that (a) accepts an **arbitrary constructed loading state** (not just "the current network") and (b) returns **per-direction** QoT. Both must be proven at Build Order step 2 because every downstream feature depends on them.

Six model decisions agreed during planning:

1. **Modulation formats come from `modulation_formats.yaml`** at the repo root: 11 formats from 300G→800G sharing a global 87.5 GBd symbol rate and 100 GHz channel spacing. Each `TransceiverMode` carries `bitrate_gbps`, `required_gsnr_db`, `symbol_rate_baud`, `channel_spacing_hz` — the latter two replicated from the YAML's globals so modes are self-contained.
2. **Fiber loss lives on `FiberType`, not on `Fiber`.** Fiber instances carry `length_km` + a `type_variety` FK; a `FiberTypeRegistry` inside `NetworkModel` holds loss/dispersion/gamma/PMD.
3. **OMS (Optical Multiplex Section) is the path primitive.** Each OMS bounds an ordered amp/fiber sequence between two endpoint sites. `Lightpath.oms_sequence: Tuple[str, ...]` is a tuple of OMS ids; the translator expands each OMS to its flat element list.
4. **Observed QoT lives in `NetworkModel._qot_state: Dict[lp_id, QoTState]`, not on `Lightpath`.** `Lightpath` stays a structural atom. `QoTState` records GSNR/OSNR/margin observed under the snapshot's loading. Keeps structural diffs clean and acknowledges QoT = f(lightpath, loading).
5. **IP link capacity = f(`mode.bitrate_gbps`, `qot_state.margin_db`).** No setter. `margin_db < 0` → capacity 0 (CLAUDE.md Design Rules #5–#6).
6. **Two-tier QoT result contract.** A scalar margin tells the agent the path failed; it doesn't tell it *where* to act. Every `compute_qot` call walks GNPy elements anyway, so the per-element progression is essentially free at generation time. We capture it once and serve it through a two-tier interface:
    - **Compact return** — `QoTState` carries `gsnr_db`, `osnr_db`, `margin_db`, and `limiting_element_id` (the stable model uid of the element contributing the largest per-element GSNR drop). Plus a `result_id` to retrieve detail.
    - **Detail on demand** — `get_qot_breakdown(result_id)` returns the cached `QoTBreakdown` (per-element snapshots + the limiting-element pointer). Never re-propagates; cap + TTL on the store match the snapshot-store discipline.
    - **Out of scope here**: sensitivity (derive by differencing breakdowns across branches) and per-channel loading attribution (next plan, lands with what-if injection).

Two CLAUDE.md adapter requirements:
- Loading state is a constructed first-class input, never a query against "current."
- Reference topologies use GNPy's advanced/explicit amplifier model, not `variable_gain`.

---

## File Structure

```
multilayer-optical-mcp/
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
├── .gitignore
├── modulation_formats.yaml                     # provided; loaded by mode loader
├── src/
│   └── multilayer_optical_mcp/
│       ├── __init__.py
│       ├── model/
│       │   ├── __init__.py
│       │   ├── assets.py                       # frozen structural entities
│       │   ├── qot.py                          # QoTState, ElementSnapshot, QoTBreakdown
│       │   ├── qot_results.py                  # QoTResultStore (id-addressed cache)
│       │   ├── modes.py                        # ModeRegistry + YAML loader
│       │   ├── network.py                      # NetworkModel + invariants + capacity
│       │   └── snapshots.py                    # COW snapshot/branch/diff/restore + lifecycle
│       ├── gnpy_adapter/
│       │   ├── __init__.py
│       │   ├── loading.py                      # Channel, LoadingState
│       │   ├── translate.py                    # OMS->uids resolution + SI builder
│       │   └── adapter.py                      # compute_qot, gated_qot, recompute_qot_under_loading
│       └── server.py                           # FastMCP wiring
├── topologies/
│   └── toy_2span.json                          # advanced-amp 2-span reference (GNPy)
├── eqpt/
│   └── eqpt_config.json                        # equipment library w/ advanced amp
└── tests/
    ├── conftest.py
    ├── model/
    │   ├── test_assets.py
    │   ├── test_modes.py
    │   ├── test_network.py
    │   ├── test_capacity_coupling.py
    │   ├── test_snapshots.py
    │   ├── test_snapshot_lifecycle.py
    │   └── test_qot_results.py
    ├── gnpy_adapter/
    │   ├── test_loading.py
    │   ├── test_translate.py
    │   ├── test_compute_qot.py
    │   ├── test_per_direction.py
    │   └── test_recompute_under_loading.py
    └── test_server.py
```

**Module boundaries:**
- `model/assets.py` — structural entities only.
- `model/qot.py` — observed-state types (`QoTState`, `ElementSnapshot`, `QoTBreakdown`).
- `model/qot_results.py` — id-addressed `QoTResultStore` with cap + TTL.
- `model/modes.py` — modulation-format registry + YAML loader.
- `model/network.py` — multilayer container + invariants + derived capacity.
- `model/snapshots.py` — COW state evolution.
- `gnpy_adapter/*` — only place that imports `gnpy`.
- `server.py` — FastMCP glue.

---

## Task 0: Project bootstrap

**Files:**
- Create: `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `.gitignore`
- Create: `src/multilayer_optical_mcp/__init__.py`, `src/multilayer_optical_mcp/model/__init__.py`, `src/multilayer_optical_mcp/gnpy_adapter/__init__.py`
- Create: `tests/__init__.py`, `tests/conftest.py`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "multilayer-optical-mcp"
version = "0.1.0"
description = "MCP server for multi-layer optical network reasoning (GNPy QoT + IP-over-optical)."
requires-python = ">=3.11"
readme = "CLAUDE.md"
dependencies = [
    "mcp[cli]>=1.0",
    "gnpy==2.11",
    "networkx>=3.2",
    "numpy>=1.26",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-cov>=5", "ruff>=0.5"]

[project.scripts]
multilayer-optical-mcp = "multilayer_optical_mcp.server:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"
```

- [ ] **Step 2: Write `requirements.txt`**

```
mcp[cli]==1.2.0
gnpy==2.11
networkx==3.3
numpy==1.26.4
pyyaml==6.0.1
```

- [ ] **Step 3: Write `requirements-dev.txt`**

```
-r requirements.txt
pytest==8.3.2
pytest-cov==5.0.0
ruff==0.6.2
```

- [ ] **Step 4: Write `.gitignore`**

```
__pycache__/
*.py[cod]
*.egg-info/
.venv/
.pytest_cache/
.coverage
htmlcov/
.mypy_cache/
.ruff_cache/
build/
dist/
```

- [ ] **Step 5: Create empty package files**

`src/multilayer_optical_mcp/__init__.py`:
```python
__version__ = "0.1.0"
```

`src/multilayer_optical_mcp/model/__init__.py`: empty.
`src/multilayer_optical_mcp/gnpy_adapter/__init__.py`: empty.
`tests/__init__.py`: empty.
`tests/conftest.py`:
```python
# Shared fixtures land here as the suite grows.
```

- [ ] **Step 6: Verify install + empty pytest run**

Run: `pip install -e ".[dev]"`
Expected: succeeds.

Run: `pytest`
Expected: `no tests ran` (exit code 5 is fine).

- [ ] **Step 7: Commit**

```bash
git init
git add .
git commit -m "chore: project bootstrap (pyproject, pip, pytest layout)"
```

---

## Task 1: Frozen structural dataclasses + QoT types

**Files:**
- Create: `src/multilayer_optical_mcp/model/assets.py`
- Create: `src/multilayer_optical_mcp/model/qot.py`
- Test: `tests/model/test_assets.py`

All structural entities are frozen dataclasses. `qot.py` holds three observed-state types: `QoTState` (compact summary, used both as adapter return and snapshot-stored state), `ElementSnapshot` (one element's contribution along a path), and `QoTBreakdown` (ordered snapshots + the limiting-element pointer).

- [ ] **Step 1: Write failing test**

`tests/model/__init__.py`: empty.

`tests/model/test_assets.py`:
```python
from multilayer_optical_mcp.model.assets import (
    OpticalNode, FiberType, Fiber, Amplifier, ROADM, Transceiver,
    TransceiverMode, OMS, Lightpath, Router, IPLink, Service,
    SRLG, RiskGroup, Direction,
)
from multilayer_optical_mcp.model.qot import (
    QoTState, ElementSnapshot, QoTBreakdown,
)


def test_fiber_is_frozen_and_carries_only_instance_state():
    f = Fiber(id="f1", a_end="amp-A", z_end="amp-B",
              length_km=80.0, type_variety="SSMF")
    assert f.length_km == 80.0
    assert not hasattr(f, "loss_coef_db_per_km")  # lives on FiberType


def test_fiber_type_carries_loss_and_optical_params():
    ft = FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2)
    assert ft.loss_coef_db_per_km == 0.2
    assert ft.dispersion > 0


def test_transceiver_mode_carries_bitrate_baud_spacing():
    m = TransceiverMode(
        id="400G@7.1dB",
        bitrate_gbps=400.0,
        required_gsnr_db=7.1,
        symbol_rate_baud=87.5e9,
        channel_spacing_hz=100e9,
    )
    assert m.bitrate_gbps == 400.0
    assert m.symbol_rate_baud == 87.5e9


def test_oms_carries_endpoints_and_element_sequence():
    oms = OMS(id="oms-AZ", src_node_id="trx A", dst_node_id="trx Z",
              elements=("amp-1", "fiber-1", "amp-2", "fiber-2"))
    assert len(oms.elements) == 4


def test_lightpath_uses_oms_sequence_no_slot_width_no_margin():
    lp = Lightpath(id="lp1", oms_sequence=("oms-AZ",),
                   mode_id="400G@7.1dB", center_freq_hz=193.4e12)
    assert lp.oms_sequence == ("oms-AZ",)
    assert not hasattr(lp, "slot_width_hz")
    assert not hasattr(lp, "margin_db")
    assert not hasattr(lp, "path")


def test_ip_link_bound_to_lightpath_no_capacity_field():
    link = IPLink(id="ip-1", a_router="R1", z_router="R2", lightpath_id="lp1")
    assert not hasattr(link, "capacity_gbps")


def test_direction_enum():
    assert Direction.FORWARD.value == "forward"
    assert Direction.BACKWARD.value == "backward"


def test_qot_state_carries_limiting_element_and_derived_feasibility():
    ok = QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=3.5,
                  limiting_element_id="east edfa in A to ILA")
    bad = QoTState(gsnr_db=10.0, osnr_db=12.0, margin_db=-0.5,
                   limiting_element_id="east fiber ILA to Z")
    assert ok.mode_feasible is True
    assert bad.mode_feasible is False
    assert bad.limiting_element_id == "east fiber ILA to Z"


def test_qot_state_defaults_limiting_element_to_none():
    s = QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=3.5)
    assert s.limiting_element_id is None


def test_element_snapshot_records_per_element_contribution():
    es = ElementSnapshot(
        element_id="east fiber A to ILA",
        gsnr_db_after=18.0,
        osnr_db_after=20.0,
        gsnr_delta_db=-2.0,
        ase_contribution_db=-1.2,
        nli_contribution_db=-0.8,
    )
    assert es.gsnr_delta_db == -2.0


def test_qot_breakdown_holds_ordered_snapshots_and_limiting_pointer():
    bd = QoTBreakdown(
        snapshots=(
            ElementSnapshot("amp1", 22.0, 23.0, 0.0, 0.0, 0.0),
            ElementSnapshot("fiber1", 19.0, 20.5, -3.0, -1.5, -1.5),
            ElementSnapshot("amp2", 18.5, 20.0, -0.5, -0.5, 0.0),
        ),
        limiting_element_id="fiber1",
    )
    assert bd.snapshots[1].element_id == "fiber1"
    assert bd.limiting_element_id == "fiber1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/model/test_assets.py -v`
Expected: FAIL — ImportError on every new name.

- [ ] **Step 3: Implement `assets.py`**

`src/multilayer_optical_mcp/model/assets.py`:
```python
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Tuple


class Direction(str, Enum):
    FORWARD = "forward"
    BACKWARD = "backward"


@dataclass(frozen=True)
class OpticalNode:
    id: str
    kind: str  # "roadm" | "amplifier_site" | "transceiver_site"


@dataclass(frozen=True)
class FiberType:
    """Intrinsic optical properties of a fiber class (SSMF, NZDSF, ...).
    A `Fiber` instance references one of these by `type_variety`."""
    type_variety: str
    loss_coef_db_per_km: float
    dispersion: float = 1.67e-05    # s/m/m
    gamma: float = 0.00127          # 1/W/m
    pmd_coef: float = 1.265e-15     # s/sqrt(m)


@dataclass(frozen=True)
class Fiber:
    """A physical span. Length per-instance; optical params come from FiberType."""
    id: str
    a_end: str
    z_end: str
    length_km: float
    type_variety: str


@dataclass(frozen=True)
class Amplifier:
    id: str
    type_variety: str               # MUST match an advanced-model entry in eqpt_config
    gain_db: float
    nf_db: float
    tilt_db: float = 0.0


@dataclass(frozen=True)
class ROADM:
    id: str
    target_pch_out_db: float = -20.0


@dataclass(frozen=True)
class Transceiver:
    id: str
    site: str
    supported_mode_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class TransceiverMode:
    id: str
    bitrate_gbps: float
    required_gsnr_db: float
    symbol_rate_baud: float
    channel_spacing_hz: float


@dataclass(frozen=True)
class OMS:
    """Optical Multiplex Section: endpoint-to-endpoint ordered list of
    amp/fiber uids. Endpoints are ROADM or transceiver-site uids; `elements`
    holds the booster/ILA/preamp + spans between them, in order."""
    id: str
    src_node_id: str
    dst_node_id: str
    elements: Tuple[str, ...]


@dataclass(frozen=True)
class Lightpath:
    id: str
    oms_sequence: Tuple[str, ...]
    mode_id: str
    center_freq_hz: float


@dataclass(frozen=True)
class Router:
    id: str
    site: str


@dataclass(frozen=True)
class IPLink:
    id: str
    a_router: str
    z_router: str
    lightpath_id: str


@dataclass(frozen=True)
class Service:
    id: str
    src_router: str
    dst_router: str
    demand_gbps: float
    working_path: Tuple[str, ...] = ()
    protection_path: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SRLG:
    id: str
    asset_ids: Tuple[str, ...]


@dataclass(frozen=True)
class RiskGroup:
    id: str
    asset_ids: Tuple[str, ...]
    metadata: dict = field(default_factory=dict)
```

- [ ] **Step 4: Implement `qot.py`**

`src/multilayer_optical_mcp/model/qot.py`:
```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class QoTState:
    """Compact summary of QoT for a lightpath under some loading.

    `limiting_element_id` is the stable model uid of the element contributing
    the largest per-element GSNR drop along the path. Agents feed it straight
    into `inject_degradation` or `compute_paths` constraints — no string parsing.
    """
    gsnr_db: float
    osnr_db: float
    margin_db: float
    limiting_element_id: Optional[str] = None

    @property
    def mode_feasible(self) -> bool:
        return self.margin_db >= 0


@dataclass(frozen=True)
class ElementSnapshot:
    """One element's contribution as the probe carrier propagated through it.
    Deltas are *after - before* (typically negative for amps/fibers)."""
    element_id: str
    gsnr_db_after: float
    osnr_db_after: float
    gsnr_delta_db: float
    ase_contribution_db: float
    nli_contribution_db: float


@dataclass(frozen=True)
class QoTBreakdown:
    """Per-element propagation along an OMS-sequence path, plus the limiting
    element. Cached by `QoTResultStore`; served by `get_qot_breakdown`."""
    snapshots: Tuple[ElementSnapshot, ...]
    limiting_element_id: Optional[str]
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/model/test_assets.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/multilayer_optical_mcp/model/assets.py \
        src/multilayer_optical_mcp/model/qot.py \
        tests/model/test_assets.py tests/model/__init__.py
git commit -m "feat(model): frozen structural entities + QoT observed-state types"
```

---

## Task 2: ModeRegistry + YAML loader

**Files:**
- Create: `src/multilayer_optical_mcp/model/modes.py`
- Test: `tests/model/test_modes.py`

- [ ] **Step 1: Write failing test**

`tests/model/test_modes.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/model/test_modes.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `modes.py`**

`src/multilayer_optical_mcp/model/modes.py`:
```python
from __future__ import annotations
from pathlib import Path
from typing import Iterable, Tuple
import yaml
from .assets import TransceiverMode


class ModeRegistry:
    def __init__(self, modes: Iterable[TransceiverMode]) -> None:
        self._by_id = {m.id: m for m in modes}

    def get(self, mode_id: str) -> TransceiverMode:
        return self._by_id[mode_id]

    def list(self) -> Tuple[TransceiverMode, ...]:
        return tuple(self._by_id.values())


def load_modulation_formats(yaml_path: Path) -> ModeRegistry:
    raw = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8"))
    spacing_hz = float(raw["channel_spacing_ghz"]) * 1e9
    baud = float(raw["symbol_rate_gbaud"]) * 1e9
    modes = []
    for f in raw["formats"]:
        bitrate = float(f["bitrate_gbps"])
        threshold = float(f["snr_threshold_db"])
        modes.append(TransceiverMode(
            id=f"{int(bitrate)}G@{threshold}dB",
            bitrate_gbps=bitrate,
            required_gsnr_db=threshold,
            symbol_rate_baud=baud,
            channel_spacing_hz=spacing_hz,
        ))
    return ModeRegistry(modes)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/model/test_modes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/modes.py tests/model/test_modes.py
git commit -m "feat(model): mode registry + modulation_formats.yaml loader"
```

---

## Task 3: NetworkModel container (with FiberType + OMS + invariants)

**Files:**
- Create: `src/multilayer_optical_mcp/model/network.py`
- Test: `tests/model/test_network.py`

Invariants on add:
1. `Fiber.type_variety` → registered `FiberType`.
2. `OMS.elements` → all registered fibers or amplifiers.
3. `Lightpath.oms_sequence` → all registered OMS.
4. `Lightpath.mode_id` → registered mode.
5. `IPLink.lightpath_id` → registered lightpath.

- [ ] **Step 1: Write failing test**

`tests/model/test_network.py`:
```python
import pytest
from multilayer_optical_mcp.model.assets import (
    Fiber, FiberType, Amplifier, Lightpath, Router, IPLink, OMS, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel


def _registry():
    return ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ])


def _bare():
    return NetworkModel(modes=_registry())


def test_fiber_requires_registered_type():
    n = _bare()
    with pytest.raises(ValueError, match="unknown fiber type"):
        n.add_fiber(Fiber(id="f1", a_end="A", z_end="B",
                          length_km=80.0, type_variety="SSMF"))


def test_fiber_accepted_after_type_registered():
    n = _bare()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    f = Fiber(id="f1", a_end="A", z_end="B", length_km=80.0, type_variety="SSMF")
    n.add_fiber(f)
    assert n.get_fiber("f1") is f
    assert n.get_fiber_type("SSMF").loss_coef_db_per_km == 0.2


def test_oms_requires_existing_elements():
    n = _bare()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    with pytest.raises(ValueError, match="neither fiber nor amplifier"):
        n.add_oms(OMS(id="oms1", src_node_id="A", dst_node_id="B",
                      elements=("amp1", "fiber-missing")))


def test_lightpath_requires_existing_oms():
    n = _bare()
    with pytest.raises(ValueError, match="unknown OMS"):
        n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms-missing",),
                                  mode_id="100G-QPSK", center_freq_hz=193.4e12))


def test_ip_link_requires_existing_lightpath():
    n = _bare()
    n.add_router(Router(id="R1", site="A"))
    n.add_router(Router(id="R2", site="B"))
    with pytest.raises(ValueError, match="unknown lightpath"):
        n.add_ip_link(IPLink(id="ip1", a_router="R1", z_router="R2",
                             lightpath_id="lp-missing"))


def test_full_happy_path_add_chain():
    n = _bare()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="amp1", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="f1", a_end="amp1", z_end="amp2",
                      length_km=80.0, type_variety="SSMF"))
    n.add_amplifier(Amplifier(id="amp2", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    n.add_oms(OMS(id="oms1", src_node_id="trxA", dst_node_id="trxB",
                  elements=("amp1", "f1", "amp2")))
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms1",),
                              mode_id="100G-QPSK", center_freq_hz=193.4e12))
    n.add_router(Router(id="R1", site="A"))
    n.add_router(Router(id="R2", site="B"))
    n.add_ip_link(IPLink(id="ip1", a_router="R1", z_router="R2",
                         lightpath_id="lp1"))
    assert n.get_ip_link("ip1").lightpath_id == "lp1"
    assert n.get_oms("oms1").elements == ("amp1", "f1", "amp2")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/model/test_network.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `network.py`**

`src/multilayer_optical_mcp/model/network.py`:
```python
from __future__ import annotations
from dataclasses import replace
from typing import Dict, Tuple
from .assets import (
    OpticalNode, FiberType, Fiber, Amplifier, ROADM, Transceiver, OMS,
    Lightpath, Router, IPLink, Service, SRLG, RiskGroup,
)
from .modes import ModeRegistry
from .qot import QoTState


class NetworkModel:
    def __init__(self, modes: ModeRegistry) -> None:
        self.modes = modes
        self._fiber_types: Dict[str, FiberType] = {}
        self._optical_nodes: Dict[str, OpticalNode] = {}
        self._fibers: Dict[str, Fiber] = {}
        self._amplifiers: Dict[str, Amplifier] = {}
        self._roadms: Dict[str, ROADM] = {}
        self._transceivers: Dict[str, Transceiver] = {}
        self._oms: Dict[str, OMS] = {}
        self._lightpaths: Dict[str, Lightpath] = {}
        self._routers: Dict[str, Router] = {}
        self._ip_links: Dict[str, IPLink] = {}
        self._services: Dict[str, Service] = {}
        self._srlgs: Dict[str, SRLG] = {}
        self._risk_groups: Dict[str, RiskGroup] = {}
        self._qot_state: Dict[str, QoTState] = {}

    def register_fiber_type(self, ft: FiberType) -> None:
        self._fiber_types[ft.type_variety] = ft

    def get_fiber_type(self, type_variety: str) -> FiberType:
        return self._fiber_types[type_variety]

    def add_optical_node(self, n: OpticalNode) -> None: self._optical_nodes[n.id] = n

    def add_fiber(self, f: Fiber) -> None:
        if f.type_variety not in self._fiber_types:
            raise ValueError(f"unknown fiber type {f.type_variety!r}")
        self._fibers[f.id] = f

    def add_amplifier(self, a: Amplifier) -> None: self._amplifiers[a.id] = a
    def add_roadm(self, r: ROADM) -> None: self._roadms[r.id] = r
    def add_transceiver(self, t: Transceiver) -> None: self._transceivers[t.id] = t

    def add_oms(self, oms: OMS) -> None:
        for el in oms.elements:
            if el not in self._fibers and el not in self._amplifiers:
                raise ValueError(
                    f"OMS {oms.id!r}: element {el!r} is neither fiber nor amplifier"
                )
        self._oms[oms.id] = oms

    def add_lightpath(self, lp: Lightpath) -> None:
        self.modes.get(lp.mode_id)
        for oms_id in lp.oms_sequence:
            if oms_id not in self._oms:
                raise ValueError(f"unknown OMS {oms_id!r}")
        self._lightpaths[lp.id] = lp

    def add_router(self, r: Router) -> None: self._routers[r.id] = r

    def add_ip_link(self, link: IPLink) -> None:
        if link.lightpath_id not in self._lightpaths:
            raise ValueError(f"unknown lightpath {link.lightpath_id!r}")
        self._ip_links[link.id] = link

    def add_service(self, s: Service) -> None: self._services[s.id] = s
    def add_srlg(self, g: SRLG) -> None: self._srlgs[g.id] = g
    def add_risk_group(self, g: RiskGroup) -> None: self._risk_groups[g.id] = g

    def get_fiber(self, fid: str) -> Fiber: return self._fibers[fid]
    def get_amplifier(self, aid: str) -> Amplifier: return self._amplifiers[aid]
    def get_oms(self, oid: str) -> OMS: return self._oms[oid]
    def get_lightpath(self, lpid: str) -> Lightpath: return self._lightpaths[lpid]
    def get_ip_link(self, lid: str) -> IPLink: return self._ip_links[lid]
    def get_router(self, rid: str) -> Router: return self._routers[rid]

    def list_fiber_types(self) -> Tuple[FiberType, ...]: return tuple(self._fiber_types.values())
    def list_oms(self) -> Tuple[OMS, ...]: return tuple(self._oms.values())
    def list_lightpaths(self) -> Tuple[Lightpath, ...]: return tuple(self._lightpaths.values())
    def list_ip_links(self) -> Tuple[IPLink, ...]: return tuple(self._ip_links.values())
    def list_services(self) -> Tuple[Service, ...]: return tuple(self._services.values())
    def list_srlgs(self) -> Tuple[SRLG, ...]: return tuple(self._srlgs.values())
    def list_risk_groups(self) -> Tuple[RiskGroup, ...]: return tuple(self._risk_groups.values())
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/model/test_network.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/network.py tests/model/test_network.py
git commit -m "feat(model): NetworkModel with FiberType, OMS, and structural invariants"
```

---

## Task 4: Derived IP capacity + QoT state coupling

**Files:**
- Modify: `src/multilayer_optical_mcp/model/network.py`
- Test: `tests/model/test_capacity_coupling.py`

- [ ] **Step 1: Write failing test**

`tests/model/test_capacity_coupling.py`:
```python
import pytest
from multilayer_optical_mcp.model.assets import (
    FiberType, Amplifier, Fiber, OMS, Lightpath, Router, IPLink, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState


def _model_with_lightpath(mode_id="200G-16QAM"):
    reg = ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
        TransceiverMode(id="200G-16QAM", bitrate_gbps=200.0,
                        required_gsnr_db=18.5, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ])
    n = NetworkModel(modes=reg)
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
                              mode_id=mode_id, center_freq_hz=193.4e12))
    n.add_router(Router(id="R1", site="A"))
    n.add_router(Router(id="R2", site="B"))
    n.add_ip_link(IPLink(id="ip1", a_router="R1", z_router="R2",
                         lightpath_id="lp1"))
    return n


def test_capacity_reads_mode_bitrate_when_margin_positive():
    n = _model_with_lightpath("200G-16QAM")
    n.set_qot_state("lp1", QoTState(gsnr_db=22.0, osnr_db=24.0, margin_db=3.5,
                                    limiting_element_id="amp1"))
    assert n.ip_link_capacity_gbps("ip1") == 200.0


def test_capacity_follows_mode_downshift():
    n = _model_with_lightpath("200G-16QAM")
    n.set_qot_state("lp1", QoTState(gsnr_db=22.0, osnr_db=24.0, margin_db=3.0))
    assert n.ip_link_capacity_gbps("ip1") == 200.0
    n.set_lightpath_mode("lp1", "100G-QPSK")
    n.set_qot_state("lp1", QoTState(gsnr_db=17.0, osnr_db=19.0, margin_db=5.0))
    assert n.ip_link_capacity_gbps("ip1") == 100.0


def test_capacity_zero_when_margin_negative():
    n = _model_with_lightpath("200G-16QAM")
    n.set_qot_state("lp1", QoTState(gsnr_db=18.0, osnr_db=20.0, margin_db=-0.5))
    assert n.ip_link_capacity_gbps("ip1") == 0.0


def test_capacity_raises_without_qot_state():
    n = _model_with_lightpath("200G-16QAM")
    with pytest.raises(LookupError, match="no QoT state"):
        n.ip_link_capacity_gbps("ip1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/model/test_capacity_coupling.py -v`
Expected: FAIL.

- [ ] **Step 3: Extend `network.py`**

Append:
```python
    def set_qot_state(self, lp_id: str, state: QoTState) -> None:
        if lp_id not in self._lightpaths:
            raise KeyError(lp_id)
        self._qot_state[lp_id] = state

    def get_qot_state(self, lp_id: str) -> QoTState:
        if lp_id not in self._qot_state:
            raise LookupError(f"no QoT state recorded for lightpath {lp_id!r}")
        return self._qot_state[lp_id]

    def set_lightpath_mode(self, lp_id: str, mode_id: str) -> None:
        self.modes.get(mode_id)
        lp = self._lightpaths[lp_id]
        self._lightpaths[lp_id] = replace(lp, mode_id=mode_id)

    def ip_link_capacity_gbps(self, link_id: str) -> float:
        link = self._ip_links[link_id]
        lp = self._lightpaths[link.lightpath_id]
        state = self._qot_state.get(lp.id)
        if state is None:
            raise LookupError(f"no QoT state recorded for lightpath {lp.id!r}")
        if state.margin_db < 0:
            return 0.0
        return self.modes.get(lp.mode_id).bitrate_gbps
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/model/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/network.py tests/model/test_capacity_coupling.py
git commit -m "feat(model): derive IP capacity from QoT state + mode bitrate"
```

---

## Task 5: Snapshot create / branch / restore (COW)

**Files:**
- Create: `src/multilayer_optical_mcp/model/snapshots.py`
- Test: `tests/model/test_snapshots.py`

- [ ] **Step 1: Write failing test**

`tests/model/test_snapshots.py`:
```python
import pytest
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, Lightpath, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.snapshots import SnapshotStore


def _seed() -> NetworkModel:
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
    return n


def test_snapshot_create_returns_id():
    store = SnapshotStore(initial=_seed())
    assert isinstance(store.create(), str)


def test_branch_is_isolated_from_parent():
    store = SnapshotStore(initial=_seed())
    parent = store.create()
    branch = store.branch(parent)
    store.get(branch).add_amplifier(Amplifier(id="amp-new",
        type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    with pytest.raises(KeyError):
        store.get(parent).get_amplifier("amp-new")
    assert store.get(branch).get_amplifier("amp-new").id == "amp-new"


def test_qot_state_is_carried_into_clone():
    store = SnapshotStore(initial=_seed())
    store.current().set_qot_state("lp1",
        QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=2.5,
                 limiting_element_id="f1"))
    sid = store.create()
    cloned = store.get(sid).get_qot_state("lp1")
    assert cloned.margin_db == 2.5
    assert cloned.limiting_element_id == "f1"


def test_restore_replaces_current():
    store = SnapshotStore(initial=_seed())
    sid = store.create()
    store.current().add_amplifier(Amplifier(id="amp-extra",
        type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    store.restore(sid)
    with pytest.raises(KeyError):
        store.current().get_amplifier("amp-extra")


def test_unknown_id_raises():
    store = SnapshotStore(initial=_seed())
    with pytest.raises(KeyError):
        store.get("nope")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/model/test_snapshots.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `snapshots.py`**

`src/multilayer_optical_mcp/model/snapshots.py`:
```python
from __future__ import annotations
import uuid
from typing import Dict
from .network import NetworkModel


class SnapshotStore:
    def __init__(self, initial: NetworkModel) -> None:
        self._current = initial
        self._snapshots: Dict[str, NetworkModel] = {}

    def current(self) -> NetworkModel:
        return self._current

    def create(self) -> str:
        sid = uuid.uuid4().hex
        self._snapshots[sid] = self._clone(self._current)
        return sid

    def branch(self, parent_id: str) -> str:
        parent = self._snapshots[parent_id]
        bid = uuid.uuid4().hex
        new = self._clone(parent)
        self._snapshots[bid] = new
        self._current = new
        return bid

    def get(self, sid: str) -> NetworkModel:
        return self._snapshots[sid]

    def restore(self, sid: str) -> None:
        self._current = self._clone(self._snapshots[sid])

    @staticmethod
    def _clone(m: NetworkModel) -> NetworkModel:
        clone = NetworkModel(modes=m.modes)
        clone._fiber_types = dict(m._fiber_types)
        clone._optical_nodes = dict(m._optical_nodes)
        clone._fibers = dict(m._fibers)
        clone._amplifiers = dict(m._amplifiers)
        clone._roadms = dict(m._roadms)
        clone._transceivers = dict(m._transceivers)
        clone._oms = dict(m._oms)
        clone._lightpaths = dict(m._lightpaths)
        clone._routers = dict(m._routers)
        clone._ip_links = dict(m._ip_links)
        clone._services = dict(m._services)
        clone._srlgs = dict(m._srlgs)
        clone._risk_groups = dict(m._risk_groups)
        clone._qot_state = dict(m._qot_state)
        return clone
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/model/test_snapshots.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/snapshots.py tests/model/test_snapshots.py
git commit -m "feat(model): COW snapshot/branch/restore including QoT state"
```

---

## Task 6: Snapshot diff

**Files:**
- Modify: `src/multilayer_optical_mcp/model/snapshots.py`
- Test: extend `tests/model/test_snapshots.py`

- [ ] **Step 1: Write failing test**

Append to `tests/model/test_snapshots.py`:
```python
def test_diff_added_oms():
    store = SnapshotStore(initial=_seed())
    a = store.create()
    store.current().add_oms(OMS(id="oms2", src_node_id="X", dst_node_id="Y",
                                elements=("amp1", "f1", "amp2")))
    b = store.create()
    diff = store.diff(a, b)
    assert "oms2" in diff["oms"]["added"]


def test_diff_modified_qot_state():
    store = SnapshotStore(initial=_seed())
    store.current().set_qot_state("lp1",
        QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=2.5))
    a = store.create()
    store.current().set_qot_state("lp1",
        QoTState(gsnr_db=18.0, osnr_db=20.0, margin_db=0.5))
    b = store.create()
    diff = store.diff(a, b)
    assert "lp1" in diff["qot_state"]["modified"]


def test_diff_modified_lightpath_mode():
    store = SnapshotStore(initial=_seed())
    a = store.create()
    store.current().modes._by_id["50G-BPSK"] = TransceiverMode(
        id="50G-BPSK", bitrate_gbps=50.0, required_gsnr_db=8.0,
        symbol_rate_baud=32e9, channel_spacing_hz=50e9,
    )
    store.current().set_lightpath_mode("lp1", "50G-BPSK")
    b = store.create()
    diff = store.diff(a, b)
    assert "lp1" in diff["lightpaths"]["modified"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/model/test_snapshots.py::test_diff_added_oms -v`
Expected: FAIL.

- [ ] **Step 3: Add `diff` to `SnapshotStore`**

Append to `src/multilayer_optical_mcp/model/snapshots.py`:
```python
    def diff(self, a_id: str, b_id: str) -> dict:
        a = self._snapshots[a_id]
        b = self._snapshots[b_id]
        return {
            "fiber_types": _delta(a._fiber_types, b._fiber_types),
            "fibers": _delta(a._fibers, b._fibers),
            "amplifiers": _delta(a._amplifiers, b._amplifiers),
            "oms": _delta(a._oms, b._oms),
            "lightpaths": _delta(a._lightpaths, b._lightpaths),
            "ip_links": _delta(a._ip_links, b._ip_links),
            "routers": _delta(a._routers, b._routers),
            "services": _delta(a._services, b._services),
            "srlgs": _delta(a._srlgs, b._srlgs),
            "risk_groups": _delta(a._risk_groups, b._risk_groups),
            "qot_state": _delta(a._qot_state, b._qot_state),
        }


def _delta(a: dict, b: dict) -> dict:
    a_keys, b_keys = set(a), set(b)
    return {
        "added": tuple(sorted(b_keys - a_keys)),
        "removed": tuple(sorted(a_keys - b_keys)),
        "modified": tuple(sorted(k for k in a_keys & b_keys if a[k] != b[k])),
    }
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/model/test_snapshots.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/snapshots.py tests/model/test_snapshots.py
git commit -m "feat(model): structured snapshot diff including QoT state"
```

---

## Task 7: Snapshot lifecycle (cap + TTL)

**Files:**
- Modify: `src/multilayer_optical_mcp/model/snapshots.py`
- Test: `tests/model/test_snapshot_lifecycle.py`

- [ ] **Step 1: Write failing test**

`tests/model/test_snapshot_lifecycle.py`:
```python
import time
import pytest
from multilayer_optical_mcp.model.assets import TransceiverMode
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.snapshots import SnapshotStore


def _empty():
    return NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="100G-QPSK", bitrate_gbps=100.0,
                        required_gsnr_db=12.0, symbol_rate_baud=32e9,
                        channel_spacing_hz=50e9),
    ]))


def test_max_snapshots_cap_evicts_oldest():
    store = SnapshotStore(initial=_empty(), max_snapshots=3)
    s1 = store.create(); s2 = store.create(); s3 = store.create()
    s4 = store.create()  # evicts s1
    with pytest.raises(KeyError):
        store.get(s1)
    assert store.get(s2) is not None
    assert store.get(s4) is not None


def test_ttl_reaps_expired():
    store = SnapshotStore(initial=_empty(), ttl_seconds=0.05)
    s = store.create()
    time.sleep(0.1)
    store.reap()
    with pytest.raises(KeyError):
        store.get(s)


def test_default_no_cap_no_ttl():
    store = SnapshotStore(initial=_empty())
    ids = [store.create() for _ in range(10)]
    for sid in ids:
        assert store.get(sid) is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/model/test_snapshot_lifecycle.py -v`
Expected: FAIL.

- [ ] **Step 3: Replace lifecycle bits in `snapshots.py`**

```python
import time
from collections import OrderedDict
# ... existing imports ...


class SnapshotStore:
    def __init__(
        self,
        initial: NetworkModel,
        max_snapshots: int | None = None,
        ttl_seconds: float | None = None,
    ) -> None:
        self._current = initial
        self._snapshots: "OrderedDict[str, NetworkModel]" = OrderedDict()
        self._created_at: Dict[str, float] = {}
        self._max = max_snapshots
        self._ttl = ttl_seconds

    def _store(self, sid: str, model: NetworkModel) -> None:
        self._snapshots[sid] = model
        self._created_at[sid] = time.monotonic()
        if self._max is not None and len(self._snapshots) > self._max:
            oldest, _ = self._snapshots.popitem(last=False)
            self._created_at.pop(oldest, None)

    def current(self) -> NetworkModel:
        return self._current

    def create(self) -> str:
        sid = uuid.uuid4().hex
        self._store(sid, self._clone(self._current))
        return sid

    def branch(self, parent_id: str) -> str:
        parent = self._snapshots[parent_id]
        bid = uuid.uuid4().hex
        new = self._clone(parent)
        self._store(bid, new)
        self._current = new
        return bid

    def reap(self) -> tuple[str, ...]:
        if self._ttl is None:
            return ()
        now = time.monotonic()
        expired = [sid for sid, t in self._created_at.items() if now - t > self._ttl]
        for sid in expired:
            self._snapshots.pop(sid, None)
            self._created_at.pop(sid, None)
        return tuple(expired)

    # `get`, `restore`, `diff`, `_clone` unchanged from Tasks 5-6
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/model/ -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/snapshots.py tests/model/test_snapshot_lifecycle.py
git commit -m "feat(model): snapshot store cap + TTL reaping"
```

---

## Task 8: QoTResultStore (id-addressed breakdown cache)

**Files:**
- Create: `src/multilayer_optical_mcp/model/qot_results.py`
- Test: `tests/model/test_qot_results.py`

Mirrors `SnapshotStore`'s lifecycle: id-addressed, capped, reapable. Stashes a `QoTBreakdown` per `compute_qot` call so the detail tier can serve it without re-propagation.

- [ ] **Step 1: Write failing test**

`tests/model/test_qot_results.py`:
```python
import time
import pytest
from multilayer_optical_mcp.model.qot import (
    QoTBreakdown, ElementSnapshot,
)
from multilayer_optical_mcp.model.qot_results import QoTResultStore


def _bd():
    return QoTBreakdown(
        snapshots=(
            ElementSnapshot("amp1", 22.0, 23.0, 0.0, 0.0, 0.0),
            ElementSnapshot("fiber1", 19.0, 20.5, -3.0, -1.5, -1.5),
        ),
        limiting_element_id="fiber1",
    )


def test_put_returns_unique_id_and_get_round_trips():
    store = QoTResultStore()
    rid1 = store.put(_bd())
    rid2 = store.put(_bd())
    assert rid1 != rid2
    assert store.get(rid1).limiting_element_id == "fiber1"


def test_get_unknown_id_raises():
    store = QoTResultStore()
    with pytest.raises(KeyError):
        store.get("nope")


def test_cap_evicts_oldest():
    store = QoTResultStore(max_results=2)
    r1 = store.put(_bd()); r2 = store.put(_bd()); r3 = store.put(_bd())
    with pytest.raises(KeyError):
        store.get(r1)
    assert store.get(r2) is not None
    assert store.get(r3) is not None


def test_ttl_reaps_expired():
    store = QoTResultStore(ttl_seconds=0.05)
    rid = store.put(_bd())
    time.sleep(0.1)
    store.reap()
    with pytest.raises(KeyError):
        store.get(rid)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/model/test_qot_results.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `qot_results.py`**

`src/multilayer_optical_mcp/model/qot_results.py`:
```python
from __future__ import annotations
import time
import uuid
from collections import OrderedDict
from typing import Dict, Tuple
from .qot import QoTBreakdown


class QoTResultStore:
    """Id-addressed cache of QoTBreakdown values. One entry per compute_qot
    call. Capped and TTL-reapable; same discipline as SnapshotStore — an
    open-ended what-if session would otherwise grow unbounded."""

    def __init__(
        self,
        max_results: int | None = 512,
        ttl_seconds: float | None = 600.0,
    ) -> None:
        self._items: "OrderedDict[str, QoTBreakdown]" = OrderedDict()
        self._created_at: Dict[str, float] = {}
        self._max = max_results
        self._ttl = ttl_seconds

    def put(self, breakdown: QoTBreakdown) -> str:
        rid = uuid.uuid4().hex
        self._items[rid] = breakdown
        self._created_at[rid] = time.monotonic()
        if self._max is not None and len(self._items) > self._max:
            oldest, _ = self._items.popitem(last=False)
            self._created_at.pop(oldest, None)
        return rid

    def get(self, rid: str) -> QoTBreakdown:
        return self._items[rid]

    def reap(self) -> Tuple[str, ...]:
        if self._ttl is None:
            return ()
        now = time.monotonic()
        expired = [r for r, t in self._created_at.items() if now - t > self._ttl]
        for r in expired:
            self._items.pop(r, None)
            self._created_at.pop(r, None)
        return tuple(expired)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/model/test_qot_results.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/qot_results.py tests/model/test_qot_results.py
git commit -m "feat(model): QoTResultStore (id-addressed breakdown cache with cap + TTL)"
```

---

# Phase 2 — GNPy Adapter

## Task 9: Toy 2-span topology with advanced amplifier model

**Files:**
- Create: `eqpt/eqpt_config.json`
- Create: `topologies/toy_2span.json`
- Test: `tests/gnpy_adapter/test_translate.py` (smoke step)

- [ ] **Step 1: Write smoke test**

`tests/gnpy_adapter/__init__.py`: empty.

`tests/gnpy_adapter/test_translate.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/gnpy_adapter/test_translate.py -v`
Expected: FAIL.

- [ ] **Step 3: Write `eqpt/eqpt_config.json`**

```json
{
  "Edfa": [
    {
      "type_variety": "advanced_toy",
      "type_def": "advanced_model",
      "gain_flatmax": 25,
      "gain_min": 15,
      "p_max": 21,
      "nf_fit_coeff": [0.0, 0.0, 0.0, 5.5],
      "nf_ripple": [0.0],
      "dgt": [1.0],
      "gain_ripple": [0.0],
      "out_voa_auto": false,
      "allowed_for_design": true
    }
  ],
  "Fiber": [
    {"type_variety": "SSMF", "dispersion": 1.67e-05,
     "gamma": 0.00127, "pmd_coef": 1.265e-15}
  ],
  "Span": [
    {"power_mode": true, "delta_power_range_db": [0, 0, 0.5],
     "max_fiber_lineic_loss_for_raman": 0.25, "target_extended_gain": 2.5,
     "max_length": 150, "length_units": "km", "max_loss": 28,
     "padding": 10, "EOL": 0, "con_in": 0, "con_out": 0}
  ],
  "Roadm": [
    {"target_pch_out_db": -20, "add_drop_osnr": 38, "pmd": 0,
     "restrictions": {"preamp_variety_list": [], "booster_variety_list": []}}
  ],
  "SI": [
    {"f_min": 191.3e12, "baud_rate": 87.5e9, "f_max": 196.1e12,
     "spacing": 100e9, "power_dbm": 0, "power_range_db": [0, 0, 1],
     "roll_off": 0.15, "tx_osnr": 40, "sys_margins": 2}
  ],
  "Transceiver": [
    {
      "type_variety": "vendor-A",
      "frequency": {"min": 191.35e12, "max": 196.1e12},
      "mode": [
        {"format": "300G", "baud_rate": 87.5e9, "OSNR": 4.8, "bit_rate": 300e9,
         "roll_off": 0.15, "tx_osnr": 40, "min_spacing": 100e9, "cost": 1},
        {"format": "400G", "baud_rate": 87.5e9, "OSNR": 7.1, "bit_rate": 400e9,
         "roll_off": 0.15, "tx_osnr": 40, "min_spacing": 100e9, "cost": 1},
        {"format": "800G", "baud_rate": 87.5e9, "OSNR": 15.1, "bit_rate": 800e9,
         "roll_off": 0.15, "tx_osnr": 40, "min_spacing": 100e9, "cost": 1}
      ]
    }
  ]
}
```

- [ ] **Step 4: Write `topologies/toy_2span.json`**

```json
{
  "elements": [
    {"uid": "trx A", "type": "Transceiver"},
    {"uid": "trx Z", "type": "Transceiver"},
    {"uid": "east edfa in A to ILA", "type": "Edfa",
     "type_variety": "advanced_toy",
     "operational": {"gain_target": 20, "tilt_target": 0}},
    {"uid": "east fiber A to ILA", "type": "Fiber",
     "type_variety": "SSMF",
     "params": {"length": 80, "length_units": "km", "loss_coef": 0.2, "att_in": 0, "con_in": 0, "con_out": 0}},
    {"uid": "east edfa in ILA to Z", "type": "Edfa",
     "type_variety": "advanced_toy",
     "operational": {"gain_target": 20, "tilt_target": 0}},
    {"uid": "east fiber ILA to Z", "type": "Fiber",
     "type_variety": "SSMF",
     "params": {"length": 80, "length_units": "km", "loss_coef": 0.2, "att_in": 0, "con_in": 0, "con_out": 0}}
  ],
  "connections": [
    {"from_node": "trx A", "to_node": "east edfa in A to ILA"},
    {"from_node": "east edfa in A to ILA", "to_node": "east fiber A to ILA"},
    {"from_node": "east fiber A to ILA", "to_node": "east edfa in ILA to Z"},
    {"from_node": "east edfa in ILA to Z", "to_node": "east fiber ILA to Z"},
    {"from_node": "east fiber ILA to Z", "to_node": "trx Z"}
  ]
}
```

- [ ] **Step 5: Run tests; Step 6: Commit**

```bash
pytest tests/gnpy_adapter/test_translate.py -v
git add eqpt/ topologies/ tests/gnpy_adapter/
git commit -m "feat(gnpy): toy 2-span topology with advanced amplifier model"
```

---

## Task 10: LoadingState

**Files:**
- Create: `src/multilayer_optical_mcp/gnpy_adapter/loading.py`
- Test: `tests/gnpy_adapter/test_loading.py`

- [ ] **Step 1: Write failing test**

`tests/gnpy_adapter/test_loading.py`:
```python
import pytest
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState


def test_empty_loading():
    assert LoadingState.empty().channels == ()


def test_channel_carries_mode_and_grid_slot():
    ch = Channel(193.4e12, 100e9, 0.0, "400G@7.1dB")
    assert ch.mode_id == "400G@7.1dB"


def test_union_combines_disjoint():
    a = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, "400G@7.1dB"),))
    b = LoadingState(channels=(Channel(193.5e12, 100e9, 0.0, "300G@4.8dB"),))
    assert len(a.union(b).channels) == 2


def test_union_rejects_spectrum_clash():
    a = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, "400G@7.1dB"),))
    b = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, "300G@4.8dB"),))
    with pytest.raises(ValueError, match="spectrum clash"):
        a.union(b)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/gnpy_adapter/test_loading.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `loading.py`**

`src/multilayer_optical_mcp/gnpy_adapter/loading.py`:
```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class Channel:
    center_freq_hz: float
    slot_width_hz: float
    power_dbm: float
    mode_id: str

    @property
    def low_hz(self) -> float:
        return self.center_freq_hz - self.slot_width_hz / 2

    @property
    def high_hz(self) -> float:
        return self.center_freq_hz + self.slot_width_hz / 2


@dataclass(frozen=True)
class LoadingState:
    channels: Tuple[Channel, ...] = ()

    @classmethod
    def empty(cls) -> "LoadingState":
        return cls(channels=())

    def union(self, other: "LoadingState") -> "LoadingState":
        for a in self.channels:
            for b in other.channels:
                if a.low_hz < b.high_hz and b.low_hz < a.high_hz:
                    raise ValueError(
                        f"spectrum clash: {a.center_freq_hz:.3e} vs {b.center_freq_hz:.3e}"
                    )
        return LoadingState(channels=self.channels + other.channels)
```

- [ ] **Step 4: Run tests; Step 5: Commit**

```bash
pytest tests/gnpy_adapter/test_loading.py -v
git add src/multilayer_optical_mcp/gnpy_adapter/loading.py tests/gnpy_adapter/test_loading.py
git commit -m "feat(gnpy): LoadingState as constructed channel set"
```

---

## Task 11: Translator — OMS → uids + LoadingState → SI

**Files:**
- Create: `src/multilayer_optical_mcp/gnpy_adapter/translate.py`
- Test: extend `tests/gnpy_adapter/test_translate.py`

- [ ] **Step 1: Write failing test**

Append to `tests/gnpy_adapter/test_translate.py`:
```python
from multilayer_optical_mcp.model.assets import (
    FiberType, Fiber, Amplifier, OMS, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.gnpy_adapter.translate import (
    build_si_for_loading, resolve_oms_path_to_uids,
)


def _toy_model_oms():
    n = NetworkModel(modes=ModeRegistry([
        TransceiverMode(id="400G@7.1dB", bitrate_gbps=400.0,
                        required_gsnr_db=7.1, symbol_rate_baud=87.5e9,
                        channel_spacing_hz=100e9),
    ]))
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="east edfa in A to ILA",
        type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_amplifier(Amplifier(id="east edfa in ILA to Z",
        type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="east fiber A to ILA",
        a_end="east edfa in A to ILA", z_end="east edfa in ILA to Z",
        length_km=80.0, type_variety="SSMF"))
    n.add_fiber(Fiber(id="east fiber ILA to Z",
        a_end="east edfa in ILA to Z", z_end="trx Z",
        length_km=80.0, type_variety="SSMF"))
    n.add_oms(OMS(id="oms-AZ", src_node_id="trx A", dst_node_id="trx Z",
                  elements=(
                      "east edfa in A to ILA",
                      "east fiber A to ILA",
                      "east edfa in ILA to Z",
                      "east fiber ILA to Z",
                  )))
    return n


def test_resolve_single_oms_returns_its_elements():
    n = _toy_model_oms()
    assert resolve_oms_path_to_uids(n, ("oms-AZ",)) == (
        "east edfa in A to ILA",
        "east fiber A to ILA",
        "east edfa in ILA to Z",
        "east fiber ILA to Z",
    )


def test_resolve_unknown_oms_raises():
    n = _toy_model_oms()
    import pytest
    with pytest.raises(KeyError):
        resolve_oms_path_to_uids(n, ("oms-nope",))


def test_build_si_for_single_channel():
    loading = LoadingState(channels=(
        Channel(193.4e12, 100e9, 0.0, "400G@7.1dB"),
    ))
    si = build_si_for_loading(loading, baud_rate=87.5e9,
                              roll_off=0.15, tx_osnr=40.0)
    carriers = list(si.carriers) if hasattr(si, "carriers") else list(si)
    assert len(carriers) == 1


def test_build_si_empty_loading_returns_empty_si():
    si = build_si_for_loading(LoadingState.empty(), baud_rate=87.5e9,
                              roll_off=0.15, tx_osnr=40.0)
    carriers = list(si.carriers) if hasattr(si, "carriers") else list(si)
    assert len(carriers) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/gnpy_adapter/test_translate.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `translate.py`**

`src/multilayer_optical_mcp/gnpy_adapter/translate.py`:
```python
from __future__ import annotations
from pathlib import Path
from typing import Any, Tuple
from gnpy.core.info import create_input_spectral_information
from gnpy.tools.json_io import load_equipment, load_network
from ..model.network import NetworkModel
from .loading import LoadingState

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EQPT = REPO_ROOT / "eqpt" / "eqpt_config.json"
DEFAULT_TOPO = REPO_ROOT / "topologies" / "toy_2span.json"


def load_toy(eqpt_path: Path = DEFAULT_EQPT,
             topo_path: Path = DEFAULT_TOPO) -> tuple[Any, Any]:
    eqpt = load_equipment(eqpt_path)
    network = load_network(topo_path, eqpt)
    return eqpt, network


def resolve_oms_path_to_uids(
    model: NetworkModel, oms_sequence: Tuple[str, ...],
) -> Tuple[str, ...]:
    uids: list[str] = []
    for oms_id in oms_sequence:
        oms = model.get_oms(oms_id)
        uids.extend(oms.elements)
    return tuple(uids)


def build_si_for_loading(
    loading: LoadingState,
    *, baud_rate: float, roll_off: float, tx_osnr: float,
) -> Any:
    if not loading.channels:
        from gnpy.core.info import SpectralInformation
        return SpectralInformation(frequency=[], baud_rate=[], slot_width=[],
                                   signal=[], nli=[], ase=[],
                                   roll_off=[], chromatic_dispersion=[],
                                   pmd=[], pdl=[], latency=[],
                                   delta_pdb_per_channel=[], tx_osnr=[],
                                   tx_power=[], label=[])
    freqs = [c.center_freq_hz for c in loading.channels]
    slots = [c.slot_width_hz for c in loading.channels]
    powers_w = [10 ** ((c.power_dbm - 30) / 10) for c in loading.channels]
    return create_input_spectral_information(
        f_min=min(freqs),
        f_max=max(freqs),
        roll_off=roll_off,
        baud_rate=baud_rate,
        spacing=min(slots),
        tx_osnr=tx_osnr,
        tx_power=powers_w[0],
    )
```

(GNPy 2.11's `create_input_spectral_information` may want `power` instead of `tx_power`; adjust to the installed version. Do **not** silently drop channels.)

- [ ] **Step 4: Run tests; Step 5: Commit**

```bash
pytest tests/gnpy_adapter/test_translate.py -v
git add src/multilayer_optical_mcp/gnpy_adapter/translate.py tests/gnpy_adapter/test_translate.py
git commit -m "feat(gnpy): OMS->uids resolution + LoadingState->SI builder"
```

---

## Task 12: `compute_qot` — records breakdown, returns (QoTState, result_id)

**Files:**
- Create: `src/multilayer_optical_mcp/gnpy_adapter/adapter.py`
- Test: `tests/gnpy_adapter/test_compute_qot.py`

The load-bearing task. `compute_qot` walks the OMS-resolved element sequence, captures one `ElementSnapshot` per element, identifies the element with the largest negative GSNR delta as the `limiting_element_id`, stashes the full `QoTBreakdown` in the supplied `QoTResultStore`, and returns `(QoTState, result_id)`.

- [ ] **Step 1: Write failing test**

`tests/gnpy_adapter/test_compute_qot.py`:
```python
import math
from multilayer_optical_mcp.model.assets import (
    Direction, FiberType, Fiber, Amplifier, OMS, TransceiverMode,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.gnpy_adapter.adapter import compute_qot


def _toy_model():
    reg = ModeRegistry([
        TransceiverMode(id="400G@7.1dB", bitrate_gbps=400.0,
                        required_gsnr_db=7.1, symbol_rate_baud=87.5e9,
                        channel_spacing_hz=100e9),
        TransceiverMode(id="800G@15.1dB", bitrate_gbps=800.0,
                        required_gsnr_db=15.1, symbol_rate_baud=87.5e9,
                        channel_spacing_hz=100e9),
        TransceiverMode(id="impossible", bitrate_gbps=1.0,
                        required_gsnr_db=100.0, symbol_rate_baud=87.5e9,
                        channel_spacing_hz=100e9),
    ])
    n = NetworkModel(modes=reg)
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="east edfa in A to ILA",
        type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_amplifier(Amplifier(id="east edfa in ILA to Z",
        type_variety="advanced_toy", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="east fiber A to ILA",
        a_end="east edfa in A to ILA", z_end="east edfa in ILA to Z",
        length_km=80.0, type_variety="SSMF"))
    n.add_fiber(Fiber(id="east fiber ILA to Z",
        a_end="east edfa in ILA to Z", z_end="trx Z",
        length_km=80.0, type_variety="SSMF"))
    n.add_oms(OMS(id="oms-AZ", src_node_id="trx A", dst_node_id="trx Z",
                  elements=(
                      "east edfa in A to ILA",
                      "east fiber A to ILA",
                      "east edfa in ILA to Z",
                      "east fiber ILA to Z",
                  )))
    return n


def test_compute_qot_returns_state_and_result_id():
    n = _toy_model(); store = QoTResultStore()
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, "400G@7.1dB"),))
    state, rid = compute_qot(model=n, store=store, oms_sequence=("oms-AZ",),
                             direction=Direction.FORWARD,
                             mode_id="400G@7.1dB", loading=loading)
    assert math.isfinite(state.gsnr_db)
    assert math.isfinite(state.osnr_db)
    assert isinstance(state.mode_feasible, bool)
    assert isinstance(rid, str) and rid


def test_breakdown_cached_in_store_with_one_snapshot_per_element():
    n = _toy_model(); store = QoTResultStore()
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, "400G@7.1dB"),))
    _, rid = compute_qot(model=n, store=store, oms_sequence=("oms-AZ",),
                         direction=Direction.FORWARD,
                         mode_id="400G@7.1dB", loading=loading)
    bd = store.get(rid)
    # Four elements in the OMS -> four snapshots.
    assert len(bd.snapshots) == 4
    # Snapshots are labeled with the resolved model uids in order.
    assert bd.snapshots[0].element_id == "east edfa in A to ILA"
    assert bd.snapshots[3].element_id == "east fiber ILA to Z"


def test_limiting_element_id_is_stable_uid_not_human_string():
    """Agents feed this back into other tools verbatim — no parsing."""
    n = _toy_model(); store = QoTResultStore()
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, "400G@7.1dB"),))
    state, rid = compute_qot(model=n, store=store, oms_sequence=("oms-AZ",),
                             direction=Direction.FORWARD,
                             mode_id="400G@7.1dB", loading=loading)
    bd = store.get(rid)
    assert state.limiting_element_id == bd.limiting_element_id
    if state.limiting_element_id is not None:
        # The id must exactly match one of the model uids in the OMS.
        valid = {"east edfa in A to ILA", "east fiber A to ILA",
                 "east edfa in ILA to Z", "east fiber ILA to Z"}
        assert state.limiting_element_id in valid


def test_arbitrary_loading_superset_is_evaluated_without_provisioning():
    """The load-bearing property: loading can include a channel the model
    never saw. Adapter MUST compute QoT for it."""
    n = _toy_model(); store = QoTResultStore()
    loading = LoadingState(channels=(
        Channel(193.4e12, 100e9, 0.0, "400G@7.1dB"),
        Channel(193.5e12, 100e9, 0.0, "800G@15.1dB"),
    ))
    state, _ = compute_qot(model=n, store=store, oms_sequence=("oms-AZ",),
                           direction=Direction.FORWARD,
                           mode_id="400G@7.1dB", loading=loading)
    assert math.isfinite(state.gsnr_db)


def test_mode_feasible_flips_when_gsnr_below_required():
    n = _toy_model(); store = QoTResultStore()
    loading = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, "impossible"),))
    state, _ = compute_qot(model=n, store=store, oms_sequence=("oms-AZ",),
                           direction=Direction.FORWARD,
                           mode_id="impossible", loading=loading)
    assert state.mode_feasible is False
    assert state.margin_db < 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/gnpy_adapter/test_compute_qot.py -v`
Expected: FAIL — `compute_qot` missing.

- [ ] **Step 3: Implement `adapter.py`**

`src/multilayer_optical_mcp/gnpy_adapter/adapter.py`:
```python
from __future__ import annotations
import math
from typing import Dict, Tuple
from gnpy.core.utils import lin2db
from ..model.assets import Direction
from ..model.network import NetworkModel
from ..model.qot import QoTState, ElementSnapshot, QoTBreakdown
from ..model.qot_results import QoTResultStore
from .loading import LoadingState
from .translate import load_toy, build_si_for_loading, resolve_oms_path_to_uids


def _path_elements(network, uids: Tuple[str, ...]):
    by_uid = {n.uid: n for n in network.nodes}
    missing = [u for u in uids if u not in by_uid]
    if missing:
        raise KeyError(f"unknown uids in path: {missing}")
    return [by_uid[u] for u in uids]


def _carrier_index(si, freq_hz: float) -> int:
    for i, f in enumerate(si.frequency):
        if math.isclose(f, freq_hz, rel_tol=1e-9):
            return i
    raise KeyError(f"carrier at {freq_hz} Hz not found in SI")


def _read_idx(si, idx: int) -> tuple[float, float, float, float]:
    """Return (gsnr_db, osnr_db, ase_db, nli_db) for carrier `idx`."""
    gsnr_db = float(lin2db(si.gsnr[idx]))
    osnr_db = float(lin2db(si.osnr[idx]))
    ase_db = float(lin2db(si.ase[idx])) if hasattr(si, "ase") else math.nan
    nli_db = float(lin2db(si.nli[idx])) if hasattr(si, "nli") else math.nan
    return gsnr_db, osnr_db, ase_db, nli_db


def compute_qot(
    *,
    model: NetworkModel,
    store: QoTResultStore,
    oms_sequence: Tuple[str, ...],
    direction: Direction,
    mode_id: str,
    loading: LoadingState,
) -> tuple[QoTState, str]:
    """End-to-end QoT for `mode_id` along `oms_sequence` in `direction` under
    `loading`. Always builds a full QoTBreakdown (free since we walk every
    element anyway) and stashes it in `store`. Returns the compact state plus
    the result_id that retrieves the breakdown via `get_qot_breakdown`."""
    _eqpt, network = load_toy()
    uids = resolve_oms_path_to_uids(model, oms_sequence)
    elements = _path_elements(network, uids)
    if direction == Direction.BACKWARD:
        elements = list(reversed(elements))
        uids = tuple(reversed(uids))
    probe = next((c for c in loading.channels if c.mode_id == mode_id), None)
    if probe is None:
        raise ValueError(f"loading does not include a channel for mode {mode_id!r}")
    mode = model.modes.get(mode_id)
    si = build_si_for_loading(loading, baud_rate=mode.symbol_rate_baud,
                              roll_off=0.15, tx_osnr=40.0)
    idx0 = _carrier_index(si, probe.center_freq_hz)
    prev_gsnr_db, _, _, _ = _read_idx(si, idx0)

    snapshots: list[ElementSnapshot] = []
    worst_drop = 0.0
    limiting_uid: str | None = None
    for el, uid in zip(elements, uids):
        si = el(si)
        idx = _carrier_index(si, probe.center_freq_hz)
        gsnr_db, osnr_db, ase_db, nli_db = _read_idx(si, idx)
        delta = gsnr_db - prev_gsnr_db
        snapshots.append(ElementSnapshot(
            element_id=uid,
            gsnr_db_after=gsnr_db,
            osnr_db_after=osnr_db,
            gsnr_delta_db=delta,
            ase_contribution_db=ase_db,
            nli_contribution_db=nli_db,
        ))
        if delta < worst_drop:
            worst_drop = delta
            limiting_uid = uid
        prev_gsnr_db = gsnr_db

    final = snapshots[-1]
    breakdown = QoTBreakdown(snapshots=tuple(snapshots),
                             limiting_element_id=limiting_uid)
    rid = store.put(breakdown)
    state = QoTState(
        gsnr_db=final.gsnr_db_after,
        osnr_db=final.osnr_db_after,
        margin_db=final.gsnr_db_after - mode.required_gsnr_db,
        limiting_element_id=limiting_uid,
    )
    return state, rid
```

(GNPy 2.11 may expose `si.gsnr` / `si.ase` / `si.nli` / `si.frequency` differently. If so, read off the last `Transceiver` element. The failing tests will surface the right API; fix the read, not the test.)

- [ ] **Step 4: Run tests**

Run: `pytest tests/gnpy_adapter/test_compute_qot.py -v`
Expected: all 5 PASS.

- [ ] **Step 5: Ground-truth comment**

Document the ground-truth GSNR in a comment in `test_compute_qot.py`:
```python
# GROUND TRUTH (gnpy==2.11, toy_2span.json, 400G@7.1dB @ 193.4 THz, P=0 dBm):
# expected GSNR ~ <fill in measured value> dB.
# Updates on intentional gnpy bumps only.
```

- [ ] **Step 6: Commit**

```bash
git add src/multilayer_optical_mcp/gnpy_adapter/adapter.py tests/gnpy_adapter/test_compute_qot.py
git commit -m "feat(gnpy): compute_qot returns (state, breakdown_id) under arbitrary loading"
```

---

## Task 13: Per-direction `gated_qot`

**Files:**
- Modify: `src/multilayer_optical_mcp/gnpy_adapter/adapter.py`
- Test: `tests/gnpy_adapter/test_per_direction.py`

`gated_qot` calls `compute_qot` per direction and returns the **worse** result (state + its breakdown's result_id). The unused direction's breakdown stays in the store until its TTL/cap kicks in — that's the contract: every compute call leaves a cached breakdown, the caller surfaces only the gating one.

- [ ] **Step 1: Write failing test**

`tests/gnpy_adapter/test_per_direction.py`:
```python
import math
from multilayer_optical_mcp.model.assets import Direction
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.gnpy_adapter.adapter import compute_qot, gated_qot
from tests.gnpy_adapter.test_compute_qot import _toy_model


LOADING = LoadingState(channels=(Channel(193.4e12, 100e9, 0.0, "400G@7.1dB"),))


def test_symmetric_topology_forward_and_backward_match():
    n = _toy_model(); store = QoTResultStore()
    fwd, _ = compute_qot(model=n, store=store, oms_sequence=("oms-AZ",),
                         direction=Direction.FORWARD,
                         mode_id="400G@7.1dB", loading=LOADING)
    bwd, _ = compute_qot(model=n, store=store, oms_sequence=("oms-AZ",),
                         direction=Direction.BACKWARD,
                         mode_id="400G@7.1dB", loading=LOADING)
    assert math.isclose(fwd.gsnr_db, bwd.gsnr_db, abs_tol=0.05)


def test_gated_qot_returns_worse_of_two_directions(monkeypatch):
    from multilayer_optical_mcp.gnpy_adapter import adapter as adapter_mod

    calls = []
    def fake(**kwargs):
        calls.append(kwargs["direction"])
        if kwargs["direction"] == Direction.FORWARD:
            return (QoTState(gsnr_db=20.0, osnr_db=22.0, margin_db=8.0,
                             limiting_element_id="east fiber A to ILA"),
                    "rid-fwd")
        return (QoTState(gsnr_db=10.0, osnr_db=12.0, margin_db=-2.0,
                         limiting_element_id="east edfa in ILA to Z"),
                "rid-bwd")

    monkeypatch.setattr(adapter_mod, "compute_qot", fake)
    n = _toy_model(); store = QoTResultStore()
    state, rid = gated_qot(model=n, store=store, oms_sequence=("oms-AZ",),
                           mode_id="400G@7.1dB", loading=LOADING)
    assert state.gsnr_db == 10.0
    assert state.mode_feasible is False
    assert state.limiting_element_id == "east edfa in ILA to Z"
    assert rid == "rid-bwd"
    assert set(calls) == {Direction.FORWARD, Direction.BACKWARD}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/gnpy_adapter/test_per_direction.py -v`
Expected: FAIL.

- [ ] **Step 3: Add `gated_qot` to `adapter.py`**

Append:
```python
def gated_qot(
    *,
    model: NetworkModel,
    store: QoTResultStore,
    oms_sequence: Tuple[str, ...],
    mode_id: str,
    loading: LoadingState,
) -> tuple[QoTState, str]:
    """Return the worse of forward/backward QoT, along with its breakdown id."""
    fwd_state, fwd_rid = compute_qot(model=model, store=store,
                                     oms_sequence=oms_sequence,
                                     direction=Direction.FORWARD,
                                     mode_id=mode_id, loading=loading)
    bwd_state, bwd_rid = compute_qot(model=model, store=store,
                                     oms_sequence=oms_sequence,
                                     direction=Direction.BACKWARD,
                                     mode_id=mode_id, loading=loading)
    if fwd_state.gsnr_db <= bwd_state.gsnr_db:
        return fwd_state, fwd_rid
    return bwd_state, bwd_rid
```

- [ ] **Step 4: Run tests; Step 5: Commit**

```bash
pytest tests/gnpy_adapter/ -v
git add src/multilayer_optical_mcp/gnpy_adapter/adapter.py tests/gnpy_adapter/test_per_direction.py
git commit -m "feat(gnpy): per-direction QoT with worse-direction gating"
```

---

## Task 14: `recompute_qot_under_loading`

**Files:**
- Modify: `src/multilayer_optical_mcp/gnpy_adapter/adapter.py`
- Test: `tests/gnpy_adapter/test_recompute_under_loading.py`

For every lightpath in the model, run `gated_qot` and write the result. Returns `{lp_id: (QoTState, result_id)}` so callers can pull per-lightpath breakdowns on demand.

- [ ] **Step 1: Write failing test**

`tests/gnpy_adapter/test_recompute_under_loading.py`:
```python
from multilayer_optical_mcp.model.assets import (
    Lightpath, Router, IPLink,
)
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.gnpy_adapter.loading import Channel, LoadingState
from multilayer_optical_mcp.gnpy_adapter.adapter import recompute_qot_under_loading
from tests.gnpy_adapter.test_compute_qot import _toy_model


def _model_with_lightpath():
    n = _toy_model()
    n.add_lightpath(Lightpath(id="lp1", oms_sequence=("oms-AZ",),
                              mode_id="400G@7.1dB", center_freq_hz=193.4e12))
    n.add_router(Router(id="R1", site="A"))
    n.add_router(Router(id="R2", site="Z"))
    n.add_ip_link(IPLink(id="ip1", a_router="R1", z_router="R2",
                         lightpath_id="lp1"))
    return n


def test_recompute_writes_state_and_returns_result_ids():
    n = _model_with_lightpath(); store = QoTResultStore()
    loading = LoadingState(channels=(
        Channel(193.4e12, 100e9, 0.0, "400G@7.1dB"),
    ))
    results = recompute_qot_under_loading(model=n, store=store, loading=loading)
    state, rid = results["lp1"]
    # Recorded on the model.
    assert n.get_qot_state("lp1") == state
    # Breakdown reachable from the store.
    bd = store.get(rid)
    assert bd.snapshots
    # And capacity derives correctly.
    cap = n.ip_link_capacity_gbps("ip1")
    assert cap == (400.0 if state.mode_feasible else 0.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/gnpy_adapter/test_recompute_under_loading.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement in `adapter.py`**

Append:
```python
def recompute_qot_under_loading(
    *, model: NetworkModel, store: QoTResultStore, loading: LoadingState,
) -> Dict[str, tuple[QoTState, str]]:
    """For every lightpath, compute gated QoT under `loading`, write the
    compact state on the model, and surface each per-lightpath breakdown id
    so callers can pull detail without re-propagating."""
    results: Dict[str, tuple[QoTState, str]] = {}
    for lp in model.list_lightpaths():
        state, rid = gated_qot(model=model, store=store,
                               oms_sequence=lp.oms_sequence,
                               mode_id=lp.mode_id, loading=loading)
        model.set_qot_state(lp.id, state)
        results[lp.id] = (state, rid)
    return results
```

- [ ] **Step 4: Run tests; Step 5: Commit**

```bash
pytest tests/ -v
git add src/multilayer_optical_mcp/gnpy_adapter/adapter.py tests/gnpy_adapter/test_recompute_under_loading.py
git commit -m "feat(gnpy): recompute_qot_under_loading surfaces breakdown ids"
```

---

## Task 15: FastMCP server shell

**Files:**
- Create: `src/multilayer_optical_mcp/server.py`
- Test: `tests/test_server.py`

Wires `SnapshotStore` + `QoTResultStore`. Exposes the seven Phase-1–2 tools, including `get_qot_breakdown` for the detail tier.

- [ ] **Step 1: Write failing test**

`tests/test_server.py`:
```python
from multilayer_optical_mcp.server import build_app


def test_server_registers_phase_1_and_2_tools():
    app = build_app()
    names = {t.name for t in app.list_tools_sync()}
    expected = {
        "get_transceiver_modes",
        "snapshot_create", "snapshot_branch", "snapshot_restore", "snapshot_diff",
        "compute_qot", "recompute_qot_under_loading",
        "get_qot_breakdown",
    }
    assert expected.issubset(names)


def test_get_transceiver_modes_returns_eleven_yaml_modes():
    app = build_app()
    result = app.call_tool_sync("get_transceiver_modes", {})
    ids = {m["id"] for m in result}
    assert len(ids) == 11
    assert "300G@4.8dB" in ids and "800G@15.1dB" in ids
```

(`list_tools_sync` / `call_tool_sync` are FastMCP test helpers; if the installed version names them differently, use the equivalent in-memory client.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_server.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `server.py`**

`src/multilayer_optical_mcp/server.py`:
```python
from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from .model.assets import Direction
from .model.modes import load_modulation_formats
from .model.network import NetworkModel
from .model.qot_results import QoTResultStore
from .model.snapshots import SnapshotStore
from .gnpy_adapter.loading import Channel, LoadingState
from .gnpy_adapter.adapter import (
    compute_qot as _compute_qot,
    recompute_qot_under_loading as _recompute,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
MOD_FORMATS_YAML = REPO_ROOT / "modulation_formats.yaml"


def build_app() -> FastMCP:
    app = FastMCP("multilayer-optical-mcp")
    modes = load_modulation_formats(MOD_FORMATS_YAML)
    model = NetworkModel(modes=modes)
    snapshots = SnapshotStore(initial=model, max_snapshots=64, ttl_seconds=3600)
    results = QoTResultStore(max_results=512, ttl_seconds=600)

    @app.tool()
    def get_transceiver_modes() -> list[dict]:
        return [{
            "id": m.id, "bitrate_gbps": m.bitrate_gbps,
            "required_gsnr_db": m.required_gsnr_db,
            "symbol_rate_baud": m.symbol_rate_baud,
            "channel_spacing_hz": m.channel_spacing_hz,
        } for m in modes.list()]

    @app.tool()
    def snapshot_create() -> dict:
        return {"id": snapshots.create()}

    @app.tool()
    def snapshot_branch(parent_id: str) -> dict:
        return {"id": snapshots.branch(parent_id)}

    @app.tool()
    def snapshot_restore(snapshot_id: str) -> dict:
        snapshots.restore(snapshot_id)
        return {"restored": snapshot_id}

    @app.tool()
    def snapshot_diff(a_id: str, b_id: str) -> dict:
        return snapshots.diff(a_id, b_id)

    def _loading_from(channels: list[dict]) -> LoadingState:
        return LoadingState(channels=tuple(
            Channel(c["center_freq_hz"], c["slot_width_hz"],
                    c["power_dbm"], c["mode_id"]) for c in channels
        ))

    @app.tool()
    def compute_qot(
        oms_sequence: list[str],
        direction: str,
        mode_id: str,
        loading_channels: list[dict],
    ) -> dict:
        state, rid = _compute_qot(
            model=snapshots.current(), store=results,
            oms_sequence=tuple(oms_sequence),
            direction=Direction(direction),
            mode_id=mode_id, loading=_loading_from(loading_channels),
        )
        return {"gsnr_db": state.gsnr_db, "osnr_db": state.osnr_db,
                "margin_db": state.margin_db, "mode_feasible": state.mode_feasible,
                "limiting_element_id": state.limiting_element_id,
                "result_id": rid}

    @app.tool()
    def recompute_qot_under_loading(loading_channels: list[dict]) -> dict:
        out = _recompute(model=snapshots.current(), store=results,
                         loading=_loading_from(loading_channels))
        return {
            lp: {
                "gsnr_db": s.gsnr_db, "osnr_db": s.osnr_db,
                "margin_db": s.margin_db, "mode_feasible": s.mode_feasible,
                "limiting_element_id": s.limiting_element_id,
                "result_id": rid,
            } for lp, (s, rid) in out.items()
        }

    @app.tool()
    def get_qot_breakdown(result_id: str) -> dict:
        bd = results.get(result_id)
        return {
            "limiting_element_id": bd.limiting_element_id,
            "snapshots": [asdict(s) for s in bd.snapshots],
        }

    return app


def main() -> None:
    build_app().run()
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_server.py -v`
Expected: PASS.

- [ ] **Step 5: Smoke-run**

Run: `multilayer-optical-mcp --help`
Expected: FastMCP help text.

- [ ] **Step 6: Commit**

```bash
git add src/multilayer_optical_mcp/server.py tests/test_server.py
git commit -m "feat(server): FastMCP shell exposing phase 1-2 tools incl. get_qot_breakdown"
```

---

## Verification

1. **Full suite green:** `pytest -v`.
2. **YAML drives modes:** loader returns 11 modes from `modulation_formats.yaml`.
3. **GNPy version pin holds:** `import gnpy; gnpy.__version__ == "2.11"`. Drift requires re-baselining the ground-truth GSNR comment in `test_compute_qot.py`.
4. **Load-bearing adapter contract:** `test_compute_qot.py::test_arbitrary_loading_superset_is_evaluated_without_provisioning` alone. Passing means downstream phases can proceed.
5. **Coupling sanity:** `test_capacity_coupling.py`.
6. **Two-tier QoT contract:** `compute_qot` returns `limiting_element_id` as a model uid; `get_qot_breakdown(result_id)` returns ordered per-element snapshots; cache cap+TTL works (`test_qot_results.py`).
7. **MCP smoke:** launch `multilayer-optical-mcp`, connect via `npx @modelcontextprotocol/inspector multilayer-optical-mcp`. Call `compute_qot`; pull `get_qot_breakdown(result_id)` for detail.

**Deferred to a follow-up plan (call out before scoping the next plan):**
- Read tools (`get_topology`, `get_lightpaths`, `get_services`, `get_traffic_matrix`, `list_srlgs`), IP-layer queries.
- Risk groups (`define_risk_group`, `get_exposure`).
- Solvers (`compute_paths`, `compute_disjoint_paths`, `check_disjointness`, `solve_rsa`, `solve_allocation`).
- IP-over-optical simulation (`simulate_ip_routing`, `get_grooming_map`, `get_affected_services`, `reroute_service`).
- What-if (`whatif_margin_threshold_sweep`, `inject_degradation`, `inject_failure`).
- Per-channel loading attribution in `recompute_qot_under_loading` (which interferer drove which survivor's margin drop). Pairs naturally with `inject_degradation`.
- Sensitivity tooling. Derive by differencing `QoTBreakdown`s across branches; no adapter change needed.
- Validate / commit / reconcile.
- Multi-OMS, multi-fiber topologies beyond the toy 2-span.
- Model-driven GNPy translation. Phase 2 loads the toy GNPy JSON directly; the model side mirrors uids.

CLAUDE.md's Build Order step 2 demands the loading-state and per-direction contracts be proven first — that is the work this plan delivers, now with the per-element detail the agent needs to *act* on a margin verdict instead of just reading it.
