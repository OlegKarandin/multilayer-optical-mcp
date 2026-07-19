# Findings: unifying routing/restoration at the service level on the multilayer graph

Handoff doc from a design-analysis session (2026-07-12). Captures the current
state of `solve_allocation`, restoration, disjointness, and output richness, plus
the proposed simplification and its open design forks. Next step is a
**brainstorming** pass on the unified `route_service` primitive before any code.

---

## TL;DR

- `solve_allocation` **is** implemented and exposed, but it routes on the **flat
  optical graph**, not the multilayer graph restoration uses — a contract gap vs.
  CLAUDE.md (which says it "operates over a multi-layer graph ... route each over
  the layered graph").
- Two graph substrates exist and don't share code: **flat optical** (`solvers.py`)
  vs. **layered IP+optical** (`multilayer_graph.py`). restoration uses layered;
  allocation/rsa use flat.
- **Disjointness is enforced on the flat graph only.** The layered graph has no
  disjoint-pair capability (only an avoid-set prune hook).
- **`evaluate_objective` is not implemented** — the single recurring gap across all
  three disaster scenarios; the agent's ranking/weighting input.
- The user's proposed simplification (operate at the service level; unify
  first-time routing and restoration on the multilayer graph; do disjointness by
  enumerating K unique multilayer paths and selecting 2 disjoint) is **sound and
  buildable**, with one correctness trap (flatten reused lightpaths to physical
  assets) and one dependency (evaluate_objective for the agentic story).

---

## 1. `solve_allocation` — status and the substrate gap

Implemented in `src/multilayer_optical_mcp/model/allocation.py`, exposed at
`server.py:359`. Greenfield heuristic: lights **new** lightpaths from a per-site
transponder inventory to place many weighted demands; typed
`solution`/`partial`/`no_solution`.

- Docstring `allocation.py:12`: *"greenfield only — grooming onto existing
  lightpaths is Step 5."* It never traverses existing lightpaths.
- Routes via `compute_paths` / `compute_disjoint_paths` on the **flat optical OMS
  graph** + spectrum grid (`allocation.py:160`, `:176`).
- CLAUDE.md contract (lines 269–278) says it should route over the **layered
  graph** whose edges carry capacity + spectrum constraints — i.e. the exact
  `multilayer_graph.py` substrate. **Not met today.**

Why it matters for the final user (`CLAUDE-disaster.md`): the strongest scenario is
storm-scarcity cross-layer (disaster build order 8) — place contending services
**over survivors** when transponders are scarce. Grooming onto existing lightpaths'
residual capacity is the whole point under scarcity. A greenfield-only allocator
burns transponders and returns false `partial`/`no_solution`, and gives the agent a
**different answer than `compute_restoration`** for "can this ride survivors?".

---

## 2. The two graph substrates

### Flat optical graph — `model/solvers.py`
- `build_oms_graph` → directed MultiDiGraph, optical nodes as vertices, one edge
  per OMS (direction-strict; parallel OMS stay distinct).
- `compute_paths` (k-shortest, `weight ∈ {hops, length}`), `check_disjointness`
  (audit), `compute_disjoint_paths` (find a disjoint pair; `best_effort` →
  min-overlap `PARTIAL`; else `NO_SOLUTION`).
- Disjointness keys are namespaced (`phys:`/`node:`/`srlg:`/`rg:`) via
  `exposure.path_basis_keys` so `basis="union"` never collides key kinds.
- Used by: `solve_rsa`, `solve_allocation`.

### Layered IP+optical graph — `model/multilayer_graph.py`
- `build_layered_graph(model, forbidden_assets=..., grid=...)` → MultiDiGraph,
  Zhu/Mukherjee per-wavelength layers, no wavelength conversion. Vertices:
  `(ACCESS, node)` and `(WL, node, lam)`. Edges:
  - **LPE** access(u)→access(v): reuse an existing lightpath (carries
    `lightpath_id`, `residual_gbps`; absent when margin<0/residual 0/forbidden).
    Weight `_W_LPE=1.0`.
  - **WLE** (WL,u,lam)→(WL,v,lam): one free slot on an OMS. Weight `0.1`.
  - **TxE** access(u)→(WL,u,lam): originate a new lightpath. Weight `5.0`
    (discouraged but reachable).
  - **RxE** (WL,v,lam)→access(v): terminate. Weight `0.0`.
