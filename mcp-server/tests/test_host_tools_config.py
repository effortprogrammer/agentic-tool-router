from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


def _load_hub_module():
    package = types.ModuleType("mcp_tool_router")
    package.__path__ = []
    sys.modules.setdefault("mcp_tool_router", package)

    mod_http = types.ModuleType("mcp_tool_router.mcp_http")
    setattr(mod_http, "HttpMcpClient", object)
    sys.modules.setdefault("mcp_tool_router.mcp_http", mod_http)

    mod_stdio = types.ModuleType("mcp_tool_router.mcp_stdio")
    setattr(mod_stdio, "StdioMcpClient", object)
    sys.modules.setdefault("mcp_tool_router.mcp_stdio", mod_stdio)

    mod_registry = types.ModuleType("mcp_tool_router.registry")
    setattr(mod_registry, "ServerRegistry", object)
    setattr(mod_registry, "ServerSpec", object)
    sys.modules.setdefault("mcp_tool_router.registry", mod_registry)

    mod_router = types.ModuleType("mcp_tool_router.router")
    setattr(mod_router, "ToolRouter", object)
    sys.modules.setdefault("mcp_tool_router.router", mod_router)

    module_path = Path(__file__).resolve().parents[1] / "mcp_tool_router" / "hub.py"
    spec = importlib.util.spec_from_file_location("mcp_tool_router.hub", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load hub module for tests")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_hub = _load_hub_module()


class HostToolsConfigTests(unittest.TestCase):
    def test_parse_host_command_variants(self) -> None:
        self.assertEqual(_hub._parse_host_command("python3 -V"), ["python3", "-V"])
        self.assertEqual(_hub._parse_host_command(["python3", "-V"]), ["python3", "-V"])
        self.assertEqual(_hub._parse_host_command(None), [])

    def test_load_opencode_host_tools(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "opencode.json"
            payload = {
                "routerHostTools": [
                    {
                        "providerId": "opencode",
                        "name": "project_search",
                        "description": "Search project",
                        "command": ["python3", "-c", "print('ok')"],
                        "inputSchema": {
                            "type": "object",
                            "properties": {"pattern": {"type": "string"}},
                            "required": ["pattern"],
                        },
                        "enabled": True,
                        "readOnlyHint": True,
                    },
                    {
                        "name": "disabled_tool",
                        "command": ["python3", "-c", "print('skip')"],
                        "enabled": False,
                    },
                    {
                        "name": "default_provider_tool",
                        "command": "python3 -c \"print('ok')\"",
                        "enabled": True,
                    },
                ]
            }
            p.write_text(json.dumps(payload), encoding="utf-8")

            host_tools, definitions = _hub._load_opencode_host_tools(str(p))

            self.assertIn("opencode", host_tools)
            self.assertIn("project_search", host_tools["opencode"])
            self.assertEqual(
                host_tools["opencode"]["project_search"].command,
                ["python3", "-c", "print('ok')"],
            )

            self.assertIn("opencode", definitions)
            self.assertEqual(definitions["opencode"][0]["name"], "project_search")
            self.assertNotIn("disabled_tool", host_tools["opencode"])
            self.assertIn("default_provider_tool", host_tools["opencode"])

    def test_load_opencode_native_tools_from_runtime_endpoints(self) -> None:
        with (
            mock.patch.object(
                _hub,
                "_resolve_opencode_server_url",
                return_value="http://127.0.0.1:4096",
            ),
            mock.patch.object(
                _hub,
                "_http_json",
                side_effect=[
                    ["read_file", "router_select_tools"],
                    {"default": {"openai": "gpt-5.2"}},
                    [
                        {
                            "id": "read_file",
                            "description": "Read files",
                            "parameters": {
                                "type": "object",
                                "properties": {"path": {"type": "string"}},
                            },
                        }
                    ],
                ],
            ),
        ):
            host_tools, definitions = _hub._load_opencode_native_tools("ignored.json")

        self.assertIn("opencode-native", host_tools)
        self.assertIn("read_file", host_tools["opencode-native"])
        self.assertNotIn("router_select_tools", host_tools["opencode-native"])
        self.assertEqual(
            host_tools["opencode-native"]["read_file"].execution,
            "opencode",
        )
        self.assertEqual(definitions["opencode-native"][0]["name"], "read_file")

    def test_load_opencode_native_tools_fails_closed(self) -> None:
        with (
            mock.patch.object(
                _hub,
                "_resolve_opencode_server_url",
                return_value="http://127.0.0.1:4096",
            ),
            mock.patch.object(
                _hub,
                "_http_json",
                side_effect=RuntimeError("server down"),
            ),
        ):
            host_tools, definitions = _hub._load_opencode_native_tools("ignored.json")

        self.assertEqual(host_tools, {})
        self.assertEqual(definitions, {})

    def test_resolve_opencode_server_url_from_logs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log_dir = root / ".local" / "share" / "opencode" / "log"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "2026-02-22T081233.log").write_text(
                "INFO service=server url=http://127.0.0.1:52123/session/ses_abc SEARCH\n",
                encoding="utf-8",
            )

            with (
                mock.patch.dict(_hub.os.environ, {}, clear=True),
                mock.patch.object(
                    _hub.Path,
                    "home",
                    return_value=root,
                ),
                mock.patch.object(
                    _hub,
                    "_opencode_url_reachable",
                    side_effect=lambda url: url == "http://127.0.0.1:52123",
                ),
            ):
                resolved = _hub._resolve_opencode_server_url()

        self.assertEqual(resolved, "http://127.0.0.1:52123")


if __name__ == "__main__":
    unittest.main()
