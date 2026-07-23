# Commit QoT Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `commit_plan`'s live path recompute QoT on the live model after actuation, so a freshly-committed lightpath reports derived IP-link capacity instead of reading dark, and add a regression + convergence guard test.

**Architecture:** The live commit path (`commit.py`) actuates ops onto `store.current()` via `apply_op` but never recomputes QoT — unlike scoring (`objective.py` seeds predicted GSNR) and `build_operating_network` (`scenario.py` runs a `settle` recompute). We add one post-actuation recompute call on the live model, reusing the existing `validate.recompute_if_possible` helper, exposed as an injectable seam mirroring the existing `actuator` parameter and `scenario.settle`. Best-effort: a recompute failure must not un-report a successful actuation.

**Tech Stack:** Python 3, pytest, GNPy 2.14.0 (synthesized adapter path), existing in-memory `NetworkModel`.

## Global Constraints

- Run all pytest/python via `conda run -n multilayer-optical-mcp` (project conda env).
- Solver/commit outcomes are typed, never exceptions — a live commit must always return a `CommitResult`, never raise (CLAUDE.md).
- Read vs. mutate strictly separated; ground truth is only mutated on the confirmed live path, never in dry-run or validation (CLAUDE.md).
- Capacity is derived from mode and gated by margin ≥ 0; QoT margin is an output, never set as an input (CLAUDE.md).
- QoT recompute uses the real GNPy adapter via `recompute_qot_under_loading`; tests that must stay QoT-free inject a no-op seam.

---

### Task 1: Post-actuation recompute on the live commit path (the fix)

**Files:**
- Modify: `src/multilayer_optical_mcp/model/commit.py` (imports, `commit_plan` signature + live path ~lines 84-154)
- Test: `tests/model/test_commit_qot_convergence.py` (Create)

**Interfaces:**
- Consumes:
  - `validate.recompute_if_possible(model: NetworkModel, store: QoTResultStore) -> None` — already defined in `validate.py:246`; recomputes QoT for all lightpaths under the model's own loading, no-ops when the model has no lightpaths.
  - `commit_plan(store: SnapshotStore, plan: Plan, *, store_results: QoTResultStore, dry_run=True, confirm=False, actuator=full_actuator, basis="physical", level="link", dropped_tolerance_gbps=0.0) -> CommitResult` — existing.
  - `tests.phase7_topology.new_model()`, `add_bidir_span(m, src, dst, oms_id) -> str` — synthesizable bidirectional span builder.
  - `NetworkModel.get_qot_state(lp_id) -> QoTState` (raises `LookupError` if unseeded); `NetworkModel.ip_link_capacity_gbps(link_id) -> float` (raises `LookupError` if no QoT).
- Produces:
  - `commit_plan(..., recompute: Callable[[NetworkModel, QoTResultStore], None] = recompute_if_possible)` — new keyword-only param; later tasks pass `recompute=lambda m, s: None` to isolate.

- [ ] **Step 1: Write the failing regression test**

Create `tests/model/test_commit_qot_convergence.py`:

```python
from multilayer_optical_mcp.model.assets import Lightpath, IPLink
from multilayer_optical_mcp.model.qot_results import QoTResultStore
from multilayer_optical_mcp.model.snapshots import SnapshotStore
from multilayer_optical_mcp.model.plan import Plan, ProvisionLightpath
from multilayer_optical_mcp.model.commit import commit_plan
from tests.phase7_topology import new_model, add_bidir_span


def _base():
    """One synthesizable A<->B span (oms1 + paired reverse oms1_rev)."""
    m = new_model()
    add_bidir_span(m, "A", "B", "oms1")
    return m


def _provision_one():
    return Plan(ops=(
        ProvisionLightpath(
            lightpath=Lightpath(id="lpX", oms_sequence=("oms1",), mode_id="400G",
                                center_freq_hz=193.4e12),
            ip_link=IPLink(id="ipX", a_router="rA", z_router="rB",
                           lightpath_id="lpX")),
    ))


def test_live_commit_seeds_qot_so_link_is_not_dark():
    """The §2 regression: after a confirmed live commit, the freshly-provisioned
    lightpath must have recorded QoT and its IP link must report derived (>0)
    capacity — not read dark via the LookupError path."""
    store = SnapshotStore(_base())
    results = QoTResultStore()
    result = commit_plan(store, _provision_one(), store_results=results,
                         dry_run=False, confirm=True)
    assert result.status == "committed"
    live = store.current()
    # Before the fix, both of these raise LookupError (no QoT seeded on commit).
    st = live.get_qot_state("lpX")
    assert st.margin_db >= 0                      # 400G over one 80km span is feasible
    assert live.ip_link_capacity_gbps("ipX") > 0  # capacity derived, link is lit
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_commit_qot_convergence.py::test_live_commit_seeds_qot_so_link_is_not_dark -v`
Expected: FAIL with `LookupError: no QoT state recorded for lightpath 'lpX'` (raised inside `get_qot_state`), proving the live path never recomputes.

