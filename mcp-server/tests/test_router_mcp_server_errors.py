from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest


def _load_router_mcp_module():
    package = types.ModuleType("mcp_tool_router")
    package.__path__ = []
    sys.modules.setdefault("mcp_tool_router", package)

    mod_hub = types.ModuleType("mcp_tool_router.hub")
    setattr(mod_hub, "ToolRouterHub", object)
    sys.modules.setdefault("mcp_tool_router.hub", mod_hub)

    module_path = (
        Path(__file__).resolve().parents[1] / "mcp_tool_router" / "router_mcp_server.py"
    )
    spec = importlib.util.spec_from_file_location(
        "mcp_tool_router.router_mcp_server", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load router MCP server module for tests")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_router_mcp = _load_router_mcp_module()


class RouterMcpErrorClassificationTests(unittest.TestCase):
    def test_classifies_opencode_native_error(self) -> None:
        source = _router_mcp._classify_error_source(
            "Failed OpenCode request POST http://127.0.0.1:4096/session/foo/message: timed out"
        )
        self.assertEqual(source, "opencode-native")

    def test_classifies_mcp_server_error(self) -> None:
        source = _router_mcp._classify_error_source(
            "MCP server did not respond within 120.0s"
        )
        self.assertEqual(source, "mcp-server")

    def test_formats_error_with_source_operation_and_tool(self) -> None:
        err = RuntimeError("MCP server did not respond within 120.0s")
        rendered = _router_mcp._format_tool_error(
            err,
            operation="call_tool",
            tool_id="opencode-native:read",
        )
        self.assertIn("source=mcp-server", rendered)
        self.assertIn("operation=call_tool", rendered)
        self.assertIn("toolId=opencode-native:read", rendered)


if __name__ == "__main__":
    unittest.main()
