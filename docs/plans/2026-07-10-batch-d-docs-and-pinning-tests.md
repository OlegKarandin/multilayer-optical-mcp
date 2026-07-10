# Batch D — Documentation & Pinning-Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land Part 3 ("Batch D") of
`docs/plans/2026-07-07-fix-roadmap-correctness-then-optimizations.md`: lock in the
remaining `docs/inspection-roadmap.md` findings that are documentation-only (no behavior
change) as docstrings/comments at their exact code sites, plus the one real pinning test
(S2-1). Parts 1 and 2 of that master plan (all correctness + optimization batches, C1–C9,
O1–O3) are already landed on `master` — confirmed via `git log`.

**Architecture:** No production logic changes. Each task touches one file (or a tightly
related pair), adding a docstring/comment at the exact site the roadmap finding names, and
is verified by running that file's existing test suite (must stay green — a comment-only
change should never move behavior) plus, where noted, the ground-truth bridge test as a
safety net.

**Tech Stack:** Python, pytest, existing repo conventions (conda env
`multilayer-optical-mcp`).

## Global Constraints

- Every command runs via `conda run -n multilayer-optical-mcp pytest ...` (project
  convention — see the `project-conda-env` memory).
- One commit per task, commit message format `docs(<scope>): <summary> (<finding ids>)`.
- No physics/behavior changes in this batch. If any edit looks like it would change
  computed output, stop and flag it — it does not belong in Batch D.
- Findings already resolved by earlier landed batches must NOT be re-litigated here; where
  the roadmap's Batch D line said "check first" (S7-9), the check is done below (see Task
  6) and the finding is confirmed still-open as a documentation item.

---

## Pre-flight: confirm baseline is green

- [ ] **Step 1: Run the full suite before making any changes**

Run: `conda run -n multilayer-optical-mcp pytest -q`
Expected: all tests pass (this is the baseline; Batch D must not regress it).

---

### Task 1: S2-1 — pin the adjacent-channel `union` invariant

**Files:**
- Modify: `tests/gnpy_adapter/test_loading.py`

**Interfaces:**
- Consumes: `Channel`, `LoadingState` from `multilayer_optical_mcp.gnpy_adapter.loading`
  (unchanged; no production code touched).

No code fix needed — the roadmap confirms `LoadingState.union`'s strict-`<` overlap
predicate already allows touching (non-overlapping) channels. This task only adds the
regression test that pins the invariant explicitly, per the roadmap's own ACTION text: *"a
future change relaxing `<` to `<=` would silently break 50 GHz-spaced grids."*

- [ ] **Step 1: Add the pinning test**

Current end of file (`tests/gnpy_adapter/test_loading.py`):

```python
def test_union_rejects_spectrum_clash():
    a = LoadingState(channels=(Channel(193.4e12, 100e9, None, "400G@7.1dB"),))
    b = LoadingState(channels=(Channel(193.4e12, 100e9, None, "300G@4.8dB"),))
    with pytest.raises(ValueError, match="spectrum clash"):
        a.union(b)
```

Append:

```python


def test_union_allows_adjacent_channels():
    """S2-1: touching-but-not-overlapping channels (a.high_hz == b.low_hz) must
    union cleanly. The overlap predicate is strict '<' on both sides on purpose —
    relaxing it to '<=' would reject legitimate adjacent channels on a 50 GHz grid.
    Pinned here so that relaxation regresses loudly instead of silently."""
    a = LoadingState(channels=(Channel(193.40e12, 100e9, None, "400G@7.1dB"),))
    b = LoadingState(channels=(Channel(193.50e12, 100e9, None, "300G@4.8dB"),))
    assert a.channels[0].high_hz == b.channels[0].low_hz  # exactly touching
    merged = a.union(b)
    assert len(merged.channels) == 2
```

- [ ] **Step 2: Run it**

Run: `conda run -n multilayer-optical-mcp pytest tests/gnpy_adapter/test_loading.py -v`
Expected: 5 passed (the 4 existing + the new one), no code change required for it to pass.

- [ ] **Step 3: Commit**

```bash
git add tests/gnpy_adapter/test_loading.py
git commit -m "$(cat <<'EOF'
test(loading): pin strict-< adjacency in LoadingState.union (S2-1)

No behavior change — LoadingState.union already allows touching (a.high_hz ==
b.low_hz) channels. Pins the invariant so a future <= relaxation regresses
loudly instead of silently breaking 50 GHz-spaced grids.
EOF
)"
```

---

### Task 2: S1-8 + S5-6 — `network.py` docstrings

**Files:**
- Modify: `src/multilayer_optical_mcp/model/network.py`

**Interfaces:** none (docstrings only).

- [ ] **Step 1: Document `set_service_working_path` as a statement of intent (S1-8)**

Find:

```python
    def set_service_working_path(
        self, service_id: str, ip_path: Tuple[str, ...],
    ) -> None:
        from .ip_routing import is_contiguous_path
```

Replace with:

