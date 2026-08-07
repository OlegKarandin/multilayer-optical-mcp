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
