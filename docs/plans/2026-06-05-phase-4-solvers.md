# multilayer-optical-mcp — Phase 4: Solvers (routing, disjointness, RSA, allocation)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development to implement each function test-first. Outcomes are typed (`solution`/`partial`/`no_solution`), never exceptions.

**Goal:** Land Build-order **Step 4 (Solvers)** in two slices: (Part 1) k-shortest routing + disjointness over the optical OMS graph; (Part 2) spectrum-occupancy state, `check_spectrum_feasibility`, `solve_rsa`, and the heuristic greenfield `solve_allocation`.

**Architecture:** Pure deterministic functions over `NetworkModel`. Routing over an OMS graph (optical nodes = vertices, OMS = edges). Disjointness keyed by a named basis. Spectrum stored as per-OMS slot bitmasks. SNR reached **only** through the GNPy adapter (route-first, mode-from-SNR). No solving inside the LLM; all results structured dicts.

**Tech Stack:** Python 3.11+, FastMCP, NetworkX ≥3.2, gnpy 2.11.1, pytest. No new deps.

---

# Part 1 — routing + disjointness slice  *(STATUS: shipped, commit `65b1735`)*

## Context

Steps 1–3 committed: multilayer model + COW state engine, GNPy adapter with
arbitrary-loading / per-direction QoT, read-tool + risk-group surface. Part 1 implements
the first three solver tools — the scenario-1 differentiator — reusing existing
machinery and unblocking the demo. Routing is over the **optical OMS graph**.

## Hard contracts honored

- Solver outcomes typed (`solution`/`no_solution`/`partial`), never exceptions.
- Disjointness relative to a **named basis** (`physical|srlg|risk_group|union`) and
  **level** (`node|link|srlg|risk_group`); degree not binary (best-effort).
- Computation (`compute_disjoint_paths`) vs verification (`check_disjointness`) are
  distinct first-class tools.

## Implementation (as built)

- **`model/solvers.py`** — `SolverStatus` enum; `OmsPath`, `RoutingResult`,
  `DisjointnessResult` frozen dataclasses; `build_oms_graph` (MultiGraph: optical nodes +
  one edge per OMS); `_enumerate_oms_paths` (node-simple paths via
  `nx.shortest_simple_paths`, **expanding parallel-OMS choices per hop** so parallel
  segments between the same node pair are not collapsed); `compute_paths`,
  `check_disjointness`, `compute_disjoint_paths`.
- **`model/exposure.py`** — factored `oms_seq_asset_set` / `oms_seq_node_set` out of the
  IP-link expansion; added `path_basis_keys` (namespaced `phys:`/`node:`/`srlg:`/`rg:`
  keys so `union` is a plain set-union and shared keys split back into
  `shared_assets`/`shared_groups`).
- **`model/views.py`** — `routing_result_dict`, `disjointness_result_dict`.
- **`server.py`** — `@app.tool()` `compute_paths`, `check_disjointness`,
  `compute_disjoint_paths`.

## Testing (as built)

`tests/model/test_solvers.py` + `tests/test_server_phase4.py`: both routes for `k=2`;
typed `no_solution` for disconnected dst; physical-disjoint True; the scenario-1 catch
(same pair correlated under an injected risk group); `union` = intersection of
constraints; best-effort `partial` min-overlap pair. **114 tests green.**

---

# Part 2 — spectrum state + `solve_rsa` + `solve_allocation`  *(STATUS: to execute)*

## Context

Remainder of Step 4 — the spectrum-assignment and allocation solvers `CLAUDE.md` lists
last, plus the spectrum-occupancy state and `check_spectrum_feasibility` physical-layer
tool they need (currently unimplemented).

### Decisions settled with the user