```python
    def set_service_working_path(
        self, service_id: str, ip_path: Tuple[str, ...],
    ) -> None:
        """Set the service's intended working path (validated for connectivity only).

        S1-8: this is a statement of INTENT, not a guarantee the path is up. It
        deliberately does not check link-up status (margin >= 0) — restoration
        and make-before-break planning must be able to pin a path before its
        lightpaths are provisioned, or while a link is degraded mid-migration.
        `simulate_ip_routing` is the place that reads actual capacity/margin and
        reports real drops; this method never does.
        """
        from .ip_routing import is_contiguous_path
```

- [ ] **Step 2: Document the per-direction collapse in `ip_link_capacity_gbps` (S5-6)**

Find:

```python
    def ip_link_capacity_gbps(self, link_id: str) -> float:
        link = self._ip_links[link_id]
        lp = self._lightpaths[link.lightpath_id]
        state = self._qot_state.get(lp.id)
        if state is None:
            raise LookupError(f"no QoT state recorded for lightpath {lp.id!r}")
        if state.margin_db < 0:
            return 0.0
        return self.modes.get(lp.mode_id).bitrate_gbps
```

Replace with:

```python
    def ip_link_capacity_gbps(self, link_id: str) -> float:
        """Derived IP link capacity: the bound lightpath's mode bitrate, gated to
        0 when margin < 0. Raises LookupError when no QoT has been recorded yet
        (caller must recompute first — see ip_routing.simulate_ip_routing's guard).

        S5-6: QoT is stored as a single QoTState per lightpath (the worse of
        forward/backward, per gated_qot's min), so a per-direction asymmetric
        degradation — CLAUDE.md's storm-damages-one-fiber-direction scenario —
        cannot manifest as a directional IP capacity change; the IP layer is
        undirected by construction. Documented as a known modeling boundary, not
        a bug: directional IP capacity would require a QoTState keyed by
        (lightpath, direction), which no current caller needs.
        """
        link = self._ip_links[link_id]
        lp = self._lightpaths[link.lightpath_id]
        state = self._qot_state.get(lp.id)
        if state is None:
            raise LookupError(f"no QoT state recorded for lightpath {lp.id!r}")
        if state.margin_db < 0:
            return 0.0
        return self.modes.get(lp.mode_id).bitrate_gbps
```

- [ ] **Step 3: Run the relevant tests**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_network.py tests/model/test_assets.py tests/model/test_capacity_coupling.py -q`
Expected: all pass, unchanged from baseline.

- [ ] **Step 4: Commit**

```bash
git add src/multilayer_optical_mcp/model/network.py
git commit -m "$(cat <<'EOF'
docs(model): document working-path intent and single-QoTState boundary (S1-8, S5-6)

set_service_working_path is a statement of intended routing, not a link-up
check — simulate_ip_routing is the place that reports real drops.
ip_link_capacity_gbps stores one QoTState per lightpath, so per-direction
optical asymmetry never reaches the IP layer; documented as a known modeling
boundary. No behavior change.
EOF
)"
```

---

### Task 3: S5-2, S5-7, S5-9 — `ip_routing.py` module + docstrings

**Files:**
- Modify: `src/multilayer_optical_mcp/model/ip_routing.py`

**Interfaces:** none (docstrings only).

- [ ] **Step 1: Extend the module docstring with the Stage 5 assumptions block**

Find:

```python
"""IP routing models and functions."""

from __future__ import annotations
```

Replace with:

```python
"""IP routing models and functions.

Coupling chokepoint: capacity is DERIVED from the bound lightpath's QoT-gated
mode (NetworkModel.ip_link_capacity_gbps), never stored — CLAUDE.md's
derived-capacity rule. `simulate_ip_routing` is a pure read: it never reroutes,
never mutates state, and (S5-4) never raises out of the MCP surface.

Stage 5 assumptions (recorded explicitly, from the inspection roadmap):
- Every IP link's lightpath is assumed to have a recorded QoT state; before the
  first `recompute_qot_under_loading`, `ip_link_capacity_gbps` raises
  LookupError and callers here treat that as capacity "unknown" (S5-4), never a
  crash.
- `working_path` is the only load-bearing path; `protection_path` is standby and
  contributes zero load.
- The IP layer is undirected: one capacity scalar per link, either-orientation
  traversal in `is_contiguous_path` (S5-6 — per-direction optical asymmetry
  never reaches this layer; see NetworkModel.ip_link_capacity_gbps).
- `offered_load_per_link`'s dict is keyed only by currently-existing IP links;
  pinned paths always reference live links (no `remove_ip_link` exists).
"""

from __future__ import annotations
```

- [ ] **Step 2: Document the `by_service` duplicate-LP-id choice (S5-2)**

Find:

```python
@dataclass(frozen=True)
class GroomingMap:
    """Bidirectional mapping between services and lightpaths.

    Attributes:
        by_service: Map from service id to tuple of lightpath ids on its working path.
        by_lightpath: Map from lightpath id to tuple of service ids using it (sorted).
    """

    by_service: Dict[str, Tuple[str, ...]]
    by_lightpath: Dict[str, Tuple[str, ...]]
