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

from .model.assets import Lightpath
from .model.ip_assets import IPLink, Service
from .model.qot import QoTState

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
    links, services. Lightpaths are sorted by id so the same model and meta
    always produce byte-identical JSON for that collection; ordered paths
    (`oms_sequence`, `working_path`, `protection_path`) keep their physical
    order regardless.

    IP links and services are emitted in the model's own iteration order
    (insertion order, `list_ip_links`/`list_services` -- see network.py) and
    deliberately NOT id-sorted: `simulate_ip_routing` builds
    `IPRoutingResult.utilizations` by walking `list_ip_links()` in that same
    order, and its equality is order-sensitive. A build interleaves working
    and protection IP links per demand (`ipl-cand-...`, `ipl-prot-...`), so
    an alphabetical sort would group all "cand" links before all "prot" links
    -- same values, different tuple order -- and silently break
    `simulate_ip_routing(reloaded) == simulate_ip_routing(built)` even though
    every field's value round-tripped correctly. This is still deterministic
    for a fixed model instance (repeated dumps of the same, unmutated model
    iterate its dicts in the same order every time), it just does not imply
    id order.

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
        for link in model.list_ip_links()
    ]
    services = [
        {"id": s.id, "src_router": s.src_router, "dst_router": s.dst_router,
         "demand_gbps": s.demand_gbps, "working_path": list(s.working_path),
         "protection_path": list(s.protection_path)}
        for s in model.list_services()
    ]
    return {
        "format_version": FORMAT_VERSION,
        "meta": {**meta, "topology_fingerprint": fingerprint},
        "lightpaths": lightpaths,
        "ip_links": ip_links,
        "services": services,
    }


class StateFileError(ValueError):
    """A state document is malformed, is the wrong format version, or
    references something the imported topology does not contain. Raised
    instead of a bare ValueError/KeyError so startup fails with a structured,
    actionable message rather than a stack trace -- same contract as PlanError
    (model/plan.py)."""


def load_state(model: "NetworkModel", data: dict) -> None:
    """Apply a state document to a freshly imported model, in place.

    Order is load-bearing and not incidental: IP links reference lightpath ids
    and services reference IP link ids, and the model's mutators reject a
    forward reference outright. Lightpaths, then IP links, then services.
    """
    version = data.get("format_version")
    if version != FORMAT_VERSION:
        raise StateFileError(
            f"unsupported state-file format_version {version!r}; "
            f"this build supports {FORMAT_VERSION}")

    lightpath_recs = data.get("lightpaths", [])
    # Two passes, not one interleaved: add_lightpath invalidates recorded QoT
    # for every OTHER lightpath already added that shares one of its OMS (see
    # optical_network.py's _invalidate_qot_sharing_oms). Setting QoT inline
    # per-record would have a later lightpath on a shared OMS silently wipe
    # the QoT just restored for an earlier one -- add every lightpath first,
    # then restore QoT once no further invalidation can happen.
    for rec in lightpath_recs:
        try:
            model.add_lightpath(Lightpath(
                id=rec["id"], oms_sequence=tuple(rec["oms_sequence"]),
                mode_id=rec["mode_id"], center_freq_hz=rec["center_freq_hz"]))
        except (ValueError, KeyError, LookupError) as exc:
            raise StateFileError(f"lightpath {rec.get('id')!r}: {exc}") from exc

    for rec in lightpath_recs:
        qot = rec.get("qot")
        if qot is None:
            continue
        try:
            model.set_qot_state(rec["id"], QoTState(
                gsnr_db=qot["gsnr_db"], osnr_db=qot["osnr_db"],
                margin_db=qot["margin_db"],
                limiting_element_id=qot.get("limiting_element_id")))
        except (ValueError, KeyError, LookupError) as exc:
            raise StateFileError(f"lightpath {rec.get('id')!r}: {exc}") from exc

    for rec in data.get("ip_links", []):
        try:
            model.add_ip_link(IPLink(
                id=rec["id"], a_router=rec["a_router"], z_router=rec["z_router"],
                lightpath_id=rec["lightpath_id"]))
        except (ValueError, KeyError) as exc:
            raise StateFileError(f"ip_link {rec.get('id')!r}: {exc}") from exc

    for rec in data.get("services", []):
        try:
            model.add_service(Service(
                id=rec["id"], src_router=rec["src_router"],
                dst_router=rec["dst_router"], demand_gbps=rec["demand_gbps"],
                working_path=tuple(rec["working_path"]),
                protection_path=tuple(rec.get("protection_path", ()))))
        except (ValueError, KeyError) as exc:
            raise StateFileError(f"service {rec.get('id')!r}: {exc}") from exc
