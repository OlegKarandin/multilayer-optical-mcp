# Design: `route_service` + `evaluate_objective`

Design spec from a brainstorming session (2026-07-13). Follows the handoff in
`docs/service-level-routing-findings.md`, which identified the substrate gap
(flat vs. layered graph), the missing `evaluate_objective`, and proposed a
service-level unification. All open forks from that doc's §6 are resolved here.

Next step after user approval: `writing-plans` to produce the implementation plan.

---

## Goal

Unify first-time routing and restoration at the **service level** on the
**multilayer (layered IP+optical) graph**, add disjoint-pair routing to that
graph, and implement `evaluate_objective` as the real cost vector that ranks
candidates — retiring the proxy `cost_facets`.

Two new tools/behaviours:

1. **`route_service`** — one service-level routing/restoration primitive with a
   protected (disjoint working+protection) mode, over the shared `place_demands`
   engine.
2. **`evaluate_objective`** — the 7-term cost vector + weighted scalar that ranks
   `route_service` candidates and scores whole states.

Plus a **rebase of `solve_allocation`** onto the same shared engine (it is
greenfield-only on the flat graph today; the contract says it should route on the
layered graph and groom onto survivors).

---

## Decisions locked (the resolved forks)

| Fork | Decision |
|---|---|
| Scope | `route_service` **and** `evaluate_objective` together (they are coupled: the menu needs a ranker). |
| Disjoint-pair method | **A: enumerate-K + pairwise-filter.** Harvest `k` multilayer candidates, flatten each to physical footprint, pairwise-scan for disjoint pairs; `best_effort` → min-overlap. Reuses `solvers.compute_disjoint_paths` logic; gives degraded-restoration for free. |
| Orchestration | **Rebase `solve_allocation` now.** Two thin orchestrators over one engine: `route_service` (menu, no-consume) and `solve_allocation` (packer, consumes). |
| Footprint granularity | **Physical-only.** Disjointness = shared fibers/SRLGs/risk-groups via `path_basis_keys`. Matches the "SRLG-disjoint but both aerial" thesis and the existing λ-free candidate dedup. λ stays a provisioning concern. |
| Ranking model | **State-level scorer + apply-to-branch ranking.** `evaluate_objective(state, weights)` scores a materialized state; candidates are ranked by cloning, applying, and scoring. Proxy `cost_facets` retired. |
| Candidate materialization | **Clone + reuse real `apply_op`/commit machinery.** A candidate becomes a small `Plan`; `apply_op` onto `current.clone()`; then score. One "apply a change" codepath, so scoring and real commit cannot drift. |
| Protected output | **Menu of disjoint pairs.** Return top-N disjoint pairs ranked by combined `cost_vector`; if none fully disjoint, top-N min-overlap pairs as `PARTIAL`. Consistent with the unprotected menu. |
| `compute_restoration` | **Kept as a thin wrapper** delegating to `route_service(avoid=…, protected=False)`, so the existing tool name/tests survive. |

### Why the "expensive scoring" concern dissolved

`simulate_ip_routing` (`ip_routing.py:201`) is a **linear read** — it routes
nothing, only pins already-declared paths and sums load. The one genuinely
expensive thing (GNPy QoT propagation) is **already paid during candidate
generation**: every `NewLightpathRun` carries `gsnr_db`/`mode_id`/`bitrate_gbps`
(`multilayer_graph.py:399`). So scoring every candidate with the full vector is
cheap; no two-tier "cheap facets vs. real vector" compromise is needed.

---

## Architecture

```
                    place_demands (multilayer_graph.py)   ← unchanged engine
                              │  emits Placement(reused_lightpaths, new_runs)
        ┌─────────────────────┼──────────────────────────┐
        │                     │                          │
  route_service         solve_allocation            compute_restoration
  (menu, no-consume)     (packer, consumes)          (thin wrapper → route_service)
        │                     │
        └──────── disjoint_pair (multilayer_disjoint.py) ─┘   ← NEW shared primitive
                  flatten Placement → footprint keys → pairwise-scan + best_effort
                              │
                    evaluate_objective (objective.py)    ← NEW scorer
                    clone + apply_op + simulate_ip_routing → 7-term vector + scalar
```