```

Replace with:

```python
@dataclass(frozen=True)
class GroomingMap:
    """Bidirectional mapping between services and lightpaths.

    Attributes:
        by_service: Map from service id to tuple of lightpath ids on its working
            path, IN PATH ORDER, WITH DUPLICATES if the path crosses one
            lightpath twice (a routing loop). S5-2: chosen over dedup so
            by_service stays a faithful trace of the path, consistent with how
            offered_load_per_link sums demand per link traversal (also not
            deduped). by_lightpath (the reverse map) DOES dedup service ids,
            since "does this service use this lightpath" is boolean.
        by_lightpath: Map from lightpath id to tuple of service ids using it (sorted).
    """

    by_service: Dict[str, Tuple[str, ...]]
    by_lightpath: Dict[str, Tuple[str, ...]]
```

- [ ] **Step 3: Document `overflow_gbps` vs `dropped_services` non-summability (S5-9)**

Find:

```python
@dataclass(frozen=True)
class IPRoutingResult:
    utilizations: Tuple[LinkUtilization, ...]
    congested_links: Tuple[str, ...]      # utilization > 1, not down
    down_links: Tuple[str, ...]           # every down link (capacity 0), loaded or idle
    dropped_services: Tuple[DroppedService, ...]
    overflow_gbps: float                  # Σ max(0, offered-cap) over congested links
```

Replace with:

```python
@dataclass(frozen=True)
class IPRoutingResult:
    """S5-9: `overflow_gbps` (excess on congested links) and `dropped_services`
    (losses on down links) are computed from disjoint link SETS — a link is
    either congested or down, never both — but one SERVICE can appear in both:
    once via a congested link's overflow and again, via a different link on its
    path, in dropped_services. Do not sum the two as "total lost traffic": that
    double-counts any such service."""
    utilizations: Tuple[LinkUtilization, ...]
    congested_links: Tuple[str, ...]      # utilization > 1, not down
    down_links: Tuple[str, ...]           # every down link (capacity 0), loaded or idle
    dropped_services: Tuple[DroppedService, ...]
    overflow_gbps: float                  # Σ max(0, offered-cap) over congested links
```

- [ ] **Step 4: Document the failure→drop coupling and the C4-1 softening (S5-7)**

Find:

```python
def simulate_ip_routing(model: NetworkModel) -> IPRoutingResult:
    """Pure read: account pinned working_path demand onto IP links and report
    utilization, congestion, and drops. Routes nothing."""
```

Replace with:

```python
def simulate_ip_routing(model: NetworkModel) -> IPRoutingResult:
    """Pure read: account pinned working_path demand onto IP links and report
    utilization, congestion, and drops. Routes nothing.

    S5-7: this function never consults `model.failed_assets()` directly — it
    trusts that a QoT recompute has already left the right sentinel in
    `_qot_state`. Calling `model.mark_failed(...)` and never recomputing will
    NOT surface as a drop here (the stale feasible QoT is still on record);
    `whatif.inject_failure` writes the -inf sentinel immediately, which is why
    it is the documented entry point for failures. Since the C4-1 fix (S8-1),
    `recompute_qot_under_loading` also re-derives the sentinel from
    `model.failed_assets()` on every call regardless of whether
    `inject_failure` ever ran, so the gap is narrower than it once was: any
    recompute after a bare `mark_failed` self-heals it. But a bare
    `mark_failed` with no recompute at all still will not down anything here.
    """
```

- [ ] **Step 5: Run the relevant tests**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_ip_routing.py tests/model/test_layer_consistency.py tests/model/test_capacity_coupling.py -q`
Expected: all pass, unchanged from baseline.

- [ ] **Step 6: Commit**

```bash
git add src/multilayer_optical_mcp/model/ip_routing.py
git commit -m "$(cat <<'EOF'
docs(ip): record Stage 5 assumptions and the by_service/overflow/failure invariants (S5-2, S5-7, S5-9)

Module docstring gains the Stage 5 assumptions block. Documents: by_service
preserves path order with duplicates by design (S5-2); overflow_gbps and
dropped_services must not be summed, they can double-count one service
(S5-9); simulate_ip_routing never consults _failed_assets directly, so a bare
mark_failed() without a recompute is missed here, though any recompute since
the C4-1 fix now self-heals it (S5-7). No behavior change.
EOF
)"
```

---

### Task 4: S6-5, S6-7, S6-8, S6-9 — `solvers.py` module + docstrings

**Files:**
- Modify: `src/multilayer_optical_mcp/model/solvers.py`

**Interfaces:** none (docstrings only).

- [ ] **Step 1: Extend the module docstring with the Stage 6 assumptions block**

Find:

```python
"""Step-4 solvers: routing + disjointness over the optical OMS graph.

Deterministic, pure functions over the NetworkModel. Outcomes are typed
(`SolverStatus`); "no path" / "no disjoint pair" are typed results, never
raised exceptions (CLAUDE.md core design rule).

Routing is over an OMS graph: optical nodes are vertices, each OMS is an edge.
Parallel OMS between the same node pair are distinct routes, so candidate
enumeration walks node-simple-paths and expands the parallel-edge choices per
hop (node-level k-shortest alone would collapse parallel OMS into one route).
"""
```

Replace with:

