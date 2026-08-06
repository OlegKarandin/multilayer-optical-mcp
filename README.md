# multilayer-optical-mcp

[![CI](https://github.com/OlegKarandin/multilayer-optical-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/OlegKarandin/multilayer-optical-mcp/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

An MCP server that exposes multi-layer optical network analysis as a clean set
of callable tools. It wraps [GNPy](https://github.com/Telecominfraproject/oopt-gnpy)
for physical-layer quality-of-transmission (QoT) evaluation and adds the
primitives an agent (or a human operator) needs to ask **what-if** questions,
route lightpaths, reason about shared-risk groups, and validate change plans
before committing them.

## What it does

- **What-if analysis** — a physics-free margin-threshold screening sweep and
  a physically-grounded degradation path (`inject_degradation` →
  `recompute_qot_under_loading` → threshold crossings). Margin is always an
  *output*, never a dial.
- **Dynamic risk groups** — runtime-injected asset partitions, tested
  independently from static design-time SRLGs, so routing and disjointness
  queries can be re-audited against a partition that didn't exist when a
  working/protection pair was first certified.
- **IP-over-optical coupling** — IP link capacity is *derived* from the
  underlying lightpath's transceiver mode, never stored; a modulation
  downshift or a margin going negative propagates automatically to reduced
  (or zero) IP capacity.
- **Deterministic solvers** — k-shortest paths, disjoint-path computation,
  RSA, and a heuristic multi-layer allocator, each returning a typed
  `solution` / `partial` / `no_solution` result.
- **Snapshot/branch state engine** — every mutation is simulatable on a
  branch before it touches ground truth; `validate_plan` must pass before
  `commit_plan`, and `reconcile()` reads back actual state after a live
  commit to surface drift from partial control-plane failures.

## Architecture

```
                    MCP client (any agent, or a human via a host app)
                                    |
                          MCP tool surface (this server)
                                    |
        +---------------+-----------+-----------+----------------+
        |               |                       |                |
   State engine     GNPy adapter           Solvers          Validator
   (snapshot/        (QoT under            (k-shortest,      (typed
    branch/diff)      loading, spectrum     RSA, disjoint,    violation
                      feasibility)          heuristic alloc)  lists)
                                    |
                          Network model (in-memory)
                  IP layer over optical layer; services;
                  static SRLGs + dynamic risk groups
```

## Install

Requires Python >= 3.11.

```bash
pip install -e ".[dev]"
```

The `dev` extra pulls in the `server` extra (`mcp[cli]`, `pydantic`) plus
`pytest`, `pytest-cov`, and `ruff`. For an exact, reproducible pin set instead
of the loose `pyproject.toml` ranges, use:

```bash
pip install -r requirements-dev.txt
```

## Run

```bash
multilayer-optical-mcp
```

Runs the MCP server over stdio, ready to be pointed at by any MCP-speaking
client or host application.

## Test

```bash
pytest tests/
```

The suite is deterministic and seeded — same inputs, same outputs, every
time. A handful of slow, real-GNPy ground-truth tests are skipped by default;
opt in with:

```bash
MOMCP_RUN_GNPY_E2E=1 pytest tests/
```

## License

Apache-2.0 — see [`LICENSE`](LICENSE).