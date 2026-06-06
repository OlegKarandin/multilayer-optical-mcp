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
from .model.qot_results import QoTResultStore
from .model.snapshots import SnapshotStore
from .gnpy_adapter.loading import Channel, LoadingState
from .gnpy_adapter.adapter import (
    compute_qot as _compute_qot,
    recompute_qot_under_loading as _recompute,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
MOD_FORMATS_YAML = REPO_ROOT / "modulation_formats.yaml"


def build_app() -> FastMCP:
    """Construct and return the FastMCP application with all phase 1-2 tools."""
    app = FastMCP("multilayer-optical-mcp")
    modes = load_modulation_formats(MOD_FORMATS_YAML)
    model = NetworkModel(modes=modes)
    snapshots = SnapshotStore(initial=model, max_snapshots=64, ttl_seconds=3600)
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
                    power_dbm=c["power_dbm"],
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
        feasibility_result_dict, placement_result_dict,
        ip_topology_dict, grooming_map_dict, ip_routing_result_dict,
        affected_services_dict,
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
    def reroute_service(service_id: str, ip_path: list[str]) -> dict:
        """Move a service's working_path onto a different IP-link sequence.
        Validates contiguity src->dst; raises on an invalid path."""
        model = snapshots.current()
        model.set_service_working_path(service_id, tuple(ip_path))
        svc = model.get_service(service_id)
        return {"service_id": svc.id, "working_path": list(svc.working_path)}

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
        qot = make_adapter_evaluator(model, results)
        return placement_result_dict(
            _solve_rsa(model, qot, demands, objective=objective, constraints=constraints))

    @app.tool()
    def solve_allocation(
        demands: list[dict], spare_inventory: dict,
        objective: str = "max_placed", weights: dict | None = None,
    ) -> dict:
        """Greenfield heuristic: light new lightpaths from a per-site transponder
        count to serve as many weighted demands as possible. Each demand:
        {id, src, dst, demand_gbps, protected?}. Returns a typed
        solution/partial/no_solution with placed and unplaced demands."""
        model = snapshots.current()
        qot = make_adapter_evaluator(model, results)
        return placement_result_dict(
            _solve_allocation(model, qot, demands, spare_inventory,
                              objective=objective, weights=weights))

    return app


def main() -> None:
    """Entry-point for running the MCP server (stdio transport)."""
    build_app().run()