```python
"""Step-4 solvers: routing + disjointness over the optical OMS graph.

Deterministic, pure functions over the NetworkModel. Outcomes are typed
(`SolverStatus`); "no path" / "no disjoint pair" are typed results, never
raised exceptions (CLAUDE.md core design rule).

Routing is over an OMS graph: optical nodes are vertices, each OMS is an edge.
Parallel OMS between the same node pair are distinct routes, so candidate
enumeration walks node-simple-paths and expands the parallel-edge choices per
hop (node-level k-shortest alone would collapse parallel OMS into one route).

Stage 6 assumptions (recorded explicitly, from the inspection roadmap):
- The OMS routing graph is DIRECTED (S6-4): one edge per OMS in its travel
  direction; a bidirectional span is two independent directed OMS/edges, so
  compute_paths(A,B) can never return the B->A OMS and compute_disjoint_paths
  can never return the two directions of one span as a "disjoint" pair.
- Avoidance is layer-agnostic per-OMS-edge pruning, applied twice
  (build_oms_graph and re-threaded through _oms_between) — the design's key
  correctness property; it holds because both filter on the same `forbidden`
  set (see S6-9 below).
- `avoid.assets` intersects the OMS asset set (oms id + fiber/amp/roadm
  elements) PLUS both endpoint nodes, so naming a ROADM id in avoid.assets
  prunes every OMS through it, not just one fiber at that site.
- Enumeration is deterministic: `_oms_between` sorts by (length, id) when
  weight="length", else by id.
- Disjointness keys are namespaced (`phys:`/`node:`/`srlg:`/`rg:`) so
  basis="union" never collides two different kinds of key.
"""
```

- [ ] **Step 2: Document the approximate length ordering (S6-5)**

Find:

```python
    """Yield up to `k` OMS-sequence routes src->dst, shortest first, expanding
    parallel OMS per hop. `weight="hops"` (default) orders by segment count;
    `weight="length"` orders by total fiber km (the routing objective for RSA,
    since reachable SNR tracks length). When *max_node_paths* is set, stop after
    that many distinct node paths have been expanded (parallels within them do
    not count toward the limit) — so a highly-parallel earlier node path cannot
    starve topological diversity in the disjoint-pair search."""
```

Replace with:

```python
    """Yield up to `k` OMS-sequence routes src->dst, shortest first, expanding
    parallel OMS per hop. `weight="hops"` (default) orders by segment count;
    `weight="length"` orders by total fiber km (the routing objective for RSA,
    since reachable SNR tracks length). When *max_node_paths* is set, stop after
    that many distinct node paths have been expanded (parallels within them do
    not count toward the limit) — so a highly-parallel earlier node path cannot
    starve topological diversity in the disjoint-pair search.

    S6-5: `weight="length"` is only APPROXIMATELY length-ordered, not a true
    k-shortest-by-km guarantee — the collapsed simple graph gives each hop the
    MINIMUM parallel-OMS length, so node paths are ranked by best-case
    parallel, and hop expansion then emits every parallel per hop in odometer
    order (not re-sorted by realized total length). `solve_rsa` relies on this
    ordering as a heuristic proxy for reachable SNR, not a certified guarantee;
    the first `k` results are not provably shortest-by-fiber-km."""
```

- [ ] **Step 3: Document the `oms_length_km`/forbidden-filter sync invariant (S6-9)**

Find:

```python
    simple = nx.DiGraph()
    simple.add_nodes_from(g.nodes)
    for u, v in g.edges():
        if by_length:
            w = min(oms_length_km(model, o) for o in _oms_between(model, u, v, forbidden=forbidden))
```

Replace with:

```python
    simple = nx.DiGraph()
    simple.add_nodes_from(g.nodes)
    for u, v in g.edges():
        if by_length:
            # S6-9: min() assumes _oms_between(u, v, forbidden=forbidden) is
            # non-empty for every edge g.edges() yields — true only because `g`
            # (from build_oms_graph) and _oms_between are filtered on the SAME
            # `forbidden` set. If the two filters ever diverge this raises
            # ValueError on an empty min() rather than silently misrouting;
            # that's intentional — a loud failure, not a defensive fallback.
            w = min(oms_length_km(model, o) for o in _oms_between(model, u, v, forbidden=forbidden))
```

- [ ] **Step 4: Document the `risk_groups` avoid-key misnomer (S6-7)**

Find:

```python
def _avoid_sets(constraints: Optional[dict]) -> Tuple[frozenset, frozenset]:
    """Extract (avoid_assets, avoid_risk_groups) from a constraints dict.
    Missing/empty -> empty sets (no pruning)."""
```

Replace with:

```python
def _avoid_sets(constraints: Optional[dict]) -> Tuple[frozenset, frozenset]:
    """Extract (avoid_assets, avoid_risk_groups) from a constraints dict.
    Missing/empty -> empty sets (no pruning).

    S6-7: despite the name, the `risk_groups` constraint key matches BOTH
    RiskGroup ids and static SRLG ids — see forbidden_oms, which iterates
    `list_srlgs() + list_risk_groups()`. An id collision between an SRLG and a
    RiskGroup expands both. Intentional (one avoid-key for "any named group"),
    not a bug — documented rather than split into two keys."""
```

