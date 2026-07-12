# multilayer-optical-mcp — Phase 7: Validate + commit + reconcile (gated mutation)

> **RECONCILIATION (2026-07-11, executing Part 4 of the master fix plan on branch
> `phase-7-validate-commit-reconcile`).** This plan predates correctness batches C1–C6,
> which have since landed on master and altered two of its task snippets. Adjustments
> applied during execution (per Part 4 of `2026-07-07-fix-roadmap-correctness-then-optimizations.md`):
> - **Task 1:** `NetworkModel.clone()` already exists (Batch C3-1, `f7e822a`) with the
>   correct shape — unfrozen, grid-carrying, no `_optical_nodes` (removed in O3-1). Step 3
>   is a **no-op**; only `diff_models` + `SnapshotStore.put` are new work.
> - **Task 2:** `simulate_ip_routing` already exists in its C5 form (working-only load,
>   idle-down enumeration `down_links`, the `LookupError` no-QoT guard). The failover-aware
>   rewrite **preserves** the C5-1 "no-QoT-recorded" structured state and the C5-5
>   `down_links` contract, as Part 4 adj #2 requires.
> - **Task 4:** validator physics is sound because `recompute_qot_under_loading` already
>   skips failed assets (C4/S8-1) and builds per-OMS interferer combs (C2) — Part 4 adj #3.
> - **C6/S7-5** (`NewLightpathRun.src_node/.dst_node`, directed OMS solver) and **C9** (layered
>   MultiDiGraph) have landed, so provisioning consumes travel-direction-correct routing — Part 4 adj #4/#5.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Use superpowers:test-driven-development for every function. Steps use checkbox (`- [ ]`) syntax for tracking. **Depends on Phases 1–6 being merged** — in particular the arbitrary-loading contract (`recompute_qot_under_loading`), the margin-gated `ip_link_capacity_gbps`, `simulate_ip_routing`, the OMS disjointness audit (`check_disjointness`), the spectrum state, and the what-if injection layer. This plan reuses all of them and adds **no new physics**.

**Goal:** Land Build-order **Step 7 (validate + commit + reconcile)** — the gated mutation surface. A **plan** is an ordered sequence of operations (provision/teardown a lightpath, reroute a service, downshift a mode). `validate_plan` replays the sequence on a clone and returns a **typed violation list checked at every intermediate state, not just the endpoints** (mode-infeasibility, spectrum clash, IP-link overload, dropped traffic, disjointness collapse — with the make-before-break transient surfaced as a `transient` flag). The single-op mutation tools (`provision_lightpath`, `teardown_lightpath`, `set_modulation_format`) flip the bound IP link as a consequence. `commit_plan(dry_run)` simulates or — behind an explicit approval gate and a prior clean validation — actuates. `reconcile()` reads back actual state after a live commit and surfaces partial-failure drift as typed entries.

**Architecture:** A plan is a tuple of typed `PlanOp` dataclasses, each with an `apply(model)` that mutates a branch in place. `validate_plan` clones ground truth, replays op-by-op, and after **each** op recomputes QoT under `loading_from_model` (which now includes any just-provisioned channel — the old ∪ new overlap, for free) and runs a per-state check battery. A finding present at a non-final state but absent at the committed endpoint is tagged `transient=True` — that *is* the make-before-break window. Commit reuses the same replay: `dry_run` applies to a branch and returns the diff; a live commit actuates op-by-op through an injectable `Actuator` (default succeeds; a partial-failure actuator is the test seam), records the *intended* end-state as a snapshot, and `reconcile()` diffs actual-vs-intended into typed `Drift`. Ground truth is never touched by validation or dry-run.

**Tech Stack:** Python 3.11+, gnpy (pinned), NetworkX ≥3.2, pytest, FastMCP. No new deps. Run tests with `conda run -n multilayer-optical-mcp pytest`.

---

## Context (what already exists — do not rebuild)

- **`recompute_qot_under_loading(*, model, store, loading)`** (`gnpy_adapter/adapter.py:356`) computes gated (worse-direction) QoT for every lightpath under a loading, writes `QoTState` onto the model, returns `{lp_id: (QoTState, rid)}`. The validator calls this once per intermediate state.
- **`loading_from_model(model)`** (`model/whatif.py:28`) builds one `Channel` per *currently-existing* lightpath. After a provision op (before the matching teardown) it returns the overlap set automatically — this is the entire make-before-break mechanism. Reuse verbatim.
- **`ip_link_capacity_gbps(link_id)`** (`model/network.py:223`) returns `0.0` when the bound lightpath's `margin_db < 0`, else `mode.bitrate_gbps`. The validator never sets margin; it reads capacity, which already encodes the mode-feasibility gate.
- **`simulate_ip_routing(model)`** (`model/ip_routing.py:92`) → `IPRoutingResult{utilizations, congested_links, down_links, dropped_services, overflow_gbps}`. The IP-overload and dropped-traffic checks read this directly.
- **`check_disjointness(model, path_a, path_b, basis, level)`** (`model/solvers.py:166`) audits two **OMS-sequence** paths and returns `shared_assets`/`shared_groups`. Disjointness-collapse maps a service's working/protection **IP** paths to OMS sequences and calls this.
- **Spectrum state** (`model/spectrum.py`): `SpectrumGrid.default()`, `slot_of(freq)`. A clash is two lightpaths on the same `(oms, slot)`.
- **Snapshot clone/diff** (`model/snapshots.py`): `_clone` copies every `_*` dict + `_failed_assets`; `diff` reports added/removed/modified per registry. **Any new registry or field MUST be carried in `_clone` and `diff`.**
- **`set_lightpath_mode(lp_id, mode_id)`** (`network.py:216`) and **`set_service_working_path(service_id, ip_path)`** (`network.py:147`, contiguity-checked) already exist — the mode-downshift and reroute ops wrap them. `reroute_service` is already a tool (`server.py:230`); Phase 7 adds the other mutation tools and the plan/commit surface around them.
- **Server test harness:** `app._tool_manager._tools[name].fn(**kwargs)` invokes a tool's function; `app._snapshots` reaches the live `SnapshotStore` (see `tests/test_server_phase6.py:12`).
- **Asset-id conventions** from `model_from_abstract_graph` (edge src `0`→dst `1`, `num_spans=2`): fibers `fiber_0_1_0/1`, amps `amp_0_1_0/1`, OMS `oms_0_1` and `oms_1_0`. Reused in tests below, same as Phase 6.

---

## Decisions settled (call out at spec review if you disagree)

1. **A plan is an ordered tuple of typed `PlanOp`; intermediate state = the model after each cumulative op.** Replaying op-by-op and validating after each is what "checks every intermediate state of a sequence" means concretely. The make-before-break overlap is not a special construction — it is just the state between a provision and its teardown, and `loading_from_model` already includes both channels there. Rejected: a separate before/after pair API (the exact re-architecture CLAUDE.md's step-2 contract exists to avoid).
2. **The CLAUDE.md "transient overload" item is a `transient: bool` flag on the violation, not a parallel violation type.** A transient is the *same* physical event (a mode-infeasibility, an IP overload, a drop) observed at a non-endpoint state and gone at the committed endpoint. Encoding it as a flag on `MODE_INFEASIBLE` / `IP_LINK_OVERLOAD` / `DROPPED_TRAFFIC` (with the `state_index`) keeps the type set small and lets the agent filter `transient=True` to see exactly the make-before-break window. Flagged for review: if you want a distinct `TRANSIENT_OVERLOAD` type, it is a one-line enum add plus tagging — but two types for one event is redundant.
3. **`teardown_lightpath` removes the lightpath and its bound IP links from the model.** It does *not* sentinel them. A removed lightpath stops contributing a channel to `loading_from_model` and a slot to the spectrum state — which is the whole point of make-before-break (the new lightpath must not permanently clash with the torn-down one). Consequence: a service still referencing a removed IP link must surface as *dropped*, so `simulate_ip_routing` gains tolerance for missing links (`reason="link_removed"`). This is additive — existing paths never reference missing links.
4. **"Optical capacity" is subsumed by the `SPECTRUM_CLASH` *type*, but the *detail* carries the remediation discriminator.** Grid exhaustion and a slot collision are detected as the same observable (two lightpaths on one `(oms, slot)`), so they share a type. But the agent needs to know which repair applies — and that distinction lives in the detail, not in a second type. Every `SPECTRUM_CLASH` therefore reports `retune_candidates`: for each clashing lightpath, the slots free on *every* OMS of its path. Non-empty ⇒ retune to a free frequency on the same path (cheap, local). Empty ⇒ the path is spectrally exhausted; the agent must reroute (call `compute_paths`/`solve_rsa`). A separate `OPTICAL_CAPACITY` *type* would fire on the identical `len > 1` test and still force the agent to re-derive free-slot availability itself, so it adds a type without adding information. **General principle (applies to every violation): the type is for triage; the detail must carry enough locally-cheap context for the agent to choose a feasible reoptimization without re-running a solver to learn what kind of failure it faces.** Accordingly: `MODE_INFEASIBLE` reports `gsnr_db`/`required_gsnr_db`/`deficit_db` and `feasible_downshift_modes` (empty ⇒ a downshift cannot recover it, reroute/repair needed); `IP_LINK_OVERLOAD` reports `overflow_gbps` (how much to offload); `DROPPED_TRAFFIC` reports `demand_gbps` and `reason`; `DISJOINTNESS_COLLAPSE` reports `shared_assets`/`shared_groups` (what to route around). The validator never computes the fix — it hands over the discriminators that point at the right solver.
5. **Disjointness-collapse is checked at the committed (final) state only and is never `transient`.** It is an endpoint property of the plan ("does the committed working/protection pair share risk under the required basis?"), per CLAUDE.md scenario 1. `validate_plan` takes `basis` (default `physical`) and `level` (default `link`); every service with a non-empty protection path is audited.
6. **Dropped traffic is gated by an aggregate `dropped_tolerance_gbps` (default `0.0`).** If the summed demand of dropped services at a state exceeds the tolerance, one `DROPPED_TRAFFIC` violation per dropped service is emitted for that state. Tolerance is an *aggregate* gate so an operator can permit a known small loss. A service that declares demand but rides no IP path (empty `working_path`) is folded into the same dropped accounting with `reason="unrouted"`, so a demand can never silently vanish from the conservation check — it is lost traffic and counts against the tolerance like any other drop.
7. **A live commit requires both a clean internal validation and an explicit `confirm=True`.** CLAUDE.md's "approval-gated and requires a prior successful validate_plan": the server enforces the *validation* part by validating inside `commit_plan` and refusing on any violation; the human-approval part is the explicit `confirm` flag (the operator/harness sets it). No hidden global "last validated" state to get stale — the gate is per-call and self-contained.
8. **The control plane is an injectable `Actuator` (default = apply-all-succeed).** Live actuation loops ops through it; an op the actuator reports failed is not applied. Tests inject a partial-failure actuator. `commit_plan` records the *intended* end-state (all ops applied on a clone) as a snapshot; `reconcile()` diffs current actual vs that intended snapshot into typed `Drift`. This is how the server stays control-plane-agnostic but not reconciliation-agnostic (CLAUDE.md).
9. **A structurally invalid plan is a typed `INVALID_PLAN` violation, not an exception.** `apply_op` raises `PlanError` on a bad reference — a teardown/downshift of an unknown lightpath, a **duplicate provision** (a lightpath or IP-link id that already exists), or an unknown op kind. `validate_plan` catches it during replay and returns a single `INVALID_PLAN` violation carrying the failing `op_index` and message, then stops (a plan cannot be meaningfully validated past an op that does not apply, so any prior-state findings are dropped — fix the plan first). This is the typed-results rule (CLAUDE.md "All tool results are structured … never prose") reaching the malformed-input case: the agent gets a structured "op 2 references unknown lightpath X", not a stack trace. The duplicate-provision guard lives in `apply_op`, so the validator **and** the live single-op mutation tools reject it identically.
10. **Protection-path viability is checked at the committed state, separately from disjointness.** `DISJOINTNESS_COLLAPSE` proves working and protection won't fail *together*; it says nothing about whether protection can *carry the load* when failover calls it. A protection path that is perfectly disjoint but already dark (its lightpath margin < 0 → capacity 0, or the link removed) or undersized (bottleneck capacity < demand) is a latent restoration failure a change-plan validator must catch. `PROTECTION_NOT_VIABLE` fires per service whose protection path has any dead link or insufficient bottleneck capacity, with detail `{demand_gbps, protection_capacity_gbps, dead_links, bottleneck_link}` so the agent knows whether to repair/re-light (dead links) or re-route/upgrade (undersized bottleneck). Endpoint-only and never `transient`, like disjointness — it is a property of the committed plan, not of a transient mid-sequence state.
11. **Protection is dedicated 1:1: reserved bandwidth + auto-failover.** Protection capacity is *reserved* (not actively carried in steady state) and cannot be double-booked — `reserved_capacity_per_link` sums each protected service's full demand onto every link of its protection path, summed across services (dedicated, so it holds under any single-or-simultaneous failover). Two consequences. **(a) `simulate_ip_routing` is failover-aware:** a service rides its working path while up and switches to its reserved protection path the instant any working link goes down/removed (`active_load_per_link` via `_active_path`); it is `dropped` only when *both* paths are unusable, and services currently on protection are reported in a new `restored_services` field — this is what makes `inject_failure(working)` + `simulate` surface the disaster-restoration survival. **(b) A new endpoint check `PROTECTION_OVERSUBSCRIBED`** enforces the reservation: per link, `working_load + reserved > capacity` is a violation. Because oversubscription is rejected, the reserved failover capacity is *guaranteed present*, which is exactly what makes gap #4's `PROTECTION_NOT_VIABLE` correct with its cheap **nominal** check (no per-service contingency simulation needed). `offered_load_per_link` stays **working-only** (planning/nominal, feeds the admission check + ip_topology); the failover-aware view lives in `active_load_per_link`/`simulate_ip_routing`. NB: this models the post-failover quasi-static *state*, not the switching *instant* (still out of scope — CLAUDE.md transient gap).

---

## File structure

- **Modify `src/multilayer_optical_mcp/model/network.py`** — add `clone()` (the single home for deep-copy), `remove_lightpath(lp_id)`, `remove_ip_link(link_id)`.
- **Modify `src/multilayer_optical_mcp/model/snapshots.py`** — `_clone` delegates to `model.clone()`; extract `diff_models(a, b)` as a free function (so `reconcile` can diff two model objects without storing both); add `SnapshotStore.put(model) -> id` (register an externally-built model, for the intended end-state).
- **Modify `src/multilayer_optical_mcp/model/ip_routing.py`** — keep `offered_load_per_link` working-only; add `reserved_capacity_per_link`, `active_load_per_link`, `_active_path`/`_link_is_up`/`_first_bad_link`; make `simulate_ip_routing` failover-aware (active-path placement, `restored_services`, drops only when both paths fail, `reason ∈ {link_down, link_removed, unrouted}`); add `restored_services` to `IPRoutingResult`.
- **Create `src/multilayer_optical_mcp/model/plan.py`** — `PlanOp` subtypes (`ProvisionLightpath`, `TeardownLightpath`, `RerouteService`, `SetModulationFormat`), `Plan`, `PlanError`, `apply_op` (with the duplicate-id guard → `PlanError`), `replay`, `plan_from_dict`, `service_oms_sequence`.
- **Create `src/multilayer_optical_mcp/model/validate.py`** — `ViolationType` (incl. `INVALID_PLAN`, `PROTECTION_NOT_VIABLE`, `PROTECTION_OVERSUBSCRIBED`), `Violation`, `ValidationReport`, the per-state check helpers (mode / spectrum / IP), the endpoint checks (disjointness + protection viability + protection oversubscription), `validate_plan`.
- **Create `src/multilayer_optical_mcp/model/commit.py`** — `Actuator` protocol, `full_actuator`, `actuate`, `CommitResult`, `Drift`, `DriftReport`, `drift_from_diff`, `commit_plan`, `reconcile`.
- **Modify `src/multilayer_optical_mcp/model/views.py`** — `validation_report_dict`, `commit_result_dict`, `drift_report_dict`; add `restored` to `ip_routing_result_dict`.
- **Modify `src/multilayer_optical_mcp/server.py`** — tools `validate_plan`, `provision_lightpath`, `teardown_lightpath`, `set_modulation_format`, `commit_plan`, `reconcile`.
- **Create `tests/model/test_plan.py`** — op application, replay, `plan_from_dict`, `service_oms_sequence`.
- **Create `tests/model/test_validate.py`** — each violation type on a steady-state final plan.
- **Create `tests/model/test_validate_transient.py`** — the headline: a finding present mid-sequence and gone at the endpoint is `transient=True`; reordering to make-before-break clears it.
- **Create `tests/model/test_commit_reconcile.py`** — dry-run leaves ground truth untouched; live commit with a partial-failure actuator surfaces drift.
- **Create `tests/test_server_phase7.py`** — the six tools end-to-end through FastMCP.

---

# Part A — model plumbing (no GNPy, no tools)

## Task 1: `NetworkModel.clone()`, `diff_models`, `SnapshotStore.put`

**Files:**
- Modify: `src/multilayer_optical_mcp/model/network.py`
- Modify: `src/multilayer_optical_mcp/model/snapshots.py`
- Test: `tests/model/test_snapshots.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/model/test_snapshots.py
from multilayer_optical_mcp.model.snapshots import SnapshotStore, diff_models
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.assets import TransceiverMode
from multilayer_optical_mcp.model.network import NetworkModel


def _empty_model():
    return NetworkModel(modes=ModeRegistry([TransceiverMode(
        id="400G@7.1dB", bitrate_gbps=400.0, required_gsnr_db=7.1,
        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)]))


def test_clone_is_independent():
    m = _empty_model()
    c = m.clone()
    c.define_risk_group("rg1", ("x",))
    assert "rg1" not in m._risk_groups       # parent untouched
    assert "rg1" in c._risk_groups


def test_diff_models_matches_store_diff():
    a = _empty_model()
    b = a.clone()
    b.define_risk_group("rg1", ("x",))
    d = diff_models(a, b)
    assert d["risk_groups"]["added"] == ("rg1",)


def test_put_registers_external_model():
    base = _empty_model()
    store = SnapshotStore(base)
    other = base.clone()
    other.define_risk_group("rg9", ("y",))
    sid = store.put(other)
    assert store.get(sid) is not other          # stored a clone, not the live object
    assert "rg9" in store.get(sid)._risk_groups
```

- [ ] **Step 2: Run to verify failure**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_snapshots.py -k "clone_is_independent or diff_models or put_registers" -v`
Expected: FAIL (`clone`, `diff_models`, `put` undefined).

- [ ] **Step 3: Add `clone()` to `NetworkModel`.** Append after `is_failed` (end of `network.py`):

```python
    # ---------------------------------------------------------------- copy-on-write

    def clone(self) -> "NetworkModel":
        """Deep-ish copy: new registries, shared immutable (frozen) values and the
        shared ModeRegistry. The single home for model duplication — SnapshotStore
        and validate/commit all route through here."""
        c = NetworkModel(modes=self.modes)
        c._fiber_types = dict(self._fiber_types)
        c._fibers = dict(self._fibers)
        c._amplifiers = dict(self._amplifiers)
        c._roadms = dict(self._roadms)
        c._transceivers = dict(self._transceivers)
        c._oms = dict(self._oms)
        c._lightpaths = dict(self._lightpaths)
        c._routers = dict(self._routers)
        c._ip_links = dict(self._ip_links)
        c._services = dict(self._services)
        c._srlgs = dict(self._srlgs)
        c._risk_groups = dict(self._risk_groups)
        c._qot_state = dict(self._qot_state)
        c._failed_assets = set(self._failed_assets)
        return c
```

- [ ] **Step 4: Refactor `snapshots.py` to delegate, add `diff_models` + `put`.** Replace the `_clone` staticmethod and `diff` method, and add the free function + `put`:

```python
    def put(self, model: NetworkModel) -> str:
        """Register an externally-constructed model under a fresh id (stores a
        clone, so later mutation of the argument cannot corrupt the snapshot)."""
        sid = uuid.uuid4().hex
        self._store(sid, model.clone())
        return sid

    def diff(self, a_id: str, b_id: str) -> dict:
        return diff_models(self._snapshots[a_id], self._snapshots[b_id])

    @staticmethod
    def _clone(m: NetworkModel) -> NetworkModel:
        return m.clone()
```

and add at the bottom of `snapshots.py` (keep `_delta` and `_delta_set`):

```python
def diff_models(a: NetworkModel, b: NetworkModel) -> dict:
    """Structured per-registry delta between two model objects (snapshot-agnostic
    so reconcile can diff live-vs-intended without storing both)."""
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
        "failed_assets": _delta_set(a._failed_assets, b._failed_assets),
    }
