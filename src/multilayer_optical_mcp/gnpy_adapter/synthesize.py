from __future__ import annotations

from typing import Any, Dict, List

from ..model.network import NetworkModel

ROADM_TARGET_PCH_OUT_DB = -20.0
ROADM_ADD_DROP_OSNR = 33.0


def nf_type_variety(nf_db: float) -> str:
    """Stable name for the advanced-model Edfa type_variety carrying flat NF=nf_db."""
    return f"adv_nf_{nf_db:g}"


def model_to_gnpy_equipment(model: NetworkModel) -> Dict[str, Any]:
    """Build the GNPy equipment dict with one advanced_model Edfa per distinct NF."""
    nfs = sorted({amp.nf_db for amp in model._amplifiers.values()})
    edfa = [
        {
            "type_variety": nf_type_variety(nf),
            "type_def": "advanced_model",
            "gain_flatmax": 25,
            "gain_min": 0,
            "p_max": 23,
            "advanced_config_from_json": {
                "nf_fit_coeff": [0.0, 0.0, 0.0, float(nf)],
                "f_min": 191.275e12,
                "f_max": 196.125e12,
                "nf_ripple": [0.0],
                "dgt": [1.0],
                "gain_ripple": [0.0],
            },
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

    def connect(a: str, b: str) -> None:
        if (a, b) not in seen:
            seen.add((a, b))
            connections.append({"from_node": a, "to_node": b})

    for t in model._transceivers.values():
        connect(t.id, f"roadm_{t.site}")
        connect(f"roadm_{t.site}", t.id)

    for oms in model.list_oms():
        chain = list(oms.elements)
        for a, b in zip(chain, chain[1:]):
            connect(a, b)
        connect(chain[-1], f"roadm_{oms.dst_node_id}")

    return {"elements": elements, "connections": connections}
