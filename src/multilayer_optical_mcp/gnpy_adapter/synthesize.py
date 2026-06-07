from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from ..model.network import NetworkModel

ROADM_TARGET_PCH_OUT_DB = -20.0
ROADM_ADD_DROP_OSNR = 33.0


def nf_type_variety(nf_db: float) -> str:
    """Stable name for the advanced-model Edfa type_variety carrying flat NF=nf_db."""
    return f"adv_nf_{nf_db:g}"


def _adv_config_path(nf: float, tmpdir: Path) -> str:
    """Write an advanced_model NF config file and return its path string."""
    cfg = {
        "nf_fit_coeff": [0.0, 0.0, 0.0, float(nf)],
        "f_min": 191.275e12,
        "f_max": 196.125e12,
        "nf_ripple": [0.0],
        "dgt": [1.0],
        "gain_ripple": [0.0],
    }
    p = tmpdir / f"adv_nf_{nf:g}.json"
    p.write_text(json.dumps(cfg))
    return str(p)


def model_to_gnpy_equipment(model: NetworkModel,
                             _tmpdir: "Path | None" = None) -> Dict[str, Any]:
    """Build the GNPy equipment dict with one advanced_model Edfa per distinct NF.

    ``advanced_config_from_json`` is set to a file-path string (gnpy 2.11.1 reads
    it as a path, not an inline dict).  A temporary directory is created once per
    call; pass ``_tmpdir`` to control the location (tests may do this).
    """
    if _tmpdir is None:
        _tmpdir = Path(tempfile.mkdtemp())
    nfs = sorted({amp.nf_db for amp in model._amplifiers.values()})
    edfa = [
        {
            "type_variety": nf_type_variety(nf),
            "type_def": "advanced_model",
            "gain_flatmax": 25,
            "gain_min": 0,
            "p_max": 23,
            "advanced_config_from_json": _adv_config_path(nf, _tmpdir),
            "out_voa_auto": False,
            "allowed_for_design": True,
        }
        for nf in nfs
    ]
    return {
        "Edfa": edfa,
        "Fiber": [{"type_variety": "SSMF", "dispersion": 1.67e-05,
                   "effective_area": 83e-12, "pmd_coef": 1.265e-15}],
        "Span": [{"power_mode": True, "delta_power_range_db": [0, 0, 0.5],
                  "max_fiber_lineic_loss_for_raman": 0.25, "target_extended_gain": 2.5,
                  "max_length": 150, "length_units": "km", "max_loss": 28,
                  "padding": 10, "EOL": 0, "con_in": 0, "con_out": 0}],
        "Roadm": [{"target_pch_out_db": ROADM_TARGET_PCH_OUT_DB,
                   "add_drop_osnr": ROADM_ADD_DROP_OSNR, "pmd": 0, "pdl": 0,
                   "restrictions": {"preamp_variety_list": [], "booster_variety_list": []}}],
        "SI": [{"f_min": 191.3e12, "baud_rate": 87.5e9, "f_max": 196.1e12,
                "spacing": 100e9, "power_dbm": 0, "power_range_db": [0, 0, 1],
                "roll_off": 0.15, "tx_osnr": 40, "sys_margins": 2}],
        "Transceiver": [{"type_variety": "vendor-A",
                         "frequency": {"min": 191.35e12, "max": 196.1e12}, "mode": []}],
    }


