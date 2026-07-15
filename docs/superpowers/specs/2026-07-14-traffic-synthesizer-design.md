# Design: seeded gravity traffic synthesizer / operating-network builder

Design spec for follow-up item 1 in
`docs/superpowers/plans/2026-07-13-followups-and-next-steps.md` — the "un-written
sibling spec" for demand-matrix generation.

## What this is (and is NOT)

This is **scenario setup / bootstrapping**, not a runtime MCP tool. The MCP server
operates on an *already-operating* network; its tools are for **reoptimization**
(what-if, restoration, routing under a disaster). This component runs **before** the
server is live and manufactures the pre-disaster operating steady state the server
then boots from:

```
bare topology
  → generate gravity demands            (pure, seeded)
  → solve_allocation places them        (existing packer)
  → return the loaded `work` model       (already provisioned via the real path)
  → final recompute_qot_under_loading    (settle QoT/capacity to ground truth)
  ─────────────────────────────────────────────────────────────────────
  = a LOADED operating NetworkModel: lightpaths lit, IP links bound, grooming
    populated, services carrying load — the ground-truth snapshot the server starts
    from, and a reusable seeded demo/test fixture.
```

- **Not `@app.tool()`.** It is an offline builder (a package function, optionally a
  thin CLI wrapper), outside the read/mutate tool contract entirely.
- It **creates** ground truth; there is no prior ground truth to protect, so the
  read-vs-mutate tool rules do not apply to it.
- **Primary artifact = the loaded operating `NetworkModel`.** The demand list and
  convergence report are provenance byproducts of the build.
- **Why here, not the downstream app:** disaster-agnostic. Capacity planning and
  defrag reuse the same builder; it knows nothing about events/geo (repo hard rule).

---

## 1. Components (two isolated units + one small enabling change)

Split so the pure statistical model is testable without any solver.

### A. `model/traffic.py` — pure demand generator
`generate_demands(model, seed, scale, alpha, unit_gbps, protected_fraction,
node_mass=None) -> list[demand]`. No solver, no QoT. Deterministic function of its
args. Emits the frozen `solve_allocation` schema:
`[{id, src, dst, demand_gbps, protected}]` (src/dst = optical node ids).

### B. `model/scenario.py` — the operating-network builder / convergence driver
`build_operating_network(model, seed, target_mean_util=0.6, max_util_cap=0.95,
unit_gbps=100.0, protected_fraction=0.3, alpha=1.0, node_mass=None,
spare_inventory=None, max_iters=24) -> ScenarioResult`. Runs the utilization
convergence loop (below), materializes the loaded model, and returns
`ScenarioResult(model, demands, report)`.

### C. `solve_allocation` — return the consumed clone (small enabling change)
`solve_allocation` already builds the fully-loaded model on its internal `work`
clone: every demand is provisioned via `_objective.apply_candidate` →
`_provision_and_seed_run` → the real `apply_op(ProvisionLightpath)` path
(`allocation.py:387,404`), then it **discards `work`** and returns only the
`AllocationResult`. Add an opt-in so the builder receives that already-materialized
model (e.g. a `return_model: bool = False` flag returning `(result, work)`, or a thin
`solve_allocation_model(...)` wrapper). No re-provisioning, no id/wavelength
reconstruction, zero divergence from the canonical path.

---

## 2. Gravity demand model (component A — deterministic)

```
1. mass(n) = node_mass[n] if override given, else degree(n).
2. dist(u,v) = shortest-path length_km over edges (edges carry length_km).
3. w(u,v) = mass(u)·mass(v) / dist(u,v)^alpha   for all ordered pairs u≠v with a
   path; pairs with no path get w=0 and are skipped.
4. offered(u,v) = scale · w(u,v)/Σw, quantized into ceil(offered/unit_gbps) demands
   of unit_gbps each. A pair with w>0 gets ≥1 unit once scale is large enough to
   round up.
5. protected=true for the top `protected_fraction` of demands ranked by w(u,v)
   (hub-weighted); deterministic tie-break on (src, dst, unit-index).
```

Sub-line-rate `unit_gbps` (default 100, vs 300–800 Gbps line rates in
`modulation_formats.yaml`) means multiple demands ride one lightpath — grooming is
exercised, which is the point under scarcity.