Two new modules; two thin orchestrators over the existing `place_demands`; the
`apply_op` machinery reused verbatim for scoring.

---

## Component 1 — `model/multilayer_disjoint.py` (NEW)

The one adapter the simplification hinges on: flatten a multilayer `Placement` to
its **total physical OMS set**, then reuse the existing keying.

```python
def placement_footprint_keys(model, placement, *, basis, level) -> frozenset[str]:
    oms_seq: list[str] = []
    for lp_id in placement.reused_lightpaths:          # reused LP → its oms_sequence
        oms_seq += list(model.get_lightpath(lp_id).oms_sequence)
    for run in placement.new_lightpaths:               # new run → its oms_sequence
        oms_seq += list(run.oms_sequence)
    return path_basis_keys(model, tuple(oms_seq), basis=basis, level=level)
```

```python
def disjoint_pairs(model, candidates, *, basis, level, best_effort, top_n):
    # Key each candidate via placement_footprint_keys.
    # O(k²) scan: collect fully-disjoint pairs (shared == ∅);
    # if none and best_effort, collect pairs ranked by len(shared) (min-overlap).
    # Return up to top_n pairs, each with split_shared_keys(shared) for reporting.
```

- **Correctness trap handled here:** reused lightpaths are flattened to
  `oms_sequence`, never treated as opaque ids. Two "different" lightpaths sharing
  a fiber correctly read as correlated — the demo thesis one layer down.
- Endpoint exclusions come free: `path_basis_keys` already strips each path's own
  endpoint ROADMs/nodes (the fix recorded in `disjointness-endpoint-roadm-latent`).
- The pairwise-scan + `best_effort` min-overlap logic is lifted from
  `solvers.compute_disjoint_paths` (`solvers.py:311-333`), operating on
  `Placement`s instead of `OmsPath`s. Same min-overlap semantics (S6-8: minimizes
  count of shared namespaced keys, not physical severity — documented, not weighted).

---

## Component 2 — `model/objective.py` (NEW)

`evaluate_objective(model, weights)` scores a **materialized state**. Candidate
scoring goes through `score_candidate` / `score_pair`, which build a `Plan`, apply
it on a clone, and call `evaluate_objective` on the clone.

```python
def evaluate_objective(model, weights=None) -> ObjectiveResult:
    # 7-term vector from a cheap pass over the (already-applied) model:
    #   spectrum_used    — occupied slots across OMS (build_spectrum_state)
    #   transponders     — 2 × #lightpaths (net of spare inventory when supplied)
    #   max_util         — max link utilization (simulate_ip_routing)
    #   dropped_traffic  — Σ dropped demand + overflow, S5-9 no-double-count
    #   added_latency    — Σ active-path propagation (absolute; agent diffs states)
    #   total_margin     — Σ lightpath margin_db (BENEFIT: subtracted in scalar)
    #   services_at_risk — #services whose active path holds a within-threshold
    #                      lightpath (reuse whatif_margin_threshold_sweep logic)
    # weighted scalar = Σ wᵢ·termᵢ, with total_margin subtracted; missing wᵢ = 1.0.

def score_candidate(store_or_model, candidate, weights) -> ObjectiveResult:
    work = model.clone()
    for op in candidate_to_plan(candidate).ops:   # provision new runs + reroute IP
        apply_op(work, op)
    return evaluate_objective(work, weights)

def score_pair(model, working, protection, weights) -> ObjectiveResult:
    # Same, but the Plan carries BOTH legs (protection reserves 1:1, does not load).
```

- **Sign convention:** all terms are costs except `total_margin` (a benefit,
  subtracted). Higher margin ⇒ lower scalar.
- `ObjectiveResult` carries the raw 7-term vector **and** the weighted scalar. The
  raw vector is always returned so an agent can re-weight without re-running.
