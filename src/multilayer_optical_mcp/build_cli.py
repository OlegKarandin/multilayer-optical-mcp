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
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .model.allocation import make_adapter_evaluator
from .model.modes import load_modulation_formats
from .model.qot_results import QoTCache, QoTResultStore
from .model.scenario import build_operating_network
from .model.solvers import SolverStatus
from .state_file import dump_state, running_gnpy_version, topology_fingerprint
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


# Remediation hint per `limit` label (ScenarioReport's five-label vocabulary:
# "none", "max_util_cap", "no_disjoint_pair", "spare_inventory", "other").
# "other" is deliberately unmapped -- _limit_from_reasons already only falls
# back to it when no known substring matched, so there is no single flag to
# suggest.
_LIMIT_HINTS = {
    "max_util_cap": "raise --max-util-cap or lower --target-mean-util",
    "no_disjoint_pair": "pass --protection-best-effort to relax the "
                        "disjointness requirement",
    "spare_inventory": "increase the spare transponder inventory",
}


def _limit_hint(limit: str) -> str:
    if limit == "none":
        # "none" covers both build_operating_network's own <2-site early
        # return and the general "no scale ever produced a feasible
        # allocation" case -- there is no single flag for either.
        return ("the topology may have fewer than two sites, or no offered "
                "load level produced a feasible allocation")
    return _LIMIT_HINTS.get(limit, "")


def _validate_out_path(parser: argparse.ArgumentParser, out: str) -> None:
    """Fail fast on a bad --out before the (potentially tens-of-minutes) build
    runs, not after: build_operating_network's cost is exactly what a state
    file exists to avoid re-paying, so discarding it to a FileNotFoundError or
    a permission error on write is the failure this guards against."""
    parent = Path(out).resolve().parent
    if not parent.is_dir():
        parser.exit(2, f"--out's parent directory does not exist: {parent}\n")
    if not os.access(parent, os.W_OK):
        parser.exit(2, f"--out's parent directory is not writable: {parent}\n")


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    _validate_out_path(parser, args.out)

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

    # Gate BEFORE writing: all of these produce a well-formed but useless file.
    # NO_SOLUTION checked first: it subsumes the <2-site early return (which
    # also reports n_demands=0), and that case needs the no_solution message,
    # not the pair-density/unit-gbps advice below, which cannot help a
    # one-site topology.
    if rep.status is SolverStatus.NO_SOLUTION:
        hint = _limit_hint(rep.limit)
        detail = f"; {hint}" if hint else ""
        parser.exit(2, f"build returned no_solution (limit={rep.limit})"
                       f"{detail}; no state file written.\n")
    if rep.n_demands == 0:
        parser.exit(2, "build produced 0 demands: the gravity generator "
                       "quantizes each pair to round(offered / unit_gbps), so a "
                       "large full matrix rounds every pair to zero. Set "
                       "--pair-density (e.g. 0.02) or raise --unit-gbps.\n")
    if rep.unplaced_count:
        hint = _limit_hint(rep.limit)
        detail = f" ({hint})" if hint else ""
        print(f"warning: {rep.unplaced_count} demand(s) unplaced "
              f"(limit={rep.limit}){detail}:", file=sys.stderr)
        for reason, count in sorted(rep.unplaced_reasons.items(),
                                    key=lambda kv: (-kv[1], kv[0])):
            print(f"  {count} x {reason}", file=sys.stderr)

    meta = {
        "topology_path": str(args.topology),
        "gnpy_version": running_gnpy_version(),
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