- `place_demands(model, g, qot, src, dst, demand_gbps, policy, k, grid)`:
  IGABAG single-demand k-best placement. Policies: `groom_only` (drop TxE),
  `new_only` (drop LPE), `groom_or_new` (full).
  - Mechanics: `_collapse_to_simple` (MultiDiGraph→simple DiGraph, min parallel
    weight) → `nx.shortest_simple_paths` (unique node paths) → `_parse_paths`
    re-expands each into concrete `(reused_lightpaths, new_runs)` via
    `itertools.product` over parallel edges.
  - **Dedup key is λ-free route identity** (`multilayer_graph.py:376`): same OMS
    route on λ=0 vs λ=1 is one candidate. A candidate does **not** commit to a
    wavelength (provisioning re-runs spectrum assignment).
  - Budgets: `_PATH_BUDGET=64` distinct routes, `_DEFAULT_K=8` kept,
    `_RAW_PATH_CAP=1024` raw-path safety valve.
  - New runs are QoT'd via `allocation._build_loading` + `_best_feasible_mode`
    against the committed spectrum snapshot only (S7-10: runs assumed OMS-disjoint
    within one placement).
- `residual_gbps` for a reused lightpath = min over its bound IP links of
  (derived capacity − offered load); margin-gated (0 when margin<0).
- Used by: `restoration.py`.

### restoration — `model/restoration.py`
- `compute_restoration(model, qot, service_id, avoid)`: read-only enumeration of
  recovery candidates for **one service** over survivors. Resolves
  `Router.site == optical-node id`. Builds layered graph pruned by
  `_forbidden_assets(avoid)`, harvests `groom_or_new` + `new_only` frontiers,
  dedups on λ-free route identity, returns typed candidates.
- Candidate carries `lever` (`ip_reroute`/`optical_reroute`/`hybrid`),
  `reused_lightpaths`, `new_lightpaths`, `restored_gbps`, `shortfall_gbps`,
  `cost_facets`.
- **`cost_facets` are PROXIES** (`restoration.py:66`): `transponders` assumes 2 per
  new lightpath ignoring spare inventory; `hops` is lightpath count not fiber hops.
  Comment: *"until evaluate_objective's richer cost vector (CLAUDE.md) lands as the
  ranking function instead."*

---

## 3. Disjointness — how it's enforced (and where it isn't)

- **Flat graph (`solve_allocation`/`solve_rsa`): YES.** `_place_protected`
  (`allocation.py:172`) calls `compute_disjoint_paths(basis, level, best_effort)`,
  gets a disjoint working+protection **pair**, spectrum-assigns both, commits only
  if both legs place. Full `basis ∈ {physical, srlg, risk_group, union}` ×
  `level ∈ {node, link, srlg, risk_group}`, with `best_effort` min-overlap.
- **Layered graph (`place_demands`/restoration): NONE.** `place_demands` routes a
  **single** path — no working+protection-pair concept. restoration only prunes an
  avoid-set so the restored path dodges the hazard; it does **not** guarantee the
  restored working path is disjoint from the surviving protection path.
- **Consequence:** re-basing `solve_allocation` on the layered graph (to get
  grooming) would *lose* the disjointness it gets today from
  `compute_disjoint_paths`. Disjointness must be added to the layered path world.
- **The hook already exists:** `build_layered_graph(forbidden_assets=...)` takes a
  prune-set, so layered disjointness is buildable via place-working → prune its
  assets → place-protection, OR via enumerate-K + pairwise-filter (see §6).

---

## 4. Greenfield fill vs. restoration — same? combine?

- `solve_allocation` **is** the greenfield "fill the network before the disaster"
  tool. In greenfield (empty network) restoration's LPE edges vanish, so
  `place_demands` emits only new lightpaths — the **placement math converges**.
- But they are **different orchestrators**:
  | | `solve_allocation` | `compute_restoration` |
  |---|---|---|
  | Demands | many, weighted, **sequential** | **one** service |
  | Resources | **consumes** (spectrum + inventory decrement between demands) | **read-only**, consumes nothing |
  | Returns | one chosen placement per demand | **k candidate menu** per service |
  | Substrate today | flat optical | layered |
- **Recommendation: share the engine (`place_demands` over the layered graph),
  keep two thin orchestrators.** allocation = sequential packer that consumes;
  restoration = menu generator that doesn't. Fully merging conflates a packer with
  a menu.