- [ ] **Step 3: Add the `recompute_if_possible` import**

In `src/multilayer_optical_mcp/model/commit.py`, extend the existing validate import (currently `from .validate import ValidationReport, validate_plan`):

```python
from .validate import ValidationReport, recompute_if_possible, validate_plan
```

- [ ] **Step 4: Add the injectable `recompute` seam to the signature**

In `commit_plan`'s signature (`commit.py:84-95`), add a keyword-only param after `actuator`:

```python
def commit_plan(
    store: SnapshotStore,
    plan: Plan,
    *,
    store_results: QoTResultStore,
    dry_run: bool = True,
    confirm: bool = False,
    actuator: Actuator = full_actuator,
    recompute: Callable[[NetworkModel, QoTResultStore], None] = recompute_if_possible,
    basis: str = "physical",
    level: str = "link",
    dropped_tolerance_gbps: float = 0.0,
) -> CommitResult:
```

- [ ] **Step 5: Call recompute after actuation on the live path**

Replace the live-path tail (`commit.py:150-154`, from `applied, failed = actuate(...)` through the final `return`) with:

```python
    applied, failed = actuate(current, plan, actuator)

    # Post-actuation recompute: seed QoT for freshly-lit lightpaths on the LIVE
    # model, so a just-committed lightpath reports derived capacity instead of
    # reading dark via ip_link_capacity_gbps's LookupError path. This is the live
    # twin of the recompute validate_plan runs on its discarded clone, and of
    # scenario.settle. Best-effort: a recompute failure must not un-report a
    # successful actuation (live state is already mutated and the intended
    # snapshot recorded), so swallow it and leave QoT unseeded — today's behavior
    # — rather than raise out of a committed plan (CLAUDE.md: typed, never raises).
    try:
        recompute(current, store_results)
    except Exception:
        pass

    status = "committed" if failed == 0 else "committed_with_failures"
    return CommitResult(status=status, dry_run=False, applied_ops=applied,
                        failed_ops=failed, intended_snapshot_id=intended_id,
                        validation=report, diff=None)
```

- [ ] **Step 6: Run the regression test to verify it passes**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_commit_qot_convergence.py::test_live_commit_seeds_qot_so_link_is_not_dark -v`
Expected: PASS.

- [ ] **Step 7: Run the existing commit/reconcile suite to confirm no regression**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_commit_reconcile.py -v`
Expected: PASS (all 4 existing tests still green — the flaky-actuator partial-commit test still yields `committed_with_failures` with drift; recompute is best-effort and does not alter actuation counts).

- [ ] **Step 8: Commit**

```bash
git add src/multilayer_optical_mcp/model/commit.py tests/model/test_commit_qot_convergence.py
git commit -m "fix(commit): recompute QoT on live model post-actuation

commit_plan's live path actuated ops onto store.current() but never
recomputed QoT, so a freshly-provisioned lightpath read dark (LookupError
in ip_link_capacity_gbps) until some later recompute fired — while scoring
seeds predicted GSNR immediately. Add a best-effort post-actuation recompute
(reusing validate.recompute_if_possible) as an injectable seam mirroring the
actuator param and scenario.settle."
```

---

### Task 2: Convergence + injectable-seam guard tests

**Files:**
- Modify: `tests/model/test_commit_qot_convergence.py` (add two tests)

**Interfaces:**
- Consumes: everything from Task 1, plus `NetworkModel.clone()`, `plan.apply_op(model, op)`, `objective.evaluate_objective(model) -> ObjectiveResult` (fields include `total_margin: float`).

- [ ] **Step 1: Write the convergence test (reality == an independent settle recompute)**

Rationale in a comment: scoring seeds *predicted* GSNR, but the post-commit recompute produces the *real* interferer-comb GSNR (the thing `settle` reconciles), so we do NOT assert bit-identity with a predicted seed. We assert the committed live model's QoT equals an independent `recompute_if_possible` of the same end-state — i.e. reality is settled, not dark — and that `evaluate_objective` sees that margin.

