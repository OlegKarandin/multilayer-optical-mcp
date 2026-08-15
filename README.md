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

## Demo

`multilayer-optical-network`'s
[`examples/risk_group_exposure.py`](https://github.com/OlegKarandin/multilayer-optical-network/blob/master/examples/risk_group_exposure.py)
walks the flagship scenario as direct library calls: a protected service whose
working/protection legs were routed physically disjoint, until a
runtime-injected risk group (standing in for whatever a downstream geo-mapper
decided intersects a hazard polygon — this repo never sees the polygon
itself) reveals they were correlated all along. Exposure audit, disjointness
re-check under two bases, a real failure injection proving the correlation,
replan, validate, and a live commit + reconcile.

The equivalent sequence through this server's MCP tool surface, against a
model seeded with the same `german_17` reference network:

```
define_risk_group(rg_id="hazard-zone-1", asset_ids=[<the two spans>])
get_exposure(service_id=<svc>, risk_group_id="hazard-zone-1")
    -> both_intersect: true
check_disjointness(path_a=<working oms>, path_b=<protection oms>,
                    basis="physical",   level="link")  -> disjoint: true
check_disjointness(path_a=<working oms>, path_b=<protection oms>,
                    basis="risk_group", level="link")  -> disjoint: false,
                                                           shared_groups: ["hazard-zone-1"]
inject_failure(asset_ids=[<the two spans>])            -> protection leg down too
compute_disjoint_paths(src=<a>, dst=<z>, basis="risk_group", level="link")
commit_plan(plan={...}, dry_run=true,  basis="risk_group", level="link")
commit_plan(plan={...}, dry_run=false, confirm=true, basis="risk_group", level="link")
reroute_service(service_id=<svc>, ip_path=[...], which="protection")
```

`tests/e2e/test_flood_scenario.py` asserts the `define_risk_group` /
`get_exposure` / `commit_plan` (dry-run then live) / `reroute_service` steps
above through `call_tool` against a real GNPy-built network, after driving
the disjointness/failure-injection/replan steps at the model layer (the same
solvers back both surfaces, so the finding is identical either way). Gated
behind `OPTICAL_NET_RUN_GNPY_E2E=1`, since it drives the real GNPy adapter.