```

> Note: the old `diff` listed exactly these registries; `diff_models` is the same body moved to a free function. The `_clone`-delegates-to-`clone()` change is behaviour-preserving — existing snapshot tests cover it.

- [ ] **Step 5: Run to verify pass**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_snapshots.py -v`
Expected: PASS (new + all pre-existing snapshot tests).

- [ ] **Step 6: Commit**

```bash
git add src/multilayer_optical_mcp/model/network.py src/multilayer_optical_mcp/model/snapshots.py tests/model/test_snapshots.py
git commit -m "feat(model): NetworkModel.clone() single-homes copy; diff_models + SnapshotStore.put"
```

---

## Task 2: `remove_lightpath` / `remove_ip_link`; failover-aware IP routing (1:1 reservation)

**Files:**
- Modify: `src/multilayer_optical_mcp/model/network.py`
- Modify: `src/multilayer_optical_mcp/model/ip_routing.py`
- Modify: `src/multilayer_optical_mcp/model/views.py` (add `restored` to `ip_routing_result_dict`)
- Test: `tests/model/test_ip_routing.py`; update key-set assertions in `tests/model/test_views.py` and `tests/test_server_phase5.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/model/test_ip_routing.py
from multilayer_optical_mcp.model.assets import (
    FiberType, Amplifier, Fiber, OMS, Lightpath, IPLink, Service,
)
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.assets import TransceiverMode
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.ip_routing import simulate_ip_routing


def _model_with_service():
    m = NetworkModel(modes=ModeRegistry([TransceiverMode(
        id="400G@7.1dB", bitrate_gbps=400.0, required_gsnr_db=7.1,
        symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)]))
    m.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    m.add_amplifier(Amplifier(id="a1", type_variety="adv", gain_db=20.0, nf_db=5.5))
    m.add_fiber(Fiber(id="fAB", a_end="a1", z_end="a2", length_km=80.0, type_variety="SSMF"))
    m.add_oms(OMS(id="omsAB", src_node_id="A", dst_node_id="B", elements=("a1", "fAB")))
    m.add_lightpath(Lightpath(id="lpAB", oms_sequence=("omsAB",),
                              mode_id="400G@7.1dB", center_freq_hz=193.4e12))
    m.add_ip_link(IPLink(id="ipAB", a_router="rA", z_router="rB", lightpath_id="lpAB"))
    m.add_service(Service(id="svc", src_router="rA", dst_router="rB",
                          demand_gbps=100.0, working_path=("ipAB",)))
    m.set_qot_state("lpAB", QoTState(gsnr_db=10.0, osnr_db=22.0, margin_db=2.0))
    return m


def test_remove_ip_link_then_service_is_dropped():
    m = _model_with_service()
    m.remove_ip_link("ipAB")
    res = simulate_ip_routing(m)        # must not KeyError on the missing link
    assert "svc" in {d.service_id for d in res.dropped_services}
    assert any(d.reason == "link_removed" for d in res.dropped_services)


def test_remove_lightpath_also_unbinds_ip_link():
    m = _model_with_service()
    m.remove_lightpath("lpAB")          # removes the lightpath and its bound IP link
    assert "lpAB" not in m._lightpaths
    assert "ipAB" not in m._ip_links
    res = simulate_ip_routing(m)
    assert "svc" in {d.service_id for d in res.dropped_services}


def _protected_model():
    """svc rA->rB: working on ipAB (lpAB/omsAB), protection on a DISJOINT
    ipCD (lpCD/omsCD); both up, demand 100 < cap 400."""
    from dataclasses import replace
    m = _model_with_service()                       # svc demand 100, working ("ipAB",)
    m.add_amplifier(Amplifier(id="a3", type_variety="adv", gain_db=20.0, nf_db=5.5))
    m.add_fiber(Fiber(id="fCD", a_end="a3", z_end="a4", length_km=80.0, type_variety="SSMF"))
    m.add_oms(OMS(id="omsCD", src_node_id="A", dst_node_id="B", elements=("a3", "fCD")))
    m.add_lightpath(Lightpath(id="lpCD", oms_sequence=("omsCD",),
                              mode_id="400G@7.1dB", center_freq_hz=193.5e12))
    m.add_ip_link(IPLink(id="ipCD", a_router="rA", z_router="rB", lightpath_id="lpCD"))
    m.set_qot_state("lpCD", QoTState(gsnr_db=10.0, osnr_db=22.0, margin_db=2.0))
    m._services["svc"] = replace(m.get_service("svc"), protection_path=("ipCD",))
    return m


def test_working_failure_fails_over_to_protection_not_dropped():
    m = _protected_model()
    m.set_qot_state("lpAB", QoTState(gsnr_db=5.0, osnr_db=18.0, margin_db=-1.0))  # working dark
    res = simulate_ip_routing(m)
    assert "svc" not in {d.service_id for d in res.dropped_services}   # survived
    assert "svc" in res.restored_services                             # via protection
    util = {u.ip_link_id: u for u in res.utilizations}
    assert util["ipCD"].offered_gbps == 100.0                        # load moved to protection
    assert util["ipAB"].offered_gbps == 0.0                          # off the dead working link


def test_both_paths_down_drops_service():
    m = _protected_model()
    m.set_qot_state("lpAB", QoTState(gsnr_db=5.0, osnr_db=18.0, margin_db=-1.0))
    m.set_qot_state("lpCD", QoTState(gsnr_db=5.0, osnr_db=18.0, margin_db=-1.0))
    res = simulate_ip_routing(m)
    assert "svc" in {d.service_id for d in res.dropped_services}
    assert "svc" not in res.restored_services


def test_reserved_capacity_sums_protection_demand():
    from multilayer_optical_mcp.model.ip_routing import reserved_capacity_per_link
    m = _protected_model()                          # svc reserves 100 on ipCD
    reserved = reserved_capacity_per_link(m)
    assert reserved["ipCD"] == 100.0
    assert reserved["ipAB"] == 0.0                  # working link carries no reservation
```

- [ ] **Step 2: Run to verify failure**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_ip_routing.py -k "remove_ip_link or remove_lightpath" -v`
Expected: FAIL (`remove_ip_link`/`remove_lightpath` undefined; current `simulate_ip_routing` would also KeyError on a missing link).

- [ ] **Step 3: Add removal methods to `NetworkModel`.** Place after `add_ip_link`/`ip_links_for_lightpath` (near the IP layer section):

```python
    def remove_ip_link(self, link_id: str) -> None:
        """Remove an IP link. Services referencing it keep the dangling id in
        their working_path; simulate_ip_routing reports them dropped."""
        self._ip_links.pop(link_id, None)

    def remove_lightpath(self, lp_id: str) -> None:
        """Tear down a lightpath: drop it, its recorded QoT, and unbind every IP
        link riding it (teardown flips the bound IP link down)."""
        for link_id in self.ip_links_for_lightpath(lp_id):
            self._ip_links.pop(link_id, None)
        self._lightpaths.pop(lp_id, None)
        self._qot_state.pop(lp_id, None)