- [ ] **Step 5: Document best-effort "minimum overlap" semantics (S6-8)**

Find:

```python
    """Find a disjoint pair src->dst under a basis/level. Returns the first
    fully-disjoint pair as SOLUTION; with best_effort=True returns the
    minimum-overlap pair as PARTIAL when no fully-disjoint pair exists; with
    best_effort=False and none disjoint, NO_SOLUTION. `weight` ∈ {"hops",
    "length"} orders candidate routes."""
```

Replace with:

```python
    """Find a disjoint pair src->dst under a basis/level. Returns the first
    fully-disjoint pair as SOLUTION; with best_effort=True returns the
    minimum-overlap pair as PARTIAL when no fully-disjoint pair exists; with
    best_effort=False and none disjoint, NO_SOLUTION. `weight` ∈ {"hops",
    "length"} orders candidate routes.

    S6-8: "minimum-overlap" (best_effort) minimizes the COUNT of shared
    namespaced keys (`len(shared)`), not physical severity — one shared SRLG
    (1 key) ranks better than two shared amps (2 keys) regardless of how many
    correlated physical assets the SRLG actually covers. Documented rather than
    weighted by asset count: the cap-32 candidate window (_DISJOINT_CANDIDATE_CAP)
    already makes an exact severity ranking unreliable, so a naive weighting
    would be false precision."""
```

- [ ] **Step 6: Run the relevant tests**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_solvers.py tests/model/test_avoidance.py -q`
Expected: all pass, unchanged from baseline.

- [ ] **Step 7: Commit**

```bash
git add src/multilayer_optical_mcp/model/solvers.py
git commit -m "$(cat <<'EOF'
docs(solvers): record Stage 6 assumptions and approximate-ordering/avoid-key invariants (S6-5, S6-7, S6-8, S6-9)

Module docstring gains the Stage 6 assumptions block. Documents:
weight="length" is an approximate (not certified) ordering RSA relies on
(S6-5); the oms_length_km min() depends on build_oms_graph and _oms_between
sharing one forbidden set (S6-9); the risk_groups avoid-key intentionally
matches both SRLG and RiskGroup ids (S6-7); best-effort disjoint overlap
minimizes shared-key count, not severity (S6-8). No behavior change.
EOF
)"
```

---

### Task 5: S7-9, S7-10, S7-12 — `multilayer_graph.py` module + docstrings

**Files:**
- Modify: `src/multilayer_optical_mcp/model/multilayer_graph.py`

**Interfaces:** none (docstrings/comments only).

**Pre-check already done (do not repeat):** the roadmap's Batch D line flagged S7-9 as
"retired if C7-7 lands first — check". C7-7 (= S2-4, per-format symbol rate) has landed,
but it only added the *plumbing* (`Channel.baud_rate_hz`/`roll_off`) — `allocation._build_loading`
(used by both `allocation.py` and `multilayer_graph.place_demands`) still constructs its
probe `Channel` without populating `baud_rate_hz`/`roll_off` from `ref_mode_id`, so the
probe falls back to `build_si_for_loading`'s scalar default regardless of which mode is
selected. **S7-9 is NOT retired** — document it as originally scoped.

- [ ] **Step 1: Extend the module docstring with the Stage 7 assumptions block**

Find (end of the module docstring):

```python
A path access(src) -> access(dst) that stays on existing lightpaths uses only
LPE edges (grooming). A path that dips via TxE -> WLEs (one lam) -> RxE realizes
a new lightpath on that wavelength. No CvtE: wavelength continuity is structural.
"""
```

Replace with:

```python
A path access(src) -> access(dst) that stays on existing lightpaths uses only
LPE edges (grooming). A path that dips via TxE -> WLEs (one lam) -> RxE realizes
a new lightpath on that wavelength. No CvtE: wavelength continuity is structural.

Stage 7 assumptions (recorded explicitly, from the inspection roadmap):
- `Router.site == optical-node id` is the src/dst -> optical-node resolution
  the restoration caller (`compute_restoration`) depends on.
- Wavelength continuity is structural (no cross-lambda edge): a new lightpath
  run is one lambda end-to-end, never converted mid-path.
- A returned candidate does NOT commit to a specific wavelength; provisioning
  must re-run spectrum assignment before actually lighting a new run.
- New-lightpath runs within ONE placement are assumed OMS-disjoint (S7-10):
  `_build_loading` QoTs each new run against the committed spectrum snapshot
  only, ignoring channels that OTHER new runs in the same placement would
  light. Harmless today because successive runs in one placement are
  separated by grooming hops on different OMS (no shared-fiber NLI between
  them) — an unstated precondition, not a proof.
- `build_layered_graph` and `place_demands` each call `build_spectrum_state`
  independently (S7-12): they agree today because both default to
  `SpectrumGrid.default()`, but passing a custom `grid` to one call and not
  the other would desync the WLE wavelength layers from the NLI loading comb.
  Thread one grid through both if that ever becomes a real caller pattern.
"""
```

- [ ] **Step 2: Document the ref-mode QoT probe inheritance (S7-9)**

Find:

```python
    spectrum = build_spectrum_state(model, grid)
    ref_mode = model.modes.list()[0].id
    out: List[Placement] = []
