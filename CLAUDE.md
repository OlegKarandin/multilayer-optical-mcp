# multilayer-optical-mcp — Multi-Layer Optical Network Reasoning Server

Package/repo name: `multilayer-optical-mcp`. GNPy is one component (the physical-
layer QoT adapter), not the whole server — the name reflects the full multi-layer
surface (IP-over-optical, routing, risk groups, what-if), not just the GNPy wrapper.

An MCP server that exposes multi-layer optical network analysis as a clean set of
callable tools. It wraps GNPy for physical-layer quality-of-transmission (QoT)
evaluation and adds the primitives an agent (or a human operator) needs to ask
**what-if** questions, route lightpaths, reason about shared-risk groups, and
validate change plans before committing them.

This repo is **disaster-agnostic infrastructure**. It knows nothing about storms,
floods, or weather feeds. It knows about topologies, lightpaths, services, risk
groups (as abstract asset partitions), QoT, and plans. Event interpretation —
turning a storm polygon into a risk group — lives in a separate application that
*depends on* this server. Keeping that boundary is a hard design rule, not a
preference: it is what makes this server reusable for defrag, capacity planning,
maintenance-window analysis, and other agents besides the disaster demo.

---

## What this is for

The hard combinatorial and physical work in optical networking — QoT estimation,
routing, spectrum assignment, disjoint-path computation — is deterministic and
already solved by mature tools. This server does **not** reinvent any of it and
makes **no novelty claim**. Its value is packaging: a single, well-typed MCP tool
surface over GNPy and standard graph/RSA algorithms, plus two primitives that are
usually hand-rolled and missing from off-the-shelf stacks:

1. **What-if analysis** — two distinct questions: a physics-free *screening* sweep
   ("which lightpaths sit within 3 dB of threshold right now?") and impairment-based
   degradation ("inject this NF/loss, recompute, report threshold crossings"). Margin
   is an *output*, never a dial — see the what-if tool group.
2. **Dynamic risk groups** — runtime-injected asset partitions (beyond static,
   design-time SRLGs) that routing and disjointness can be tested against.

Everything is deterministic and independently testable against GNPy ground truth
and solver output, with no LLM in the loop. An agent calls these tools; it does
not replace them.

---

## Core design rules (do not violate)

- **Read vs. mutate are strictly separated.** Read tools never change state.
  Mutating tools never commit to a live network without passing validation first.
- **Every mutation is simulatable before commit.** `validate_plan` must precede
  `commit_plan`. A live commit sits behind an explicit human-approval gate.
- **All tool results are structured.** Violations come back as typed lists, never
  prose. No tool returns "looks fine."
- **No solving inside the LLM.** k-shortest paths, RSA/RWA, disjoint paths, and
  heuristic resource allocation are deterministic functions the agent *calls*. They
  are never delegated to a language model.
- **IP link capacity is derived, never stored.** Capacity is a computed property
  of the underlying lightpath's transceiver mode (`mode → line rate`). There is no
  `set_capacity` tool. A modulation downshift changes the mode and capacity falls
  out automatically. The instant capacity becomes an independently-settable field,
  the two layers drift and `simulate_ip_routing` returns confident wrong numbers.
