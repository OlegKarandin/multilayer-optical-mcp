# Multi-Layer Graph + Restoration — Design

**Date:** 2026-06-14
**Status:** Approved design (brainstorming output). Implementation plan follows separately.

## Problem

When a failure (`inject_failure`) takes down assets that a service's **working and
protection** paths both cross, the service is hard-down and neither pre-planned path
recovers it. We need **restoration**: route the demand over *survivors* and re-bind it,
recovering at full or — honestly — reduced capacity.

This work also lands the **layered IP+optical graph** that CLAUDE.md already commits
`solve_allocation` to ("a layered IP-plus-optical graph whose edges carry capacity and
spectrum constraints; weighted sequential placement"). Restoration is the first consumer;
`solve_allocation` is the next (refactor deferred to a follow-up step, but the graph is
built to serve it).

## Core idea (why this shape)

Following Zhu/Zang/Mukherjee, *"A Novel Generic Graph Model for Traffic Grooming in
Heterogeneous WDM Mesh Networks"* (IEEE/ACM ToN 2003): build a layered auxiliary graph
where every constraint is an **edge type**, and solve routing + wavelength assignment +
grooming jointly with **one shortest-path**, where the **grooming policy is the edge-weight
function**. The two restoration "levers" (reuse existing lightpaths vs. light a new one)
collapse into **two weight policies over one graph**, and hybrids (groom across survivors +
light one new lightpath) fall out for free.

The paper is physics-free (static, independent edge capacities). This server is not — mode
feasibility is gated by loading-coupled QoT, capacity = f(mode) only when margin ≥ 0. So we
adapt: **weights drive routing; QoT realizes the result.**

## Decisions (locked during brainstorming)

1. **Restoration shape:** layered. Avoidance constraint on the OMS router (foundation) +
   a thin, **read-only** `compute_restoration` that *enumerates* candidate plans. The agent
   decides which to commit; execution is the separate, human-gated Phase-7 path.
2. **Granularity:** per-service — `compute_restoration(service_id, avoid)`. The agent loops
   over `affected_services` and owns ordering; survivor-capacity contention between
   successive restorations is threaded by the agent via branch/commit.
3. **Levers:** both reuse-existing and light-new, enumerated — but realized as two weight
   policies, not two code paths.
4. **Degraded restoration:** always returned. A candidate carries `restored_gbps` and
   `shortfall_gbps`; a full candidate is just `shortfall_gbps == 0`.
5. **Avoidance constraint:** **graph surgery** — prune forbidden OMS edges/nodes *before*
   enumeration (not post-filter, which collides with the k-cap and yields false
   `no_solution`). Keyed **per-OMS-edge** via `path_basis_keys`, threaded through
   `_oms_between` so a forbidden OMS is dropped while a *parallel* OMS in a different
   SRLG/risk-group survives. Honored key: `constraints={"avoid": {assets, risk_groups}}`.
6. **Multi-layer graph: implemented now**, as shared foundation for `compute_restoration`
   and (next) `solve_allocation`.
7. **Graph structure:** **per-wavelength layers, no wavelength conversion.** 48 wavelength
   layers (the existing C-band grid) + 1 lightpath layer + 1 access/IP layer. Continuity is
   enforced *structurally* (a path stays on one λ from TxE to RxE); the only way to change λ
   is to terminate/re-originate at the access layer (a transponder cost). RWA is solved
   jointly by the shortest-path, not punted to a separate first-fit.
8. **New-lightpath capacity:** **weights, not capacity.** WLE/TxE edges carry a high weight
   (new-lightpath penalty) and *no* capacity constraint — a fresh lightpath trivially fits one
   demand's granularity. Only **LPE (existing-lightpath) edges** carry residual capacity
   (`derived_cap − load`, margin-gated) and are deleted when residual < granularity. QoT runs
   **once, after routing**, on the realized new-lightpath run → best feasible mode →
   capacity / margin gate → accept, derate (degraded), or reject + fall back.
9. **Regeneration:** **structurally allow, defer wiring.** Access-layer transitions exist and
   are weighted (regen-ready), and **multi-hop grooming across existing survivor lightpaths**
   (`RxE → GrmE → LPE`) is realized this step. **Single-segment** new lightpaths only;
   multi-segment 3R regen (chaining new lightpaths, regen-node inventory, per-segment QoT) is
   deferred.

## Architecture

```
compute_restoration (read-only enumerator)        solve_allocation (next consumer)
                       \                          /
                        v                        v
                    model/multilayer_graph.py
            (layered auxiliary graph + place_demand / IGABAG)
                 |                 |                  |
        spectrum bitmask     network model       GNPy adapter
        (WLE edge set)    (LPE residual, modes)  (QoT realize, margin gate)
                                   ^
                          solvers.py avoidance
                       (graph surgery via path_basis_keys)
```

### `model/multilayer_graph.py`

**Vertices.** Per optical node: input/output ports on 48 wavelength layers + lightpath
layer + access/IP layer.

**Edges** (each `(capacity, weight)`; capacity unbounded unless noted):
- **WLE** — wavelength layer λ, u→v: exists iff `(spectrum_state[oms] >> λ) & 1 == 0`.
  Driven directly from the per-OMS bitmask in `spectrum.py`; parallel OMS → parallel WLE on
  that layer. **High weight** (new-lightpath penalty). No capacity.
- **LPE** — lightpath layer, existing lightpath u→v: **residual capacity** `derived_cap −
  load`, margin-gated (margin < 0 ⇒ edge absent). **Low weight** (reuse). Deleted in
  placement Step 1 when residual < demand granularity.
- **TxE / RxE** — access ↔ wavelength layer: present iff a transponder is available at that
  node/λ; carry the new-lightpath weight (the transponder cost). **MuxE / DmxE** — access ↔
  lightpath layer.
- **GrmE** — access layer at a node: multi-hop grooming across survivors. Regen transitions
  are present structurally but new-segment chaining is unrealized this step.

**Built against a loading state** (a snapshot/branch), so the graph composes with what-if
branches.

**`place_demand(graph, src, dst, demand_gbps, weight_policy)`** — IGABAG for one demand:
1. Delete LPE edges with residual < granularity.
2. Shortest path access-output(src) → access-input(dst). No path ⇒ `None`.
3. Realize any new-lightpath run (TxE → WLEs on one λ → RxE) as a candidate lightpath.
4. **QoT once** under current loading → worse-direction GSNR → best feasible mode. `margin <
   0` ⇒ reject (fall back to next path/λ). Mode below demand ⇒ **derate** (degraded).
5. Return the realized placement (path, new-lightpath specs, mode, realized capacity).

### `model/restoration.py`

`compute_restoration(model, service_id, avoid={assets, risk_groups})` → `RestorationResult`
(read-only; mutates nothing):
1. Build the layered graph for the current loading; **prune** by `avoid` (graph surgery).
2. Run `place_demand` for the service's `(src_router, dst_router, demand_gbps)` under
   **grooming-only** (WLE forbidden/very-high weight → reuse-existing candidate) and
   **grooming-or-new** (WLE allowed → new-lightpath candidate) policies.
3. Emit each as `RestorationCandidate { lever, ip_path | new_lightpath_spec, restored_gbps,
   shortfall_gbps, cost_facets }`. `cost_facets` computed inline (transponders, added
   hops/latency, spectrum slot) until `evaluate_objective` lands.
4. `status`: `SOLUTION` if any candidate has `shortfall_gbps == 0`; `PARTIAL` if candidates
   exist but all degraded; `NO_SOLUTION` if neither policy places.
5. Candidates sorted full-before-degraded, then by cost.

### `solvers.py` — avoidance constraint

`compute_paths` / `compute_disjoint_paths` honor `constraints={"avoid": {assets,
risk_groups}}`:
- Resolve once: per-OMS basis keys ∩ avoid-set ⇒ forbidden OMS set; a forbidden node prunes
  the node + incident OMS.
- Prune the MultiGraph from survivors only **and thread the exclusion through `_oms_between`**
  so the parallel-OMS re-expansion cannot reintroduce a forbidden sibling (and cannot drop a
  surviving parallel in a different SRLG).
- Enumerate unchanged on the pruned graph — the k-cap now counts survivors.

## Data flow (restoration)

`inject_failure(assets)` on a branch → `get_affected_services` (working ∪ protection hit) →
for each, `compute_restoration(service, avoid=failed_assets)` → agent inspects typed
candidates + cost facets → (Phase 7) `validate_plan` the chosen candidate's plan →
`commit_plan`. `compute_restoration` reads back nothing and writes nothing.

## Deferred (honest boundaries)

- Multi-segment 3R regeneration (chaining new lightpaths, regen-node inventory, per-segment
  QoT). Graph is regen-ready; wiring deferred.
- `solve_allocation` refactor onto the layered graph — the next consumer.
- Full `evaluate_objective` cost vector — `cost_facets` are computed inline meanwhile.
- Execution — `compute_restoration` only enumerates. `validate_plan` / `commit_plan` /
  `provision_lightpath` are Phase 7; candidates are shaped to feed them.

## Modeling dependency to resolve in implementation

Lever-B (new lightpath) needs `src_router → optical node`. Not stored directly; derivable as
the optical endpoint of the lightpaths the router's IP links already ride. If a router homes
to two ROADMs the mapping is ambiguous — pick and document a convention then.

## Testing (deterministic, seeded, no LLM)

- **Graph construction:** WLE set matches the OMS bitmask (incl. parallel OMS → parallel
  WLE); LPE residual = `derived_cap − load` and is absent when margin < 0.
- **Placement:** toy topology with a known groom-vs-new answer — grooming-only policy reuses
  an existing lightpath; grooming-or-new lights a new one when grooming can't carry it.
- **Avoidance:** pruning removes a forbidden OMS but keeps a parallel OMS in a different SRLG
  (the scenario-1 catch); k-cap interaction yields a survivor route, not false `no_solution`.
- **Degraded:** a forced downshift on a long survivor route yields a candidate with
  `shortfall_gbps > 0`, not a rejection.
- **Status typing:** survivors exhausted ⇒ `NO_SOLUTION`; only-degraded ⇒ `PARTIAL`.
- **Layer consistency:** a margin-negative lightpath contributes no LPE edge (capacity-0
  gate), so restoration can't groom onto a down lightpath.

## Build order within this step

1. Avoidance constraint in `solvers.py` (pure; unblocks pruning) + tests.
2. `multilayer_graph.py` graph construction (WLE/LPE/access edges) against a loading state +
   tests.
3. `place_demand` (IGABAG) with QoT realization/derate + tests.
4. `restoration.py` `compute_restoration` (two policies, candidate typing) + tests.
5. Server tool `compute_restoration` + view serializer.
