"""Tests for the FastMCP server shell (phase 1 & 2 tools).

FastMCP 1.27.x API notes:
- app._tool_manager._tools  is the dict[name, Tool]
- app._tool_manager.list_tools()  is synchronous, returns list[Tool]
- app._tool_manager.call_tool(name, args)  is a coroutine

We call tool functions directly via app._tool_manager._tools[name].fn(...)
so that the test suite stays synchronous and independent of async machinery.
"""

from __future__ import annotations

import asyncio
import pytest

from multilayer_optical_mcp.server import build_app

_EXPECTED_TOOLS = {
    "get_transceiver_modes",
    "snapshot_create",
    "snapshot_branch",
    "snapshot_restore",
    "snapshot_diff",
    "compute_qot",
    "recompute_qot_under_loading",
    "get_qot_breakdown",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _tool_names(app) -> set[str]:
    """Return the set of registered tool names."""
    return set(app._tool_manager._tools.keys())


def _call(app, name: str, **kwargs):
    """Invoke a tool function directly (sync or coroutine-aware)."""
    tool = app._tool_manager._tools[name]
    result = tool.fn(**kwargs)
    # Some tools might be async in future; handle both.
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_build_app_returns_fastmcp_instance():
    from mcp.server.fastmcp import FastMCP
    app = build_app()
    assert isinstance(app, FastMCP)


def test_server_registers_phase_1_and_2_tools():
    app = build_app()
    registered = _tool_names(app)
    assert _EXPECTED_TOOLS.issubset(registered), (
        f"Missing: {_EXPECTED_TOOLS - registered}"
    )


def test_get_transceiver_modes_returns_eleven_yaml_modes():
    app = build_app()
    modes = _call(app, "get_transceiver_modes")
    assert isinstance(modes, list)
    assert len(modes) == 11, f"Expected 11 modes, got {len(modes)}: {modes}"


def test_get_transceiver_modes_has_correct_ids():
    app = build_app()
    modes = _call(app, "get_transceiver_modes")
    ids = {m["id"] for m in modes}
    # spot-check lowest and highest bitrate modes
    assert "300G@4.8dB" in ids, f"300G@4.8dB missing from {ids}"
    assert "800G@15.1dB" in ids, f"800G@15.1dB missing from {ids}"


def test_get_transceiver_modes_fields():
    app = build_app()
    modes = _call(app, "get_transceiver_modes")
    required_keys = {"id", "bitrate_gbps", "required_gsnr_db", "symbol_rate_baud", "channel_spacing_hz"}
    for m in modes:
        assert required_keys == set(m.keys()), f"Unexpected keys in mode: {m}"


def test_snapshot_create_returns_id():
    app = build_app()
    result = _call(app, "snapshot_create")
    assert "id" in result
    assert isinstance(result["id"], str)
    assert len(result["id"]) == 32  # uuid4 hex


def test_snapshot_branch_requires_valid_parent():
    app = build_app()
    snap = _call(app, "snapshot_create")
    branch = _call(app, "snapshot_branch", parent_id=snap["id"])
    assert "id" in branch
    assert branch["id"] != snap["id"]


def test_snapshot_restore_returns_restored_key():
    app = build_app()
    snap = _call(app, "snapshot_create")
    result = _call(app, "snapshot_restore", snapshot_id=snap["id"])
    assert result == {"restored": snap["id"]}


def test_snapshot_diff_returns_structured_delta():
    app = build_app()
    a = _call(app, "snapshot_create")
    b = _call(app, "snapshot_create")
    diff = _call(app, "snapshot_diff", a_id=a["id"], b_id=b["id"])
    # diff should have the standard delta keys
    assert "lightpaths" in diff
    assert "fibers" in diff
    assert "amplifiers" in diff


def test_snapshot_restore_on_expired_id_returns_typed_error():
    """Regression for the audit's Critical finding: an evicted/unknown
    snapshot id must return a typed error, not raise KeyError."""
    from multilayer_optical_mcp.server import build_app
    app = build_app()
    out = _call(app, "snapshot_restore", snapshot_id="nope")
    assert "error" in out


def test_snapshot_branch_on_expired_id_returns_typed_error():
    from multilayer_optical_mcp.server import build_app
    app = build_app()
    out = _call(app, "snapshot_branch", parent_id="nope")
    assert "error" in out


def test_snapshot_diff_on_expired_id_returns_typed_error():
    from multilayer_optical_mcp.server import build_app
    app = build_app()
    out = _call(app, "snapshot_diff", a_id="nope", b_id="also-nope")
    assert "error" in out