```

> `ip_links_for_lightpath` raises `KeyError` if the lightpath is unknown — call `remove_lightpath` only for an existing lightpath (the teardown op guarantees this; the validator surfaces a typed error otherwise — see Task 3 Step 3).

- [ ] **Step 4: Failover-aware IP routing in `ip_routing.py`.** Keep `offered_load_per_link` as the **working-only nominal** load (its merged contract — `test_reroute_repins_working_path` depends on it). Add the reservation read, the active-path failover helpers, and the `restored_services` field, then rewrite `simulate_ip_routing` to place each service on its active path:

```python
def offered_load_per_link(model: NetworkModel) -> Dict[str, float]:
    """Working-only NOMINAL load: each service's demand on every link of its
    pinned working_path, ignoring failures. Missing links are skipped. Feeds the
    1:1 reservation-admission check and ip_topology; simulate_ip_routing uses the
    failover-aware active_load_per_link instead."""
    load: Dict[str, float] = {link.id: 0.0 for link in model.list_ip_links()}
    for svc in model.list_services():
        for ip_id in svc.working_path:
            if ip_id in load:
                load[ip_id] += svc.demand_gbps
    return load


def reserved_capacity_per_link(model: NetworkModel) -> Dict[str, float]:
    """Σ protection-path demand reserved on each IP link. Dedicated 1:1: every
    protected service reserves its full demand on every link of its protection
    path, and reservations SUM across services sharing a link, so the reservation
    holds under any single-or-simultaneous failover. Feeds PROTECTION_OVERSUBSCRIBED."""
    reserved: Dict[str, float] = {link.id: 0.0 for link in model.list_ip_links()}
    for svc in model.list_services():
        for ip_id in svc.protection_path:
            if ip_id in reserved:
                reserved[ip_id] += svc.demand_gbps
    return reserved


def _link_is_up(model: NetworkModel, ip_id: str) -> bool:
    """A link is up iff it exists and its bound lightpath carries capacity
    (margin >= 0). A removed or margin-negative link is down."""
    try:
        return model.ip_link_capacity_gbps(ip_id) > 0.0
    except (KeyError, LookupError):
        return False


def _active_path(model: NetworkModel, svc) -> Tuple[Optional[Tuple[str, ...]], str]:
    """The path a service currently rides under 1:1 auto-failover: working if all
    its links are up; else the reserved protection path if present and all up;
    else (None, "none"). Returns (path, "working" | "protection" | "none")."""
    if svc.working_path and all(_link_is_up(model, ip) for ip in svc.working_path):
        return svc.working_path, "working"
    if svc.protection_path and all(_link_is_up(model, ip) for ip in svc.protection_path):
        return svc.protection_path, "protection"
    return None, "none"


def active_load_per_link(model: NetworkModel) -> Dict[str, float]:
    """Failover-aware load: each service contributes its demand to whichever path
    it currently rides (working if up, else reserved protection). A service with
    no usable path contributes nothing — it is dropped."""
    load: Dict[str, float] = {link.id: 0.0 for link in model.list_ip_links()}
    for svc in model.list_services():
        path, _which = _active_path(model, svc)
        if path is None:
            continue
        for ip_id in path:
            if ip_id in load:
                load[ip_id] += svc.demand_gbps
    return load


def _first_bad_link(model: NetworkModel, path: Tuple[str, ...]) -> Tuple[str, str]:
    """First link in path that is removed (absent) or down (cap 0); ("", "none")
    if all up. Attributes a drop to a concrete failed link."""
    for ip_id in path:
        try:
            cap = model.ip_link_capacity_gbps(ip_id)
        except (KeyError, LookupError):
            return ip_id, "removed"
        if cap == 0.0:
            return ip_id, "down"
    return "", "none"
```

Add the `restored_services` field to `IPRoutingResult` (default `()` keeps every other constructor valid):

```python
@dataclass(frozen=True)
class IPRoutingResult:
    utilizations: Tuple[LinkUtilization, ...]
    congested_links: Tuple[str, ...]
    down_links: Tuple[str, ...]
    dropped_services: Tuple[DroppedService, ...]
    overflow_gbps: float
    restored_services: Tuple[str, ...] = ()   # riding protection after a working failure
```

Rewrite `simulate_ip_routing` to use the active load and classify each service:

```python
def simulate_ip_routing(model: NetworkModel) -> IPRoutingResult:
    """Failover-aware read: place each service on its active path (working if up,
    else reserved protection), account utilization, and report congestion, drops,
    and services restored onto protection. Routes nothing; pins paths."""
    load = active_load_per_link(model)
    utils: List[LinkUtilization] = []
    congested: List[str] = []
    down: List[str] = []
    overflow = 0.0
    for link in model.list_ip_links():
        offered = load[link.id]
        try:                                      # C5-1 (S5-4): read tool never raises
            cap = model.ip_link_capacity_gbps(link.id)
        except LookupError:
            utils.append(LinkUtilization(link.id, offered, None, None, False))
            continue                              # no QoT recorded -> capacity unknown
        is_down = cap == 0.0
        util = None if is_down else offered / cap
        utils.append(LinkUtilization(link.id, offered, cap, util, is_down))
        if is_down:
            down.append(link.id)                  # C5-2 (S5-5): every down link, loaded or idle
        elif util is not None and util > 1.0:
            congested.append(link.id)
            overflow += offered - cap
    dropped: List[DroppedService] = []
    restored: List[str] = []
    for svc in model.list_services():
        path, which = _active_path(model, svc)
        if path is None:
            if not svc.working_path:
                dropped.append(DroppedService(svc.id, "unrouted", ""))
            else:
                bad_id, kind = _first_bad_link(model, svc.working_path)
                reason = "link_removed" if kind == "removed" else "link_down"
                dropped.append(DroppedService(svc.id, reason, bad_id))
        elif which == "protection":
            restored.append(svc.id)
    return IPRoutingResult(
        utilizations=tuple(utils),
        congested_links=tuple(congested),
        down_links=tuple(down),
        dropped_services=tuple(dropped),
        overflow_gbps=overflow,
        restored_services=tuple(restored),
    )
