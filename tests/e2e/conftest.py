"""Shared session-scoped real-adapter german_17 build for e2e disaster-scenario
tests. Mirrors tests/model/test_scenario.py::test_german_17_end_to_end_real_adapter
(same seed/params) so both scenario tests reuse one ~180s build instead of two."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from multilayer_optical_mcp.model.modes import load_modulation_formats
from multilayer_optical_mcp.model.qot_results import QoTResultStore, QoTCache
from multilayer_optical_mcp.model.allocation import make_adapter_evaluator
from multilayer_optical_mcp.model.scenario import build_operating_network
from multilayer_optical_mcp.model.topology_import import model_from_abstract_graph

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def german17_built():
    if not os.environ.get("MOMCP_RUN_GNPY_E2E"):
        pytest.skip("slow real-GNPy build; set MOMCP_RUN_GNPY_E2E=1 to run")

    graph = json.loads((_REPO / "topologies/german_17.json").read_text(encoding="utf-8"))
    modes = load_modulation_formats(_REPO / "modulation_formats.yaml")
    model = model_from_abstract_graph(graph, modes=modes)
    store = QoTResultStore()
    cache = QoTCache()
    qot = make_adapter_evaluator(model, store, cache=cache)

    res = build_operating_network(
        model, seed=0, qot=qot, target_mean_util=0.4, max_util_cap=0.95,
        max_iters=10, store=store)
    assert res.model.list_lightpaths()
    return res