```

Replace with:

```python
    spectrum = build_spectrum_state(model, grid)
    # S7-9: every new-lightpath run in this placement is probed at the SAME
    # ref_mode (the registry's first mode), regardless of which mode
    # _best_feasible_mode later selects for it. Correct only while GSNR is
    # mode-independent given a fixed probe — true here because _build_loading
    # doesn't populate the probe Channel's baud_rate_hz/roll_off from
    # ref_mode_id (they fall back to build_si_for_loading's scalar default,
    # the S2-4 residual), so the probe's spectral shape doesn't vary with
    # ref_mode either. Would need to re-probe at the delivered mode if
    # per-format baud is ever threaded through this probe.
    ref_mode = model.modes.list()[0].id
    out: List[Placement] = []
```

- [ ] **Step 3: Document the OMS-disjoint-runs assumption at its use site (S7-10)**

Find:

```python
            realized: List[NewLightpathRun] = []
            feasible = True
            new_cap = float("inf")
            for oms_seq, lam, run_src, run_dst in new_runs:
                loading = _build_loading(grid, spectrum, oms_seq, lam, ref_mode)
```

Replace with:

```python
            realized: List[NewLightpathRun] = []
            feasible = True
            new_cap = float("inf")
            # S7-10: each new run is QoT'd against the committed `spectrum`
            # snapshot only — a run does not see the channels any OTHER new
            # run in this same placement would light on a shared OMS. See the
            # module docstring's Stage 7 assumptions (OMS-disjoint runs).
            for oms_seq, lam, run_src, run_dst in new_runs:
                loading = _build_loading(grid, spectrum, oms_seq, lam, ref_mode)
```

- [ ] **Step 4: Run the relevant tests**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_multilayer_graph.py tests/model/test_restoration.py tests/model/test_allocation.py -q`
Expected: all pass, unchanged from baseline.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/multilayer_graph.py
git commit -m "$(cat <<'EOF'
docs(multilayer-graph): record Stage 7 assumptions, ref-mode probe and OMS-disjoint-run invariants (S7-9, S7-10, S7-12)

Module docstring gains the Stage 7 assumptions block (Router.site convention,
structural wavelength continuity, candidate doesn't commit to a wavelength,
independent build_spectrum_state calls in build_layered_graph/place_demands
S7-12). Documents at their use sites: every new run in a placement is probed
at one ref_mode with no per-mode baud plumbed through yet, so S7-9 is
confirmed still-open post-S2-4, not retired; new runs within one placement
assume OMS-disjointness (S7-10). No behavior change.
EOF
)"
```

---

### Task 6: S7-11 — `restoration.py` cost_facets proxy semantics

**Files:**
- Modify: `src/multilayer_optical_mcp/model/restoration.py`

**Interfaces:** none (comment only).

- [ ] **Step 1: Document that `cost_facets` are proxies, not physical quantities**

Find:

```python
def _candidate(model: NetworkModel, p: Placement) -> RestorationCandidate:
    lever = _lever(p)
    cost = {
        "transponders": 2.0 * len(p.new_lightpaths),
        "new_lightpaths": float(len(p.new_lightpaths)),
        "hops": float(len(p.reused_lightpaths) + len(p.new_lightpaths)),
    }
```

Replace with:

```python
def _candidate(model: NetworkModel, p: Placement) -> RestorationCandidate:
    lever = _lever(p)
    # S7-11: cost_facets are PROXIES, not the named physical quantity. "hops"
    # is reused-plus-new LIGHTPATH count, not fiber/span hops; "transponders"
    # assumes every new lightpath needs exactly 2 (one per end) and ignores
    # any already-spare transponder inventory. compute_restoration sorts
    # candidates on these proxies (shortfall, transponders, hops) until
    # evaluate_objective's richer cost vector (CLAUDE.md) lands as the ranking
    # function instead.
    cost = {
        "transponders": 2.0 * len(p.new_lightpaths),
        "new_lightpaths": float(len(p.new_lightpaths)),
        "hops": float(len(p.reused_lightpaths) + len(p.new_lightpaths)),
    }
```

- [ ] **Step 2: Run the relevant tests**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_restoration.py -q`
Expected: all pass, unchanged from baseline.

- [ ] **Step 3: Commit**

```bash
git add src/multilayer_optical_mcp/model/restoration.py
git commit -m "$(cat <<'EOF'
docs(restoration): document cost_facets as ranking proxies, not physical units (S7-11)

hops = reused+new lightpath count, not fiber hops; transponders = 2x new
runs, ignoring spare inventory. Candidates are ordered on these proxies until
evaluate_objective replaces them. No behavior change.
EOF
)"
```

---

### Task 7: S3-2, S3-3 — `synthesize.py` hardcode documentation

**Files:**
- Modify: `src/multilayer_optical_mcp/gnpy_adapter/synthesize.py`

**Interfaces:** none (comments only).

- [ ] **Step 1: Document the flat `add_drop_osnr` hardcode (S3-2)**

Find:

```python
ROADM_TARGET_PCH_OUT_DB = -20.0
ROADM_ADD_DROP_OSNR = 33.0
```