```

> Update the now-stale `DroppedService.reason` comment ("currently always link_down") — reasons are `link_down`, `link_removed`, or `unrouted`.

- [ ] **Step 5: Surface `restored` in the serializer; update the two key-set assertions.** In `views.py`, add a top-level `restored` to `ip_routing_result_dict` (between `congestion` and `dropped`):

```python
        "congestion": list(res.congested_links),
        "restored": list(res.restored_services),
        "dropped": {
```

This adds one key to the serialized result, so update the two merged assertions that pin the exact key set — add `"restored"` to each:
- `tests/model/test_views.py` (`set(d) == {"utilizations", "congestion", "dropped"}`).
- `tests/test_server_phase5.py` (same set).

> This is the **only** merged-test change in Phase 7, and it is purely additive (a new output field). The failover *behavior* breaks nothing — every merged drop test uses an unprotected service (verified: `svc-AC`, `svc0`, `svc-AB` have no `protection_path`), so they still drop identically.

- [ ] **Step 6: Run to verify pass**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_ip_routing.py tests/model/test_views.py tests/test_server_phase5.py -v`
Expected: PASS (new failover/reservation tests + updated key-set assertions + all pre-existing).

- [ ] **Step 7: Commit**

```bash
git add src/multilayer_optical_mcp/model/network.py src/multilayer_optical_mcp/model/ip_routing.py src/multilayer_optical_mcp/model/views.py tests/model/test_ip_routing.py tests/model/test_views.py tests/test_server_phase5.py
git commit -m "feat(model): 1:1 reserved protection + failover-aware simulate_ip_routing"
```

---

# Part B — the plan model

## Task 3: `plan.py` — typed ops, `apply_op`, `replay`, `plan_from_dict`

**Files:**
- Create: `src/multilayer_optical_mcp/model/plan.py`
- Test: `tests/model/test_plan.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/model/test_plan.py
import pytest
from multilayer_optical_mcp.model.assets import (
    FiberType, Amplifier, Fiber, OMS, Lightpath, IPLink, Service,
)
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.assets import TransceiverMode
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.plan import (
    Plan, ProvisionLightpath, TeardownLightpath, RerouteService,
    SetModulationFormat, apply_op, replay, plan_from_dict, service_oms_sequence,
)

MODES = ModeRegistry([
    TransceiverMode(id="400G", bitrate_gbps=400.0, required_gsnr_db=10.0,
                    symbol_rate_baud=87.5e9, channel_spacing_hz=100e9),
    TransceiverMode(id="200G", bitrate_gbps=200.0, required_gsnr_db=7.0,
                    symbol_rate_baud=43.75e9, channel_spacing_hz=100e9),
])


def _model():
    m = NetworkModel(modes=MODES)
    m.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    m.add_amplifier(Amplifier(id="a1", type_variety="adv", gain_db=20.0, nf_db=5.5))
    m.add_fiber(Fiber(id="fAB", a_end="a1", z_end="a2", length_km=80.0, type_variety="SSMF"))
    m.add_oms(OMS(id="omsAB", src_node_id="A", dst_node_id="B", elements=("a1", "fAB")))
    return m


def test_provision_adds_lightpath_and_binds_ip_link():
    m = _model()
    op = ProvisionLightpath(
        lightpath=Lightpath(id="lp1", oms_sequence=("omsAB",), mode_id="400G",
                            center_freq_hz=193.4e12),
        ip_link=IPLink(id="ip1", a_router="rA", z_router="rB", lightpath_id="lp1"))
    apply_op(m, op)
    assert "lp1" in m._lightpaths
    assert m.get_ip_link("ip1").lightpath_id == "lp1"


def test_teardown_removes_lightpath_and_ip_link():
    m = _model()
    apply_op(m, ProvisionLightpath(
        lightpath=Lightpath(id="lp1", oms_sequence=("omsAB",), mode_id="400G",
                            center_freq_hz=193.4e12),
        ip_link=IPLink(id="ip1", a_router="rA", z_router="rB", lightpath_id="lp1")))
    apply_op(m, TeardownLightpath(lightpath_id="lp1"))
    assert "lp1" not in m._lightpaths
    assert "ip1" not in m._ip_links


def test_set_modulation_format_changes_mode():
    m = _model()
    apply_op(m, ProvisionLightpath(
        lightpath=Lightpath(id="lp1", oms_sequence=("omsAB",), mode_id="400G",
                            center_freq_hz=193.4e12), ip_link=None))
    apply_op(m, SetModulationFormat(lightpath_id="lp1", mode_id="200G"))
    assert m.get_lightpath("lp1").mode_id == "200G"


def test_replay_applies_all_ops_in_order_on_a_clone():
    m = _model()
    plan = Plan(ops=(
        ProvisionLightpath(
            lightpath=Lightpath(id="lp1", oms_sequence=("omsAB",), mode_id="400G",
                                center_freq_hz=193.4e12),
            ip_link=IPLink(id="ip1", a_router="rA", z_router="rB", lightpath_id="lp1")),
        SetModulationFormat(lightpath_id="lp1", mode_id="200G"),
    ))
    out = replay(m, plan)
    assert out.get_lightpath("lp1").mode_id == "200G"
    assert "lp1" not in m._lightpaths       # replay never touches the input model


def test_plan_from_dict_round_trips_each_op():
    plan = plan_from_dict({"ops": [
        {"op": "provision_lightpath",
         "lightpath": {"id": "lp1", "oms_sequence": ["omsAB"], "mode_id": "400G",
                       "center_freq_hz": 193.4e12},
         "ip_link": {"id": "ip1", "a_router": "rA", "z_router": "rB"}},
        {"op": "teardown_lightpath", "lightpath_id": "lp1"},
        {"op": "reroute_service", "service_id": "svc", "ip_path": ["ip1"]},
        {"op": "set_modulation_format", "lightpath_id": "lp1", "mode_id": "200G"},
    ]})
    assert isinstance(plan.ops[0], ProvisionLightpath)
    assert plan.ops[0].ip_link.lightpath_id == "lp1"   # bound to the new lightpath
    assert isinstance(plan.ops[1], TeardownLightpath)
    assert isinstance(plan.ops[2], RerouteService)
    assert isinstance(plan.ops[3], SetModulationFormat)


def test_service_oms_sequence_traces_ip_path_to_oms():
    m = _model()
    apply_op(m, ProvisionLightpath(
        lightpath=Lightpath(id="lp1", oms_sequence=("omsAB",), mode_id="400G",
                            center_freq_hz=193.4e12),
        ip_link=IPLink(id="ip1", a_router="rA", z_router="rB", lightpath_id="lp1")))
    assert service_oms_sequence(m, ("ip1",)) == ("omsAB",)


def test_provision_duplicate_lightpath_id_raises_plan_error():
    from multilayer_optical_mcp.model.plan import PlanError
    m = _model()
    op = ProvisionLightpath(
        lightpath=Lightpath(id="lp1", oms_sequence=("omsAB",), mode_id="400G",
                            center_freq_hz=193.4e12), ip_link=None)
    apply_op(m, op)
    with pytest.raises(PlanError):
        apply_op(m, op)        # same lightpath id again -> rejected, not overwritten
```

- [ ] **Step 2: Run to verify failure**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_plan.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `plan.py`**

```python
# src/multilayer_optical_mcp/model/plan.py
"""A plan is an ordered sequence of typed operations. Each op mutates a branch
model in place via apply_op; replay applies a whole plan to a fresh clone so the
input model is never touched. validate_plan/commit_plan drive these.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Tuple, Union

from .assets import IPLink, Lightpath
from .network import NetworkModel


@dataclass(frozen=True)
class ProvisionLightpath:
    """Light a new lightpath; optionally bind and bring up an IP link on it."""
    lightpath: Lightpath
    ip_link: Optional[IPLink] = None


@dataclass(frozen=True)
class TeardownLightpath:
    lightpath_id: str


@dataclass(frozen=True)
class RerouteService:
    service_id: str
    ip_path: Tuple[str, ...]


@dataclass(frozen=True)
class SetModulationFormat:
    lightpath_id: str
    mode_id: str


PlanOp = Union[ProvisionLightpath, TeardownLightpath, RerouteService, SetModulationFormat]


@dataclass(frozen=True)
class Plan:
    ops: Tuple[PlanOp, ...]


class PlanError(ValueError):
    """A plan op references something the model does not contain, or is malformed."""


def apply_op(model: NetworkModel, op: PlanOp) -> None:
    """Apply a single op to model in place. Raises PlanError on a bad reference."""
    if isinstance(op, ProvisionLightpath):
        if op.lightpath.id in model._lightpaths:
            raise PlanError(
                f"provision: lightpath {op.lightpath.id!r} already exists")
        if op.ip_link is not None and op.ip_link.id in model._ip_links:
            raise PlanError(
                f"provision: ip_link {op.ip_link.id!r} already exists")
        model.add_lightpath(op.lightpath)
        if op.ip_link is not None:
            # bind the IP link to the just-provisioned lightpath, regardless of
            # whatever lightpath_id the caller put on it
            model.add_ip_link(replace(op.ip_link, lightpath_id=op.lightpath.id))
    elif isinstance(op, TeardownLightpath):
        if op.lightpath_id not in model._lightpaths:
            raise PlanError(f"teardown: unknown lightpath {op.lightpath_id!r}")
        model.remove_lightpath(op.lightpath_id)
    elif isinstance(op, RerouteService):
        model.set_service_working_path(op.service_id, tuple(op.ip_path))
    elif isinstance(op, SetModulationFormat):
        if op.lightpath_id not in model._lightpaths:
            raise PlanError(f"set_modulation_format: unknown lightpath {op.lightpath_id!r}")
        model.set_lightpath_mode(op.lightpath_id, op.mode_id)
    else:  # pragma: no cover - exhaustive
        raise PlanError(f"unknown op {op!r}")


def replay(model: NetworkModel, plan: Plan) -> NetworkModel:
    """Apply a whole plan to a fresh clone and return it. Input model untouched."""
    work = model.clone()
    for op in plan.ops:
        apply_op(work, op)
    return work


def service_oms_sequence(model: NetworkModel, ip_path: Tuple[str, ...]) -> Tuple[str, ...]:
    """Concatenate the OMS sequences of the lightpaths under an IP path. Used by
    the disjointness-collapse check to map IP working/protection paths to the
    OMS-sequence form check_disjointness audits."""
    seq: list[str] = []
    for ip_id in ip_path:
        lp = model.get_lightpath(model.get_ip_link(ip_id).lightpath_id)
        seq.extend(lp.oms_sequence)
    return tuple(seq)


def plan_from_dict(data: dict) -> Plan:
    """Parse the MCP-facing plan JSON into typed ops.

    {"ops": [
      {"op": "provision_lightpath",
       "lightpath": {id, oms_sequence, mode_id, center_freq_hz},
       "ip_link": {id, a_router, z_router} | null},
      {"op": "teardown_lightpath", "lightpath_id": ...},
      {"op": "reroute_service", "service_id": ..., "ip_path": [...]},
      {"op": "set_modulation_format", "lightpath_id": ..., "mode_id": ...}]}
    """
    ops: list[PlanOp] = []
    for raw in data.get("ops", []):
        kind = raw["op"]
        if kind == "provision_lightpath":
            lp = raw["lightpath"]
            lightpath = Lightpath(
                id=lp["id"], oms_sequence=tuple(lp["oms_sequence"]),
                mode_id=lp["mode_id"], center_freq_hz=lp["center_freq_hz"])
            ipl = raw.get("ip_link")
            ip_link = None if ipl is None else IPLink(
                id=ipl["id"], a_router=ipl["a_router"], z_router=ipl["z_router"],
                lightpath_id=lightpath.id)
            ops.append(ProvisionLightpath(lightpath=lightpath, ip_link=ip_link))
        elif kind == "teardown_lightpath":
            ops.append(TeardownLightpath(lightpath_id=raw["lightpath_id"]))
        elif kind == "reroute_service":
            ops.append(RerouteService(service_id=raw["service_id"],
                                      ip_path=tuple(raw["ip_path"])))
        elif kind == "set_modulation_format":
            ops.append(SetModulationFormat(lightpath_id=raw["lightpath_id"],
                                           mode_id=raw["mode_id"]))
        else:
            raise PlanError(f"unknown op kind {kind!r}")
    return Plan(ops=tuple(ops))
```

- [ ] **Step 4: Run to verify pass**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_plan.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/plan.py tests/model/test_plan.py
git commit -m "feat(plan): typed PlanOps + apply_op/replay/plan_from_dict over a clone"
```

---

# Part C — the validator

## Task 4: `validate.py` — typed violations + per-state battery + `validate_plan`

**Files:**
- Create: `src/multilayer_optical_mcp/model/validate.py`
- Test: `tests/model/test_validate.py`

`validate_plan` clones ground truth, replays op-by-op, and after **each** op recomputes QoT under `loading_from_model` then runs the per-state check battery. Each finding carries its `state_index`; a finding absent at the final state is `transient=True`. Disjointness-collapse and protection-path viability are checked at the final state only. A malformed op (bad reference, duplicate id) short-circuits replay into a single `INVALID_PLAN` violation.

- [ ] **Step 1: Write the failing tests** (steady-state, one violation type each; transient is Task 5)

```python
# tests/model/test_validate.py
import pytest
from multilayer_optical_mcp.model.assets import (
    FiberType, Amplifier, Fiber, OMS, Lightpath, IPLink, Service, SRLG,
)
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.assets import TransceiverMode
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.plan import (
    Plan, RerouteService, SetModulationFormat, TeardownLightpath,
)
from multilayer_optical_mcp.model.validate import (
    validate_plan, ViolationType, Violation, ValidationReport,
)

MODES = ModeRegistry([
    TransceiverMode(id="400G", bitrate_gbps=400.0, required_gsnr_db=10.0,
                    symbol_rate_baud=87.5e9, channel_spacing_hz=100e9),
    TransceiverMode(id="200G", bitrate_gbps=200.0, required_gsnr_db=7.0,
                    symbol_rate_baud=43.75e9, channel_spacing_hz=100e9),
])


def _ip_over_optical(margin_db=2.0, demand=300.0):
    """rA-rB IP link on lpAB (400G, cap 400), carrying `demand`."""
    m = NetworkModel(modes=MODES)
    m.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    m.add_amplifier(Amplifier(id="a1", type_variety="adv", gain_db=20.0, nf_db=5.5))
    m.add_fiber(Fiber(id="fAB", a_end="a1", z_end="a2", length_km=80.0, type_variety="SSMF"))
    m.add_oms(OMS(id="omsAB", src_node_id="A", dst_node_id="B", elements=("a1", "fAB")))
    m.add_lightpath(Lightpath(id="lpAB", oms_sequence=("omsAB",), mode_id="400G",
                              center_freq_hz=193.4e12))
    m.add_ip_link(IPLink(id="ipAB", a_router="rA", z_router="rB", lightpath_id="lpAB"))
    m.add_service(Service(id="svc", src_router="rA", dst_router="rB",
                          demand_gbps=demand, working_path=("ipAB",)))
    m.set_qot_state("lpAB", QoTState(gsnr_db=12.0, osnr_db=22.0, margin_db=margin_db))
    return m


def _store():
    return QoTResultStore()


def test_empty_plan_is_clean():
    report = validate_plan(_ip_over_optical(), Plan(ops=()), store=_store())
    assert report.ok
    assert report.violations == ()


def test_downshift_below_demand_flags_ip_overload():
    # 300G demand on a 400G link is fine; downshifting to 200G (cap 200) overloads.
    m = _ip_over_optical(demand=300.0)
    plan = Plan(ops=(SetModulationFormat(lightpath_id="lpAB", mode_id="200G"),))
    report = validate_plan(m, plan, store=_store())
    kinds = {v.type for v in report.violations}
    assert ViolationType.IP_LINK_OVERLOAD in kinds
    v = next(v for v in report.violations if v.type == ViolationType.IP_LINK_OVERLOAD)
    assert v.asset_id == "ipAB"
    assert not v.transient          # overload persists at the committed endpoint


def test_teardown_under_demand_flags_dropped_traffic():
    m = _ip_over_optical(demand=300.0)
    plan = Plan(ops=(TeardownLightpath(lightpath_id="lpAB"),))
    report = validate_plan(m, plan, store=_store(), dropped_tolerance_gbps=0.0)
    kinds = {v.type for v in report.violations}
    assert ViolationType.DROPPED_TRAFFIC in kinds


def test_dropped_traffic_within_tolerance_is_clean():
    m = _ip_over_optical(demand=300.0)
    plan = Plan(ops=(TeardownLightpath(lightpath_id="lpAB"),))
    report = validate_plan(m, plan, store=_store(), dropped_tolerance_gbps=500.0)
    assert ViolationType.DROPPED_TRAFFIC not in {v.type for v in report.violations}


def test_disjointness_collapse_when_working_and_protection_share_oms():
    # working and protection both ride omsAB -> not physically disjoint.
    m = _ip_over_optical(demand=10.0)
    m.add_lightpath(Lightpath(id="lpAB2", oms_sequence=("omsAB",), mode_id="400G",
                              center_freq_hz=193.5e12))
    m.add_ip_link(IPLink(id="ipAB2", a_router="rA", z_router="rB", lightpath_id="lpAB2"))
    m.set_qot_state("lpAB2", QoTState(gsnr_db=12.0, osnr_db=22.0, margin_db=2.0))
    svc = m.get_service("svc")
    from dataclasses import replace
    m._services["svc"] = replace(svc, protection_path=("ipAB2",))
    report = validate_plan(m, Plan(ops=()), store=_store(),
                           basis="physical", level="link")
    collapse = [v for v in report.violations
                if v.type == ViolationType.DISJOINTNESS_COLLAPSE]
    assert collapse and collapse[0].asset_id == "svc"
    assert "omsAB" in collapse[0].detail["shared_assets"]


def test_spectrum_clash_detail_distinguishes_retune_from_reroute():
    # Provision a second lightpath onto lpAB's exact (oms, slot). The clashed
    # state short-circuits QoT (no GNPy), so this is fast and deterministic.
    from multilayer_optical_mcp.model.plan import ProvisionLightpath
    m = _ip_over_optical(demand=10.0)               # lpAB at 193.4 THz (slot 20) on omsAB
    plan = Plan(ops=(ProvisionLightpath(
        lightpath=Lightpath(id="lpAB2", oms_sequence=("omsAB",), mode_id="400G",
                            center_freq_hz=193.4e12), ip_link=None),))
    report = validate_plan(m, plan, store=_store())
    clash = next(v for v in report.violations
                 if v.type == ViolationType.SPECTRUM_CLASH)
    assert clash.asset_id == "omsAB" and clash.detail["slot"] == 20
    assert set(clash.detail["lightpaths"]) == {"lpAB", "lpAB2"}
    cand = clash.detail["retune_candidates"]["lpAB2"]
    assert cand, "omsAB has 47 other free slots -> retune-able (not exhausted)"
    assert 20 not in cand, "the lightpath's own clash slot is excluded"


def test_mode_infeasible_detail_lists_feasible_downshift_modes():
    # Unit-test the helper directly with a seeded negative margin (validate_plan
    # would recompute and overwrite the seed; here we assert the detail contract).
    from multilayer_optical_mcp.model.validate import _mode_infeasible_findings
    m = _ip_over_optical()                          # modes 400G(req 10) + 200G(req 7)
    m.set_qot_state("lpAB", QoTState(gsnr_db=8.0, osnr_db=20.0, margin_db=-2.0))
    (t, asset, detail), = _mode_infeasible_findings(m)
    assert t == ViolationType.MODE_INFEASIBLE and asset == "lpAB"
    assert detail["deficit_db"] == 2.0              # 400G needs 10 dB, have 8 dB
    assert "200G" in detail["feasible_downshift_modes"]  # gsnr 8 >= 200G's 7 -> downshift recovers


def test_invalid_plan_surfaces_as_typed_violation_not_exception():
    # teardown of a lightpath that does not exist -> apply_op raises PlanError,
    # which validate_plan must return as a typed INVALID_PLAN, not propagate.
    m = _ip_over_optical()
    plan = Plan(ops=(TeardownLightpath(lightpath_id="ghost"),))
    report = validate_plan(m, plan, store=_store())
    assert not report.ok
    (v,) = report.violations
    assert v.type == ViolationType.INVALID_PLAN
    assert v.asset_id == "ghost"
    assert v.detail["op_index"] == 0
    assert "ghost" in v.detail["message"]


def test_duplicate_provision_is_invalid_plan():
    from multilayer_optical_mcp.model.plan import ProvisionLightpath
    m = _ip_over_optical()                          # already has lpAB
    plan = Plan(ops=(ProvisionLightpath(
        lightpath=Lightpath(id="lpAB", oms_sequence=("omsAB",), mode_id="400G",
                            center_freq_hz=193.6e12), ip_link=None),))
    report = validate_plan(m, plan, store=_store())
    (v,) = report.violations
    assert v.type == ViolationType.INVALID_PLAN
    assert "lpAB" in v.detail["message"]


def test_unrouted_service_surfaces_as_dropped_traffic():
    from multilayer_optical_mcp.model.assets import Service
    m = _ip_over_optical(demand=300.0)
    # a service with demand but NO working path: its demand must not vanish.
    m.add_service(Service(id="ghost_demand", src_router="rA", dst_router="rB",
                          demand_gbps=50.0, working_path=()))
    report = validate_plan(m, Plan(ops=()), store=_store(),
                           dropped_tolerance_gbps=0.0)
    dropped = [v for v in report.violations if v.type == ViolationType.DROPPED_TRAFFIC]
    assert any(v.asset_id == "ghost_demand"
               and v.detail["reason"] == "unrouted" for v in dropped)


def test_protection_path_down_is_not_viable_even_when_disjoint():
    # working on omsAB; protection on a DISJOINT omsCD whose lightpath is dark
    # (margin < 0 -> capacity 0). Disjointness passes; viability must fail.
    # Asserted via the helper directly: an empty plan would recompute QoT and
    # overwrite the seeded negative margin (same pattern as the mode test).
    from multilayer_optical_mcp.model.validate import (
        _protection_viability_findings, _disjointness_findings,
    )
    from dataclasses import replace
    m = _ip_over_optical(demand=100.0)
    m.add_amplifier(Amplifier(id="a3", type_variety="adv", gain_db=20.0, nf_db=5.5))
    m.add_fiber(Fiber(id="fCD", a_end="a3", z_end="a4", length_km=80.0, type_variety="SSMF"))
    m.add_oms(OMS(id="omsCD", src_node_id="A", dst_node_id="B", elements=("a3", "fCD")))
    m.add_lightpath(Lightpath(id="lpCD", oms_sequence=("omsCD",), mode_id="400G",
                              center_freq_hz=193.7e12))
    m.add_ip_link(IPLink(id="ipCD", a_router="rA", z_router="rB", lightpath_id="lpCD"))
    m.set_qot_state("lpCD", QoTState(gsnr_db=5.0, osnr_db=18.0, margin_db=-2.0))  # dark
    m._services["svc"] = replace(m.get_service("svc"), protection_path=("ipCD",))
    (t, asset, detail), = _protection_viability_findings(m)
    assert t == ViolationType.PROTECTION_NOT_VIABLE and asset == "svc"
    assert "ipCD" in detail["dead_links"]
    assert detail["protection_capacity_gbps"] == 0.0
    # disjointness does NOT fire: working omsAB vs protection omsCD are disjoint.
    assert _disjointness_findings(m, "physical", "link") == []


def test_protection_path_undersized_is_not_viable():
    # protection UP but at a capacity below the demand it would inherit on failover.
    from multilayer_optical_mcp.model.validate import _protection_viability_findings
    from dataclasses import replace
    m = _ip_over_optical(demand=300.0)
    m.add_amplifier(Amplifier(id="a3", type_variety="adv", gain_db=20.0, nf_db=5.5))
    m.add_fiber(Fiber(id="fCD", a_end="a3", z_end="a4", length_km=80.0, type_variety="SSMF"))
    m.add_oms(OMS(id="omsCD", src_node_id="A", dst_node_id="B", elements=("a3", "fCD")))
    m.add_lightpath(Lightpath(id="lpCD", oms_sequence=("omsCD",), mode_id="200G",
                              center_freq_hz=193.7e12))           # cap 200 < demand 300
    m.add_ip_link(IPLink(id="ipCD", a_router="rA", z_router="rB", lightpath_id="lpCD"))
    m.set_qot_state("lpCD", QoTState(gsnr_db=12.0, osnr_db=22.0, margin_db=2.0))  # up
    m._services["svc"] = replace(m.get_service("svc"), protection_path=("ipCD",))
    (t, asset, detail), = _protection_viability_findings(m)
    assert t == ViolationType.PROTECTION_NOT_VIABLE and asset == "svc"
    assert detail["dead_links"] == []
    assert detail["bottleneck_link"] == "ipCD"
    assert detail["protection_capacity_gbps"] == 200.0
    assert detail["demand_gbps"] == 300.0


def test_protection_oversubscribed_when_working_plus_reserved_exceeds_cap():
    # ipCD carries svc2's WORKING 300 AND is svc's RESERVED protection 300
    # -> 600 committed on a 400 link -> oversubscribed (1:1 admission failure).
    from multilayer_optical_mcp.model.assets import Service
    from multilayer_optical_mcp.model.validate import _protection_oversubscription_findings
    from dataclasses import replace
    m = _ip_over_optical(demand=300.0)              # svc on ipAB (working), cap 400
    m.add_amplifier(Amplifier(id="a3", type_variety="adv", gain_db=20.0, nf_db=5.5))
    m.add_fiber(Fiber(id="fCD", a_end="a3", z_end="a4", length_km=80.0, type_variety="SSMF"))
    m.add_oms(OMS(id="omsCD", src_node_id="A", dst_node_id="B", elements=("a3", "fCD")))
    m.add_lightpath(Lightpath(id="lpCD", oms_sequence=("omsCD",), mode_id="400G",
                              center_freq_hz=193.7e12))
    m.add_ip_link(IPLink(id="ipCD", a_router="rA", z_router="rB", lightpath_id="lpCD"))
    m.set_qot_state("lpCD", QoTState(gsnr_db=12.0, osnr_db=22.0, margin_db=2.0))
    m._services["svc"] = replace(m.get_service("svc"), protection_path=("ipCD",))
    m.add_service(Service(id="svc2", src_router="rA", dst_router="rB",
                          demand_gbps=300.0, working_path=("ipCD",)))
    (t, link, detail), = _protection_oversubscription_findings(m)
    assert t == ViolationType.PROTECTION_OVERSUBSCRIBED and link == "ipCD"
    assert detail["working_gbps"] == 300.0 and detail["reserved_gbps"] == 300.0
    assert detail["overflow_gbps"] == 200.0
    assert "svc" in detail["reserving_services"]
```

- [ ] **Step 2: Run to verify failure**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_validate.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `validate.py`**

```python
# src/multilayer_optical_mcp/model/validate.py
"""Replay a plan op-by-op on a clone and return a typed violation list, checked
at EVERY intermediate state. No physics here: the validator recomputes QoT via
the adapter and reads simulate_ip_routing / ip_link_capacity_gbps, which already
encode the margin-feasibility gate. Margin is never set.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

from ..gnpy_adapter.adapter import recompute_qot_under_loading
from .ip_routing import (
    simulate_ip_routing, offered_load_per_link, reserved_capacity_per_link,
)
from .network import NetworkModel
from .plan import Plan, PlanError, apply_op, service_oms_sequence
from .qot_results import QoTResultStore
from .solvers import check_disjointness
from .spectrum import SpectrumGrid, build_spectrum_state, free_slots_along
from .whatif import loading_from_model


class ViolationType(str, Enum):
    MODE_INFEASIBLE = "mode_infeasible"          # lightpath margin < 0 (QoT gate)
    SPECTRUM_CLASH = "spectrum_clash"            # two lightpaths on one (oms, slot)
    IP_LINK_OVERLOAD = "ip_link_overload"        # utilization > 1
    DROPPED_TRAFFIC = "dropped_traffic"          # service lost (incl. unrouted), above tol
    DISJOINTNESS_COLLAPSE = "disjointness_collapse"
    PROTECTION_NOT_VIABLE = "protection_not_viable"  # disjoint but unusable failover
    PROTECTION_OVERSUBSCRIBED = "protection_oversubscribed"  # reserved 1:1 capacity double-booked
    INVALID_PLAN = "invalid_plan"                # malformed / bad-reference / dup-id op


@dataclass(frozen=True)
class Violation:
    type: ViolationType
    state_index: int                 # which intermediate state (0 = after first op)
    asset_id: Optional[str]
    transient: bool                  # present here but not at the committed endpoint
    detail: dict


@dataclass(frozen=True)
class ValidationReport:
    violations: Tuple[Violation, ...]
    num_states: int

    @property
    def ok(self) -> bool:
        return not self.violations


# A finding before transient-tagging: (type, asset_id, detail).
_Finding = Tuple[ViolationType, Optional[str], dict]


_RETUNE_CANDIDATE_LIMIT = 8          # cap free-slot lists so detail stays compact


def _mode_infeasible_findings(model: NetworkModel) -> List[_Finding]:
    out: List[_Finding] = []
    for lp in model.list_lightpaths():
        try:
            st = model.get_qot_state(lp.id)
        except LookupError:
            continue
        if st.margin_db < 0:
            cur = model.modes.get(lp.mode_id)
            # Lower-rate modes the current GSNR WOULD satisfy. Non-empty => a
            # downshift recovers the link (capacity falls, but it stays up);
            # empty => GSNR is below every mode's threshold, so reroute/repair,
            # not a downshift, is the only fix.
            downshift = [m.id for m in sorted(model.modes.list(),
                                              key=lambda m: -m.bitrate_gbps)
                         if m.required_gsnr_db <= st.gsnr_db
                         and m.bitrate_gbps < cur.bitrate_gbps]
            out.append((ViolationType.MODE_INFEASIBLE, lp.id, {
                "margin_db": st.margin_db,
                "gsnr_db": st.gsnr_db,
                "required_gsnr_db": cur.required_gsnr_db,
                "deficit_db": cur.required_gsnr_db - st.gsnr_db,
                "feasible_downshift_modes": downshift,
            }))
    return out


def _spectrum_clash_findings(model: NetworkModel) -> List[_Finding]:
    grid = SpectrumGrid.default()
    state = build_spectrum_state(model, grid)
    occ: Dict[Tuple[str, int], List[str]] = {}
    for lp in model.list_lightpaths():
        try:
            slot = grid.slot_of(lp.center_freq_hz)
        except ValueError:
            continue                 # off-grid carrier is not a clash we model here
        for oms_id in lp.oms_sequence:
            occ.setdefault((oms_id, slot), []).append(lp.id)
    out: List[_Finding] = []
    for (oms_id, slot), lps in occ.items():
        if len(lps) > 1:
            # Per clashing lightpath, the slots free on EVERY OMS of its path.
            # A lightpath's own (clash) slot is occupied, so it is correctly
            # absent from its own candidates. Empty list => path exhausted ->
            # reroute; non-empty => retune to any listed slot on the same path.
            retune: Dict[str, List[int]] = {}
            for lp_id in sorted(lps):
                lp = model.get_lightpath(lp_id)
                free_mask = free_slots_along(state, lp.oms_sequence, grid)
                slots = [i for i in range(grid.num_slots) if free_mask & (1 << i)]
                retune[lp_id] = slots[:_RETUNE_CANDIDATE_LIMIT]
            out.append((ViolationType.SPECTRUM_CLASH, oms_id, {
                "slot": slot,
                "lightpaths": sorted(lps),
                "retune_candidates": retune,
            }))
    return out


def _ip_findings(model: NetworkModel, dropped_tolerance_gbps: float) -> List[_Finding]:
    res = simulate_ip_routing(model)
    out: List[_Finding] = []
    util_by_link = {u.ip_link_id: u for u in res.utilizations}
    for link_id in res.congested_links:
        u = util_by_link[link_id]
        out.append((ViolationType.IP_LINK_OVERLOAD, link_id, {
            "utilization": u.utilization,
            "offered_gbps": u.offered_gbps,
            "capacity_gbps": u.capacity_gbps,
            "overflow_gbps": u.offered_gbps - u.capacity_gbps,   # how much to offload
        }))
    # simulate_ip_routing is failover-aware: it already classifies every lost
    # service (link_down / link_removed / unrouted) and excludes those restored
    # onto protection. Just gate the aggregate lost demand on the tolerance.
    dropped = res.dropped_services
    total = sum(model.get_service(d.service_id).demand_gbps for d in dropped)
    if total > dropped_tolerance_gbps:
        for d in dropped:
            out.append((ViolationType.DROPPED_TRAFFIC, d.service_id,
                        {"reason": d.reason, "on_link": d.on_link,
                         "demand_gbps": model.get_service(d.service_id).demand_gbps}))
    return out


def _disjointness_findings(model: NetworkModel, basis: str, level: str) -> List[_Finding]:
    out: List[_Finding] = []
    for svc in model.list_services():
        if not svc.working_path or not svc.protection_path:
            continue
        a = service_oms_sequence(model, svc.working_path)
        b = service_oms_sequence(model, svc.protection_path)
        res = check_disjointness(model, a, b, basis, level)
        if not res.disjoint:
            out.append((ViolationType.DISJOINTNESS_COLLAPSE, svc.id,
                        {"basis": basis, "level": level,
                         "shared_assets": list(res.shared_assets),
                         "shared_groups": list(res.shared_groups)}))
    return out


def _protection_viability_findings(model: NetworkModel) -> List[_Finding]:
    """Endpoint check (never transient): a service's protection path must be
    USABLE on failover, not merely disjoint from working. Disjointness says the
    two won't fail together; viability says protection can actually carry the load
    when it's called. Fail a service if any protection link is dark (capacity 0 —
    margin-gated down, or removed) or the bottleneck capacity along the path is
    below the demand it would inherit."""
    out: List[_Finding] = []
    for svc in model.list_services():
        if not svc.protection_path:
            continue
        dead: List[str] = []
        caps: List[Tuple[str, float]] = []
        for ip_id in svc.protection_path:
            try:
                cap = model.ip_link_capacity_gbps(ip_id)
            except (KeyError, LookupError):
                dead.append(ip_id)               # protection link missing entirely
                continue
            if cap <= 0:
                dead.append(ip_id)               # present in topology but optically dark
            caps.append((ip_id, cap))
        if dead:
            prot_cap, bottleneck = 0.0, None
        elif caps:
            bottleneck, prot_cap = min(caps, key=lambda kc: kc[1])
        else:
            prot_cap, bottleneck = 0.0, None
        if dead or prot_cap < svc.demand_gbps:
            out.append((ViolationType.PROTECTION_NOT_VIABLE, svc.id, {
                "demand_gbps": svc.demand_gbps,
                "protection_capacity_gbps": prot_cap,
                "dead_links": dead,              # repair/re-light these
                "bottleneck_link": bottleneck,   # or re-route/upgrade this
            }))
    return out


def _protection_oversubscription_findings(model: NetworkModel) -> List[_Finding]:
    """1:1 admission (endpoint, never transient): a link must hold its working
    traffic PLUS the full protection reservation of every service protected over
    it (dedicated, summed across services). working_load + reserved > capacity
    means the reserved failover capacity is not actually guaranteed — the link is
    oversubscribed. This enforcement is what lets _protection_viability_findings
    keep its cheap nominal check (no contention is possible once this passes)."""
    working = offered_load_per_link(model)          # working-only nominal load
    reserved = reserved_capacity_per_link(model)
    out: List[_Finding] = []
    for link in model.list_ip_links():
        cap = model.ip_link_capacity_gbps(link.id)
        committed = working[link.id] + reserved[link.id]
        if cap > 0 and committed > cap:
            reservers = sorted(svc.id for svc in model.list_services()
                               if link.id in svc.protection_path)
            out.append((ViolationType.PROTECTION_OVERSUBSCRIBED, link.id, {
                "working_gbps": working[link.id],
                "reserved_gbps": reserved[link.id],
                "capacity_gbps": cap,
                "overflow_gbps": committed - cap,
                "reserving_services": reservers,
            }))
    return out


def _op_target(op) -> Optional[str]:
    """Best-effort asset id an op acts on, for INVALID_PLAN.asset_id."""
    if hasattr(op, "lightpath"):
        return op.lightpath.id
    for attr in ("lightpath_id", "service_id"):
        if hasattr(op, attr):
            return getattr(op, attr)
    return None


def validate_plan(
    model: NetworkModel,
    plan: Plan,
    *,
    store: QoTResultStore,
    basis: str = "physical",
    level: str = "link",
    dropped_tolerance_gbps: float = 0.0,
) -> ValidationReport:
    """Replay `plan` on a clone of `model`, recompute QoT after each op, and
    collect typed violations at every intermediate state. A finding present at a
    non-final state but absent at the final state is tagged transient (the
    make-before-break window). Ground truth (`model`) is never mutated.
    """
    work = model.clone()

    def state_findings(m: NetworkModel) -> List[_Finding]:
        # Spectrum first: a clashed state has two carriers on one frequency, so
        # QoT is undefined (you cannot light both). Report the clash + its
        # retune/reroute discriminator and skip the QoT-dependent checks rather
        # than drive GNPy with a degenerate overlapping-carrier loading.
        clashes = _spectrum_clash_findings(m)
        if clashes:
            return clashes
        recompute_if_possible(m, store)
        return (_mode_infeasible_findings(m)
                + _ip_findings(m, dropped_tolerance_gbps))

    if not plan.ops:
        # Validate the standing state (e.g. a fresh disjointness / protection audit).
        steady = state_findings(work)
        endpoint = (_disjointness_findings(work, basis, level)
                    + _protection_viability_findings(work)
                    + _protection_oversubscription_findings(work))
        violations = [Violation(t, 0, a, False, d) for (t, a, d) in steady + endpoint]
        return ValidationReport(violations=tuple(violations), num_states=1)

    # (state_index -> list of findings). State 0 is after the first op.
    per_state: List[List[_Finding]] = []
    for op in plan.ops:
        try:
            apply_op(work, op)
        except PlanError as exc:
            # A structurally invalid plan (bad reference, duplicate id, unknown
            # op) cannot be validated past the failed op. Surface it as a single
            # typed violation instead of raising — tool results are typed lists,
            # never exceptions (CLAUDE.md). state_index/op_index = ops applied so
            # far = the 0-based index of the op that failed.
            bad = Violation(ViolationType.INVALID_PLAN, len(per_state),
                            _op_target(op), False,
                            {"op_index": len(per_state), "message": str(exc)})
            return ValidationReport(violations=(bad,), num_states=len(per_state))
        per_state.append(state_findings(work))

    final_index = len(per_state) - 1
    final_keys = {(t, a) for (t, a, _) in per_state[final_index]}

    violations: List[Violation] = []
    for idx, findings in enumerate(per_state):
        for (t, a, d) in findings:
            transient = idx != final_index and (t, a) not in final_keys
            violations.append(Violation(t, idx, a, transient, d))

    # Endpoint properties of the committed plan (never transient): disjointness
    # collapse and protection-path viability.
    for (t, a, d) in (_disjointness_findings(work, basis, level)
                      + _protection_viability_findings(work)
                      + _protection_oversubscription_findings(work)):
        violations.append(Violation(t, final_index, a, False, d))

    return ValidationReport(violations=tuple(violations), num_states=len(per_state))


def recompute_if_possible(model: NetworkModel, store: QoTResultStore) -> None:
    """Recompute QoT for all lightpaths under the model's own loading. No-op when
    there are no lightpaths (nothing to propagate)."""
    if model.list_lightpaths():
        recompute_qot_under_loading(model=model, store=store,
                                    loading=loading_from_model(model))
```

> **Note on the steady-state tests above:** `_ip_over_optical` seeds `lpAB` QoT directly, but `validate_plan` recomputes via GNPy after each op. For the *downshift* and *teardown* tests the optical layer is synthesizable (single 80 km span with an advanced amp), so the recompute produces a real positive margin and the IP-overload / dropped-traffic findings are driven by capacity, not by the seeded margin. If the synthesized margin for the 400G/200G modes on this span is negative, raise the amp gain or shorten the span so the steady-state margin is comfortably positive — the point of these tests is the IP consequence, not a marginal QoT. The disjointness test uses an empty plan, so no recompute changes its seeded QoT.

- [ ] **Step 4: Run to verify pass**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_validate.py -v`
Expected: PASS (13 tests).

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/validate.py tests/model/test_validate.py
git commit -m "feat(validate): replay-and-check validate_plan with typed per-state violations"
```

---

## Task 5: The headline — every intermediate state, not just endpoints (transient)

**Files:**
- Create: `tests/model/test_validate_transient.py`

The make-before-break lesson: a plan whose *endpoints* are both clean can still be unsafe *between* them. Bad sequencing (teardown the old path before rerouting off it) drops a service transiently; reordering to make-before-break clears it. This is a deterministic, physics-free proof that `validate_plan` checks intermediate states.

- [ ] **Step 1: Write the tests**

```python
# tests/model/test_validate_transient.py
from dataclasses import replace
from multilayer_optical_mcp.model.assets import (
    FiberType, Amplifier, Fiber, OMS, Lightpath, IPLink, Service,
)
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.assets import TransceiverMode
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.plan import (
    Plan, RerouteService, TeardownLightpath,
)
from multilayer_optical_mcp.model.validate import validate_plan, ViolationType

MODES = ModeRegistry([TransceiverMode(
    id="400G", bitrate_gbps=400.0, required_gsnr_db=10.0,
    symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)])


def _two_path_model():
    """svc rA->rB rides ipOld (lpOld/omsAB). A parallel ipNew (lpNew/omsAB2) is
    standing by. Migration moves svc from ipOld to ipNew, then tears down lpOld."""
    m = NetworkModel(modes=MODES)
    m.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    m.add_amplifier(Amplifier(id="a1", type_variety="adv", gain_db=20.0, nf_db=5.5))
    m.add_amplifier(Amplifier(id="a2", type_variety="adv", gain_db=20.0, nf_db=5.5))
    m.add_fiber(Fiber(id="fAB", a_end="a1", z_end="a2", length_km=80.0, type_variety="SSMF"))
    m.add_fiber(Fiber(id="fAB2", a_end="a3", z_end="a4", length_km=80.0, type_variety="SSMF"))
    m.add_oms(OMS(id="omsAB", src_node_id="A", dst_node_id="B", elements=("a1", "fAB")))
    m.add_oms(OMS(id="omsAB2", src_node_id="A", dst_node_id="B", elements=("a2", "fAB2")))
    m.add_lightpath(Lightpath(id="lpOld", oms_sequence=("omsAB",), mode_id="400G",
                              center_freq_hz=193.4e12))
    m.add_lightpath(Lightpath(id="lpNew", oms_sequence=("omsAB2",), mode_id="400G",
                              center_freq_hz=193.4e12))
    m.add_ip_link(IPLink(id="ipOld", a_router="rA", z_router="rB", lightpath_id="lpOld"))
    m.add_ip_link(IPLink(id="ipNew", a_router="rA", z_router="rB", lightpath_id="lpNew"))
    m.add_service(Service(id="svc", src_router="rA", dst_router="rB",
                          demand_gbps=300.0, working_path=("ipOld",)))
    m.set_qot_state("lpOld", QoTState(gsnr_db=12.0, osnr_db=22.0, margin_db=2.0))
    m.set_qot_state("lpNew", QoTState(gsnr_db=12.0, osnr_db=22.0, margin_db=2.0))
    return m


def _store():
    return QoTResultStore()


def test_break_before_make_drops_service_transiently():
    # WRONG order: teardown lpOld first (svc still on ipOld -> dropped at state 0),
    # then reroute onto ipNew (clean at the final state).
    m = _two_path_model()
    plan = Plan(ops=(
        TeardownLightpath(lightpath_id="lpOld"),
        RerouteService(service_id="svc", ip_path=("ipNew",)),
    ))
    report = validate_plan(m, plan, store=_store())
    drops = [v for v in report.violations if v.type == ViolationType.DROPPED_TRAFFIC]
    assert drops, "intermediate-state drop must be caught"
    assert any(v.transient and v.state_index == 0 for v in drops), \
        "the drop exists mid-sequence and is gone at the endpoint -> transient"


def test_make_before_break_is_clean():
    # RIGHT order: reroute onto ipNew first, then teardown lpOld. No state drops.
    m = _two_path_model()
    plan = Plan(ops=(
        RerouteService(service_id="svc", ip_path=("ipNew",)),
        TeardownLightpath(lightpath_id="lpOld"),
    ))
    report = validate_plan(m, plan, store=_store())
    assert ViolationType.DROPPED_TRAFFIC not in {v.type for v in report.violations}
    assert report.ok


def test_endpoint_only_check_would_miss_the_transient():
    # Sanity: the final state of the WRONG-order plan is itself clean — proving the
    # violation is only visible because validate_plan checks intermediate states.
    m = _two_path_model()
    bad = Plan(ops=(
        TeardownLightpath(lightpath_id="lpOld"),
        RerouteService(service_id="svc", ip_path=("ipNew",)),
    ))
    report = validate_plan(m, bad, store=_store())
    final_index = report.num_states - 1
    final_drops = [v for v in report.violations
                   if v.type == ViolationType.DROPPED_TRAFFIC
                   and v.state_index == final_index]
    assert final_drops == []      # endpoint clean; only the transient state caught it
```

- [ ] **Step 2: Run**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_validate_transient.py -v`
Expected: PASS (3 tests). If the recompute makes `lpNew`/`lpOld` margin negative on the 80 km span, raise amp gain so steady margin is positive — the transient must come from sequencing, not from a marginal QoT.

- [ ] **Step 3: Commit**

```bash
git add tests/model/test_validate_transient.py
git commit -m "test(validate): intermediate-state checking catches break-before-make drop"
```

---

# Part D — gated single-op mutation tools + the validate_plan tool

## Task 6: View serializers

**Files:**
- Modify: `src/multilayer_optical_mcp/model/views.py`
- Test: covered via Task 7 / Task 9 server tests

- [ ] **Step 1: Append serializers** (match the existing `*_dict` style):

```python
# append to src/multilayer_optical_mcp/model/views.py
def validation_report_dict(report) -> dict:
    return {
        "ok": report.ok,
        "num_states": report.num_states,
        "violations": [
            {"type": v.type.value, "state_index": v.state_index,
             "asset_id": v.asset_id, "transient": v.transient, "detail": v.detail}
            for v in report.violations
        ],
    }


def commit_result_dict(result) -> dict:
    return {
        "status": result.status,
        "dry_run": result.dry_run,
        "applied_ops": result.applied_ops,
        "failed_ops": result.failed_ops,
        "intended_snapshot_id": result.intended_snapshot_id,
        "validation": validation_report_dict(result.validation)
        if result.validation is not None else None,
        "diff": result.diff,
    }


def drift_report_dict(report) -> dict:
    return {
        "in_sync": report.in_sync,
        "drift": [
            {"registry": d.registry, "kind": d.kind, "asset_id": d.asset_id}
            for d in report.drift
        ],
    }
```

- [ ] **Step 2: Commit**

```bash
git add src/multilayer_optical_mcp/model/views.py
git commit -m "feat(views): serializers for validation report, commit result, drift report"
```

---

## Task 7: Expose `validate_plan` + the gated single-op mutation tools

**Files:**
- Modify: `src/multilayer_optical_mcp/server.py`
- Test: `tests/test_server_phase7.py` (first half)

- [ ] **Step 1: Write the failing server tests** (mutation tools + validate)

```python
# tests/test_server_phase7.py
import pytest
from multilayer_optical_mcp.server import build_app
from multilayer_optical_mcp.model.assets import (
    FiberType, Amplifier, Fiber, OMS, Lightpath, IPLink, Service,
)
from multilayer_optical_mcp.model.qot import QoTState


def _call(app, name, **kwargs):
    return app._tool_manager._tools[name].fn(**kwargs)


def _seed(app):
    n = app._snapshots.current()
    n.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    n.add_amplifier(Amplifier(id="a1", type_variety="adv", gain_db=20.0, nf_db=5.5))
    n.add_fiber(Fiber(id="fAB", a_end="a1", z_end="a2", length_km=80.0, type_variety="SSMF"))
    n.add_oms(OMS(id="omsAB", src_node_id="A", dst_node_id="B", elements=("a1", "fAB")))
    return n


def test_provision_tool_adds_lightpath_and_binds_link():
    app = build_app()
    _seed(app)
    out = _call(app, "provision_lightpath",
                lightpath={"id": "lp1", "oms_sequence": ["omsAB"], "mode_id":
                           app._snapshots.current().modes.list()[0].id,
                           "center_freq_hz": 193.4e12},
                ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})
    assert out["lightpath_id"] == "lp1"
    assert out["ip_link_id"] == "ip1"
    assert "lp1" in app._snapshots.current()._lightpaths


def test_teardown_tool_removes_lightpath():
    app = build_app()
    n = _seed(app)
    mode = n.modes.list()[0].id
    _call(app, "provision_lightpath",
          lightpath={"id": "lp1", "oms_sequence": ["omsAB"], "mode_id": mode,
                     "center_freq_hz": 193.4e12},
          ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})
    out = _call(app, "teardown_lightpath", lightpath_id="lp1")
    assert out["torn_down"] == "lp1"
    assert "lp1" not in app._snapshots.current()._lightpaths


def test_set_modulation_format_tool_changes_mode_and_capacity():
    app = build_app()
    n = _seed(app)
    modes = n.modes.list()
    hi, lo = modes[0].id, modes[-1].id
    _call(app, "provision_lightpath",
          lightpath={"id": "lp1", "oms_sequence": ["omsAB"], "mode_id": hi,
                     "center_freq_hz": 193.4e12},
          ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})
    app._snapshots.current().set_qot_state(
        "lp1", QoTState(gsnr_db=20.0, osnr_db=30.0, margin_db=5.0))
    out = _call(app, "set_modulation_format", lightpath_id="lp1", mode_id=lo)
    assert out["mode_id"] == lo
    assert app._snapshots.current().get_lightpath("lp1").mode_id == lo


def test_validate_plan_tool_returns_typed_report():
    app = build_app()
    n = _seed(app)
    mode = n.modes.list()[0].id
    _call(app, "provision_lightpath",
          lightpath={"id": "lp1", "oms_sequence": ["omsAB"], "mode_id": mode,
                     "center_freq_hz": 193.4e12},
          ip_link={"id": "ip1", "a_router": "rA", "z_router": "rB"})
    out = _call(app, "validate_plan", plan={"ops": []})
    assert "violations" in out and "ok" in out and "num_states" in out
```

- [ ] **Step 2: Run to verify failure**

Run: `conda run -n multilayer-optical-mcp pytest tests/test_server_phase7.py -v`
Expected: FAIL (tools not registered).

- [ ] **Step 3: Register the tools** in `server.py` `build_app`. Add to the import block near the other model imports (after the `from .model.ip_routing import ...` line, ~182):

```python
    from .model.plan import plan_from_dict, ProvisionLightpath, TeardownLightpath, \
        SetModulationFormat, apply_op
    from .model.assets import Lightpath as _Lightpath, IPLink as _IPLink
    from .model.validate import validate_plan as _validate_plan
    from .model.views import (
        validation_report_dict, commit_result_dict, drift_report_dict,
    )
```

and add the tools before `return app`:

```python
    @app.tool()
    def validate_plan(
        plan: dict, basis: str = "physical", level: str = "link",
        dropped_tolerance_gbps: float = 0.0,
    ) -> dict:
        """Replay a plan op-by-op on a clone and return a typed violation list,
        checked at EVERY intermediate state (not just endpoints). Violations:
        mode_infeasible, spectrum_clash, ip_link_overload, dropped_traffic (incl.
        unrouted demand), disjointness_collapse, protection_not_viable,
        protection_oversubscribed (1:1 reserved-capacity double-booking), plus
        invalid_plan for a malformed / bad-reference / duplicate-id plan — each
        with state_index and a `transient` flag for the make-before-break window.
        Read-only: ground truth is never mutated."""
        report = _validate_plan(
            snapshots.current(), plan_from_dict(plan), store=results,
            basis=basis, level=level, dropped_tolerance_gbps=dropped_tolerance_gbps)
        return validation_report_dict(report)

    @app.tool()
    def provision_lightpath(lightpath: dict, ip_link: dict | None = None) -> dict:
        """Light a new lightpath; optionally bind+bring-up an IP link on it.
        Mutates the current model — branch first (snapshot_branch) to explore."""
        op = ProvisionLightpath(
            lightpath=_Lightpath(
                id=lightpath["id"], oms_sequence=tuple(lightpath["oms_sequence"]),
                mode_id=lightpath["mode_id"], center_freq_hz=lightpath["center_freq_hz"]),
            ip_link=None if ip_link is None else _IPLink(
                id=ip_link["id"], a_router=ip_link["a_router"],
                z_router=ip_link["z_router"], lightpath_id=lightpath["id"]))
        apply_op(snapshots.current(), op)
        return {"lightpath_id": op.lightpath.id,
                "ip_link_id": op.ip_link.id if op.ip_link else None}

    @app.tool()
    def teardown_lightpath(lightpath_id: str) -> dict:
        """Tear down a lightpath and bring down every IP link bound to it.
        Mutates the current model — branch first to explore."""
        apply_op(snapshots.current(), TeardownLightpath(lightpath_id=lightpath_id))
        return {"torn_down": lightpath_id}

    @app.tool()
    def set_modulation_format(lightpath_id: str, mode_id: str) -> dict:
        """Change a lightpath's transceiver mode; the bound IP link's capacity
        propagates automatically (capacity = f(mode), margin-gated). Mutates the
        current model — branch first to explore."""
        apply_op(snapshots.current(), SetModulationFormat(
            lightpath_id=lightpath_id, mode_id=mode_id))
        return {"lightpath_id": lightpath_id, "mode_id": mode_id}
```

> `reroute_service` is already a tool (`server.py:230`); the plan's `RerouteService` op shares its `set_service_working_path` path, so no second reroute tool is added.

- [ ] **Step 4: Run to verify pass**

Run: `conda run -n multilayer-optical-mcp pytest tests/test_server_phase7.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/server.py tests/test_server_phase7.py
git commit -m "feat(server): expose validate_plan + provision/teardown/set_modulation_format"
```

---

# Part E — commit + reconcile

## Task 8: `commit.py` — actuator, `commit_plan`, `reconcile`

**Files:**
- Create: `src/multilayer_optical_mcp/model/commit.py`
- Test: `tests/model/test_commit_reconcile.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/model/test_commit_reconcile.py
from multilayer_optical_mcp.model.assets import (
    FiberType, Amplifier, Fiber, OMS, Lightpath, IPLink, Service,
)
from multilayer_optical_mcp.model.qot import QoTState
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.assets import TransceiverMode
from multilayer_optical_mcp.model.network import NetworkModel
from multilayer_optical_mcp.model.snapshots import SnapshotStore
from multilayer_optical_mcp.model.plan import (
    Plan, ProvisionLightpath, TeardownLightpath,
)
from multilayer_optical_mcp.model.commit import (
    commit_plan, reconcile, full_actuator, drift_from_diff,
)

MODES = ModeRegistry([TransceiverMode(
    id="400G", bitrate_gbps=400.0, required_gsnr_db=10.0,
    symbol_rate_baud=87.5e9, channel_spacing_hz=100e9)])


def _base():
    m = NetworkModel(modes=MODES)
    m.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    m.add_amplifier(Amplifier(id="a1", type_variety="adv", gain_db=20.0, nf_db=5.5))
    m.add_amplifier(Amplifier(id="a2", type_variety="adv", gain_db=20.0, nf_db=5.5))
    m.add_fiber(Fiber(id="f1", a_end="a1", z_end="a2", length_km=80.0, type_variety="SSMF"))
    m.add_fiber(Fiber(id="f2", a_end="a3", z_end="a4", length_km=80.0, type_variety="SSMF"))
    m.add_oms(OMS(id="oms1", src_node_id="A", dst_node_id="B", elements=("a1", "f1")))
    m.add_oms(OMS(id="oms2", src_node_id="A", dst_node_id="B", elements=("a2", "f2")))
    return m


def _two_provisions():
    return Plan(ops=(
        ProvisionLightpath(
            lightpath=Lightpath(id="lpX", oms_sequence=("oms1",), mode_id="400G",
                                center_freq_hz=193.4e12),
            ip_link=IPLink(id="ipX", a_router="rA", z_router="rB", lightpath_id="lpX")),
        ProvisionLightpath(
            lightpath=Lightpath(id="lpY", oms_sequence=("oms2",), mode_id="400G",
                                center_freq_hz=193.4e12),
            ip_link=IPLink(id="ipY", a_router="rA", z_router="rB", lightpath_id="lpY")),
    ))


def test_dry_run_does_not_touch_ground_truth():
    store = SnapshotStore(_base())
    results = QoTResultStore()
    result = commit_plan(store, _two_provisions(), store_results=results, dry_run=True)
    assert result.dry_run is True
    assert "lpX" not in store.current()._lightpaths   # ground truth untouched
    assert result.diff["lightpaths"]["added"] == ("lpX", "lpY")  # simulated delta


def test_live_commit_with_violations_is_rejected():
    store = SnapshotStore(_base())
    results = QoTResultStore()
    # tear down a lightpath that does not exist -> a plan error surfaces as rejected
    bad = Plan(ops=(TeardownLightpath(lightpath_id="nope"),))
    result = commit_plan(store, bad, store_results=results, dry_run=False, confirm=True)
    assert result.status == "rejected"
    assert "nope" not in store.current()._lightpaths


def test_live_commit_requires_confirm():
    store = SnapshotStore(_base())
    results = QoTResultStore()
    result = commit_plan(store, _two_provisions(), store_results=results,
                         dry_run=False, confirm=False)
    assert result.status == "requires_approval"
    assert "lpX" not in store.current()._lightpaths


def test_partial_commit_then_reconcile_surfaces_drift():
    store = SnapshotStore(_base())
    results = QoTResultStore()

    def flaky_actuator(model, op):
        # the second provision (lpY) "times out" at the control plane
        if isinstance(op, ProvisionLightpath) and op.lightpath.id == "lpY":
            return False
        from multilayer_optical_mcp.model.plan import apply_op
        apply_op(model, op)
        return True

    result = commit_plan(store, _two_provisions(), store_results=results,
                         dry_run=False, confirm=True, actuator=flaky_actuator)
    assert result.status == "committed_with_failures"
    assert result.failed_ops == 1
    assert "lpX" in store.current()._lightpaths       # first op actuated
    assert "lpY" not in store.current()._lightpaths   # second failed

    drift = reconcile(store, result.intended_snapshot_id)
    assert not drift.in_sync
    # intended has lpY/ipY that reality lacks -> drift names them
    drifted = {(d.registry, d.asset_id) for d in drift.drift}
    assert ("lightpaths", "lpY") in drifted
    assert ("ip_links", "ipY") in drifted
```

- [ ] **Step 2: Run to verify failure**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_commit_reconcile.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `commit.py`**

```python
# src/multilayer_optical_mcp/model/commit.py
"""Commit + reconcile: the gated, control-plane-agnostic write path.