def model_to_gnpy_topology(model: NetworkModel) -> Dict[str, Any]:
    """Build the GNPy {elements, connections} dict from the model."""
    elements: List[Dict[str, Any]] = []
    for r in model._roadms.values():
        elements.append({"uid": r.id, "type": "Roadm"})
    for t in model._transceivers.values():
        elements.append({"uid": t.id, "type": "Transceiver"})
    for a in model._amplifiers.values():
        elements.append({"uid": a.id, "type": "Edfa",
                         "type_variety": nf_type_variety(a.nf_db),
                         "operational": {"gain_target": a.gain_db, "tilt_target": 0}})
    for f in model._fibers.values():
        loss = model.get_fiber_type(f.type_variety).loss_coef_db_per_km
        elements.append({"uid": f.id, "type": "Fiber", "type_variety": f.type_variety,
                         "params": {"length": f.length_km, "length_units": "km",
                                    "loss_coef": loss, "att_in": 0,
                                    "con_in": 0, "con_out": 0}})

    connections: List[Dict[str, str]] = []
    seen: set = set()
    # Collect synthetic transceiver UIDs we need to add (for legacy test models
    # whose OMS src_node_id / dst_node_id is a bare transceiver UID, not a ROADM
    # key and not an explicitly registered transceiver).
    _synthetic_trx: set = set()

    def connect(a: str, b: str) -> None:
        if (a, b) not in seen:
            seen.add((a, b))
            connections.append({"from_node": a, "to_node": b})

    def _resolve_endpoint(node_id: str) -> str:
        """Return the GNPy UID for an OMS endpoint (src or dst).

        Priority:
        1. ``roadm_<node_id>`` exists as a real ROADM → use the ROADM uid.
        2. ``node_id`` is an explicitly registered transceiver → use it directly.
        3. Legacy / test model with bare UID → register as synthetic transceiver
           and return the raw uid.
        """
        roadm_uid = f"roadm_{node_id}"
        if roadm_uid in model._roadms:
            return roadm_uid
        if node_id in model._transceivers:
            return node_id
        # Legacy bare uid — synthesize a Transceiver element on first encounter.
        _synthetic_trx.add(node_id)
        return node_id

    for t in model._transceivers.values():
        connect(t.id, f"roadm_{t.site}")
        connect(f"roadm_{t.site}", t.id)

    for oms in model.list_oms():
        chain = list(oms.elements)
        for a, b in zip(chain, chain[1:]):
            connect(a, b)

        # Wire src → first element (so the first ROADM/element has a predecessor).
        # Skip when src resolves to the same UID as chain[0] (importer models embed
        # the ROADM as the first OMS element, so the src IS chain[0]).
        src_uid = _resolve_endpoint(oms.src_node_id)
        if src_uid != chain[0]:
            connect(src_uid, chain[0])

        # Wire last element → dst.
        dst_uid = _resolve_endpoint(oms.dst_node_id)
        connect(chain[-1], dst_uid)

    # Append synthetic transceiver elements (deduped, stable order).
    for uid in sorted(_synthetic_trx):
        elements.append({"uid": uid, "type": "Transceiver"})

    return {"elements": elements, "connections": connections}


def build_gnpy_network(model: NetworkModel):
    """Return (equipment, network) built from the model, ready to propagate.

    Reuses gnpy's network_from_json + build_network — the same code path load_toy
    uses — so synthesized results match a hand-written topology of the same shape.
    """
    from gnpy.tools.json_io import network_from_json
    from gnpy.core.network import build_network

    equipment = _equipment_from_dict(model_to_gnpy_equipment(model))
    network = network_from_json(model_to_gnpy_topology(model), equipment)
    build_network(network, equipment, pref_ch_db=0.0, pref_total_db=0.0)
    return equipment, network


def _equipment_from_dict(eqpt_dict: Dict[str, Any]):
    """Turn the equipment dict into gnpy Equipment objects.

    gnpy 2.11.1's ``Amp.from_json`` resolves ``advanced_config_from_json`` relative
    to the equipment file, so the dict must be written to a real file next to the
    already-written NF config files.  We use the same parent directory as the first
    advanced config file (guaranteed to exist when this function is called from
    ``build_gnpy_network``).  Falls back to a fresh temp dir if no EDFA entries are
    present.
    """
    from gnpy.tools.json_io import load_equipment

    # Determine a stable parent directory — use the dir of the first advanced
    # config path already written into the dict, so relative-path resolution works.
    parent: "Path | None" = None
    for entry in eqpt_dict.get("Edfa", []):
        cfg_path = entry.get("advanced_config_from_json")
        if isinstance(cfg_path, str):
            parent = Path(cfg_path).parent
            break
    if parent is None:
        parent = Path(tempfile.mkdtemp())

    eqpt_file = parent / "eqpt.json"
    eqpt_file.write_text(json.dumps(eqpt_dict))
    return load_equipment(eqpt_file)
