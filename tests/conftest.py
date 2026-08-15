# Shared fixtures land here as the suite grows.
import asyncio
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def call_tool(app, name, **kwargs):
    """Invoke a registered @app.tool() closure, sync or coroutine-aware."""
    tool = app._tool_manager._tools[name]
    result = tool.fn(**kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result
