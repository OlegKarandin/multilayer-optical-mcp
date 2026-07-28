"""FastMCP server shell — phase 1 & 2 tools.

Exposes:
  - get_transceiver_modes
  - snapshot_create / snapshot_branch / snapshot_restore / snapshot_diff
  - compute_qot
  - recompute_qot_under_loading
  - get_qot_breakdown
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .model.assets import Direction
from .model.modes import load_modulation_formats
from .model.network import NetworkModel
from .model.qot_results import QoTResultStore, HarvestCache
from .model.snapshots import SnapshotStore
from .gnpy_adapter.loading import Channel, LoadingState
from .gnpy_adapter.adapter import (
    compute_qot as _compute_qot,
    recompute_qot_under_loading as _recompute,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
MOD_FORMATS_YAML = REPO_ROOT / "modulation_formats.yaml"


def build_app(*, model: NetworkModel | None = None,
              snapshots: SnapshotStore | None = None,
              results: QoTResultStore | None = None) -> FastMCP:
    """Construct and return the FastMCP application with all phase 1-2 tools."""
    app = FastMCP("multilayer-optical-mcp")
    modes = load_modulation_formats(MOD_FORMATS_YAML)
    if snapshots is None:
        snapshots = SnapshotStore(initial=model or NetworkModel(modes=modes),
                                   max_snapshots=64, ttl_seconds=3600)
    if results is None:
        results = QoTResultStore(max_results=512, ttl_seconds=600)

    @app.tool()
    def get_transceiver_modes() -> list[dict]:
        """Return all known transceiver modes with their bitrate and GSNR thresholds."""
        return [
            {
                "id": m.id,
                "bitrate_gbps": m.bitrate_gbps,
                "required_gsnr_db": m.required_gsnr_db,
                "symbol_rate_baud": m.symbol_rate_baud,
                "channel_spacing_hz": m.channel_spacing_hz,
            }
            for m in modes.list()
        ]

    @app.tool()
    def snapshot_create() -> dict:
        """Create a snapshot of the current network state and return its id."""
        return {"id": snapshots.create()}

    @app.tool()
    def snapshot_branch(parent_id: str) -> dict:
        """Branch from an existing snapshot for isolated what-if exploration."""
        return {"id": snapshots.branch(parent_id)}

    @app.tool()
    def snapshot_restore(snapshot_id: str) -> dict:
        """Restore the current model to a previously saved snapshot."""
        snapshots.restore(snapshot_id)
        return {"restored": snapshot_id}

    @app.tool()
    def snapshot_diff(a_id: str, b_id: str) -> dict:
        """Return a structured delta between two snapshots."""
        return snapshots.diff(a_id, b_id)

    def _loading_from(channels: list[dict]) -> LoadingState:
        return LoadingState(
            channels=tuple(
                Channel(
                    center_freq_hz=c["center_freq_hz"],
                    slot_width_hz=c["slot_width_hz"],
                    # S2-2: omit or pass null for the tx_power default; a literal
                    # 0.0 now means 0 dBm, not "don't care".
                    power_dbm=c.get("power_dbm"),
                    mode_id=c["mode_id"],
                )
                for c in channels
            )
        )

    @app.tool()
    def compute_qot(
        oms_sequence: list[str],
        direction: str,
        mode_id: str,
        loading_channels: list[dict],
    ) -> dict:
        """Propagate a loading state through the GNPy model and return per-channel QoT.

        loading_channels: list of {center_freq_hz, slot_width_hz, power_dbm, mode_id}.
        direction: "forward" or "backward".
        """
        state, rid = _compute_qot(
            model=snapshots.current(),
            store=results,
            oms_sequence=tuple(oms_sequence),
            direction=Direction(direction),
            mode_id=mode_id,
            loading=_loading_from(loading_channels),
        )
        return {
            "gsnr_db": state.gsnr_db,
            "osnr_db": state.osnr_db,
            "margin_db": state.margin_db,
            "mode_feasible": state.mode_feasible,
            "limiting_element_id": state.limiting_element_id,
            "result_id": rid,
        }

    @app.tool()
    def recompute_qot_under_loading(loading_channels: list[dict]) -> dict:
        """Recompute gated QoT for every lightpath in the current model under the given loading.

        loading_channels: list of {center_freq_hz, slot_width_hz, power_dbm, mode_id}.
        Returns a dict keyed by lightpath id.
        """
        out = _recompute(
            model=snapshots.current(),
            store=results,
            loading=_loading_from(loading_channels),
        )
        return {
            lp: {
                "gsnr_db": s.gsnr_db,
                "osnr_db": s.osnr_db,
                "margin_db": s.margin_db,
                "mode_feasible": s.mode_feasible,
                "limiting_element_id": s.limiting_element_id,
                "result_id": rid,
            }
            for lp, (s, rid) in out.items()
        }

    @app.tool()
    def get_qot_breakdown(result_id: str) -> dict:
        """Retrieve the per-element QoT breakdown for a previous compute_qot call."""
        bd = results.get(result_id)
        return {
            "limiting_element_id": bd.limiting_element_id,
            "snapshots": [asdict(s) for s in bd.snapshots],
        }

    from .model.views import (
        topology_dict, lightpaths_dict, services_dict,
        traffic_matrix_dict, srlgs_dict, risk_groups_dict,
        routing_result_dict, disjointness_result_dict,
        feasibility_result_dict, placement_result_dict, allocation_result_dict,
        ip_topology_dict, grooming_map_dict, ip_routing_result_dict,
        affected_services_dict,
        margin_sweep_dict, degradation_report_dict, failure_report_dict,
        restoration_result_dict, max_feasible_mode_dict, sensitivity_result_dict,
    )
    from .model.restoration import compute_restoration as _compute_restoration
    from .model.whatif import (
        margin_threshold_sweep as _margin_sweep,
        max_feasible_mode_view as _max_feasible_mode_view,
        inject_degradation as _inject_degradation,
        inject_failure as _inject_failure,
        whatif_sensitivity as _whatif_sensitivity,
        loading_from_model,
    )
    from .model.exposure import compute_exposure
    from .model.solvers import (
        compute_paths as _compute_paths,
        check_disjointness as _check_disjointness,
        compute_disjoint_paths as _compute_disjoint_paths,
    )
    from .model.spectrum import (
        SpectrumGrid, check_spectrum_feasibility as _check_spectrum_feasibility,
    )
    from .model.allocation import (
        make_adapter_evaluator,
        solve_rsa as _solve_rsa,
        solve_allocation as _solve_allocation,
    )
    from .model.ip_routing import simulate_ip_routing as _simulate_ip_routing
    from .model.plan import (
        plan_from_dict, ProvisionLightpath, TeardownLightpath,
        SetModulationFormat, apply_op,
    )
    from .model.assets import Lightpath as _Lightpath, IPLink as _IPLink
    from .model.validate import (
        validate_plan as _validate_plan, recompute_if_possible as _recompute_if_possible,
    )
    from .model.commit import (
        commit_plan as _commit_plan, reconcile as _reconcile,
        CommitResult as _CommitResult,
    )
    from .model.views import (
        validation_report_dict, commit_result_dict, drift_report_dict,
        objective_result_dict, route_service_result_dict,
    )
    from .model.route_service import route_service as _route_service
    from .model.objective import evaluate_objective as _evaluate_objective

    # Expose the SnapshotStore on the app so tests can reach the current model.
    app._snapshots = snapshots  # type: ignore[attr-defined]

    @app.tool()
    def get_topology(layer: str = "both") -> dict:
        """Return network topology. layer ∈ {'optical', 'ip', 'both'}."""
        return topology_dict(snapshots.current(), layer=layer)

    @app.tool()
    def get_lightpaths() -> list[dict]:
        """Return all lightpaths with mode, OMS path, and QoT if available."""
        return lightpaths_dict(snapshots.current())

    @app.tool()
    def get_services() -> dict:
        """Return all services with working/protection paths and grooming map."""
        return services_dict(snapshots.current())

    @app.tool()
    def get_traffic_matrix() -> dict:
        """Return the IP demand matrix aggregated across services."""
        return traffic_matrix_dict(snapshots.current())

    @app.tool()
    def get_ip_topology() -> dict:
        """Routers and IP links; each link annotated with its underlying
        lightpath, derived (margin-gated) capacity, and current offered load."""
        return ip_topology_dict(snapshots.current())

    @app.tool()
    def get_grooming_map() -> dict:
        """Coupling #3: which demands ride which lightpaths, both directions
        (by_service and by_lightpath)."""
        return grooming_map_dict(snapshots.current())

    @app.tool()
    def get_affected_services(asset_id: str) -> dict:
        """Reverse lookup: services whose working or protection path crosses
        asset_id (IP link, lightpath, OMS, or fiber/amp/roadm uid)."""
        return affected_services_dict(snapshots.current(), asset_id)

    @app.tool()
    def simulate_ip_routing() -> dict:
        """Read-only: account pinned working_path demand onto IP links and
        report {utilizations, congestion, dropped}. Routes nothing."""
        return ip_routing_result_dict(_simulate_ip_routing(snapshots.current()))

    @app.tool()
    def reroute_service(service_id: str, ip_path: list[str], which: str = "working") -> dict:
        """Move a service's working_path (default) or protection_path onto a
        different IP-link sequence. `which` selects the leg: "working" or
        "protection". Validates contiguity src->dst; raises on an invalid path
        or an unrecognized `which`."""
        model = snapshots.current()
        if which == "working":
            model.set_service_working_path(service_id, tuple(ip_path))
            key = "working_path"
        elif which == "protection":
            model.set_service_protection_path(service_id, tuple(ip_path))
            key = "protection_path"
        else:
            raise ValueError(f"unrecognized which {which!r}: expected 'working' or 'protection'")
        svc = model.get_service(service_id)
        return {"service_id": svc.id, key: list(getattr(svc, key))}

    @app.tool()
    def list_srlgs() -> list[dict]:
        """Return all static, design-time shared risk link groups."""
        return srlgs_dict(snapshots.current())

    @app.tool()
    def get_srlg_members(srlg_id: str) -> list[str]:
        """Return the asset ids belonging to an SRLG."""
        return list(snapshots.current().get_srlg_members(srlg_id))

    @app.tool()
    def define_risk_group(
        rg_id: str, asset_ids: list[str], metadata: dict | None = None,
    ) -> dict:
        """Define a runtime risk group as an abstract asset partition.
        asset_ids are not validated against the model."""
        rg = snapshots.current().define_risk_group(
            rg_id=rg_id, asset_ids=tuple(asset_ids), metadata=metadata or {},
        )
        return {"id": rg.id, "asset_ids": list(rg.asset_ids),
                "metadata": dict(rg.metadata)}

    @app.tool()
    def list_risk_groups() -> list[dict]:
        """Return all runtime risk groups with their asset lists and metadata."""
        return risk_groups_dict(snapshots.current())

    @app.tool()
    def get_risk_group(rg_id: str) -> dict:
        """Return a single risk group by id."""
        rg = snapshots.current().get_risk_group(rg_id)
        return {"id": rg.id, "asset_ids": list(rg.asset_ids),
                "metadata": dict(rg.metadata)}

    @app.tool()
    def get_exposure(service_id: str, risk_group_id: str) -> dict:
        """Intersect a service's working and protection asset footprints with a risk group.
        both_intersect=True signals the design-time-disjoint-but-now-correlated case."""
        res = compute_exposure(snapshots.current(), service_id, risk_group_id)
        return {
            "service_id": res.service_id,
            "risk_group_id": res.risk_group_id,
            "working_intersects": res.working_intersects,
            "protection_intersects": res.protection_intersects,
            "both_intersect": res.both_intersect,
            "working_intersection": list(res.working_intersection),
            "protection_intersection": list(res.protection_intersection),
        }

    @app.tool()
    def compute_paths(
        src: str, dst: str, k: int = 3, constraints: dict | None = None,
    ) -> dict:
        """k-shortest OMS routes from src to dst over the optical layer.
        Returns a typed status; no route yields status 'no_solution'."""
        if k < 1:
            # Task-8-fix reviewer's Minor #3: k=0 falls through the solver's
            # `if emitted >= k: return` on its very first check, yielding an
            # empty path tuple -- which compute_paths then reports as typed
            # NO_SOLUTION. That status means "no route exists," which is false
            # when k=0 simply asked for none; guard the nonsensical input at
            # the tool boundary instead of overloading NO_SOLUTION's meaning.
            raise ValueError(f"k must be >= 1, got {k!r}")
        res = _compute_paths(snapshots.current(), src, dst, k, constraints)
        return routing_result_dict(res)

    @app.tool()
    def check_disjointness(
        path_a: list[str], path_b: list[str],
        basis: str = "physical", level: str = "link",
    ) -> dict:
        """Audit whether two existing OMS-sequence paths are disjoint under a
        basis ∈ {physical, srlg, risk_group, union} and level ∈ {node, link,
        srlg, risk_group}. Returns shared_assets/shared_groups when not disjoint."""
        res = _check_disjointness(snapshots.current(), path_a, path_b, basis, level)
        return disjointness_result_dict(res)

    @app.tool()
    def compute_disjoint_paths(
        src: str, dst: str,
        basis: str = "physical", level: str = "link",
        best_effort: bool = False,
    ) -> dict:
        """Find a disjoint pair src->dst under a basis/level. status 'solution'
        for a fully-disjoint pair, 'partial' for the best-effort minimum-overlap
        pair, 'no_solution' when none disjoint and best_effort is false."""
        res = _compute_disjoint_paths(
            snapshots.current(), src, dst, basis, level, best_effort)
        return disjointness_result_dict(res)

    @app.tool()
    def check_spectrum_feasibility(path: list[str], center_freq_hz: float) -> dict:
        """Is the channel at center_freq_hz free on every OMS along the path?
        Returns typed per-OMS clashes when not. slot_width is the fixed grid
        spacing (100 GHz)."""
        grid = SpectrumGrid.default()
        slot = grid.slot_of(center_freq_hz)
        res = _check_spectrum_feasibility(snapshots.current(), tuple(path), slot, grid=grid)
        return feasibility_result_dict(res)

    @app.tool()
    def solve_rsa(
        demands: list[dict], objective: str = "shortest",
        constraints: dict | None = None,
    ) -> dict:
        """Route + spectrum-assign optical demands. Each demand:
        {id, src, dst, protected?, required_gbps?, constraints?}. Mode falls out
        of the GNPy GSNR on the chosen route (highest feasible bitrate)."""
        model = snapshots.current()
        qot = make_adapter_evaluator(model, results, harvest_cache=HarvestCache())
        return placement_result_dict(
            _solve_rsa(model, qot, demands, objective=objective, constraints=constraints))

    @app.tool()
    def solve_allocation(
        demands: list[dict], spare_inventory: dict, weights: dict | None = None,
    ) -> dict:
        """Greenfield heuristic: light new lightpaths from a per-site transponder
        count to serve as many weighted demands as possible. Each demand:
        {id, src, dst, demand_gbps, protected?}. Returns a typed
        solution/partial/no_solution with placed and unplaced demands.
        weights: per-demand priority (demand id -> ordering weight, higher =
        placed first); does not feed evaluate_objective's cost vector."""
        model = snapshots.current()
        qot = make_adapter_evaluator(model, results, harvest_cache=HarvestCache())
        return allocation_result_dict(
            _solve_allocation(model, qot, demands, spare_inventory, weights=weights))

    @app.tool()
    def whatif_margin_threshold_sweep(threshold_db: float) -> dict:
        """Physics-free screening: lightpaths whose current margin is within
        threshold_db of zero (margin_db <= threshold_db), sorted ascending.
        Models no degradation; makes no causal claim. Read-only."""
        rows = _margin_sweep(snapshots.current(), threshold_db)
        return margin_sweep_dict(rows)

    @app.tool()
    def whatif_max_feasible_mode() -> list[dict]:
        """Advisory read: per-lightpath current mode vs. the highest mode its
        recorded GSNR could carry, with direction (headroom/downshift/match/
        infeasible). Never mutates and never gates — validate_plan stays the
        commit gate for mode feasibility. Read-only."""
        rows = _max_feasible_mode_view(snapshots.current())
        return max_feasible_mode_dict(rows)

    @app.tool()
    def inject_degradation(
        asset_id: str,
        nf_delta: float = 0.0,
        loss_delta: float = 0.0,
        threshold_db: float = 0.0,
    ) -> dict:
        """Branch what-if: add nf_delta dB NF to an amplifier and/or loss_delta dB
        loss to a fiber, recompute QoT under current loading, and return typed
        threshold crossings. Operate on a branch (snapshot_branch) — mutates state."""
        report = _inject_degradation(
            snapshots.current(), store=results, asset_id=asset_id,
            nf_delta=nf_delta, loss_delta=loss_delta, threshold_db=threshold_db)
        return degradation_report_dict(report)

    @app.tool()
    def inject_failure(asset_ids: list[str]) -> dict:
        """Branch what-if: mark assets failed; every lightpath crossing a failed
        asset goes down (capacity 0). Operate on a branch — mutates state."""
        report = _inject_failure(snapshots.current(), tuple(asset_ids))
        return failure_report_dict(report)

    @app.tool()
    def whatif_sensitivity(
        state_a: str, state_b: str, oms_sequence: list[str], direction: str,
        mode_id: str, loading_channels: list[dict],
    ) -> dict:
        """Diff per-element QoT contribution between two branches (e.g. a nominal
        baseline vs. one with inject_degradation applied) for the same
        path/direction/mode/loading. Isolates which asset's OWN contribution
        changed — not the cumulative GSNR-after figure, which shifts at every
        element downstream of the real cause too. Read-only; mutates neither
        branch. rows sorted by |gsnr_contribution_delta_db| descending."""
        model_a = snapshots.get(state_a)
        model_b = snapshots.get(state_b)
        res = _whatif_sensitivity(
            model_a, model_b, store=results,
            oms_sequence=tuple(oms_sequence), direction=Direction(direction),
            mode_id=mode_id, loading=_loading_from(loading_channels),
        )
        return sensitivity_result_dict(res)

    @app.tool()
    def compute_restoration(service_id: str, avoid: dict | None = None) -> dict:
        """Enumerate recovery candidates for a service over survivors. `avoid` is
        {assets?: [...], srlgs?: [...], risk_groups?: [...]} (typically a
        failure's asset set). `srlgs` matches static SRLG ids, `risk_groups`
        matches dynamic RiskGroup ids only (distinct namespaces).
        Read-only: returns typed candidates (full + degraded) with status
        solution/partial/no_solution. Does not mutate or commit anything."""
        model = snapshots.current()
        qot = make_adapter_evaluator(model, results, harvest_cache=HarvestCache())
        res = _compute_restoration(model, qot, service_id, avoid=avoid)
        return restoration_result_dict(res)

    @app.tool()
    def validate_plan(
        plan: dict, basis: str = "physical", level: str = "link",
        dropped_tolerance_gbps: float = 0.0,
    ) -> dict:
        """Replay a plan op-by-op on a clone and return a typed violation list,
        checked at EVERY intermediate state (not just endpoints). Violations:
        mode_infeasible, spectrum_clash, ip_link_overload, dropped_traffic (incl.
        unrouted demand), disjointness_collapse, protection_not_viable,
        protection_oversubscribed (1:1 reserved-capacity double-booking), plus
        invalid_plan for a malformed / bad-reference / duplicate-id plan — each
        with state_index and a `transient` flag for the make-before-break window.
        Read-only: ground truth is never mutated. NB: QoT is quasi-static; this
        does not certify the switching instant (the EDFA transient is out of scope)."""
        try:
            parsed = plan_from_dict(plan)
            report = _validate_plan(
                snapshots.current(), parsed, store=results,
                basis=basis, level=level, dropped_tolerance_gbps=dropped_tolerance_gbps)
        except Exception as exc:
            return {
                "ok": False, "num_states": 0,
                "violations": [{"type": "invalid_plan", "state_index": 0,
                                "asset_id": None, "transient": False,
                                "detail": {"message": str(exc)}}],
            }
        return validation_report_dict(report)

    @app.tool()
    def provision_lightpath(lightpath: dict, ip_link: dict | None = None) -> dict:
        """Light a new lightpath; optionally bind+bring-up an IP link on it.
        Mutates the current model — branch first (snapshot_branch) to explore."""
        op = ProvisionLightpath(
            lightpath=_Lightpath(
                id=lightpath["id"], oms_sequence=tuple(lightpath["oms_sequence"]),
                mode_id=lightpath["mode_id"], center_freq_hz=lightpath["center_freq_hz"]),
            ip_link=None if ip_link is None else _IPLink(
                id=ip_link["id"], a_router=ip_link["a_router"],
                z_router=ip_link["z_router"], lightpath_id=lightpath["id"]))
        apply_op(snapshots.current(), op)
        # Seed QoT for the freshly-lit lightpath so derived-capacity reads
        # (ip_link_capacity_gbps, _residual_gbps) report a real value instead
        # of the "unseeded" state -- the live twin of what commit_plan already
        # does on its live-commit path. Best-effort: a recompute failure must
        # not un-report the successful provision.
        try:
            _recompute_if_possible(snapshots.current(), results)
        except Exception:
            pass
        return {"lightpath_id": op.lightpath.id,
                "ip_link_id": op.ip_link.id if op.ip_link else None}

    @app.tool()
    def teardown_lightpath(lightpath_id: str) -> dict:
        """Tear down a lightpath and bring down every IP link bound to it.
        Mutates the current model — branch first to explore."""
        apply_op(snapshots.current(), TeardownLightpath(lightpath_id=lightpath_id))
        return {"torn_down": lightpath_id}

    @app.tool()
    def set_modulation_format(lightpath_id: str, mode_id: str) -> dict:
        """Change a lightpath's transceiver mode; the bound IP link's capacity
        propagates automatically (capacity = f(mode), margin-gated). Mutates the
        current model — branch first to explore."""
        apply_op(snapshots.current(), SetModulationFormat(
            lightpath_id=lightpath_id, mode_id=mode_id))
        return {"lightpath_id": lightpath_id, "mode_id": mode_id}

    @app.tool()
    def commit_plan(
        plan: dict, dry_run: bool = True, confirm: bool = False,
        basis: str = "physical", level: str = "link",
        dropped_tolerance_gbps: float = 0.0,
    ) -> dict:
        """dry_run=True simulates on a clone and returns the would-be diff without
        touching state. A live commit (dry_run=False) validates first, requires
        confirm=True, then actuates; status is 'rejected' (violations),
        'requires_approval' (unconfirmed), 'committed', or
        'committed_with_failures' (control-plane partial failure — call reconcile)."""
        try:
            parsed = plan_from_dict(plan)
        except Exception as exc:
            return commit_result_dict(_CommitResult(
                status="rejected", dry_run=dry_run, applied_ops=0, failed_ops=0,
                intended_snapshot_id=None, validation=None,
                diff={"error": str(exc)}))
        result = _commit_plan(
            snapshots, parsed, store_results=results,
            dry_run=dry_run, confirm=confirm, basis=basis, level=level,
            dropped_tolerance_gbps=dropped_tolerance_gbps)
        return commit_result_dict(result)

    @app.tool()
    def route_service(service_id: str, protected: bool = False,
                      basis: str = "physical", level: str = "link",
                      best_effort: bool = False, avoid: dict | None = None,
                      weights: dict | None = None) -> dict:
        """Service-level routing/restoration menu on the layered graph. avoid=None ->
        first-time routing; avoid={assets?,srlgs?,risk_groups?} -> restoration
        (srlgs matches static SRLG ids, risk_groups matches dynamic RiskGroup ids
        only). protected=True returns a disjoint-pair menu (best_effort ->
        min-overlap PARTIAL). Read-only. weights: per-cost-term weights for the
        7-term objective scalar (e.g. {"transponders": 2.0}); not a per-demand
        priority."""
        model = snapshots.current()
        qot = make_adapter_evaluator(model, results, harvest_cache=HarvestCache())
        res = _route_service(model, qot, service_id, protected=protected, basis=basis,
                             level=level, best_effort=best_effort, avoid=avoid, weights=weights)
        return route_service_result_dict(res)

    @app.tool()
    def evaluate_objective(state: str | None = None, weights: dict | None = None) -> dict:
        """7-term cost vector + weighted scalar for a state (snapshot id; defaults to
        current). Terms are costs except total_margin (a benefit, subtracted).
        weights: per-cost-term weights (e.g. {"transponders": 2.0}); not a
        per-demand priority."""
        model = snapshots.get(state) if state else snapshots.current()
        return objective_result_dict(_evaluate_objective(model, weights))

    @app.tool()
    def reconcile(intended_snapshot_id: str) -> dict:
        """After a live commit, diff actual network state against the intended
        end-state recorded at commit time. Returns typed drift[] (the ops the
        control plane failed to actuate); in_sync=True when reality matches."""
        return drift_report_dict(_reconcile(snapshots, intended_snapshot_id))

    return app


def main() -> None:
    """Entry-point for running the MCP server (stdio transport)."""
    build_app().run()
