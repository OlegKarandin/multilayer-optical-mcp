"""Shared synthesizable topology helpers for the Phase 7 tests.

Phase 7 was written before correctness batches C1-C6 landed. Those batches made
GNPy synthesis strict in three ways the plan's toy fixtures (a bare amp+fiber OMS)
no longer satisfy:
  * S3-11: every OMS endpoint must resolve to a registered ``roadm_<node>`` with a
    launch transceiver — a ROADM-less endpoint raises.
  * S4-2/S4-3: gated (worse-direction) QoT needs a *paired reverse OMS*
    ``(dst,src)`` for every forward ``(src,dst)`` leg, or backward propagation
    raises.
So any Phase 7 test whose `validate_plan`/recompute path runs real GNPy must build
a bidirectional, ROADM-terminated span. `add_bidir_span` centralizes that; the
forward OMS id is the caller-supplied `oms_id` so the plan's `"omsAB"`-style
references stay intact.
"""
from __future__ import annotations

from multilayer_optical_mcp.model.assets import (
    Amplifier, Fiber, FiberType, OMS, ROADM, TransceiverMode, Transceiver,
)
from multilayer_optical_mcp.model.modes import ModeRegistry
from multilayer_optical_mcp.model.network import NetworkModel

# 400G needs 10 dB, 200G needs 7 dB; a single 80 km advanced-amp span delivers
# ~18.3 dB GSNR, so both modes sit comfortably above threshold — steady-state
# findings are driven by capacity, not by a marginal QoT.
MODES = ModeRegistry([
    TransceiverMode(id="400G", bitrate_gbps=400.0, required_gsnr_db=10.0,
                    symbol_rate_baud=87.5e9, channel_spacing_hz=100e9),
    TransceiverMode(id="200G", bitrate_gbps=200.0, required_gsnr_db=7.0,
                    symbol_rate_baud=43.75e9, channel_spacing_hz=100e9),
])


def new_model(modes: ModeRegistry | None = None) -> NetworkModel:
    m = NetworkModel(modes=modes or MODES)
    m.register_fiber_type(FiberType(type_variety="SSMF", loss_coef_db_per_km=0.2))
    return m


def _ensure_site(m: NetworkModel, node: str) -> None:
    rid = f"roadm_{node}"
    if rid not in m._roadms:
        m.add_roadm(ROADM(id=rid))
        m.add_transceiver(Transceiver(id=f"trx_{node}", site=node))


def _one_dir(m: NetworkModel, src: str, dst: str, tag: str) -> str:
    """One directed span in importer shape: roadm_<src> -> booster -> fiber ->
    preamp. Matches `topology_import._add_directed_oms` — the OMS `elements`
    start at the source ROADM (the drop `roadm_<dst>` is omitted, recovered by
    `terminal_roadm_id`). Link-level disjointness still compares SPANS, not the
    shared endpoint ROADM, because `path_basis_keys` excludes each path's own
    endpoint ROADM (see `exposure.path_endpoint_exclusions`). Returns the OMS id
    (== tag)."""
    m.add_amplifier(Amplifier(id=f"boost_{tag}", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    m.add_fiber(Fiber(id=f"f_{tag}", a_end=f"roadm_{src}", z_end=f"pre_{tag}",
                      length_km=80.0, type_variety="SSMF"))
    m.add_amplifier(Amplifier(id=f"pre_{tag}", type_variety="advanced_toy",
                              gain_db=20.0, nf_db=5.5))
    m.add_oms(OMS(id=tag, src_node_id=src, dst_node_id=dst,
                  elements=(f"roadm_{src}", f"boost_{tag}", f"f_{tag}", f"pre_{tag}")))
    return tag


def add_bidir_span(m: NetworkModel, src: str, dst: str, oms_id: str) -> str:
    """Add a synthesizable bidirectional span. The FORWARD OMS is named `oms_id`;
    the paired reverse OMS (needed for backward QoT) is `oms_id + "_rev"`. Returns
    the forward OMS id."""
    _ensure_site(m, src)
    _ensure_site(m, dst)
    _one_dir(m, src, dst, oms_id)
    _one_dir(m, dst, src, oms_id + "_rev")
    return oms_id
