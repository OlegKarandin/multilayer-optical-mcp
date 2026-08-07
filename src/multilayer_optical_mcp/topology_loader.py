# src/multilayer_optical_mcp/topology_loader.py
"""Load a NetworkModel + static SRLGs from a topology JSON file at server
startup. Deployment/config-only: builds on model_from_abstract_graph and
add_srlg, both of which already exist -- this module adds no modeling logic
of its own, only a file format for supplying them at process start."""
from __future__ import annotations

import json
from pathlib import Path

from .model.assets import SRLG
from .model.modes import ModeRegistry
from .model.network import NetworkModel
from .model.topology_import import model_from_abstract_graph

REPO_ROOT = Path(__file__).resolve().parents[2]
MOD_FORMATS_YAML = REPO_ROOT / "modulation_formats.yaml"


def load_model_from_topology_file(
    path: str | Path, *, modes: ModeRegistry,
) -> NetworkModel:
    """Build a NetworkModel from a topology JSON file shaped:
    {"graph": {"nodes": [...], "edges": [...]}, "srlgs": [{"id": ..., "asset_ids": [...]}]}
    `graph` is passed verbatim to model_from_abstract_graph (unknown per-node/
    per-edge keys, e.g. lat/lon/mount_type, are ignored by that importer).
    `srlgs` is optional and defaults to none."""
    # utf-8-sig: strips a leading UTF-8 BOM if present (a no-op otherwise) --
    # Windows tools (PowerShell Set-Content -Encoding utf8, Notepad's "UTF-8"
    # save) commonly write one, and bare utf-8 decoding chokes on it.
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    model = model_from_abstract_graph(data["graph"], modes=modes)
    for srlg in data.get("srlgs", []):
        model.add_srlg(SRLG(id=srlg["id"], asset_ids=tuple(srlg["asset_ids"])))
    return model