## 3. Utilization convergence + materialization (component B)

```
CONVERGENCE LOOP on the scalar `scale` (deterministic bracketed search, e.g.
bisection over [lo, hi]):
    demands      = generate_demands(model, seed, scale, ...)
    result, work = solve_allocation(model, qot, demands, spare_inventory,
                                    return_model=True)
    util         = simulate_ip_routing(work)
    mean_util    = mean over active IP links;  max_util = max over them
    grow scale while mean_util < target_mean_util AND max_util ≤ max_util_cap
Pick the largest-scale point still satisfying the cap; keep its (demands, work).

MATERIALIZE:
    recompute_qot_under_loading(work)   # settle QoT/IP-capacity to ground truth;
                                        # replaces apply_candidate's predicted-GSNR
                                        # seed (the item-2 gap) for the setup path.
    return ScenarioResult(model=work, demands=<chosen>, report=<below>)
```

The loop is a 1-D monotone-ish search on a single knob, **not** a per-demand
optimizer: utilization rises with `scale` in discrete jumps (quantized units +
integer lightpath placement), so it is a bracketed search returning the best
feasible point. Capped at `max_iters`; on exhaustion it returns the best feasible
bracket point. Never raises — matches the repo's typed, best-effort solver contract.

Mean utilization is measured over **active IP links** (each bound to a lightpath).
Running the packer inside the loop is required: targeting utilization is only
measurable against an actual placement. `spare_inventory` defaults generous so the
setup network is provisioned to carry the intended load (scarce inventory is a
runtime/disaster concern, not a setup concern).

## 4. Output: `ScenarioResult`

```
ScenarioResult:
  model:   NetworkModel          # THE artifact — loaded operating steady state
  demands: [{id, src, dst, demand_gbps, protected}]     # provenance
  report:  { status: solution | partial | no_solution,
             achieved_mean_util, achieved_max_util, n_demands, total_offered_gbps,
             transponders_used, unplaced_count, scale,
             limit: none | max_util_cap | spare_inventory }
```

- `solution` — hit `target_mean_util` within tolerance under the cap.
- `partial` — converged *below* target because `max_util_cap` (or spare inventory)
  bound first; `report.limit` names which.
- `no_solution` — degenerate topology (<2 reachable nodes) or nothing placeable.

The `model` is snapshot-serializable, so a CLI wrapper (optional) can persist one
seeded build as the demo/fixture starting state the server loads.

## 5. Determinism & edge cases

- `numpy.random.default_rng(seed)` (only for tie-break jitter if any); otherwise
  fully deterministic. Same `(model, seed, all params)` → byte-identical `demands`
  and identical loaded `model`.
- Disconnected pairs skipped (`w=0`). All shape knobs (`alpha`, `unit_gbps`,
  `node_mass`) surfaced so tests can pin behavior.

## 6. Testing (no LLM, seedable — repo rule)

Component A (pure, no solver):
- **Determinism:** same seed/params → identical demand list.
- **Gravity shape:** a high-degree hub pair gets strictly more offered volume than a
  low-degree peripheral pair (order assertion).
- **Quantization:** every `demand_gbps` is a multiple of `unit_gbps` and ≤ 800.
- **Protection:** exactly `round(protected_fraction·N)` protected, the highest-gravity
  ones.

Component B + C (with solvers):
- **Return-clone parity:** `solve_allocation(return_model=True)`'s model has one
  lightpath/IP link per placement's new runs and matches the `AllocationResult`.
- **Convergence:** achieved mean util ≤ target and max util ≤ cap; a deliberately low
  cap forces `partial` with `limit = max_util_cap`.
- **Materialized baseline:** the returned `model` is a valid operating state —
  `simulate_ip_routing` runs with no drops on the built state, and every IP link has a
  known (margin-gated) capacity after the final recompute.
- **Fixture:** freeze one `build_operating_network(german_17, seed=0)` output as the
  seeded starting state existing `tests/model/test_allocation.py` (and the demo) can
  load, replacing hand-written demands.

## 7. Out of scope

- Time-varying / diurnal demand profiles (emits one static steady state).
- Event/geo interpretation (repo hard rule — belongs downstream).
- Any MCP tool surface; this is offline setup, not a runtime tool.
- Tuning the packer or QoT adapter; the builder only *drives* them.