dry_run simulates on a clone and returns the diff. A live commit validates,
requires explicit confirm, then actuates op-by-op through an injectable Actuator
(default: apply-all-succeed). The intended end-state is recorded as a snapshot;
reconcile diffs actual-vs-intended into typed Drift so a partial control-plane
failure surfaces as structured data, never prose (CLAUDE.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from .network import NetworkModel
from .plan import Plan, apply_op
from .qot_results import QoTResultStore
from .snapshots import SnapshotStore, diff_models
from .validate import ValidationReport, validate_plan

# An actuator applies one op to the live model and reports success. Raising or
# returning False both count as a failed op (the op is not applied).
Actuator = Callable[[NetworkModel, object], bool]


def full_actuator(model: NetworkModel, op) -> bool:
    apply_op(model, op)
    return True


def actuate(model: NetworkModel, plan: Plan, actuator: Actuator) -> Tuple[int, int]:
    """Run each op through the actuator on the live model. Returns
    (applied_count, failed_count). A failed op leaves the model unchanged for
    that op (the actuator must not partially apply)."""
    applied = failed = 0
    for op in plan.ops:
        try:
            ok = actuator(model, op)
        except Exception:
            ok = False
        if ok:
            applied += 1
        else:
            failed += 1
    return applied, failed


@dataclass(frozen=True)
class CommitResult:
    status: str                          # see below
    dry_run: bool
    applied_ops: int
    failed_ops: int
    intended_snapshot_id: Optional[str]
    validation: Optional[ValidationReport]
    diff: Optional[dict]                 # simulated delta (dry_run) else None


@dataclass(frozen=True)
class Drift:
    registry: str
    kind: str                            # "added" | "removed" | "modified"
    asset_id: str


@dataclass(frozen=True)
class DriftReport:
    in_sync: bool
    drift: Tuple[Drift, ...]


def drift_from_diff(diff: dict) -> Tuple[Drift, ...]:
    """Flatten a registry diff into typed Drift entries. Orientation is
    diff_models(actual, intended): 'added' = in intended but missing from reality
    (un-actuated op); 'removed' = in reality but not intended; 'modified' = both
    have it but they differ."""
    out: List[Drift] = []
    for registry, delta in diff.items():
        for kind in ("added", "removed", "modified"):
            for asset_id in delta.get(kind, ()):
                out.append(Drift(registry=registry, kind=kind, asset_id=asset_id))
    return tuple(out)


def commit_plan(
    store: SnapshotStore,
    plan: Plan,
    *,
    store_results: QoTResultStore,
    dry_run: bool = True,
    confirm: bool = False,
    actuator: Actuator = full_actuator,
    basis: str = "physical",
    level: str = "link",
    dropped_tolerance_gbps: float = 0.0,
) -> CommitResult:
    """Simulate (dry_run) or actuate (live) a plan against store.current().

    Status values:
      - "dry_run"               : simulated; ground truth untouched; diff returned.
      - "rejected"              : live, but validation found violations (or a plan
                                  error) — nothing actuated.
      - "requires_approval"     : live and clean, but confirm was not set.
      - "committed"             : live, clean, confirmed, all ops actuated.
      - "committed_with_failures": live, confirmed, but the actuator failed some ops
                                  — reconcile() will surface the drift.
    """
    current = store.current()

    # Always validate on a clone first. A bad-reference/duplicate-id plan comes
    # back as an INVALID_PLAN violation (report.ok is False -> rejected below), not
    # as a raise; the try/except is a belt-and-suspenders for any unexpected error.
    try:
        report = validate_plan(current, plan, store=store_results, basis=basis,
                               level=level, dropped_tolerance_gbps=dropped_tolerance_gbps)
    except Exception as exc:  # unexpected -> typed rejection, never a thrown error
        return CommitResult(status="rejected", dry_run=dry_run, applied_ops=0,
                            failed_ops=0, intended_snapshot_id=None,
                            validation=None, diff={"error": str(exc)})

    if dry_run:
        work = current.clone()
        try:
            for op in plan.ops:
                apply_op(work, op)
        except Exception as exc:
            return CommitResult(status="rejected", dry_run=True, applied_ops=0,
                                failed_ops=0, intended_snapshot_id=None,
                                validation=report, diff={"error": str(exc)})
        return CommitResult(status="dry_run", dry_run=True, applied_ops=len(plan.ops),
                            failed_ops=0, intended_snapshot_id=None,
                            validation=report, diff=diff_models(current, work))

    # Live path.
    if not report.ok:
        return CommitResult(status="rejected", dry_run=False, applied_ops=0,
                            failed_ops=0, intended_snapshot_id=None,
                            validation=report, diff=None)
    if not confirm:
        return CommitResult(status="requires_approval", dry_run=False, applied_ops=0,
                            failed_ops=0, intended_snapshot_id=None,
                            validation=report, diff=None)

    # Record the intended end-state (all ops applied on a clone) before actuating,
    # so reconcile has a target even if the control plane partially fails.
    intended = current.clone()
    for op in plan.ops:
        apply_op(intended, op)
    intended_id = store.put(intended)

    applied, failed = actuate(current, plan, actuator)
    status = "committed" if failed == 0 else "committed_with_failures"
    return CommitResult(status=status, dry_run=False, applied_ops=applied,
                        failed_ops=failed, intended_snapshot_id=intended_id,
                        validation=report, diff=None)


def reconcile(store: SnapshotStore, intended_snapshot_id: str) -> DriftReport:
    """Diff store.current() (reality, after a live commit) against the intended
    end-state recorded at commit time. Empty diff => in_sync."""
    intended = store.get(intended_snapshot_id)
    diff = diff_models(store.current(), intended)
    drift = drift_from_diff(diff)
    return DriftReport(in_sync=not drift, drift=drift)
```

- [ ] **Step 4: Run to verify pass**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_commit_reconcile.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/commit.py tests/model/test_commit_reconcile.py
git commit -m "feat(commit): gated commit_plan + reconcile with injectable actuator + typed drift"
```

---

## Task 9: Expose `commit_plan` + `reconcile`; full regression

**Files:**
- Modify: `src/multilayer_optical_mcp/server.py`
- Test: `tests/test_server_phase7.py` (second half)

- [ ] **Step 1: Append the failing server tests**

```python
# append to tests/test_server_phase7.py
def test_commit_dry_run_tool_reports_diff_without_mutating():
    app = build_app()
    n = _seed(app)
    mode = n.modes.list()[0].id
    plan = {"ops": [
        {"op": "provision_lightpath",
         "lightpath": {"id": "lpX", "oms_sequence": ["omsAB"], "mode_id": mode,
                       "center_freq_hz": 193.4e12},
         "ip_link": {"id": "ipX", "a_router": "rA", "z_router": "rB"}}]}
    out = _call(app, "commit_plan", plan=plan, dry_run=True)
    assert out["status"] == "dry_run"
    assert out["diff"]["lightpaths"]["added"] == ["lpX"]
    assert "lpX" not in app._snapshots.current()._lightpaths


def test_commit_live_requires_confirm_then_reconcile_in_sync():
    app = build_app()
    n = _seed(app)
    mode = n.modes.list()[0].id
    plan = {"ops": [
        {"op": "provision_lightpath",
         "lightpath": {"id": "lpX", "oms_sequence": ["omsAB"], "mode_id": mode,
                       "center_freq_hz": 193.4e12},
         "ip_link": {"id": "ipX", "a_router": "rA", "z_router": "rB"}}]}
    pending = _call(app, "commit_plan", plan=plan, dry_run=False, confirm=False)
    assert pending["status"] == "requires_approval"

    done = _call(app, "commit_plan", plan=plan, dry_run=False, confirm=True)
    assert done["status"] == "committed"
    assert "lpX" in app._snapshots.current()._lightpaths

    drift = _call(app, "reconcile", intended_snapshot_id=done["intended_snapshot_id"])
    assert drift["in_sync"] is True
    assert drift["drift"] == []
```

- [ ] **Step 2: Run to verify failure**

Run: `conda run -n multilayer-optical-mcp pytest tests/test_server_phase7.py -k "commit or reconcile" -v`
Expected: FAIL (tools not registered).

- [ ] **Step 3: Register the tools.** Add the import (with the other Phase-7 imports added in Task 7):

```python
    from .model.commit import commit_plan as _commit_plan, reconcile as _reconcile
```

and add before `return app`:

```python
    @app.tool()
    def commit_plan(
        plan: dict, dry_run: bool = True, confirm: bool = False,
        basis: str = "physical", level: str = "link",
        dropped_tolerance_gbps: float = 0.0,
    ) -> dict:
        """dry_run=True simulates on a clone and returns the would-be diff without
        touching state. A live commit (dry_run=False) validates first, requires
        confirm=True, then actuates; status is 'rejected' (violations),
        'requires_approval' (unconfirmed), 'committed', or
        'committed_with_failures' (control-plane partial failure — call reconcile)."""
        result = _commit_plan(
            snapshots, plan_from_dict(plan), store_results=results,
            dry_run=dry_run, confirm=confirm, basis=basis, level=level,
            dropped_tolerance_gbps=dropped_tolerance_gbps)
        return commit_result_dict(result)

    @app.tool()
    def reconcile(intended_snapshot_id: str) -> dict:
        """After a live commit, diff actual network state against the intended
        end-state recorded at commit time. Returns typed drift[] (the ops the
        control plane failed to actuate); in_sync=True when reality matches."""
        return drift_report_dict(_reconcile(snapshots, intended_snapshot_id))
```

- [ ] **Step 4: Run to verify pass**

Run: `conda run -n multilayer-optical-mcp pytest tests/test_server_phase7.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Full regression sweep**

Run: `conda run -n multilayer-optical-mcp pytest -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/multilayer_optical_mcp/server.py tests/test_server_phase7.py
git commit -m "feat(server): expose commit_plan (dry-run/gated) + reconcile"
```

---

## Self-review checklist (run before handoff)

- [ ] **Spec coverage (CLAUDE.md "Validate & commit" group):**
  - `validate_plan` → typed `violations[]` with QoT/mode-infeasibility (`MODE_INFEASIBLE`), spectrum/optical-capacity (`SPECTRUM_CLASH`), IP-link overload (`IP_LINK_OVERLOAD`), dropped traffic above tolerance incl. unrouted demand (`DROPPED_TRAFFIC`, `reason="unrouted"`), disjointness collapse (`DISJOINTNESS_COLLAPSE`), protection-path viability (`PROTECTION_NOT_VIABLE`), 1:1 protection oversubscription (`PROTECTION_OVERSUBSCRIBED`), malformed/duplicate-id plans (`INVALID_PLAN`), transient make-before-break overload (`transient=True`). **Checks every intermediate state** (Task 4 loop; proven in Task 5). ✓
  - **Remediation-rich details (Decision 4):** each violation carries the locally-cheap discriminator that points the agent at the right fix — `SPECTRUM_CLASH.retune_candidates` (retune vs reroute), `MODE_INFEASIBLE.feasible_downshift_modes`/`deficit_db` (downshift vs reroute), `IP_LINK_OVERLOAD.overflow_gbps` (how much to offload), `DISJOINTNESS_COLLAPSE.shared_assets`/`shared_groups` (what to route around). The validator never computes the fix — it routes to the solver (Task 4 detail tests). ✓
  - **Failure-mode coverage (the four audited gaps):** a malformed / bad-reference / **duplicate-id** plan returns a typed `INVALID_PLAN` instead of raising (guard in `apply_op`, caught in replay — Task 3/4); an **unrouted** demand (empty `working_path`) is conserved into `DROPPED_TRAFFIC` rather than vanishing (now reported by the failover-aware `simulate_ip_routing`, Task 2); a **disjoint-but-unusable** protection path returns `PROTECTION_NOT_VIABLE` (Task 4 endpoint check, both dead-link and undersized flavours tested). ✓
  - **1:1 protection occupation (Decision 11):** protection capacity is reserved, never double-booked — `PROTECTION_OVERSUBSCRIBED` enforces `working + reserved ≤ capacity` per link (Task 4), which makes `PROTECTION_NOT_VIABLE`'s nominal check correct by construction. `simulate_ip_routing` is failover-aware: a working failure switches a service onto its reserved protection path and reports it in `restored_services` (Task 2) rather than dropping it. `offered_load_per_link` stays working-only; `active_load_per_link` carries the failover view. ✓
  - `provision_lightpath` / `teardown_lightpath` flip the bound IP link up/down (Task 2 removal + Task 7). ✓
  - `set_modulation_format` changes the mode; capacity propagates via the existing margin-gated derivation (Task 7). ✓
  - `reroute_service` reused from Phase 5 as the `RerouteService` op. ✓
  - `commit_plan(plan, dry_run)` simulates or live-commits behind validation + `confirm` gate (Task 8/9). ✓
  - `reconcile()` reads back actual vs intended and surfaces partial-failure drift as typed entries (Task 8/9). ✓
- [ ] **Margin is an output, never an input:** the validator never sets `margin_db`; it recomputes via `recompute_qot_under_loading` and reads capacity. Confirmed across `validate.py`.
- [ ] **Ground truth untouched by read paths:** `validate_plan` and `commit_plan(dry_run=True)` operate on `model.clone()`; tests assert the live model is unchanged (Task 4 `test_replay…`, Task 8 `test_dry_run…`). ✓
- [ ] **Every new field/registry carried by clone/diff:** Phase 7 adds **no** new model registries (it reuses existing ones); `clone()` mirrors the snapshot `_clone` exactly and `diff_models` lists every registry. Re-verify the two lists match after Task 1.
- [ ] **Make-before-break composes:** `loading_from_model` at the post-provision/pre-teardown state includes both channels (the step-2 arbitrary-loading contract), and per-state validation (step-7) consumes it — Task 5 proves the IP-level transient; the optical overlap is the same mechanism via recompute.
- [ ] **Solver/commit outcomes are typed, never exceptions:** `commit_plan` converts plan errors and validation failures into `status` strings (`rejected`/`requires_approval`/…), never a thrown error to the agent (Task 8 `test_live_commit_with_violations…`). ✓
- [ ] **Type consistency:** `Plan`/`PlanOp`/`ProvisionLightpath`/`TeardownLightpath`/`RerouteService`/`SetModulationFormat`/`apply_op`/`replay`/`plan_from_dict`/`service_oms_sequence`; `ViolationType`/`Violation`/`ValidationReport`/`validate_plan`; `Actuator`/`full_actuator`/`actuate`/`CommitResult`/`Drift`/`DriftReport`/`drift_from_diff`/`commit_plan`/`reconcile`; `validation_report_dict`/`commit_result_dict`/`drift_report_dict` — names identical across plan/validate/commit/views/server/tests.
- [ ] **Placeholder scan:** no `TBD`/`...`/"handle edge cases"; the only narrative notes are the two physics-tuning hints (Task 4 Step 3, Task 5 Step 2) telling the engineer to raise amp gain if a synthesized margin lands negative — resolve them against the real synthesized GSNR, do not weaken assertions.

---

## Scope note / optional split

Step 7 is two shippable milestones: **Part 1 (Tasks 1–7)** delivers `validate_plan` + the gated single-op mutation tools — complete and testable on its own. **Part 2 (Tasks 8–9)** adds `commit_plan` + `reconcile`. If you want to ship and review validation before the write path, execute Tasks 1–7, merge, then Tasks 8–9 as a follow-up. The plan is written so the boundary is clean (Part 2 imports nothing from Part 1 beyond the already-merged plan/validate modules).

## Out of scope (already settled elsewhere or explicitly excluded)

- **Live control-plane signalling** (GMPLS/NETCONF/SDN). `commit_plan` hands ops to an `Actuator`; modelling the protocol is out of scope (CLAUDE.md "Explicitly out of scope").
- **The transient/quasi-static gap.** The GN model cannot certify the switching instant; `validate_plan` checks quasi-static intermediate *states*, not the EDFA excursion during the switch. Document this where the tool is surfaced; do not claim transient-instant certification (CLAUDE.md Risks).
- **Physical-layer optimization** (power/tilt). Provision uses the lightpath's fixed operating point; no autodesign.
- **Event/geo/weather interpretation.** Plans arrive as asset-level ops; no hazard mapping.