1. **Spectrum is STORED, efficiently — not derived per call.** (The "derived, never
   stored" rule is about *IP capacity = f(mode)*, not spectrum.) Spectrum occupancy is a
   **per-OMS slot bitmask** (integer bit-vector of `num_slots` bits). Feasibility along a
   path = bitwise OR of the path's OMS masks; first-fit = lowest zero bit; reserve = set
   the bit on each OMS in the path.
2. **Disjointness basis is a first-class protection parameter — all bases supported now,
   none deferred.** A protected demand names its basis (`physical|srlg|risk_group|union`)
   and level; the solver forwards it verbatim to the already-shipped
   `compute_disjoint_paths` (Part 1). SRLGs are already in the model, so SRLG-disjoint and
   risk-group-disjoint protection work in this slice. `physical`/`link` is only the
   *default when unspecified*. (Only **grooming** is deferred to Step 5.)
3. **Route first, mode from SNR — mode is an OUTPUT.** Route by **shortest total fiber
   length (km)** → compute GSNR on that path under the candidate loading via the **real
   GNPy adapter** → accept iff **≥1 mode is feasible** (`required_gsnr_db ≤ achieved
   GSNR`); delivered mode is the **highest-bitrate** feasible one. Per-direction: gate on
   the **worse** of FORWARD/BACKWARD GSNR.
4. **Single transponder type network-wide.** All modes share baud rate + channel spacing,
   so **GSNR is mode-independent** → one QoT evaluation per (path, direction, loading);
   mode is read off the GSNR. `spare_inventory` = transponder **count per site**.
5. **Grooming → Step 5.** `solve_allocation` here is **greenfield only**: it lights *new*
   lightpaths from spare transponders on free spectrum.

## Hard contracts (CLAUDE.md)

- **Typed outcomes, never exceptions** — `solution`/`partial`/`no_solution`. Resource
  exhaustion is a typed result, not a raise.
- **Heuristics make no optimality claim.**
- **Adapter is the only thing that talks to GNPy**; solvers reach QoT through it.
- **Deterministic** ordering and first-fit selection. **All tool results structured.**

## The load-bearing prerequisite (riskiest — do first)

Real-GNPy SNR on a *routed* path requires the routed OMS elements to resolve to gnpy uids
(`translate.py:resolve_oms_path_to_uids`), and a real routing choice requires a **2-route
gnpy topology**. The current `toy_2span.json` is a single linear chain.

- **`topologies/toy_2route.json` (new):** two ROADMs (A, Z) joined by two parallel
  amplified express paths (north / south), reusing the advanced amp model + ROADM degree
  handling in `adapter.py`. Reuse `eqpt/eqpt_config.json`.
- **`compute_qot` topology seam:** thread an optional `topo_path`/`eqpt_path` (or a
  pre-loaded `(eqpt, network)`) through `compute_qot` / `load_toy` so a path can be
  evaluated against `toy_2route.json` without changing existing single-route ground-truth
  tests. Default unchanged.
- **`QotEvaluator` seam:** a callable protocol
  `evaluate(oms_sequence, direction, mode_id, loading) -> QoTState`. Server binds it to
  the real adapter + current topology + results store; integration tests bind it to
  `toy_2route.json` (real GNPy); a few placement-logic unit tests bind a deterministic
  fake. Solvers depend only on this protocol, never on gnpy/file paths.

## Implementation

### 1. Spectrum state — `model/spectrum.py` (new)
- `SpectrumGrid` (frozen): `anchor_hz`, `spacing_hz`, `num_slots`; `freq(i)`,
  `slot_of(freq)`. Default 48 slots @ 100 GHz, anchor so slot 24 ≈ 193.4 THz.
- `build_spectrum_state(model, grid) -> Dict[str, int]` — per-OMS slot bitmask from every
  `Lightpath`.
- `free_slots_along`, `first_fit_slot`, `reserve`. `FeasibilityResult`
  (`feasible`, `clashes`); `check_spectrum_feasibility(model, oms_sequence, slot,
  extra_state=None)`.

### 2. Length-weighted routing — extend `model/solvers.py`
- `oms_length_km(model, oms_id)` = Σ fiber lengths in `oms.elements`.
- `compute_paths(..., weight="hops"|"length")`; default `"hops"`. RSA/allocation pass
  `"length"`.

### 3. Solvers — `model/allocation.py` (new)
Result types (frozen): `SpectrumAssignment` (`oms_path`, `slot_index`, `center_freq_hz`,
`mode_id`, `gsnr_db`); `DemandPlacement` (`demand_id`, `working`, `protection`);
`RSAResult`/`AllocationResult` (`status`, `placements`, `unplaced`).

- `best_feasible_mode(model, qot, oms_sequence, loading)` — one QoT call per direction,
  worse GSNR, highest-bitrate mode under threshold (or `None`) + GSNR.
- `solve_rsa(model, qot, demands, objective="shortest", constraints=None)` — route by
  length, first-fit slot, mode from SNR; protected demands via `compute_disjoint_paths`
  (basis/level from constraints, default `physical`/`link`). Run-local working spectrum
  state. Typed status.
- `solve_allocation(model, qot, demands, spare_inventory, objective="max_placed",
  weights=None)` — greenfield heuristic: per-site transponder counts, weighted ordering,
  inner `solve_rsa` routine, place iff single-lightpath best mode `bitrate ≥ demand`;
  protected → 2 disjoint lightpaths + 4 transponders; fall back / mark unplaced, never
  abort.

### 4. Serializers + tools — `model/views.py`, `server.py`
- `feasibility_result_dict`, `rsa_result_dict`, `allocation_result_dict`.
- `@app.tool()` `check_spectrum_feasibility`, `solve_rsa`, `solve_allocation`.

## Testing (TDD — test first)

- `tests/model/test_spectrum.py` — grid round-trip; `build_spectrum_state`; bitmask
  first-fit; typed clash naming the OMS.
- `tests/gnpy_adapter/test_toy_2route.py` — topology loads (advanced amp); finite GSNR on
  each route; longer route → lower GSNR.
- `tests/model/test_rsa.py` (fake `QotEvaluator` for combinatorics) — disjoint routes
  share a slot; shared OMS → different slots; mode-from-SNR picks highest feasible /
  rejects when no threshold clears; protected pair; **both bases** (`physical` succeeds,
  `srlg` over the same routes → `no_solution` once an SRLG spans both); **one real-GNPy
  integration case** on `toy_2route.json`.
- `tests/model/test_allocation.py` — greenfield consumes transponders + spectrum; scarce
  inventory → `partial`, no exception; protected → 4 transponders + 2 disjoint lightpaths;
  known-feasible → *a* feasible placement (no optimality assertion).
- `tests/test_server_phase4_rsa.py` — three tools end-to-end through FastMCP.

## Verification

1. `python -m pytest -q` — existing 114 + new all green.
2. Determinism: `test_rsa.py`/`test_allocation.py` twice → identical placements.
3. No solver raises on infeasible / resource-exhausted inputs.
4. Spectrum feasibility is bitmask ops over the path's OMS, independent of channel count.

## Out of scope (Step 5 / Step 7)

- **Grooming** + the **layered IP+optical multilayer-graph** routing (Step 5).
  Disjointness is **not** deferred.
- `provision_lightpath`/`teardown_lightpath` and the validate/commit gate (Step 7).