- **`transponders` / `added_latency` minor definitions:** transponders = 2 ×
  #lightpaths, netted against spare inventory only when an inventory is passed;
  `added_latency` is absolute total active-path propagation (the "added" delta is
  the agent's cross-state comparison, not stored here). These are the two
  intentionally-simple definitions flagged in the session.

---

## Component 3 — `route_service` tool

```
route_service(service_id, *, protected=False, basis="physical", level="link",
              best_effort=False, avoid=None, weights=None) -> RouteServiceResult
```

Modes (all over the shared `place_demands` frontier: `groom_or_new` + `new_only`,
deduped on λ-free route identity — the existing restoration harvest):

- `avoid=None` → **first-time routing** (empty net ⇒ all-new-lightpath candidates).
- `avoid={assets?, risk_groups?}` → **restoration** over survivors (reuses
  `restoration._forbidden_assets` to prune the layered graph).
- `protected=False` → up to `k` single candidates.
- `protected=True` → `disjoint_pairs(...)`; a **menu** of top-N disjoint pairs,
  or top-N min-overlap pairs as `PARTIAL` under `best_effort`.

**Read-only / no-consume** — a menu generator, same discipline as
`compute_restoration`. Scoring clones are thrown away.

Return: typed `status` (`solution`/`partial`/`no_solution`) + candidates, sorted by
the weighted scalar. Each candidate carries `lever`, `reused_lightpaths`,
`new_lightpaths` (gsnr/mode/bitrate each), `restored_gbps`, `shortfall_gbps`,
**`cost_vector`** (replaces proxy `cost_facets`), and — for protected — the paired
footprint + `shared_assets`/`shared_groups` (the *why* of any residual overlap).

`compute_restoration` is reimplemented as a thin delegation to
`route_service(service_id, avoid=avoid, protected=False)`; its existing result
shape is preserved except `cost_facets` → `cost_vector`.

---

## Component 4 — `solve_allocation` rebase (packer over the shared engine)

- Retire the flat-graph `_place_protected` / `compute_disjoint_paths` path.
- The packer loops demands in weighted order (unchanged ordering), calling
  `place_demands` on the **layered** graph; for a protected demand it calls
  `disjoint_pairs` and takes the best pair.
- **Per-demand local pick = shortest-available** (the frontier's discovery order),
  matching CLAUDE.md's "route each by shortest available path, fall back when an
  edge is exhausted." No `evaluate_objective` in the hot loop.
- **The consume axis stays in the packer:** after choosing a placement it commits
  it (`apply_op` on the working state) so spectrum/inventory decrement before the
  next demand — the one behavioural difference from `route_service`.
- **Greenfield fill is agentless.** No agent is in the loop while the network is
  being filled — the packer runs deterministically (shortest-available per demand).
  `evaluate_objective` on the final packed state is at most an **optional quality
  readout** here, not an agent decision point. The agent enters only at *disaster
  reoptimization* (comparing branches/remedies) and at `route_service` candidate
  ranking. `evaluate_objective` is not required by the greenfield packer.
- **Input demands come from the sibling traffic-generation spec** (see below), not
  from this work. Until that lands, `solve_allocation` is exercised with fixture
  demand lists.
- Gains inherited for free: **grooming onto survivors' residual capacity** (the
  storm-scarcity scenario, disaster build order 8) and multilayer disjointness.
- Typed `solution` / `partial` (placed vs. unplaced) / `no_solution` preserved; a
  budget overrun still returns best-so-far as structured data, never an exception.

---

## Component 5 — `evaluate_objective` tool exposure

Exposed at the MCP surface as `evaluate_objective(state, weights)` where `state`
resolves to a snapshot id (defaults to current). Serializer added to
`model/views.py`; tool registered in `server.py`. The disaster scenarios call it on
a branch after applying a remedy (storm step 8, heat step 9) — that path is the
public state-level use; candidate ranking is the internal use.

---

## Data flow (protected `route_service`, restoration)

1. Resolve service src/dst optical nodes (`Router.site`).
2. `build_layered_graph(model, forbidden_assets=_forbidden_assets(avoid))`.
3. `place_demands` over `groom_or_new` + `new_only` → `k` candidate `Placement`s.
4. `disjoint_pairs(candidates, basis, level, best_effort, top_n)` →
   fully-disjoint pairs (or min-overlap).
5. For each returned pair: `score_pair` (clone → `apply_op` both legs →
   `evaluate_objective`) → `cost_vector` + scalar.
6. Sort by scalar; return typed result menu.

Ground truth is never touched (steps 2–5 are read-only / on clones).

---

## Error handling / typed outcomes

- No route / no candidate → `no_solution` (never raised).
- No fully-disjoint pair, `best_effort=False` → `no_solution`.
- No fully-disjoint pair, `best_effort=True` → `partial` with min-overlap pairs +
  shared keys.
- Margin-negative candidate → capacity 0 falls out of the derived-capacity gate;
  scores as dropped/`services_at_risk`, never nominal rate.
- Every tool returns structured/typed data; no prose.

---

## Testing

- **Footprint-flatten (the trap):** two lightpaths sharing one fiber ⇒ their
  placements read as *correlated* under `physical`/`srlg`/`risk_group`. Assert
  directly that `placement_footprint_keys` intersect.
- **Disjoint-pair parity + best_effort:** on a topology where the flat solver finds
  a disjoint pair, `disjoint_pairs` finds an equivalent-footprint pair; with a
  shrunk safe region (injected risk group covering all fully-disjoint routes),
  `best_effort` returns min-overlap pairs as `PARTIAL`.
- **Menu shape:** protected mode returns multiple pairs ranked by combined
  `cost_vector`; unprotected returns the `k`-candidate frontier.
- **Scoring ↔ commit consistency:** `evaluate_objective` on a candidate's clone ==
  the vector after really committing that candidate (identical `apply_op` path ⇒
  identical numbers). Guards the no-drift property.
- **Allocation rebase:** greenfield instance still packs (LPE edges vanish ⇒
  new-lightpath-only; math converges — the §4 note); a scarcity instance grooms
  onto a survivor's residual instead of a false `no_solution`.
- **Margin-gate coupling:** a candidate pushing a lightpath margin-negative scores
  capacity 0 / dropped, not nominal line rate.
- **`compute_restoration` back-compat:** existing restoration tests pass against the
  delegating wrapper (result shape preserved; `cost_facets` → `cost_vector`).
- **Determinism:** seeded, same inputs → same menu order. No LLM in any test.

---

## Out of scope / deferred

- **Traffic / demand-matrix generation → separate sibling spec, brainstormed next.**
  A seeded demand synthesizer (emits `(src, dst, gbps)` demands) that feeds
  `solve_allocation` to produce the pre-disaster loaded steady state. Independent
  of this work — its only interface is the demand list the packer already consumes.
  Disaster-agnostic (capacity planning/defrag use it too), so it belongs in this
  repo, not the downstream app. This spec's greenfield fill *consumes* its output.
- λ-aware (spectrum-disjoint) routing — physical-only by decision.
- Global (non-greedy) allocation optimisation — the packer stays a heuristic with
  no optimality claim.
- `get_telemetry` (heatwave calibration) — optional, not part of this work.
- Physical-layer optimisation, event/geo logic — out of repo scope (CLAUDE.md).

---

## Key existing references reused

- `model/multilayer_graph.py` — `place_demands`, `Placement`, `NewLightpathRun`.
- `model/exposure.py` — `path_basis_keys`, `oms_seq_asset_set`, `split_shared_keys`.
- `model/solvers.py:285-337` — `compute_disjoint_paths` pairwise-scan + best_effort
  logic to lift.
- `model/restoration.py` — `_forbidden_assets`, `_lever`; becomes a wrapper.
- `model/ip_routing.py:201` — `simulate_ip_routing` (cheap, for the IP terms).
- `model/plan.py` `apply_op` + `model/commit.py` — the reused "apply a change" path.
- `model/network.py:61` `clone()` — copy-on-write scratch states for scoring.
- `model/views.py` — serializers; add `route_service` + `evaluate_objective` views.
- `server.py` — register the two tools.
