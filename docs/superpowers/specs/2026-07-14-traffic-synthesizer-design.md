# Design: seeded gravity traffic synthesizer

Design spec for follow-up item 1 in
`docs/superpowers/plans/2026-07-13-followups-and-next-steps.md` — the "un-written
sibling spec" for demand-matrix generation. Produces the `(src, dst, gbps)` demand
list that `solve_allocation` already consumes, yielding a pre-disaster loaded
steady state.

- **Consumer interface (already frozen, this branch does not change it):**
  `solve_allocation(model, qot, demands, spare_inventory, weights)` where
  `demands = [{id, src, dst, demand_gbps, protected?, constraints?}]`, `src`/`dst`
  are **optical node ids**. The synthesizer's whole job is to emit exactly that
  list. It has zero freedom over its output schema — the design is entirely about
  the *statistical model* (which pairs, how much), not the API shape.
- **Why here, not the downstream app:** disaster-agnostic. Capacity planning and
  defrag use the same synthesizer; its only interface is the demand list.

---

## 1. Placement & purity

New module `model/traffic.py` with one entry point `generate_traffic(...)`, exposed
as an MCP tool `generate_traffic` in `server.py`.

- **Non-mutating.** It runs `solve_allocation` / `simulate_ip_routing` on internal
  clones (as those already do) and returns *data*; it never touches ground truth.
  It belongs with the solvers, not the gated mutation tools.
- It produces the demand list that `solve_allocation` consumes — closing the loop
  the follow-ups doc calls "the missing piece."

## 2. Algorithm (deterministic: seed + params → identical output)

```
1. Build graph from model; mass(n) = node_mass override if given, else degree(n).
2. dist(u,v) = shortest-path length_km over edges (edges carry length_km).
3. Gravity weight  w(u,v) = mass(u)·mass(v) / dist(u,v)^alpha   for all ordered
   pairs u≠v with a path between them.
4. offered(u,v) = scale · w(u,v)/Σw, quantized into
   ceil(offered/unit_gbps) demands of unit_gbps each. A pair with w>0 gets ≥1 unit
   once scale is large enough to round up; pairs with no path get w=0 and are
   skipped.
5. Assign protected=true to the top `protected_fraction` of demands ranked by
   w(u,v) (hub-weighted). Deterministic tie-break on (src, dst, unit-index).
6. CONVERGENCE LOOP on the scalar `scale` (deterministic bracketed search, e.g.
   bisection):
     place  = solve_allocation(model, qot, demands(scale), spare_inventory)
     util   = simulate_ip_routing(placed state)
     measure mean_util (mean over active IP links) and max_util
     grow scale while mean_util < target_mean_util AND max_util ≤ max_util_cap
   Return the largest-scale demand set that still satisfies the cap.
```

The loop is a 1-D monotone-ish search on a single knob, **not** a per-demand
optimizer. Utilization rises with `scale` in discrete jumps (quantized units +
integer lightpath placement), so it is a bracketed search returning the best
feasible point, not a root-find assuming continuity. Matches the repo's
"heuristic, best-effort, typed result — never an exception" contract.

**Design decision (not asked, flagged for review):** the generator takes
`spare_inventory` (generous default) and runs the packer *inside* the loop, because
targeting utilization requires a placement to measure util against. It returns the
demand list as the contract plus a convergence report; the loaded state is
re-derivable deterministically by re-running the packer, so no heavy model snapshot
is returned.

## 3. Interface

```python
generate_traffic(
    seed: int,
    target_mean_util:   float = 0.6,
    max_util_cap:       float = 0.95,
    unit_gbps:          float = 100.0,
    protected_fraction: float = 0.3,
    alpha:              float = 1.0,                    # gravity distance exponent
    node_mass:          dict[node, float] | None = None,   # default: node degree
    spare_inventory:    dict[site, int]  | None = None,    # default: generous
    max_iters:          int   = 24,
) -> TrafficResult
```

Returns (typed, like the other solvers):

```
{ status: solution | partial | no_solution,
  demands: [{id, src, dst, demand_gbps, protected}],   # the contract output
  report: { achieved_mean_util, achieved_max_util, n_demands,
            total_offered_gbps, transponders_used, unplaced_count, scale,
            limit: none | max_util_cap | spare_inventory } }
```

- `solution` — hit `target_mean_util` within tolerance under the cap.
- `partial` — **cap-limited or inventory-limited**: converged *below* target because
  `max_util_cap` (or spare inventory) bound first. `report.limit` names which.
- `no_solution` — degenerate topology (e.g. fewer than 2 reachable nodes).

## 4. Determinism & edge cases

- Single `numpy.random.default_rng(seed)`, used only for tie-break jitter if any;
  the model is otherwise fully deterministic. Same `(model, seed, all params)` →
  byte-identical `demands`.
- Disconnected node pairs (no path) get `w=0` and are skipped.
- All shape knobs (`alpha`, `unit_gbps`, `node_mass`) surfaced so tests can pin
  behavior.
- Mean utilization is measured over **active IP links** (each bound to a lightpath).
- The convergence search is capped at `max_iters`; on exhaustion it returns the best
  feasible bracket point as `partial`/`solution`, never an exception.

## 5. Testing (no LLM, seedable — repo rule)

- **Determinism:** two calls, same seed/params → identical demand list.
- **Gravity shape:** a high-degree hub pair gets strictly more offered volume than a
  low-degree peripheral pair (order assertion, not absolute magnitude).
- **Quantization:** every `demand_gbps` is a multiple of `unit_gbps` and ≤ max line
  rate (800 Gbps from `modulation_formats.yaml`).
- **Protection:** exactly `round(protected_fraction · N)` demands protected, and they
  are the highest-gravity ones.
- **Convergence:** achieved mean util ≤ target and max util ≤ cap; a deliberately low
  cap forces `partial` with `limit = max_util_cap`.
- **Fixture demand set:** freeze one `generate_traffic(german_17, seed=0)` output as a
  fixture that existing `tests/model/test_allocation.py` can consume — replaces the
  hand-written demands the follow-ups doc flags.

## 6. Out of scope

- Time-varying / diurnal demand profiles (this emits one static steady state).
- Event/geo interpretation of any kind (repo hard rule — belongs downstream).
- Tuning the packer or QoT adapter; the synthesizer only *drives* them.
