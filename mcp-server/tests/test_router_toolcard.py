from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


def _load_router_module():
    module_path = Path(__file__).resolve().parents[1] / "mcp_tool_router" / "router.py"
    spec = importlib.util.spec_from_file_location("mcp_tool_router_router", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load router module for tests")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_toolcard_from_definition = _load_router_module()._toolcard_from_definition


class ToolcardSourceMetadataTests(unittest.TestCase):
    def test_toolcard_includes_provider_source_metadata(self) -> None:
        card = _toolcard_from_definition(
            "opencode-native",
            {
                "name": "read_file",
                "description": "Read file contents",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path"}
                    },
                    "required": ["path"],
                },
            },
            source_type="host",
            source_platform="opencode",
        )

        self.assertEqual(card["toolId"], "opencode-native:read_file")
        self.assertEqual(card["serverId"], "opencode-native")
        self.assertEqual(card["providerId"], "opencode-native")
        self.assertEqual(card["sourceType"], "host")
        self.assertEqual(card["sourcePlatform"], "opencode")


if __name__ == "__main__":
    unittest.main()