---

## 5. Output richness for the agent

Architecture forbids prose (CLAUDE.md: "No tool returns 'looks fine'"); everything
is structured/typed — better for an agent. Decision-grade detail that already
exists (all serializers in `model/views.py`):

- `compute_restoration`: `lever`, `reused_lightpaths`, `new_lightpaths`
  (gsnr/bitrate/mode each), `restored_gbps`, `shortfall_gbps`, `cost_facets`.
- `get_exposure`: working/protection intersect flags **+ the intersecting asset
  lists** (`server.py:288`).
- `check_disjointness`: `disjoint` + `shared_assets` + `shared_groups` (the *why*).
- `inject_degradation`: per-lightpath `margin_before/after`, `crossed`,
  `within_threshold`, `crossings`.
- `validate_plan`: typed violations with `state_index`, `transient`, `detail`
  remediation pointer.
- `simulate_ip_routing`: per-link util/`down`, congestion, dropped services with
  `reason` + `on_link`.
- `get_qot_breakdown`: **per-element** snapshots + `limiting_element_id` (deepest
  diagnostic; `server.py:146`).
- `solve_rsa`/`solve_allocation`: placements + unplaced **with reason strings**
  (but coarse: `"insufficient transponders"`, `"no feasible route/slot/mode"` —
  don't name the binding hop/resource the way restoration facets do).

**Gap:** `evaluate_objective` — the cost vector the agent weights choices against
(`{spectrum_used, transponders, max_util, dropped_traffic, added_latency,
total_margin, services_at_risk}`). Not implemented; only referenced in the
`restoration.py:66` proxy-facets comment. This is the input
`CLAUDE-disaster.md`'s step-2 agentic value ("set weights from event state") needs.
`get_telemetry` (heatwave calibration) also absent but optional ("if exposed").

---

## 6. Proposed simplification (to brainstorm)

Operate at the **service level** for both first-time routing and restoration, both
on the multilayer graph; disjointness between a service's two paths via
enumerate-K-then-filter on that graph.

Suggested shape — one tool, two modes over the shared `place_demands` core:

```
route_service(service, protected?, basis, level, best_effort, avoid?)
  unprotected: k candidates (existing place_demands frontier)
  protected:   enumerate k candidates, flatten each to physical footprint,
               pairwise-scan for a disjoint (working, protection) pair,
               best_effort -> min-overlap pair
  avoid=None            -> first-time routing (empty net -> all new lightpaths)
  avoid={failed/rg set} -> restoration
```

### The correctness trap (this IS the demo's thesis, one layer down)
Disjointness of a multilayer path must be computed on its **physical footprint**,
not lightpath identities. A path is `reused_lightpaths + new_runs`; flatten BOTH:
- new run → OMS → fibers/amps/ROADMs (`exposure.oms_seq_asset_set`).
- **reused lightpath → its `oms_sequence` → same physical assets.**

Two "different lightpaths" that share a fiber/risk-group asset are correlated.
Treating lightpath-id as the disjointness unit misses exactly the latent
correlation the storm demo is built to catch. `exposure.path_basis_keys` /
`oms_seq_asset_set` already produce the namespaced keys; need one adapter:
`Placement → total OMS set → path_basis_keys`, then reuse `compute_disjoint_paths`'
pairwise-scan + `best_effort` min-overlap logic unchanged.

### Is it sufficient / realistic / rich enough for the demo?
- **Rich:** one graph gives pure-electrical (LPE-only), optical reroute (new run),
  hybrid, capacity/mode-derived residuals, disjointness against
  physical/srlg/risk_group/union, and best-effort degraded disjointness. Only lever
  not on it is modulation downshift = `set_modulation_format` (exists, separate —
  correctly a mode change on a chosen path, not routing).
- **Realistic, with the right disclaimer:** "K-paths then disjoint-filter" is a
  heuristic, not exact (Suurballe/Bhandari don't extend to grooming or SRLG/
  risk-group disjointness; SRLG-disjoint routing is NP-hard). CLAUDE.md already
  commits to heuristic/no-optimality-claim. Failure mode is **on-narrative**:
  `best_effort` returns min-overlap when the shrinking safe region kills full
  disjointness — the degraded-restoration beat.

### Open design forks for brainstorming
1. **Disjoint-pair method:** (A) enumerate-K + pairwise-filter (gives best_effort
   min-overlap for free; matches flat solver) vs. (B) place-working → prune its
   assets → place-protection (simpler guarantee, but can miss pairs a
   slightly-longer working would enable — the trap Suurballe fixes; no natural
   min-overlap). Leaning A for the degraded-restoration story.
2. **Footprint granularity:** λ-free/physical-only (disjointness = shared fibers/
   SRLGs/risk groups) vs. λ-aware (also spectrum-disjoint). Physical-only matches
   the "SRLG-disjoint but both aerial" thesis; λ is a provisioning-time concern.
3. **Orchestration:** confirm share-engine-keep-two-orchestrators (§4) — route_service
   (menu, no consume) vs. solve_allocation (packer, consume). Where does
   first-time bulk routing of many services live — a packer mode of route_service?

### Dependencies before a "nice agentic" demo
- Build the disjoint-pair-on-multilayer primitive (above).
- Implement **`evaluate_objective`** and switch restoration/allocation ranking onto
  it (retire proxy `cost_facets`).

---

## 7. Scenario step lists (server tool sequences; `[app]` = out of server scope)

### Storm (moving cone, aerial filter, cut → replan)
1. `[app]` ingest forecast cone (hourly polygons)
2. `[app]` `map_geo_event_to_assets(cone_t, aerial_filter)` → asset list (GIS)
3. `define_risk_group(asset_list, metadata)` → rg_id (one per forecast hour)
4. per service: `get_exposure(service, rg)` → `both_intersect?` OR
   `check_disjointness(working, protection, basis=risk_group)` on deployed pair
5. `snapshot_branch(current)`
6. `compute_disjoint_paths(src, dst, basis=union, level, best_effort)` — reroute
   clear of the **union of all hours' cones**
7. `recompute_qot_under_loading(loading)` / `compute_qot(...)` — verify survival
8. if spares scarce: `evaluate_objective(state, weights)` [MISSING] +
   `solve_allocation(demands, spare_inventory, weights)`
9. `validate_plan(plan)` → typed violations (all intermediate states)
10. `commit_plan(plan, dry_run=false)` (gated) → `reconcile(snapshot)` → drift

### Flood (same pipeline, inverted filter)
Identical; only step 2's filter changes to buried/manhole/low-lying (aerial safe).
Agentic value **only** = runtime filter selection from NL report.

### Heatwave (degradation, not cut → pre-emptive cross-layer remedy)
1. `[app]` map heat region → thermally-exposed assets
2. `define_risk_group(exposed_assets, metadata)`
3. `snapshot_branch(current)`
4. `inject_degradation(asset, {nf:+x, loss:+y})` per exposed asset
5. `recompute_qot_under_loading(loading)` → margin crossings before peak
6. `whatif_margin_threshold_sweep(threshold)` → fragile cluster
7. `[app]` calibrate vs. telemetry (`get_telemetry` — not implemented, optional)
8. evaluate 3 remedies on branch: reroute (`compute_disjoint_paths`+`compute_qot`);
   downshift (`set_modulation_format`→`simulate_ip_routing`); groom
   (`get_affected_services`+`compute_restoration`/`reroute_service`)
9. `evaluate_objective(state, weights)` [MISSING] — choose by heat extent
10. `validate_plan` → `commit_plan` → `reconcile` (pre-emptive)

**Not expressible today:** `evaluate_objective` (steps storm-8, heat-9); disjoint
reroute on the layered graph when grooming is involved (§3, §6).

---

## Key file references
- `model/allocation.py` — solve_rsa, solve_allocation, `_place_protected`,
  `_build_loading`, `_best_feasible_mode`, `QotEvaluator` seam.
- `model/multilayer_graph.py` — build_layered_graph, place_demands, `_parse_paths`,
  `_collapse_to_simple`, `NewLightpathRun`, `Placement`.
- `model/restoration.py` — compute_restoration, `_forbidden_assets`, proxy
  cost_facets comment (`:66`).
- `model/solvers.py` — build_oms_graph, compute_paths, check_disjointness,
  compute_disjoint_paths.
- `model/exposure.py` — path_basis_keys, oms_seq_asset_set, split_shared_keys
  (the footprint-keying to reuse).
- `model/views.py` — all result serializers (output richness).
- `server.py` — tool surface (~40 tools); `evaluate_objective` and `get_telemetry`
  NOT present.