Append to `tests/model/test_commit_qot_convergence.py`:

```python
from multilayer_optical_mcp.model.plan import apply_op
from multilayer_optical_mcp.model.validate import recompute_if_possible
from multilayer_optical_mcp.model.objective import evaluate_objective


def test_committed_qot_matches_independent_recompute():
    """Post-commit QoT on the live model equals an independent recompute of the
    same end-state, and evaluate_objective counts that margin — the settled
    reality the scoring path predicts (within the predicted<->real comb gap)."""
    store = SnapshotStore(_base())
    results = QoTResultStore()
    plan = _provision_one()

    # Independent oracle: apply the same op to a clone of the pre-commit state and
    # recompute directly. Same computation the live commit should now perform.
    oracle = SnapshotStore(_base()).current().clone()
    for op in plan.ops:
        apply_op(oracle, op)
    recompute_if_possible(oracle, QoTResultStore())

    commit_plan(store, plan, store_results=results, dry_run=False, confirm=True)
    live = store.current()

    assert live.get_qot_state("lpX").margin_db == \
        oracle.get_qot_state("lpX").margin_db
    obj = evaluate_objective(live)
    assert obj.total_margin == live.get_qot_state("lpX").margin_db
```

- [ ] **Step 2: Write the injectable-seam test (no-op recompute leaves it dark)**

```python
def test_recompute_seam_can_be_disabled():
    """The recompute is an injectable seam: a no-op recompute reproduces the
    pre-fix behavior (link dark), proving the seam is honored and lets QoT-free
    tests opt out of driving GNPy."""
    import pytest
    store = SnapshotStore(_base())
    results = QoTResultStore()
    result = commit_plan(store, _provision_one(), store_results=results,
                         dry_run=False, confirm=True,
                         recompute=lambda m, s: None)
    assert result.status == "committed"
    with pytest.raises(LookupError):
        store.current().get_qot_state("lpX")
```

- [ ] **Step 3: Run both new tests to verify they pass**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_commit_qot_convergence.py -v`
Expected: PASS (all three tests: regression, convergence, seam).

- [ ] **Step 4: Run the full model suite to confirm no wider regression**

Run: `conda run -n multilayer-optical-mcp pytest tests/model -q`
Expected: PASS (no failures introduced; existing behavior unchanged elsewhere).

- [ ] **Step 5: Commit**

```bash
git add tests/model/test_commit_qot_convergence.py
git commit -m "test(commit): guard live-commit QoT convergence and recompute seam

Assert a committed lightpath's QoT equals an independent settle recompute
(not a predicted seed — the predicted<->real comb gap is expected), that
evaluate_objective counts that margin, and that the recompute seam can be
disabled for QoT-free tests."
```

---

## Self-Review

**Spec coverage** (against the three approved design sections):
- Section 1 (fix in `commit_plan`): Task 1 — post-actuation `recompute` call on `current`, reusing `recompute_if_possible`, injectable seam defaulting to real, best-effort try/except, runs on the live path only (dry-run/rejected/requires-approval untouched). ✓ Runs on `committed_with_failures` too: the call sits before the `status = ...` line, unconditional on the live confirmed path. ✓
- Section 2 (assert "no longer dark" strongly, numeric only within tolerance): Task 1 regression asserts capacity > 0 and QoT present; Task 2 convergence asserts equality against an *independent recompute* (the honest invariant) rather than a predicted seed, avoiding the predicted↔real brittleness the design flagged. ✓
- Section 3 (new file, synthesizable phase7 topology, additive + ~5-line hook): Task 1/2 create `tests/model/test_commit_qot_convergence.py` on `add_bidir_span`; no changes to `validate_plan`/`reconcile`/scoring. ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases" — every code step shows full code and exact commands.

**Type consistency:** `recompute: Callable[[NetworkModel, QoTResultStore], None]` matches `recompute_if_possible(model, store)`; `get_qot_state` → `QoTState.margin_db`; `evaluate_objective` → `ObjectiveResult.total_margin`; `add_bidir_span(m, "A", "B", "oms1")` yields forward OMS `oms1` referenced by the provision op. Consistent across tasks.

**Note for the implementer:** `NetworkModel`, `Callable` are already imported at the top of `commit.py` (`from .network import NetworkModel`; `from typing import Callable, ...`) — no new typing import needed beyond `recompute_if_possible`.
