# src/multilayer_optical_mcp/state_file.py
"""Read/write the operating-state file: the loaded steady state a build
produces, as a DELTA on top of an authored topology JSON.

Deployment/config-only, same charter as topology_loader.py -- this module adds
no modeling logic. It serializes exactly the four registries a build populates
(lightpaths, their QoT, IP links, services) and re-applies them through the
model's own mutators. The physical layer is deliberately NOT serialized: it is
re-derived every startup by model_from_abstract_graph, so it cannot round-trip
wrong. See docs/superpowers/specs/2026-08-07-operating-state-file-design.md.
"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:                                   # import cycle-free typing
    from .model.network import NetworkModel

FORMAT_VERSION = 1


def topology_fingerprint(data: dict) -> str:
    """Stable content hash of a topology document's `graph` and `srlgs`.

    Only those two keys participate: they are everything topology_loader reads,
    so a comment or provenance key added alongside them must not invalidate a
    state file. Canonical JSON (sorted keys, no whitespace) makes the digest
    independent of formatting and key order.
    """
    payload = {"graph": data.get("graph", {}), "srlgs": data.get("srlgs", [])}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _qot_record(model: "NetworkModel", lp_id: str) -> dict | None:
    """All four QoTState fields, or None when the build recorded no QoT."""
    try:
        q = model.get_qot_state(lp_id)
    except LookupError:
        return None
    return {"gsnr_db": q.gsnr_db, "osnr_db": q.osnr_db, "margin_db": q.margin_db,
            "limiting_element_id": q.limiting_element_id}


def dump_state(model: "NetworkModel", *, fingerprint: str, meta: dict) -> dict:
    """Serialize the registries a build populates: lightpaths (with QoT), IP
    links, services. Collections are sorted by id so the same model and meta
    always produce byte-identical JSON; ordered paths (`oms_sequence`,
    `working_path`, `protection_path`) keep their physical order.

    `meta` is provenance only -- nothing in it is fed back into the model on
    load. `fingerprint` is written into it as `topology_fingerprint`.
    """
    lightpaths = [
        {"id": lp.id, "oms_sequence": list(lp.oms_sequence), "mode_id": lp.mode_id,
         "center_freq_hz": lp.center_freq_hz, "qot": _qot_record(model, lp.id)}
        for lp in sorted(model.list_lightpaths(), key=lambda x: x.id)
    ]
    ip_links = [
        {"id": link.id, "a_router": link.a_router, "z_router": link.z_router,
         "lightpath_id": link.lightpath_id}
        for link in sorted(model.list_ip_links(), key=lambda x: x.id)
    ]
    services = [
        {"id": s.id, "src_router": s.src_router, "dst_router": s.dst_router,
         "demand_gbps": s.demand_gbps, "working_path": list(s.working_path),
         "protection_path": list(s.protection_path)}
        for s in sorted(model.list_services(), key=lambda x: x.id)
    ]
    return {
        "format_version": FORMAT_VERSION,
        "meta": {**meta, "topology_fingerprint": fingerprint},
        "lightpaths": lightpaths,
        "ip_links": ip_links,
        "services": services,
    }
