# Design: sparse, seeded pair selection in `generate_demands`

Follow-up to `docs/superpowers/specs/2026-07-14-traffic-synthesizer-design.md`
(`model/traffic.py`). Today `generate_demands` builds a full O(N²) gravity-weighted
demand matrix: every reachable node pair gets *some* offered volume (possibly
rounding to zero units at low `scale`, but every pair is considered). This adds a
`pair_density` knob so `seed` can also decide **which pairs carry any traffic at
all** — a sparse subset, not the full matrix — matching the fact that real
backbone traffic matrices are typically sparse: most node pairs don't exchange
direct traffic regardless of gravity weight.

## Motivation

Realism, not performance. The full matrix means every reachable pair is "in play"
at every scale, which is not how real operator traffic matrices look — significant
flows concentrate on a subset of pairs. This is a modeling-accuracy gap independent
of how fast `generate_demands` runs.

## Scope

One new optional parameter on `generate_demands`:

```python
def generate_demands(
    model: NetworkModel,
    *,
    seed: int,
    scale: float,
    alpha: float = 1.0,
    unit_gbps: float = 100.0,
    protected_fraction: float = 0.3,
    node_mass: Optional[Dict[str, float]] = None,
    mass_jitter: float = 0.15,
    pair_density: Optional[float] = None,   # NEW
) -> List[dict]:
```

`pair_density=None` (the default) reproduces today's full-matrix behavior
byte-for-byte: no new RNG draws happen, so every existing test — including
`test_seed_only_enters_through_jitter` (whose premise is "seed's only effect is
mass jitter") and the frozen fixture
(`test_generate_demands_reproduces_frozen_german_17_fixture`) — passes unmodified.
Sparsity is strictly opt-in.

## Mechanism: weighted Bernoulli per pair

Real sparse matrices concentrate on high-gravity pairs (hub-to-hub, hub-to-leaf),
so pair survival probability scales with gravity weight rather than being
weight-blind. Insert a filtering pass immediately after the existing `pair_w`
weight computation (`traffic.py:80-93`), before volume/quantization:

```python
if pair_density is not None:
    mean_w = total_w / len(pair_w) if pair_w else 0.0
    kept: List[tuple] = []
    for u, v, w in pair_w:
        p_active = min(1.0, pair_density * w / mean_w) if mean_w > 0.0 else 0.0
        if rng.random() < p_active:
            kept.append((u, v, w))
    pair_w, total_w = kept, sum(w for _, _, w in kept)
```

Key properties:

- **Draw order and seed reuse.** Iterates `pair_w` in the same deterministic order
  it was built (nodes pre-sorted, `traffic.py:82`), and draws from the *same* `rng`
  object already seeded for mass jitter — but only *after* every jitter draw has
  already been consumed. Enabling `pair_density` therefore never perturbs jitter
  values; it only adds draws after them. This is also why a separate `pair_seed`
  parameter is unnecessary: the user's ask was specifically for `seed` to drive
  pair selection, and reusing the existing stream in fixed post-jitter order gives
  that directly, with the same "reproducible variety across seeds" property the
  jitter already has.
- **Weighting shape.** `p_active` scales with `w / mean_w`, so a pair with
  above-mean gravity weight is likely (and, above `mean_w / pair_density`,
  certain) to survive, while below-mean pairs are the first to drop as `pair_density`
  shrinks. There is deliberately no value of `pair_density` that reproduces the
  full matrix exactly (even at `pair_density=1.0`, below-mean pairs still have
  `p_active < 1`) — that's fine precisely because the feature is opt-in via the
  `None` sentinel, not via `pair_density=1.0`. The parameter is a genuine
  probabilistic density knob, not a disguised on/off switch.
- **Renormalization.** `total_w` is recomputed over the *surviving* pairs only, so
  `offered = scale * w / total_w` keeps `scale`'s existing contract — "total
  offered volume" — intact regardless of density. This matters because `scale` is
  exactly the value `build_operating_network`'s bracket+bisection search tunes to
  hit a target utilization; if excluded pairs silently ate part of `scale`'s
  meaning, the search's semantics would drift between sparse and dense runs.
- **Degenerate case.** If every pair happens to drop (possible at very low density
  on a small graph), `pair_w` is empty and `total_w = 0.0`; the existing
  `if total_w > 0.0` guard (`traffic.py:98`) already returns `demands=[]` for that
  case — no new failure mode, consistent with the repo's "never throw, return the
  honest empty/typed result" pattern.
- **Downstream untouched.** Unit expansion, quantization, and the
  `protected_fraction` top-K-by-weight ranking all operate on the (already
  filtered) demand records exactly as today; no special-casing needed there since
  filtering happens purely at the `pair_w` stage before records are ever built.

## Testing (additive; existing suite must stay green unmodified)

- **Determinism:** same `(seed, pair_density)` → identical output.
- **Reproducible variety:** different seeds with the same `pair_density` → different
  active-pair sets, each individually reproducible.
- **Weighted inclusion:** on the existing `_star_model` fixture
  (`test_traffic.py`), hub-incident pairs survive at a measurably higher rate
  across seeds than peripheral pairs at low `pair_density`.
- **Volume renormalization:** total offered volume across surviving pairs stays
  ≈ `scale` (within quantization), regardless of `pair_density`.
- **Degenerate/empty case:** a density low enough to plausibly drop every pair
  returns `[]`, not an error.
- **Regression:** `test_traffic.py`, `test_scenario.py`, and the frozen
  `german_17_demands_seed0.json` fixture all pass unmodified — an explicit
  acceptance criterion, not incidental.

## Explicitly out of scope

- Changing the default behavior of `generate_demands` (full matrix stays the
  default; see Scope).
- A separate `pair_seed` parameter — `seed` already drives this via the shared
  `rng` stream (see Mechanism).
- Any change to `build_operating_network` or the packer — `pair_density` is a
  pure-generator concern; the scenario builder just forwards it as an optional
  kwarg if/when exposed there.
