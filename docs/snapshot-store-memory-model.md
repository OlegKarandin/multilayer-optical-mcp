# SnapshotStore memory model

Traces a `create()` → `branch()` → `apply_nf_delta()` sequence, showing
exactly what each pointer points to and which objects are shared vs independent.

---

## Initial state

```
store._current  ──► ModelA
store._snapshots = {}                        (empty)

ModelA
  _amplifiers: { "a1" ──► Amp(nf=5.5) }    object X
  _fibers:     { "f1" ──► Fiber(km=80) }    object Y
```

---

## After `create()`

`_snapshots: [s1]`

```
store._current  ──► ModelA
store._snapshots = { "s1" ──► ModelA_clone }

ModelA                          ModelA_clone
  _amplifiers: {                  _amplifiers: {
    "a1" ──► Amp(nf=5.5) ◄──────── "a1" ──► Amp(nf=5.5)
  }                     object X  }
  _fibers: {                      _fibers: {
    "f1" ──► Fiber(km=80) ◄──────── "f1" ──► Fiber(km=80)
  }                     object Y  }
```

Two separate dict containers. Both point at the same frozen objects X and Y.

---

## After `branch("s1")`

`_snapshots: [s1, b1]`

```
store._current  ──► ModelB          ← new, clone of ModelA_clone
store._snapshots = {
    "s1" ──► ModelA_clone,
    "b1" ──► ModelB
}

ModelA_clone                    ModelB
  _amplifiers: {                  _amplifiers: {
    "a1" ──► Amp(nf=5.5) ◄──────── "a1" ──► Amp(nf=5.5)
  }                     object X  }
  _fibers: {                      _fibers: {
    "f1" ──► Fiber(km=80) ◄──────── "f1" ──► Fiber(km=80)
  }                     object Y  }
```

Three separate dict containers (ModelA, ModelA_clone, ModelB).
All still pointing at objects X and Y.

ModelA (the original `_current`) is now unreferenced — lost.

---

## After `apply_nf_delta("a1", +2.0)` on `_current` (ModelB)

`_snapshots: [s1, b1]`

```
Inside apply_nf_delta:
  old_amp = ModelB._amplifiers["a1"]       → object X  Amp(nf=5.5)
  new_amp = replace(old_amp, nf_db=7.5)   → object Z  Amp(nf=7.5)  NEW
  ModelB._amplifiers["a1"] = new_amp

store._current  ──► ModelB
store._snapshots = {
    "s1" ──► ModelA_clone,
    "b1" ──► ModelB
}

ModelA_clone                    ModelB
  _amplifiers: {                  _amplifiers: {
    "a1" ──► Amp(nf=5.5)          "a1" ──► Amp(nf=7.5)
  }           object X  }                     object Z (new)
  _fibers: {                      _fibers: {
    "f1" ──► Fiber(km=80) ◄──────── "f1" ──► Fiber(km=80)
  }                     object Y  }
```

- X is still alive — ModelA_clone still points to it.
- Z is brand new — only ModelB points to it.
- Y is still shared — `_fibers` was never touched, so both dicts still point at the same object.

---

## Key takeaways

- `create()` and `branch()` both call `_store()`, so both consume a slot from the
  `max_snapshots` cap. Branching from a snapshot can evict the very snapshot you
  branched from if the cap is tight.
- The copy is **eager**: all dict containers are duplicated immediately when
  `_clone()` runs, not lazily when a write occurs.
- Isolation is **replace-on-write**: `dataclasses.replace()` always produces a new
  object and stores it only in the mutating model's dict. The frozen constraint on
  all value types makes sharing safe — no dict entry can be mutated in place.
- Only the entries that have been explicitly replaced diverge between snapshots.
  Everything else remains shared until touched.
