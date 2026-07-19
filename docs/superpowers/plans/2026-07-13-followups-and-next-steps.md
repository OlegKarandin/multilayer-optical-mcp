# Follow-ups & next steps after route_service + evaluate_objective

Durable record of work deferred during the `route_service` + `evaluate_objective`
branch (merged to `master` as `ddc0fb4`, 2026-07-13, 359 tests passing). Replaces
the ephemeral `.git/sdd/progress.md` ledger for anything that outlives that branch.

Source plan: `docs/superpowers/plans/2026-07-13-route-service-and-evaluate-objective.md`
Design spec: `docs/superpowers/specs/2026-07-13-route-service-and-evaluate-objective-design.md`

---

## 1. Traffic / demand-matrix generation — the un-written sibling spec (NEXT)

The design spec ("Out of scope / deferred") and `docs/service-level-routing-findings.md` §6
both point to this as the immediate next work, "brainstormed next." **It was never
brainstormed or written** — no spec, plan, or note exists yet. This is the missing piece.

**What it is:** a seeded, deterministic demand synthesizer that emits `(src, dst, gbps)`
demands and feeds `solve_allocation` to produce the pre-disaster loaded steady state.

**Why it belongs here (not the downstream app):** disaster-agnostic — capacity planning
and defrag use it too. Its only interface is the demand list the packer already consumes,
so it's fully decoupled from this branch's work.

**Interface already in place (this branch):** `solve_allocation(model, qot, demands,
spare_inventory, weights)` consumes `demands` as `[{id, src, dst, demand_gbps,
protected?, constraints?}]` (src/dst = optical node ids). `weights` here = per-demand
priority (ordering), NOT cost-term weights. The synthesizer just has to emit that list.

**Open design questions for the brainstorm:**
- Demand model: uniform random pairs? gravity model (population/degree-weighted)? a fixed
  matrix scaled by a load factor? Seeded for determinism (repo rule: same seed → same output).
- Magnitude distribution for `gbps` (relative to transceiver line rates / mode table).
- How much to load: target a utilization level, a transponder budget, or "fill until first
  `no_solution`"?
- Protected fraction: what share of demands request protection.
- Determinism + a fixture demand set for tests (until then `solve_allocation` runs on
  hand-written fixture demands, as it does now in `tests/model/test_allocation.py` /
  `test_allocation_rebase.py`).

**Suggested first step:** run `superpowers:brainstorming` on this, then `writing-plans`.

---

## 2. Scoring ↔ commit QoT convergence (from the whole-branch review — real, pre-existing)

**Gap:** the branch's central claim is that candidate scoring (clone → `apply_op` → seed
QoT from the run's predicted `gsnr_db`) matches what a real commit produces. But
`model/commit.py`'s live path (`commit_plan`, `dry_run=False`) applies the ops via
`apply_op` and **never recomputes QoT on the live/intended model** — QoT recompute only
happens inside `validate_plan`'s discarded internal clone. So right after a live commit, a
newly-provisioned lightpath has **no** recorded QoT (its IP link reads as unknown/down
capacity via `ip_link_capacity_gbps`'s `LookupError` path) until some later
`recompute_qot_under_loading` / `validate_plan` runs — whereas scoring seeds QoT
immediately. **The two paths produce different numbers for the same freshly-committed
lightpath until a recompute fires.**

- **Pre-existing:** `commit.py` is untouched by this branch; the branch built the
  "scoring predicts commit" contract on top of it without closing this.
- **No test** compares `score_candidate` / `route_service` against an actual `commit_plan`
  result (only against its own clone steps — a structural, not empirical, guarantee).

**Fix options (pick one):**
- (a) Have `commit_plan`'s live path call `recompute_if_possible` on `current`/`intended`
  after actuation, so the model is left consistent with what validation/scoring assumed.
- (b) Add an integration test: real `commit_plan(dry_run=False, confirm=True)` + a follow-up
  recompute, asserting post-commit `evaluate_objective` == pre-commit `score_candidate`
  prediction. Document the interim gap if (a) isn't done.

---

## 3. DRY / layering polish (Minor — batch into a cleanup PR)

- **Harvest + λ-free dedup is duplicated** between `route_service._harvest` and
  `allocation._harvest_alloc` (same policies `groom_or_new`/`new_only`, same dedup key).
  Fold into one shared helper so the dedup key / `placement_materializable` filter can't
  drift between call sites. `compute_restoration` (now a wrapper) is already consistent.
- **Layering inversion:** `_forbidden_assets` / `_lever` live in `restoration.py` but
  `route_service.py` imports them, and `compute_restoration` imports `route_service` via a
  **function-local** import to dodge the cycle. Hoist `_forbidden_assets`/`_lever` into a
  neutral module (e.g. `multilayer_graph.py` or a small helpers module) so neither file
  depends on the other.
- **`_status` reimplemented 3 ways** (allocation / route_service / restoration) — same
  "any zero-shortfall → SOLUTION" semantics, three shapes. Share one helper.
- **`_harvest_alloc` hardcodes `k=8`** instead of reusing the module's `_ROUTE_CAP = 8`.
- **`solve_allocation`'s `objective: str` param is vestigial** (never read) — remove or wire.
- **`_stitch_ip_path` silent truncation** (`objective.py`) relies on a downstream
  `is_contiguous_path` raise to surface a broken walk; add a why-it's-safe comment (or a
  typed guard) since finding #1 showed "should not happen" placements can happen.
- **Per-demand `build_layered_graph(work)` rebuild** in `solve_allocation` is correct
  (loading changes each iteration) but wants a one-line "intentional, not hoistable" comment.

---

## 4. Test-coverage nits (Minor)

- `evaluate_objective`'s snapshot-by-id branch (`snapshots.get(state)`) is untested — only
  the `current()` branch is exercised.
- `route_service_result_dict`'s nested `RoutePair` leg (`working`/`protection`) contents are
  asserted only implicitly (the shape test fabricates non-empty leg `cost_vector`s, whereas
  real `route_service` emits `{}` for legs).
- `evaluate_objective`'s raw-vector test under-verifies 5 of 7 terms numerically (only
  `transponders` and the scalar sign are pinned to ground truth).
- `dropped_traffic`'s "disjoint sets ⇒ no double count" invariant has no dedicated
  regression test; it's correct under today's `simulate_ip_routing` semantics but would
  break silently if those change.
- `disjoint_pairs` has no `top_n > 2` truncation / tie-break test.
