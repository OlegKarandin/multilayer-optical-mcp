"""JSON-safety boundary for QoT-derived floats. Shared by views.py's hand-built
dict serializers and violations.py's Pydantic SafeFloat annotated type, so both
the direct-dict tool-return path and the real-MCP-protocol Pydantic path
sanitize non-finite floats via the exact same function -- no duplicated logic
to drift between the two."""
from __future__ import annotations

import math


def safe_float(x):
    """JSON-safety boundary for QoT-derived floats. `-inf` is used internally
    as the failed-asset margin sentinel (whatif.inject_failure) so the
    margin-feasibility gate reads it as infeasible; `+inf`/NaN also occur
    (no-noise GSNR, missing-baseline margin_before). Python's json module
    happily emits the bare `Infinity`/`-Infinity`/`NaN` tokens, which are not
    valid JSON per RFC 8259 and a strict client parser rejects it. Replace a
    non-finite float with a same-named JSON string sentinel here, at the
    serialization boundary, so callers doing real math on QoTState/DegradationRow
    fields upstream keep working with actual +-inf/NaN; only the outgoing dict
    is sanitized. Finite floats and non-float values pass through unchanged."""
    if isinstance(x, float) and not math.isfinite(x):
        if x != x:  # NaN is the only float that is not equal to itself
            return "NaN"
        return "Infinity" if x > 0 else "-Infinity"
    return x
