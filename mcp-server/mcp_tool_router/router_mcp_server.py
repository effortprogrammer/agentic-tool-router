from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from typing import Any, Iterable

from .hub import ToolRouterHub

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "mcp-tool-router"
SERVER_VERSION = "0.1.0"

DEFAULT_TOP_K = 20
DEFAULT_BUDGET_TOKENS = 1500


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RouterMcpServer:
    def __init__(
        self,
        hub: ToolRouterHub,
        default_session: str = "default",
    ) -> None:
        self._hub = hub
        self._default_session = default_session
        self._tools = [
            {
                "name": "router_select_tools",
                "description": (
                    "Select the most relevant MCP tools for a query. "
                    "Returns tool IDs and (optionally) tool definitions."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "User request to route.",
                        },
                        "sessionId": {
                            "type": "string",
                            "description": "Session identifier.",
                        },
                        "topK": {
                            "type": "integer",
                            "description": "Max tools to return.",
                        },
                        "budgetTokens": {
                            "type": "integer",
                            "description": "Tool token budget.",
                        },
                        "includeTools": {
                            "type": "boolean",
                            "description": "Include selected tool definitions in the response.",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["bm25", "regex"],
                            "description": "Search mode: 'bm25' (default) for ranked keyword search, 'regex' for pattern matching.",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "router_call_tool",
                "description": "Call a selected MCP tool by toolId.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "toolId": {
                            "type": "string",
                            "description": "{serverId}:{toolName}",
                        },
                        "arguments": {
                            "type": "object",
                            "description": "Tool arguments.",
                        },
                        "sessionId": {
                            "type": "string",
                            "description": "Session identifier.",
                        },
                        "reduce": {
                            "type": "boolean",
                            "description": "Return reduced result alongside raw output.",
                        },
                    },
                    "required": ["toolId"],
                },
            },
            {
                "name": "router_tool_info",
                "description": (
                    "Get detailed information about a specific tool including its full JSON schema. "
                    "Use this to inspect a tool's parameters before calling it."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "toolId": {
                            "type": "string",
                            "description": "Tool ID in '{serverId}:{toolName}' format.",
                        },
                    },
                    "required": ["toolId"],
                },
            },
        ]

    def serve(self) -> None:
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" not in message:
                self._handle_notification(message)
                continue
            response = self._handle_request(message)
            self._write_response(response)

    def _handle_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method == "initialized":
            return None
        return None

    def _handle_request(self, message: dict[str, Any]) -> dict[str, Any]:
        request_id = message.get("id")
        try:
            result = self._dispatch(message.get("method"), message.get("params") or {})
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except RpcError as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": exc.code, "message": exc.message},
            }
        except Exception as exc:  # pragma: no cover - safety net
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    def _dispatch(self, method: str | None, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return self._handle_initialize(params)
        if method == "tools/list":
            return {"tools": self._tools}
        if method == "tools/call":
            return self._handle_tools_call(params)
        raise RpcError(-32601, f"Unknown method '{method}'.")

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        _ = params
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _handle_tools_call(self, params: dict[str, Any]) -> Any:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "router_select_tools":
            payload = self._select_tools(arguments)
        elif name == "router_call_tool":
            payload = self._call_tool(arguments)
        elif name == "router_tool_info":
            payload = self._tool_info(arguments)
        else:
            raise RpcError(-32601, f"Unknown tool '{name}'.")
        return _wrap_content(payload)

    def _select_tools(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise RpcError(-32602, "router_select_tools requires 'query'.")
        session_id = str(arguments.get("sessionId") or self._default_session)
        top_k = _coerce_int(arguments.get("topK"), DEFAULT_TOP_K)
        budget_tokens = _coerce_int(
            arguments.get("budgetTokens"), DEFAULT_BUDGET_TOKENS
        )
        include_tools = bool(arguments.get("includeTools", True))
        mode = arguments.get("mode")
        if mode not in ("bm25", "regex", None):
            mode = None

        tool_ids = self._hub.select_tools(
            session_id,
            query,
            top_k=top_k,
            budget_tokens=budget_tokens,
            mode=mode,
        )
        result: dict[str, Any] = {"selectedToolIds": tool_ids}
        if include_tools:
            result["tools"] = self._format_tools(tool_ids)
        return result

    def _call_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tool_id = str(arguments.get("toolId") or arguments.get("tool_id") or "").strip()
        if not tool_id:
            raise RpcError(-32602, "router_call_tool requires 'toolId'.")
        payload = arguments.get("arguments") or {}
        result = self._hub.call_tool(tool_id, payload)

        session_id = arguments.get("sessionId") or self._default_session
        if session_id:
            self._hub.router.mark_tool_used(str(session_id), tool_id)

        if bool(arguments.get("reduce")):
            reduced = self._hub.router.reduce_result(tool_id, result)
            return {"toolId": tool_id, "rawResult": result, "reduced": reduced}
        return result

    def _tool_info(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tool_id = str(arguments.get("toolId") or arguments.get("tool_id") or "").strip()
        if not tool_id:
            raise RpcError(-32602, "router_tool_info requires 'toolId'.")
        router = self._hub.router
        card = router.get_tool_card(tool_id)
        raw = router.get_raw_tool(tool_id)
        if card is None and raw is None:
            raise RpcError(-32602, f"Tool '{tool_id}' not found.")
        result: dict[str, Any] = {"toolId": tool_id}
        if card is not None:
            result["toolCard"] = card
        if raw is not None:
            result["rawDefinition"] = raw
        return result

    def _format_tools(self, tool_ids: Iterable[str]) -> list[dict[str, Any]]:
        router = self._hub.router
        tools: list[dict[str, Any]] = []
        for tool_id in tool_ids:
            card = router.get_tool_card(tool_id) or {}
            raw = router.get_raw_tool(tool_id) or {}
            tool: dict[str, Any] = dict(raw) if raw else {}
            if "name" not in tool and card.get("toolName"):
                tool["name"] = card["toolName"]
            if "description" not in tool and card.get("description"):
                tool["description"] = card["description"]
            if "inputSchema" not in tool:
                if "input_schema" in tool:
                    tool["inputSchema"] = tool["input_schema"]
                elif card:
                    tool["inputSchema"] = _schema_from_card(card)
            tool["toolId"] = tool_id
            tool["serverId"] = card.get("serverId") or tool_id.split(":", 1)[0]
            tool["toolName"] = card.get("toolName") or tool.get("name")
            tools.append(tool)
        return tools

    def _write_response(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _wrap_content(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and "content" in payload:
        return payload
    text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return {"content": [{"type": "text", "text": text}]}


def _schema_from_card(card: dict[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    required: list[str] = []
    for arg in card.get("args", []):
        if not isinstance(arg, dict):
            continue
        name = arg.get("name")
        if not name:
            continue
        prop: dict[str, Any] = {}
        if "description" in arg:
            prop["description"] = arg["description"]
        type_hint = arg.get("typeHint")
        if isinstance(type_hint, str):
            base_type = type_hint.split(":", 1)[0]
            if base_type in {
                "string",
                "number",
                "integer",
                "boolean",
                "object",
                "array",
            }:
                prop["type"] = base_type
            else:
                prop["type"] = "string"
        else:
            prop["type"] = "string"
        if "example" in arg:
            prop["example"] = arg["example"]
        props[str(name)] = prop
        if arg.get("required"):
            required.append(str(name))
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def _coerce_int(value: Any, default: int) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _default_routerd_cmd() -> str:
    env = os.environ.get("ROUTERD")
    if env:
        return env
    if shutil.which("tool-routerd"):
        return "tool-routerd"
    return "node packages/daemon/dist/cli.js"


def _parse_id_list(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def _load_hub() -> tuple[ToolRouterHub, str, bool, list[str], str]:
    config_path = os.environ.get("OPENCODE_CONFIG", "~/.config/opencode/opencode.json")
    include_disabled = os.environ.get(
        "ROUTER_INCLUDE_DISABLED", "true"
    ).lower() not in {"0", "false", "no"}
    ignore_ids = _parse_id_list(os.environ.get("ROUTER_IGNORE_IDS"))
    router_id = os.environ.get("ROUTER_MCP_ID")
    if router_id:
        ignore_ids.add(router_id)
    if not ignore_ids:
        ignore_ids.add("router")
    routerd_cmd = _default_routerd_cmd()
    hub = ToolRouterHub.from_opencode_config(
        config_path,
        routerd_path=routerd_cmd,
        auto_sync=True,
        include_disabled=include_disabled,
        ignore_ids=sorted(ignore_ids),
    )
    return hub, config_path, include_disabled, sorted(ignore_ids), routerd_cmd


class _ConfigWatcher(threading.Thread):
    def __init__(
        self,
        hub: ToolRouterHub,
        config_path: str,
        include_disabled: bool,
        ignore_ids: Iterable[str],
        routerd_cmd: str,
        interval: float = 1.0,
    ) -> None:
        super().__init__(daemon=True)
        self._hub = hub
        self._config_path = os.path.expanduser(config_path)
        self._include_disabled = include_disabled
        self._ignore_ids = list(ignore_ids)
        self._routerd_cmd = routerd_cmd
        self._interval = max(0.25, interval)
        self._stop_event = threading.Event()
        self._last_mtime = self._current_mtime()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                mtime = self._current_mtime()
                if mtime and self._last_mtime and mtime > self._last_mtime:
                    self._reload()
                if mtime:
                    self._last_mtime = mtime
            except Exception:
                pass
            self._stop_event.wait(self._interval)

    def _current_mtime(self) -> float | None:
        try:
            return os.path.getmtime(self._config_path)
        except OSError:
            return None

    def _reload(self) -> None:
        registry = ToolRouterHub.from_opencode_config(  # type: ignore[arg-type]
            self._config_path,
            routerd_path=self._routerd_cmd,
            auto_sync=True,
            include_disabled=self._include_disabled,
            ignore_ids=self._ignore_ids,
        ).registry
        self._hub.reload_registry(registry)


def main() -> int:
    hub, config_path, include_disabled, ignore_ids, routerd_cmd = _load_hub()
    interval = _coerce_int(os.environ.get("ROUTER_WATCH_INTERVAL"), 1)
    watcher = _ConfigWatcher(
        hub,
        config_path,
        include_disabled,
        ignore_ids,
        routerd_cmd,
        interval=float(max(0.25, interval)),
    )
    watcher.start()
    default_session = os.environ.get("ROUTER_SESSION_ID", "default")
    server = RouterMcpServer(hub, default_session=default_session)
    try:
        server.serve()
    finally:
        watcher.stop()
        hub.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
