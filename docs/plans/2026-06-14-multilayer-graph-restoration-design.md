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
function**. Restoration becomes **k-best placement over this one graph**: enumerate the
cost-ordered frontier of recovery paths, each of which may reuse existing lightpaths
(`ip_reroute`), light new ones (`optical_reroute`), or **both in a single path** (`hybrid` —
groom across survivors and light one new lightpath to bridge a gap). New-lightpath edges
carry a *moderate* weight (a few groom-hops' worth, not a 1000× wall) so hybrids interleave
into the frontier instead of ranking behind every pure-groom path. A `new_only` pass
(drop LPE) supplements the frontier to guarantee the pure-optical fallback is present even
when many degraded groom variants would otherwise exhaust the k-best budget.

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
3. **Levers:** reuse-existing (`ip_reroute`), light-new (`optical_reroute`), and **hybrid**
   (both in one path), enumerated via k-best placement over the one graph — not separate code
   paths. New-lightpath edges carry a moderate (not prohibitive) weight so hybrids surface.
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
8. **New-lightpath capacity:** **weights, not capacity.** WLE/TxE edges carry a *moderate*
   weight (new-lightpath penalty, a few groom-hops' worth — not prohibitive, so hybrids
   interleave) and *no* capacity constraint — a fresh lightpath trivially fits one demand's
   granularity. Only **LPE (existing-lightpath) edges** carry residual capacity
   (`derived_cap − load`, margin-gated). QoT runs **once per new run, after routing** → best
   feasible mode → capacity / margin gate → accept, derate (degraded), or reject + fall back.
9. **Hybrids + regeneration:** a recovery may **groom across survivors and light new
   lightpaths in one path** (`hybrid` lever). Each new-lightpath run is QoT'd independently,
   so a path that chains new lightpaths with an O-E-O at an intermediate access node is
   *enumerable* (the regen-reach benefit — a long detour split into individually-feasible
   segments). **Decided 2026-07-24: regen-node transponder inventory is assumed infinite.**
   Pre-planning regen capacity is a nontrivial planning problem in its own right; gating
   `validate_plan`/`commit_plan` on spare transponders was considered and deliberately not
   built (see `docs/2026-07-19-open-todos.md` §5/§6). `solve_allocation`'s separate
   `spare_inventory` packer still exists for callers who want scarcity modeling explicitly.

## Architecture

```
compute_restoration (read-only enumerator)        solve_allocation (next consumer)
                       \                          /
                        v                        v
                    model/multilayer_graph.py
           (layered auxiliary graph + place_demands / IGABAG k-best)
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
  that layer. Low weight. No capacity.
- **LPE** — lightpath layer, existing lightpath u→v: **residual capacity** `derived_cap −
  load`, margin-gated (margin < 0 ⇒ edge absent), absent when residual ≤ 0. **Low weight**
  (reuse). `restored_gbps` is bottlenecked by the min residual along the reused LPE edges.
- **TxE** — access → wavelength layer: originate a new lightpath. **Moderate weight** (a few
  groom-hops' worth — discourages but doesn't starve new segments, so hybrids interleave).
  **RxE** — wavelength → access: terminate (zero weight).
- **GrmE** — access layer at a node: multi-hop grooming across survivors, and the O-E-O point
  where a hybrid switches between a reused lightpath and a new one.

**Built against a loading state** (a snapshot/branch), so the graph composes with what-if
branches.

**`place_demands(graph, src, dst, demand_gbps, policy, k)`** — IGABAG k-best for one demand,
returning up to `k` **distinct** placements (the cost-ordered frontier), each possibly
degraded or hybrid:
1. Walk `shortest_simple_paths` access(src) → access(dst) on the policy graph (bounded by a
   path budget). Empty list when no path exists.
2. For each distinct path, parse it into reused lightpaths (LPE) and new-lightpath runs
   (TxE → WLEs on one λ → RxE).
3. **QoT once per new run** under current loading → worse-direction GSNR → best feasible mode.
   `margin < 0` ⇒ run infeasible, skip this candidate. `restored_gbps = min(demand, groom
   bottleneck residual, new-run mode rate)`; `shortfall = demand − restored` (0 ⇒ full).
4. Collect up to `k` distinct feasible placements.

### `model/restoration.py`

`compute_restoration(model, service_id, avoid={assets, risk_groups})` → `RestorationResult`
(read-only; mutates nothing):
1. Build the layered graph for the current loading; **prune** by `avoid` (forbidden assets +
   risk-group/SRLG members; *not* their endpoint nodes — a failed fiber must not condemn its
   healthy end ROADMs and the survivor OMS sharing them).
2. Harvest candidates via `place_demands` over **`groom_or_new`** (the cost-ordered frontier:
   groom + hybrid + cheap new) **and `new_only`** (guarantees the pure-optical fallback even
   when many degraded groom variants would exhaust the k-best budget). Dedup across buckets.
3. Classify each by lever (`ip_reroute` / `optical_reroute` / `hybrid`) and emit
   `RestorationCandidate { lever, reused_lightpaths, new_lightpaths, restored_gbps,
   shortfall_gbps, cost_facets }`. `cost_facets` inline (transponders, new_lightpaths, hops)
   until `evaluate_objective` lands.
4. `status`: `SOLUTION` if any candidate has `shortfall_gbps == 0`; `PARTIAL` if candidates
   exist but all degraded; `NO_SOLUTION` if none place.
5. Candidates sorted full-before-degraded, then by (transponders, hops).

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

- Regen-node **transponder inventory gating** — multi-new-lightpath recoveries are
  *enumerated* (with their transponder cost); spare-transponder availability at regen nodes
  is **not** checked anywhere in `validate_plan`/`commit_plan`. This was originally scoped
  for Phase 7 but decided 2026-07-24 to stay unbuilt: regen capacity is assumed infinite
  rather than pre-planned (see `docs/2026-07-19-open-todos.md` §5/§6).
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
- **Placement:** toy topology with a known answer — `groom_only` reuses an existing
  lightpath; `new_only` lights a new one; `groom_or_new` finds a hybrid (groom one leg + light
  the next) when an existing lightpath covers only part of the route.
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
3. `place_demands` (IGABAG k-best) with QoT realization/derate + hybrid test.
4. `restoration.py` `compute_restoration` (groom_or_new + new_only, three-lever typing) + tests.
5. Server tool `compute_restoration` + view serializer.
