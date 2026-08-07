# src/multilayer_optical_mcp/build_cli.py
"""Offline operating-network builder: topology in, state file out.

Separate from the server entry point on purpose. This is a batch job -- minutes
on a small topology, tens of minutes on a 143-node one, because it drives GNPy
through the packer's convergence search. Running it per server spawn is exactly
what the state file exists to avoid, so it is a console script, not an MCP tool
(see model/scenario.py's module docstring on why the builder is not a tool).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .model.allocation import make_adapter_evaluator
from .model.modes import load_modulation_formats
from .model.qot_results import QoTCache, QoTResultStore
from .model.scenario import build_operating_network
from .model.solvers import SolverStatus
from .state_file import dump_state, topology_fingerprint
from .topology_loader import MOD_FORMATS_YAML, load_model_from_topology_file


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="multilayer-optical-mcp-build",
        description="Build a loaded operating network and write it to a state "
                    "file for `multilayer-optical-mcp --topology … --state …`.")
    p.add_argument("--topology", required=True, help="Topology JSON to build on")
    p.add_argument("--out", required=True, help="State file to write")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--target-mean-util", type=float, default=0.4)
    p.add_argument("--max-util-cap", type=float, default=0.95)
    p.add_argument("--pair-density", type=float, default=None,
                   help="Sparsity knob in (0,1]. Omit for the full demand "
                        "matrix -- but note that on a large topology the full "
                        "matrix quantizes every pair to zero demands.")
    p.add_argument("--unit-gbps", type=float, default=100.0)
    p.add_argument("--protected-fraction", type=float, default=0.3)
    p.add_argument("--max-iters", type=int, default=24)
    p.add_argument("--protection-basis", default=None,
                   choices=["physical", "srlg", "risk_group", "union"])
    p.add_argument("--protection-level", default=None,
                   choices=["node", "link", "srlg", "risk_group"])
    p.add_argument("--protection-best-effort", action="store_true")
    return p


def _protection_constraints(args) -> dict | None:
    if not (args.protection_basis or args.protection_level
            or args.protection_best_effort):
        return None
    out: dict = {"best_effort": args.protection_best_effort}
    if args.protection_basis:
        out["basis"] = args.protection_basis
    if args.protection_level:
        out["level"] = args.protection_level
    return out


def _gnpy_version() -> str:
    """Provenance only. Stored QoT is read back as-is, so a version change does
    not invalidate a state file -- it matters only if the client later calls
    recompute_qot_under_loading, which is why the server warns rather than
    refusing on a mismatch."""
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("gnpy")
    except PackageNotFoundError:
        return "unknown"


def main() -> None:
    parser = _parser()
    args = parser.parse_args()

    modes = load_modulation_formats(MOD_FORMATS_YAML)
    raw = json.loads(Path(args.topology).read_text(encoding="utf-8-sig"))
    fingerprint = topology_fingerprint(raw)
    model = load_model_from_topology_file(args.topology, modes=modes)

    store = QoTResultStore()
    qot = make_adapter_evaluator(model, store, cache=QoTCache())
    params = {
        "seed": args.seed, "target_mean_util": args.target_mean_util,
        "max_util_cap": args.max_util_cap, "pair_density": args.pair_density,
        "unit_gbps": args.unit_gbps, "protected_fraction": args.protected_fraction,
        "max_iters": args.max_iters,
        "protection_constraints": _protection_constraints(args),
    }
    res = build_operating_network(
        model, qot=qot, store=store, seed=args.seed,
        target_mean_util=args.target_mean_util, max_util_cap=args.max_util_cap,
        pair_density=args.pair_density, unit_gbps=args.unit_gbps,
        protected_fraction=args.protected_fraction, max_iters=args.max_iters,
        protection_constraints=_protection_constraints(args))
    rep = res.report

    # Gate BEFORE writing: both of these produce a well-formed but useless file.
    if rep.n_demands == 0:
        parser.exit(2, "build produced 0 demands: the gravity generator "
                       "quantizes each pair to round(offered / unit_gbps), so a "
                       "large full matrix rounds every pair to zero. Set "
                       "--pair-density (e.g. 0.02) or raise --unit-gbps.\n")
    if rep.status is SolverStatus.NO_SOLUTION:
        parser.exit(2, f"build returned no_solution (limit={rep.limit}); "
                       f"no state file written.\n")
    if rep.unplaced_count:
        print(f"warning: {rep.unplaced_count} demand(s) unplaced "
              f"(limit={rep.limit}):", file=sys.stderr)
        for reason, count in sorted(rep.unplaced_reasons.items(),
                                    key=lambda kv: (-kv[1], kv[0])):
            print(f"  {count} x {reason}", file=sys.stderr)

    meta = {
        "topology_path": str(args.topology),
        "gnpy_version": _gnpy_version(),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "params": params,
        "report": {
            "status": rep.status.value,
            "achieved_mean_util": rep.achieved_mean_util,
            "achieved_max_util": rep.achieved_max_util,
            "n_demands": rep.n_demands,
            "transponders_used": rep.transponders_used,
            "unplaced_count": rep.unplaced_count,
            "unplaced_reasons": dict(rep.unplaced_reasons),
            "scale": rep.scale,
            "limit": rep.limit,
        },
    }
    doc = dump_state(res.model, fingerprint=fingerprint, meta=meta)
    Path(args.out).write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"wrote {args.out}: {len(doc['lightpaths'])} lightpaths, "
          f"{len(doc['ip_links'])} IP links, {len(doc['services'])} services",
          file=sys.stderr)