Replace with:

```python
ROADM_TARGET_PCH_OUT_DB = -20.0
# S3-2: every synthesized ROADM gets this add/drop OSNR penalty regardless of
# hardware — there is no per-instance field on the `ROADM` dataclass to source
# a different value from (unlike target_pch_out_db, which IS per-instance;
# see S3-5 and model_to_gnpy_topology). A vendor ROADM with a genuinely
# different add/drop floor is not representable without adding that field to
# `ROADM` and threading it through synthesis the same way S3-5 did for
# target_pch_out_db.
ROADM_ADD_DROP_OSNR = 33.0
```

- [ ] **Step 2: Document the flat NF polynomial (S3-3)**

Find:

```python
def _adv_config_path(nf: float, tmpdir: Path) -> str:
    """Write an advanced_model NF config file and return its path string."""
    cfg = {
        "nf_fit_coeff": [0.0, 0.0, 0.0, float(nf)],
        # S3-10: the amp NF-fit band is one guard band wider than the SI channel
        # band on each edge (see bands.py). Derived, not a bare literal.
        "f_min": AMP_BAND.f_min_hz,
```

Replace with:

```python
def _adv_config_path(nf: float, tmpdir: Path) -> str:
    """Write an advanced_model NF config file and return its path string."""
    cfg = {
        # S3-3: a flat (degree-0) polynomial — NF is constant across gain, not
        # gain-dependent as on a real EDFA. Required shape for CLAUDE.md's
        # advanced_model requirement (nf_fit_coeff must exist so
        # inject_degradation's NF delta actually takes effect — see the
        # gnpy-nf-injection-advanced-model memory), but a simplification versus
        # a real per-amp gain-dependent NF curve.
        "nf_fit_coeff": [0.0, 0.0, 0.0, float(nf)],
        # S3-10: the amp NF-fit band is one guard band wider than the SI channel
        # band on each edge (see bands.py). Derived, not a bare literal.
        "f_min": AMP_BAND.f_min_hz,
```

- [ ] **Step 3: Run the relevant tests**

Run: `conda run -n multilayer-optical-mcp pytest tests/gnpy_adapter/test_synthesize.py tests/gnpy_adapter/test_ground_truth_bridge.py -q`
Expected: all pass, GSNR values unchanged (comment-only edit; this is a safety-net run, not
a re-pin — no literal changed).

- [ ] **Step 4: Commit**

```bash
git add src/multilayer_optical_mcp/gnpy_adapter/synthesize.py
git commit -m "$(cat <<'EOF'
docs(gnpy): document the flat add_drop_osnr and NF-polynomial hardcodes (S3-2, S3-3)

ROADM_ADD_DROP_OSNR has no per-instance source (unlike target_pch_out_db,
fixed in S3-5). nf_fit_coeff is a flat degree-0 polynomial, the shape
advanced_model injection requires but not gain-dependent like a real EDFA.
No behavior change; ground-truth bridge re-run as a safety net, values
unchanged.
EOF
)"
```

---

### Task 8: S3-add-4, S3-add-5 — `topology_import.py` module + importer assumptions

**Files:**
- Modify: `src/multilayer_optical_mcp/model/topology_import.py`

**Interfaces:** none (module docstring + comments only).

- [ ] **Step 1: Add a module docstring with the importer assumptions block**

Find (top of file):

```python
from __future__ import annotations

import math
from typing import Any, Dict, List


def split_link_into_spans(
```

Replace with:

```python
# src/multilayer_optical_mcp/model/topology_import.py
"""Model-build half of Phase 6a: NetworkModel <- an abstract node/edge graph.

Call order: model_from_abstract_graph(graph, modes) -> per node adds
roadm_/trx_/router_<id>; per edge _edge_spans (-> split_link_into_spans when
span_lengths_km is absent/inconsistent) then _add_directed_oms TWICE (both
directions), each emitting a booster + per-span (fiber, amp) + an OMS whose
elements start at roadm_<src>. The synthesis half (synthesize.py, adapter.py)
is Stage 3/4 of the inspection roadmap.

Importer assumptions (recorded explicitly, from the inspection roadmap):
- One ROADM/Transceiver/Router per node; roadm_<id> / trx_<id> / router_<id>
  naming. `Router.site == optical-node id` is the src_router -> optical-node
  convention Stage 7 restoration depends on.
- Every edge is bidirectional -> two independent directed OMS with independent
  amp chains (amp_<src>_<dst>_* vs amp_<dst>_<src>_*). The importer builds
  correct reverse-chain impairments, which the adapter (post-S4-2/A2 fix) now
  actually propagates into BACKWARD QoT.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List


def split_link_into_spans(
```

- [ ] **Step 2: Document `num_spans` as unread (S3-add-5)**

Find:

```python
def _edge_spans(edge: Dict[str, Any]) -> List[float]:
    spans = edge.get("span_lengths_km")
```

Replace with:

```python
def _edge_spans(edge: Dict[str, Any]) -> List[float]:
    """Resolve an edge's per-span lengths from span_lengths_km, or derive them
    from length_km via split_link_into_spans.

    S3-add-5: `edge["num_spans"]`, if present, is NEVER read or cross-checked
    against len(span_lengths_km) / the derived span count. A graph whose
    num_spans disagrees with the actual span count imports silently."""
    spans = edge.get("span_lengths_km")
```

