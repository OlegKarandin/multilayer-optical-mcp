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

    return app


def main() -> None:
    """Entry-point for running the MCP server (stdio transport)."""
    build_app().run()