- **Mode feasibility is gated by margin; capacity is zero when margin is negative.**
  Capacity = f(mode) **only when margin ≥ 0**. Capacity falls out of mode, but mode
  does *not* fall out of GSNR anywhere unless this rule forces it: if recomputed
  margin for a lightpath goes negative (e.g. transient loading pushed GSNR below the
  mode's required threshold), the link is **down** (capacity 0), not running at
  nominal line rate. Without this, `simulate_ip_routing` reads nominal capacity off
  a mode the optical layer can no longer support — the confident-wrong-number
  failure the derived-capacity rule guards against, one layer up. Every loading
  recompute re-evaluates this flag.
- **State is addressed by explicit snapshot IDs, and branches are bounded.** Agents
  branch, mutate, compare, commit only the chosen branch; ground truth is never
  mutated during exploration. Branches are an explicit resource: capped in number
  and/or reaped on a TTL. A long what-if session that opens hundreds of branches of
  an in-memory multilayer model is an unbounded-memory footgun in exactly this
  server's intended workload, so branch lifecycle is part of the contract, not an
  afterthought.
- **Solver outcomes are typed, never exceptions.** Every solver returns a typed
  status: `solution` (with the result), `no_solution` (infeasible — e.g. no disjoint
  pair, or demands that don't fit), or `partial` (a heuristic that placed some but
  not all demands, returning what it placed and what it couldn't). A heuristic that
  hits its iteration/time budget returns its best result so far as `partial` or
  `solution`, never a thrown error — the agent must always receive structured data
  where the contract promises it. Heuristics are not exact optimizers and make no
  optimality claim; the contract is "a feasible result or an honest `no_solution`,"
  not "the optimum."
- **No event/geo/weather logic in this repo.** No `map_geo_event_to_assets`, no
  forecast ingestion. Risk groups arrive as asset lists via `define_risk_group`.
  If you find yourself adding a weather dependency, it belongs in the downstream
  app, not here.

---

## Architecture

```
                    MCP client (any agent, or a human via a host app)
                                    |
                          MCP tool surface (this server)
                                    |
        +---------------+-----------+-----------+----------------+
        |               |                       |                |
   State engine     GNPy adapter           Solvers          Validator
   (snapshot/        (QoT under            (k-shortest,      (typed
    branch/diff)      loading, spectrum     RSA, disjoint,    violation
                      feasibility)          heuristic alloc)  lists)
                                    |
                          Network model (in-memory)
                  IP layer over optical layer; services;
                  static SRLGs + dynamic risk groups
```

The **network model** is the single source of truth: an IP layer over an optical
layer, plus a service inventory, static SRLGs, and a mutable risk-group registry.

The **GNPy adapter** is the only component that talks to GNPy. It exposes QoT
evaluation as a pure forward function over an **arbitrary loading state**. Two
contract decisions are baked in here, at the adapter, not discovered later:

- **A loading state is a first-class input, not "the current network."** QoT is
  computed for *any* channel set you hand it — including one that is not, and never
  was, committed. A make-before-break migration is a *sequence* of loading states,
  and its worst state (old set ∪ new set, both lit on a span) is an intermediate
  that never gets committed yet must be evaluated *before* commit. So the adapter
  takes `loading` as a constructed set (e.g. `current ∪ {new_channel}`), computes
  the GN model for it, and never requires you to provision a channel to ask about
  it. Building the adapter around a before/after *pair* instead is the single
  re-architecture this design exists to avoid — see Build order step 2.
- **QoT is per-direction.** Asymmetric degradation is physically real — a bidirectional
  OMS is two physically distinct fiber runs with independent amp/fiber chains (see
  `topology_import.py`'s `_add_directed_oms`), so e.g. a construction dig-in or a
  splice fault can damage one direction and not its reverse. `compute_qot` takes a
  direction; a lightpath's usable mode is gated by the *worse* direction.
  **Storm/heatwave degradation, by contrast, is modeled symmetric** — the disaster
  consumer applies `inject_degradation` equally to both directions of an affected
  fiber, since a storm cell or a heat event degrades a physical span, not a single
  strand (see CLAUDE-disaster.md). The per-direction contract stays load-bearing for
  the asymmetric failure modes above, not as a storm-specific requirement. Note
  GNPy's `path_request_run` does forward+backward propagation in one pass, so
  per-direction QoT likely means driving propagation per direction with
  per-direction span parameters rather than getting it free — verify against your
  pinned GNPy version (see Requirements).

Given a path, direction, transceiver mode, fixed per-span operating points, and a
loading state, the adapter returns GSNR/OSNR/margin. **No power/tilt optimization**
is in scope; this server models physical feasibility, it does not autodesign the
line system.

---

## The IP-over-optical model

Two layers stacked is not the point; the **coupling** between them is. An IP link
is not an abstract edge — it *is* a lightpath, and its capacity is a function of
that lightpath's transceiver mode, which is a function of the GSNR the optical
layer delivers. A physical-layer change (degradation, reroute onto a longer path,
a downshift) can shrink an IP link, which changes IP routing feasibility, which
changes which demands fit. That feedback is the reason the cross-layer scenarios
are non-trivial. Model the layers as independent and you get two simulators that
share names and produce plausible wrong answers on every cross-layer question.

**Optical layer:** ROADMs, fibers, spans, amplifiers; lightpaths as paths through
it with a transceiver mode and current channel loading.

**IP layer:** routers as nodes; links as edges, where **each link is bound to an
underlying lightpath**; a traffic/demand matrix; IGP/SR-TE routing producing
per-link utilization, congestion, and dropped traffic when demand exceeds capacity.

**Three couplings that must be represented explicitly:**

1. **IP link = lightpath (bidirectional binding).** Given an IP link, find its
   lightpath; given a lightpath, find the IP link(s) it serves. Provisioning a
   lightpath brings its IP link up; tearing one down brings it down.
2. **Capacity = f(mode).** IP link capacity is *read from* the transceiver mode
   table, not stored. `set_modulation_format` 16QAM→QPSK halves the line rate, and
   the bound IP link's capacity must halve as an automatic consequence — see the
   derived-capacity design rule.
3. **Grooming map: IP demand → lightpath.** Which demands ride which lightpaths.
   This is what makes "which services are affected if this lightpath degrades" and
   "can I groom this demand onto survivors" answerable. Without it the IP-reroute
   and groom-away restoration options have nothing to operate on.

The IP layer itself is standard graph work (NetworkX). The entire difficulty is
keeping it consistent with the optical layer through these three couplings.

---

## Disjointness

Disjointness is a modelling concept, not just a solver call, and the description of
it carries the central thesis of the downstream scenarios. Three distinctions the
tools must respect:

**1. Disjointness is relative to a named basis.** A pair of paths can be disjoint
w.r.t. one partition and correlated w.r.t. another — that is the whole point of
scenario 1 (SRLG-disjoint but both aerial inside a storm cone). Every disjointness
query names its basis:
- `physical` — no shared fiber/span.
- `srlg` — no shared static, design-time group.
- `risk_group` — no shared dynamic, runtime-injected group.
- `union` — disjoint w.r.t. the union of selected bases.
The interesting operation is testing the *same* pair against *multiple* bases; a
pair certified disjoint against SRLGs at design time is the input, and a newly
injected risk group is the basis that exposes the latent correlation.

**2. Degree of disjointness is not binary.** `node`, `link`/`span`, `srlg`, and
`risk_group` levels are different requests. When a hazard has swallowed half the
safe routes, *fully* disjoint may not exist and the agent needs a **best-effort /
maximally-disjoint** result (minimum shared risk) rather than failure — which is
exactly the degraded-restoration situation.

**3. Computation is distinct from verification.** *Finding* a disjoint pair
(routing) and *auditing* whether an already-deployed working/protection pair is
still disjoint w.r.t. a partition that didn't exist when it was certified are
different operations. The audit direction is the one that catches the design-time
miss; it is first-class, not a side effect of the compute tool.

---

## Tool surface

Group tools by capability. Names are stable contract; argument schemas are typed.

### State (read-only)
- `get_topology(layer)` — IP, optical, or both.
- `get_lightpaths()` — active lightpaths with path, mode, loading.
- `get_services()` — services with working/protection paths and grooming.
- `get_traffic_matrix()` — IP demand matrix.
- `list_srlgs()` / `get_srlg_members(id)` — static, design-time groups.

### IP-over-optical (read + reverse lookups)
- `get_ip_topology()` — routers and links; each link annotated with its underlying
  lightpath id, capacity (derived from mode), and current load.
- `get_grooming_map()` — IP demand → lightpath assignments.
- `get_affected_services(lightpath_or_asset)` — reverse lookup: which demands/
  services ride a given lightpath, or any lightpath crossing a given asset. The
  workhorse for every restoration scenario.

### State management (snapshots)
- `snapshot_create()` → id
- `snapshot_branch(id)` → id — copy-on-write branch for exploration.
- `snapshot_restore(id)` — roll back to a known-good state.
- `snapshot_diff(a, b)` — structured delta between two states.

### Physical layer (GNPy adapter)
- `compute_qot(path, direction, mode, loading_state)` → `{gsnr, osnr, margin,
  mode_feasible}` — `loading_state` is an arbitrary constructed channel set, not
  "current"; `direction` is required; `mode_feasible` is the margin ≥ 0 gate.
- `recompute_qot_under_loading(loading_state)` — QoT for all lightpaths in a given
  loading state (channels added/dropped change NLI). Accepts a constructed set,
  including the make-before-break overlap (old ∪ new), so transient states are
  evaluable without provisioning. The load-bearing call — see Risks.
- `check_spectrum_feasibility(path, slot_width, center_freq)` → feasibility +
  clash details.
- `get_transceiver_modes()` → table of `(mode → required GSNR, line rate)`.

### Risk groups (the differentiating primitive)
- `define_risk_group(asset_list, metadata)` → rg_id — runtime partition, beyond
  static SRLGs. Takes an **asset list**, never an event or geometry.
- `list_risk_groups()` / `get_risk_group(id)`
- `get_exposure(service, risk_group)` → whether working **and** protection both
  intersect the group (the design-time-disjoint-but-now-correlated case).

### Routing & planning (deterministic solvers)
- `compute_paths(src, dst, k, constraints)` — k-shortest under constraints.
- `compute_disjoint_paths(src, dst, basis, level, best_effort)` — find a disjoint
  pair. `basis ∈ {physical, srlg, risk_group, union}` (with the specific groups when
  not physical); `level ∈ {node, link, srlg, risk_group}`; `best_effort=true`
  returns the maximally-disjoint pair when full disjointness is infeasible. With
  `best_effort=false` and no disjoint pair, returns a typed `no_solution`, not an
  error.
- `check_disjointness(path_a, path_b, basis, level)` — the audit primitive: verify
  whether two *existing* paths are disjoint w.r.t. a given basis. Returns the shared
  assets/groups when they are not. This is the scenario-1 catch — re-checking a
  deployed working/protection pair against a freshly injected risk group.
- `solve_rsa(demands, objective, constraints)` — routing + spectrum assignment.
- `solve_allocation(demands, spare_inventory, weights)` — **heuristic**
  allocation for the joint case where many services contend for scarce transponders/
  spectrum. Operates over a multi-layer graph (IP demands routed over a layered
  IP-plus-optical graph whose edges carry capacity and spectrum constraints);
  typical approach is weighted sequential placement — order demands by the agent's
  weights, route each over the layered graph by shortest available path, fall back
  to the next option when an edge is exhausted. Returns a typed status (`solution` /
  `partial` with placed-vs-unplaced demands / `no_solution`). Not an exact
  optimizer; makes no optimality claim. A budget overrun yields the best result so
  far as structured data, never an exception.
- `simulate_ip_routing(state)` → `{utilizations, congestion, dropped}` — reads
  capacities derived from modes, so a downshift on a branch correctly surfaces as
  reduced capacity and possible congestion.
- `evaluate_objective(state, weights)` → cost vector with optical and IP terms:
  `{spectrum_used, transponders, max_util, dropped_traffic, added_latency,
  total_margin, services_at_risk}`. These IP terms are what the cross-layer
  weighting trades against transponder/spectrum cost.

### What-if analysis (the headline feature)
Margin is an **output**, never an input. The same dB of impairment moves margin
differently on a short well-margined span and a long marginal one, so "dial margin
down 3 dB" is not a physical operation. Two honest tools, for two different
questions:
- `whatif_margin_threshold_sweep(threshold_db)` → lightpaths whose *current* margin
  sits within `threshold_db` of zero. Deliberately physics-free **screening/triage**
  ("what is fragile right now"). Makes no causal claim and models no degradation.
- `inject_degradation(asset, delta)` — apply `{nf:+x, loss:+y}` on a branch, then
  `recompute_qot_under_loading` and report threshold crossings. This is the
  physically-grounded degradation path: perturb the *impairment*, let margin move as
  a consequence. Heat, aging, and any modeled impairment feed here.
- `inject_failure(asset_set)` — mark assets failed on a branch.
- `whatif_sensitivity(state_a, state_b, path, direction, mode, loading)` — diff
  per-element QoT contribution between two branches (typically a nominal baseline and
  one with `inject_degradation` applied). Isolates which physical asset's own
  contribution to GSNR/OSNR changed, not the cumulative post-element figure (which
  echoes the same root cause at every downstream element). Read-only; mutates neither
  branch.

### Validate & commit (mutating, gated)
- `validate_plan(plan)` → typed `violations[]`: QoT, spectrum clash, optical
  capacity, **mode infeasibility** (a lightpath whose margin goes negative in any
  intermediate state, so its link is down at nominal mode), **disjointness collapse**
  (a committed plan whose working and protection paths share a fiber/SRLG/risk-group
  under the required basis), **IP link overload** (post-change utilization > derived
  capacity), **dropped traffic above tolerance**, and **transient overload** during
  make-before-break (old and new lightpaths both loading a fiber while a demand is
  mid-migration). Validation checks **every intermediate state of a sequence**, not
  just endpoints. Must run before any commit.
- `provision_lightpath(spec)` / `teardown_lightpath(id)` — also flip the bound IP
  link up/down.
- `reroute_service(service, ip_path)` — move an IP demand onto a different IP path
  over survivors (the IP-reroute restoration option). Distinct from optical reroute
  (which changes a lightpath's optical path).
- `set_modulation_format(transponder, mode)` — changes the mode; the bound IP
  link's capacity propagates automatically through the model (no separate edit).
- `commit_plan(plan, dry_run)` — `dry_run=true` simulates; live commit is
  approval-gated and requires a prior successful `validate_plan`.
- `reconcile()` → typed `drift[]` — after a live commit, re-read actual network
  state and diff against intended state. A live commit hands actuation to an
  external control plane that can **partially fail** (three of five lightpaths
  provision, the fourth times out), leaving reality matching neither the prior
  snapshot nor the target. The server is control-plane-agnostic but **cannot be
  reconciliation-agnostic** — model-vs-reality divergence is its problem regardless
  of which control plane caused it. The commit path is not write-only; it reads
  back. Drift surfaces as typed violations, not prose.

---

## Build order

1. **Network model + state engine.** In-memory multilayer model with
   snapshot/branch/diff. Get copy-on-write right first; everything else mutates
   through it.
2. **GNPy adapter — with the full loading contract from day one.** Wire
   `compute_qot` and prove correct GSNR on a toy 2–3 span topology. Then prove the
   two contract properties that are expensive to retrofit:
   (a) **arbitrary loading state** — construct a channel set that is a *superset* of
   what is provisioned (the make-before-break overlap, old ∪ new) and confirm the
   adapter computes QoT for it **without provisioning the extra channel first**. If
   it can, the transient case at step 7 is free; if it can't, you found out on day
   one instead of re-architecting the loading model late.
   (b) **per-direction QoT** — confirm you can evaluate one fiber direction degraded
   and the other not. This is the load-bearing integration and the riskiest; do it,
   in full, before building anything on top. Pin the GNPy version here (see
   Requirements) so ground-truth tests don't drift on upgrade.
3. **Read tools + risk groups.** `get_*`, SRLGs, `define_risk_group`,
   `get_exposure`. Cheap and unblock the demo's exposure logic.
4. **Solvers.** Wrap k-shortest, disjoint-path, RSA; add the heuristic
   `solve_allocation` (multi-layer-graph sequential placement) last.
5. **IP-over-optical layer.** Build the IP model and the three couplings first —
   no tools yet, just the consistent data model plus `simulate_ip_routing`. Prove a
   downshift on a branch propagates to reduced IP capacity and shows congestion.
   *Then* expose the IP tools (`get_ip_topology`, `get_grooming_map`,
   `get_affected_services`, `reroute_service`). Prove the coupling before exposing
   the tools, same discipline as proving `recompute_qot_under_loading` before
   building on the adapter.
6. **What-if + injection.** `whatif_margin_delta`, `inject_degradation`,
   `inject_failure` on branches.
7. **Validate + commit + reconcile.** Typed violation list (optical + IP), checking
   every intermediate state of a sequence, not just endpoints — the transient
   overlap proven evaluable at step 2 is checked here. Then the gated mutation tools,
   and `reconcile()` to read back actual state after a live commit and surface drift
   from partial failures. The commit path reads back; it is not write-only.

Ship a runnable zero-hardware demo (toy topology, seeded, deterministic) as early
as step 3 — it is what makes the repo legible and star-worthy. Real-hardware
commit paths can be stubbed behind the same tool contract.

---

## Testing

- **Deterministic, seedable replay.** Same inputs → same outputs, always.
- **GNPy ground-truth tests.** Adapter outputs checked against GNPy run directly.
- **Solver tests.** Routing/RSA against small instances with known correct paths;
  the heuristic allocator against instances where a feasible placement is known to
  exist (assert it finds *a* feasible result, not the optimum) and instances known
  to be infeasible (assert it returns `no_solution`, not a wrong placement).
- **Layer-consistency tests.** A mode change must propagate to IP capacity; a
  lightpath teardown must bring its IP link down; `simulate_ip_routing` must reflect
  both. Critically: a loading change that pushes a lightpath's margin negative must
  drop its link to capacity 0 (the margin-feasibility gate), *not* leave it at
  nominal line rate. Test the couplings directly, not just each layer in isolation.
- **Reconcile test.** Simulate a partial commit failure (subset of ops succeed) and
  confirm `reconcile()` surfaces the model-vs-reality drift as typed violations.
- **No LLM in any test.** The entire server is testable without a model; the agent
  is a separate concern in a separate repo.

---

## Requirements (resolved — bake these in, don't rediscover)

- **Reference topologies must use GNPy's advanced/explicit amplifier model.** The
  advanced model takes NF and ripple from config, so `inject_degradation(nf:+x)`
  works. The simplified `variable_gain` model derives NF from
  `nf_min`/`nf_max`/`gain_flatmax` and will **silently no-op** on a per-instance NF
  delta — the heatwave scenario would appear to run and change nothing. Either use
  the advanced model in reference topologies, or make `inject_degradation` rewrite
  the amp config rather than pass a delta. Do not rely on a bare NF delta against a
  `variable_gain` amp.
- **Pin the GNPy version in ground-truth tests.** GNPy's Raman solver and some
  amplifier models involve numerical optimization that is deterministic given a seed
  but sensitive to solver tolerances across versions. Pin the version or the
  ground-truth tests drift on upgrade.
- **Loading state and per-direction QoT are adapter-contract requirements**, proven
  at Build order step 2, not deferred. (See the GNPy adapter section.)

## Risks / still genuinely open

- **`recompute_qot_under_loading` correctness under constructed loading.** Confirm
  GNPy returns correct GSNR for a loading set you built by hand (not one it derived
  from a committed network), on a topology you control. If GNPy only does single-shot
  propagation tied to a request, you may need to drive it per loading-scenario.
- **Transient/quasi-static gap.** The GN model is quasi-static and won't capture
  EDFA power excursions during a switch. `validate_plan` cannot certify the
  switching instant; carry margin or sequence to minimize simultaneous changes,
  and document that QoT is invalid for the transient window.
- **Layers desync in the make-before-break window.** While both old and new
  lightpaths exist, both load the fiber (changing survivors' QoT) and the migrating
  demand may be on neither, both, or split across them depending on sequencing.
  `validate_plan` must check IP *and* optical at every intermediate state — handled
  by the arbitrary-loading-state contract (step 2) plus per-intermediate-state
  validation (step 7), but verify the two compose correctly on a real 3-state
  sequence.

---

## Explicitly out of scope

- Physical-layer optimization (power/tilt/launch-power tuning).
- Weather, geo, or any event interpretation. (Belongs in the downstream app.)
- **Control-plane signalling.** The server is control-plane-agnostic: it produces
  validated plans and hands actuation to whatever control plane the operator runs
  (GMPLS/RSVP-TE, OpenROADM/NETCONF, vendor SDN), behind `commit_plan`. It does not
  model distributed signalling, label distribution, or crankback. It borrows the
  SRLG *concept* from that world without adopting the protocol.
- Any claim of research novelty. This is reusable integration infrastructure.