- [ ] **Step 3: Document `Fiber.a_end`/`z_end` as decorative (S3-add-4)**

Find:

```python
        n.add_fiber(Fiber(id=fid, a_end=f"roadm_{src}" if i == 0 else f"amp_{src}_{dst}_{i-1}",
                          z_end=aid, length_km=float(span_km), type_variety=fiber_type))
```

Replace with:

```python
        # S3-add-4: a_end/z_end are DECORATIVE. They skip the booster (span
        # 0's true predecessor in `elements`) and synthesis wires connections
        # purely from OMS.elements order (synthesize.py), never from these
        # fields. Auditing physical adjacency from Fiber.a_end/z_end reads a
        # topology one hop off from what actually propagates.
        n.add_fiber(Fiber(id=fid, a_end=f"roadm_{src}" if i == 0 else f"amp_{src}_{dst}_{i-1}",
                          z_end=aid, length_km=float(span_km), type_variety=fiber_type))
```

- [ ] **Step 4: Run the relevant tests**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_topology_import.py -q`
Expected: all pass, unchanged from baseline.

- [ ] **Step 5: Commit**

```bash
git add src/multilayer_optical_mcp/model/topology_import.py
git commit -m "$(cat <<'EOF'
docs(import): record importer assumptions, decorative Fiber ends, unread num_spans (S3-add-4, S3-add-5)

Adds a module docstring with the Stage 3 addendum's importer assumptions
(roadm_/trx_/router_ naming, Router.site convention, bidirectional
independent-amp-chain OMS). Documents Fiber.a_end/z_end as decorative
(synthesis wires from OMS.elements order, not these fields) and
edge["num_spans"] as never read/cross-checked. No behavior change.
EOF
)"
```

---

### Task 9: Stage 8 assumptions — `whatif.py` module docstring

**Files:**
- Modify: `src/multilayer_optical_mcp/model/whatif.py`

**Interfaces:** none (docstring only).

- [ ] **Step 1: Extend the module docstring with the Stage 8 assumptions block**

Find:

```python
"""Branch-scoped what-if + injection.

Margin is an OUTPUT: the sweep screens recorded margin; inject_degradation
perturbs a physical parameter and lets margin move via recompute. None of these
mutate ground truth — callers pass a branch model.
"""
```

Replace with:

```python
"""Branch-scoped what-if + injection.

Margin is an OUTPUT: the sweep screens recorded margin; inject_degradation
perturbs a physical parameter and lets margin move via recompute. None of these
mutate ground truth — callers pass a branch model.

Stage 8 assumptions (recorded explicitly, from the inspection roadmap):
- Injection mutates the model's physical parameters in place on a BRANCH;
  correctness depends on snapshots._clone copying the mutated
  _amplifiers/_fibers AND _failed_assets, and diff() reporting failed_assets
  — verified present.
- `inject_failure`'s -inf QoT sentinel — not `_failed_assets` alone — is the
  mechanism that reaches `simulate_ip_routing` (see
  ip_routing.simulate_ip_routing's S5-7 docstring for how a bare
  model.mark_failed() without a subsequent recompute would be missed there).
- `margin_threshold_sweep` is a pure read; lightpaths with no recorded QoT are
  omitted, never defaulted. Margin is never an input anywhere in this module.
"""
```

- [ ] **Step 2: Run the relevant tests**

Run: `conda run -n multilayer-optical-mcp pytest tests/model/test_whatif.py tests/model/test_injection_layer_consistency.py tests/test_server_phase6.py -q`
Expected: all pass, unchanged from baseline.

- [ ] **Step 3: Commit**

```bash
git add src/multilayer_optical_mcp/model/whatif.py
git commit -m "$(cat <<'EOF'
docs(whatif): record Stage 8 assumptions in the module docstring

Branch-clone coupling to _failed_assets, the -inf-sentinel-is-load-bearing
mechanism (cross-referenced with the S5-7 docstring), and
margin_threshold_sweep's pure-read/no-default contract. No behavior change.
EOF
)"
```

---

## Final verification

- [ ] **Step 1: Run the full suite**

Run: `conda run -n multilayer-optical-mcp pytest -q`
Expected: same pass count as the pre-flight baseline (no regressions, no new failures) —
Batch D is documentation-only.

- [ ] **Step 2: Update the master plan's Batch D status**

In `docs/plans/2026-07-07-fix-roadmap-correctness-then-optimizations.md`, add a `STATUS`
note under "PART 3 — DOCUMENTATION & PINNING-TEST BATCH (Batch D)" recording it landed,
following the style of the existing `STATUS (2026-07-10): ...` notes on Batches C7–C9/O1–O3.

- [ ] **Step 3: Commit the status update**

```bash
git add docs/plans/2026-07-07-fix-roadmap-correctness-then-optimizations.md
git commit -m "$(cat <<'EOF'
docs(plan): mark Batch D landed (documentation + S2-1 pinning test)
EOF
)"
```
