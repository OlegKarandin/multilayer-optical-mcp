# multilayer-optical-mcp

An MCP server that exposes multi-layer optical network analysis as a clean set
of callable tools: physical-layer QoT (via GNPy), IP-over-optical routing,
shared-risk groups, what-if analysis, and gated plan validate/commit.

This repo holds only the MCP tool surface (`server.py`) and its integration
tests. The underlying deterministic simulator — the IP-over-optical model,
GNPy adapter, solvers, and validator — lives in
[`multilayer-optical-network`](https://github.com/OlegKarandin/multilayer-optical-network),
which this package depends on. See that repo's README to use the simulator as
a standalone library.

## Install

```
pip install .[dev]
```

## Run

```
multilayer-optical-mcp --topology <topology.json> --state <state.json>
```

## Test

```
pytest tests/ -v
OPTICAL_NET_RUN_GNPY_E2E=1 pytest tests/e2e/ -v    # slow: real-GNPy build
```

See `multilayer-optical-network`'s `CLAUDE.md` for the full design charter
(core design rules, tool surface, disjointness model) this server implements.
