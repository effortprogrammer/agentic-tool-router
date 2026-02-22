from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import time
import sys
import threading
import types
import unittest
from unittest import mock


def _load_gateway_module():
    package = types.ModuleType("mcp_tool_router")
    package.__path__ = []
    sys.modules.setdefault("mcp_tool_router", package)

    mod_hub = types.ModuleType("mcp_tool_router.hub")
    setattr(mod_hub, "ToolRouterHub", object)
    setattr(mod_hub, "_resolve_opencode_server_url", lambda: "http://127.0.0.1:4096")
    sys.modules.setdefault("mcp_tool_router.hub", mod_hub)

    module_path = (
        Path(__file__).resolve().parents[1]
        / "mcp_tool_router"
        / "opencode_gateway_server.py"
    )
    spec = importlib.util.spec_from_file_location(
        "mcp_tool_router.opencode_gateway_server", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load gateway module for tests")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_gateway = _load_gateway_module()


class _DummyHub:
    def __init__(self, selected: list[str]) -> None:
        self._selected = selected

    def select_tools(
        self,
        session_id: str,
        query: str,
        top_k: int,
        budget_tokens: int,
    ) -> list[str]:
        _ = (session_id, query, top_k, budget_tokens)
        return list(self._selected)


class GatewayServerTests(unittest.TestCase):
    def test_query_text_from_parts(self) -> None:
        text = _gateway._query_text_from_parts(
            [
                {"type": "text", "text": "first"},
                {"type": "file", "url": "x"},
                {"type": "text", "text": "second"},
            ]
        )
        self.assertEqual(text, "first\nsecond")

    def test_map_selected_to_runtime_ids(self) -> None:
        runtime_ids = {"read", "grep", "github_create_pull_request"}
        selected = [
            "opencode-native:read",
            "opencode-native:grep",
            "github:create_pull_request",
        ]
        mapped = _gateway._map_selected_to_runtime_ids(selected, runtime_ids)
        self.assertEqual(mapped, ["read", "grep", "github_create_pull_request"])

    def test_inject_tools_allowlist_intersects_existing_tools(self) -> None:
        state = _gateway._GatewayState(
            hub=_DummyHub(["opencode-native:read", "opencode-native:grep"]),
            config=_gateway.GatewayConfig(
                bind_host="127.0.0.1",
                bind_port=4141,
                upstream_url="http://127.0.0.1:4096",
                request_timeout_sec=5,
                stream_timeout_sec=0,
                select_timeout_sec=2,
                select_top_k=20,
                select_budget_tokens=1500,
                default_session_id="default",
                max_runtime_ids_cache_sec=3,
            ),
            lock=threading.Lock(),
        )
        body = {
            "parts": [{"type": "text", "text": "read file"}],
            "tools": {"read": True, "grep": False},
        }

        with (
            mock.patch(
                "mcp_tool_router.opencode_gateway_server._runtime_tool_ids",
                return_value={"read", "grep", "bash"},
            ),
            mock.patch(
                "mcp_tool_router.opencode_gateway_server._update_session_permissions"
            ),
        ):
            patched = _gateway._inject_tools_allowlist(
                state,
                "/session/ses_123/message",
                {},
                json.dumps(body).encode("utf-8"),
            )

        parsed = json.loads(patched.decode("utf-8"))
        self.assertEqual(parsed["tools"], {"read": True})

    def test_inject_tools_allowlist_disables_all_when_empty_selection(self) -> None:
        state = _gateway._GatewayState(
            hub=_DummyHub([]),
            config=_gateway.GatewayConfig(
                bind_host="127.0.0.1",
                bind_port=4141,
                upstream_url="http://127.0.0.1:4096",
                request_timeout_sec=5,
                stream_timeout_sec=0,
                select_timeout_sec=2,
                select_top_k=20,
                select_budget_tokens=1500,
                default_session_id="default",
                max_runtime_ids_cache_sec=3,
            ),
            lock=threading.Lock(),
        )
        body = {"parts": [{"type": "text", "text": "hello"}]}

        with (
            mock.patch(
                "mcp_tool_router.opencode_gateway_server._runtime_tool_ids",
                return_value={"read", "grep", "bash"},
            ),
            mock.patch(
                "mcp_tool_router.opencode_gateway_server._update_session_permissions"
            ),
        ):
            patched = _gateway._inject_tools_allowlist(
                state,
                "/session/ses_123/message",
                {},
                json.dumps(body).encode("utf-8"),
            )

        parsed = json.loads(patched.decode("utf-8"))
        self.assertEqual(parsed["tools"], {"bash": False, "grep": False, "read": False})

    def test_should_stream_proxy_event_path(self) -> None:
        self.assertTrue(_gateway._should_stream_proxy("/event", {}))

    def test_should_stream_proxy_accept_header(self) -> None:
        self.assertTrue(
            _gateway._should_stream_proxy(
                "/session/ses_1/message",
                {"Accept": "text/event-stream"},
            )
        )

    def test_should_not_stream_proxy_session_message_post(self) -> None:
        self.assertFalse(
            _gateway._should_stream_proxy(
                "/session/ses_1/message",
                {},
                "POST",
            )
        )

    def test_select_timeout_fails_open_without_hang(self) -> None:
        class _SlowHub:
            def select_tools(self, **kwargs):
                _ = kwargs
                time.sleep(0.2)
                return ["opencode-native:read"]

        state = _gateway._GatewayState(
            hub=_SlowHub(),
            config=_gateway.GatewayConfig(
                bind_host="127.0.0.1",
                bind_port=4141,
                upstream_url="http://127.0.0.1:4096",
                request_timeout_sec=5,
                stream_timeout_sec=0,
                select_timeout_sec=0.01,
                select_top_k=20,
                select_budget_tokens=1500,
                default_session_id="default",
                max_runtime_ids_cache_sec=3,
            ),
            lock=threading.Lock(),
        )
        body = {"parts": [{"type": "text", "text": "hello"}]}
        start = time.time()
        patched = _gateway._inject_tools_allowlist(
            state,
            "/session/ses_123/message",
            {},
            json.dumps(body).encode("utf-8"),
        )
        elapsed = time.time() - start
        self.assertLess(elapsed, 0.15)
        self.assertEqual(patched.decode("utf-8"), json.dumps(body))

    def test_update_session_permissions_deduplicates_same_rules(self) -> None:
        state = _gateway._GatewayState(
            hub=_DummyHub([]),
            config=_gateway.GatewayConfig(
                bind_host="127.0.0.1",
                bind_port=4141,
                upstream_url="http://127.0.0.1:4096",
                request_timeout_sec=5,
                stream_timeout_sec=0,
                select_timeout_sec=2,
                select_top_k=20,
                select_budget_tokens=1500,
                default_session_id="default",
                max_runtime_ids_cache_sec=3,
            ),
            lock=threading.Lock(),
            session_permission_hashes={},
        )

        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False

        with mock.patch(
            "mcp_tool_router.opencode_gateway_server.urlrequest.urlopen",
            return_value=response,
        ) as mocked_open:
            _gateway._update_session_permissions(
                state,
                session_id="ses_1",
                directory=None,
                runtime_all_ids={"read", "bash"},
                allowed_ids={"read"},
            )
            _gateway._update_session_permissions(
                state,
                session_id="ses_1",
                directory=None,
                runtime_all_ids={"read", "bash"},
                allowed_ids={"read"},
            )

        self.assertEqual(mocked_open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
