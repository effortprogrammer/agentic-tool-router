from __future__ import annotations

from dataclasses import dataclass
import json
import queue
import re
import shlex
import subprocess
import threading
from typing import Any, Iterable


_ROUTERD_TIMEOUT: float = 30.0


@dataclass
class RouterConfig:
    routerd_path: str | None = None
    transport: str = "stdio"


class _StdioJsonRpcClient:
    def __init__(self, argv: list[str], timeout: float = _ROUTERD_TIMEOUT) -> None:
        self._timeout = timeout
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._lock = threading.Lock()
        self._pending: dict[int, queue.Queue[dict]] = {}
        self._next_id = 1
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def request(self, method: str, params: dict | None = None) -> Any:
        if self._closed:
            raise RuntimeError("JSON-RPC client is closed")
        request_id, pending = self._reserve_id()
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        line = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(line + "\n")
            self._proc.stdin.flush()
        except Exception as exc:
            self._fail_all_pending(f"Failed to write request: {exc}")
            raise
        response = self._await_response(pending)
        if "error" in response:
            raise RuntimeError(f"JSON-RPC error: {response['error']}")
        return response.get("result")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        finally:
            self._proc.terminate()
            self._reader.join(timeout=1)

    def _reserve_id(self) -> tuple[int, queue.Queue[dict]]:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            pending = queue.Queue(maxsize=1)
            self._pending[request_id] = pending
            return request_id, pending

    def _await_response(self, pending: queue.Queue[dict]) -> dict:
        try:
            return pending.get(timeout=self._timeout)
        except queue.Empty:
            raise RuntimeError(
                f"routerd did not respond within {self._timeout}s"
            ) from None

    def _read_loop(self) -> None:
        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            response_id = message.get("id")
            if response_id is None:
                continue
            with self._lock:
                pending = self._pending.pop(response_id, None)
            if pending is not None:
                pending.put(message)
        self._closed = True
        self._fail_all_pending("routerd closed")

    def _fail_all_pending(self, reason: str) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.put({"error": {"message": reason}})


class ToolRouter:
    def __init__(
        self, routerd_path: str | None = None, transport: str = "stdio"
    ) -> None:
        self._config = RouterConfig(routerd_path=routerd_path, transport=transport)
        self._client: _StdioJsonRpcClient | None = None
        self._raw_tool_cache: dict[str, dict[str, Any]] = {}
        self._tool_cache: dict[str, dict[str, Any]] = {}

    def _rpc(self) -> _StdioJsonRpcClient:
        if self._config.transport != "stdio":
            raise ValueError(f"Unsupported transport: {self._config.transport}")
        if self._client is None:
            argv = self._routerd_argv()
            self._client = _StdioJsonRpcClient(argv)
        return self._client

    def _routerd_argv(self) -> list[str]:
        if self._config.routerd_path:
            return shlex.split(self._config.routerd_path)
        return ["tool-routerd"]

    def sync_from_mcp(self, server_id: str, mcp_client: Any) -> None:
        """Sync MCP tools into the router catalog.

        Expected flow:
        - tools = mcp_client.tools_list()
        - convert to ToolCard
        - catalog.upsertTools
        """
        tools = mcp_client.tools_list()
        if isinstance(tools, dict):
            tools = tools.get("tools", [])
        tool_cards: list[dict[str, Any]] = []
        for tool in tools or []:
            if not isinstance(tool, dict):
                continue
            card = _toolcard_from_mcp(server_id, tool)
            if not card:
                continue
            tool_id = card["toolId"]
            raw_tool = dict(tool)
            if "name" not in raw_tool:
                raw_tool["name"] = card["toolName"]
            self._raw_tool_cache[tool_id] = raw_tool
            self._tool_cache[tool_id] = card
            tool_cards.append(card)
        tool_cards.sort(key=lambda item: item["toolId"])
        self._rpc().request("catalog.upsertTools", {"tools": tool_cards})

    def get_tool_card(self, tool_id: str) -> dict[str, Any] | None:
        return self._tool_cache.get(tool_id)

    def get_tool_cards(self, tool_ids: Iterable[str]) -> list[dict[str, Any]]:
        return [
            self._tool_cache[tool_id]
            for tool_id in tool_ids
            if tool_id in self._tool_cache
        ]

    def get_raw_tool(self, tool_id: str) -> dict[str, Any] | None:
        return self._raw_tool_cache.get(tool_id)

    def get_raw_tools(self, tool_ids: Iterable[str]) -> list[dict[str, Any]]:
        return [
            self._raw_tool_cache[tool_id]
            for tool_id in tool_ids
            if tool_id in self._raw_tool_cache
        ]

    def select_tools(
        self,
        session_id: str,
        query: str,
        top_k: int = 20,
        budget_tokens: int = 1500,
        mode: str | None = None,
    ) -> list[str]:
        params: dict[str, Any] = {
            "sessionId": session_id,
            "query": query,
            "topK": top_k,
            "budgetTokens": budget_tokens,
        }
        if mode is not None:
            params["mode"] = mode
        result = self._rpc().request("ws.update", params)
        return list((result or {}).get("selectedToolIds", []))

    def mark_tool_used(self, session_id: str, tool_id: str) -> None:
        """Mark a tool as used for working-set recency tracking."""
        self._rpc().request("ws.markUsed", {"sessionId": session_id, "toolId": tool_id})

    def reduce_result(self, tool_id: str | None, raw_result: dict) -> dict:
        """Reduce a tool call result to a compact, deterministic form."""
        params: dict[str, Any] = {"rawResult": raw_result}
        if tool_id is not None:
            params["toolId"] = tool_id
        result = self._rpc().request("result.reduce", params)
        return result or {}

    def reset_catalog(self) -> None:
        self._rpc().request("catalog.reset", {})

    def close(self) -> None:
        """Release any daemon resources."""
        if self._client is None:
            return None
        self._client.close()
        self._client = None
        return None


def _toolcard_from_mcp(server_id: str, tool: dict[str, Any]) -> dict[str, Any]:
    tool_name = tool.get("name") or tool.get("toolName")
    if not tool_name:
        return {}
    annotations = tool.get("annotations") or {}
    title = tool.get("title") or annotations.get("title")
    description = tool.get("description") or annotations.get("description")
    tags = sorted(_string_list(tool.get("tags") or annotations.get("tags")))
    synonyms = sorted(_string_list(tool.get("synonyms") or annotations.get("synonyms")))
    if not tags:
        tags = _derive_tags(tool_name, title, description)
    if not synonyms:
        synonyms = _derive_synonyms(tool_name)
    tool_id = f"{server_id}:{tool_name}"
    card: dict[str, Any] = {
        "toolId": tool_id,
        "toolName": tool_name,
        "serverId": server_id,
        "tags": tags,
        "synonyms": synonyms,
        "args": _args_from_schema(
            tool.get("inputSchema") or tool.get("input_schema") or {}
        ),
        "examples": _examples_from_tool(
            tool.get("examples") or annotations.get("examples")
        ),
        "authHint": sorted(
            _string_list(tool.get("authHint") or annotations.get("authHint"))
        ),
    }
    if title:
        card["title"] = str(title)
    if description:
        card["description"] = str(description)
    side_effect = tool.get("sideEffect") or annotations.get("sideEffect")
    if not side_effect:
        if annotations.get("destructiveHint") or tool.get("destructiveHint"):
            side_effect = "destructive"
        elif annotations.get("readOnlyHint") or tool.get("readOnlyHint"):
            side_effect = "read"
    if side_effect:
        card["sideEffect"] = side_effect
    if "openWorldHint" in annotations:
        card["openWorldHint"] = bool(annotations["openWorldHint"])
    if "idempotentHint" in annotations:
        card["idempotentHint"] = bool(annotations["idempotentHint"])
    cost_hint = tool.get("costHint") or annotations.get("costHint")
    if cost_hint:
        card["costHint"] = cost_hint
    popularity = tool.get("popularity") or annotations.get("popularity")
    if popularity is not None:
        card["popularity"] = popularity
    updated_at = tool.get("updatedAt") or annotations.get("updatedAt")
    if updated_at:
        card["updatedAt"] = updated_at
    return card


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [json.dumps(value, separators=(",", ":"), sort_keys=True)]
    if isinstance(value, Iterable):
        return [str(item) for item in value if item is not None]
    return [str(value)]


_DERIVED_STOPWORDS = {
    "a",
    "an",
    "and",
    "by",
    "for",
    "from",
    "in",
    "into",
    "of",
    "on",
    "or",
    "per",
    "the",
    "to",
    "via",
    "with",
}


def _normalize_text(value: str) -> str:
    if not value:
        return ""
    normalized = re.sub(r"[_-]+", " ", value)
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", normalized)
    normalized = re.sub(r"([A-Za-z])([0-9])", r"\1 \2", normalized)
    normalized = re.sub(r"([0-9])([A-Za-z])", r"\1 \2", normalized)
    normalized = normalized.lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return normalized.strip()


def _tokenize_keywords(*parts: Any) -> list[str]:
    text = " ".join(str(part) for part in parts if part)
    normalized = _normalize_text(text)
    if not normalized:
        return []
    tokens: list[str] = []
    for token in normalized.split():
        if len(token) < 2:
            continue
        if token in _DERIVED_STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def _derive_tags(tool_name: str, title: Any, description: Any) -> list[str]:
    tokens = _tokenize_keywords(tool_name, title)
    if len(tokens) < 3:
        tokens.extend(_tokenize_keywords(description))
    if not tokens:
        return []
    return sorted(set(tokens))


def _derive_synonyms(tool_name: str) -> list[str]:
    if not tool_name:
        return []
    normalized = _normalize_text(tool_name)
    synonyms = set()
    if normalized and normalized != tool_name.lower():
        synonyms.add(normalized)
    return sorted(synonyms)


def _args_from_schema(schema: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        return []
    required = schema.get("required") or []
    if not isinstance(required, list):
        required = []
    args: list[dict[str, Any]] = []
    for name in sorted(properties.keys()):
        prop = properties.get(name) or {}
        if not isinstance(prop, dict):
            prop = {}
        arg: dict[str, Any] = {"name": name}
        desc = prop.get("description")
        if desc:
            arg["description"] = str(desc)
        type_hint = _type_hint(prop)
        if type_hint:
            arg["typeHint"] = type_hint
        if name in required:
            arg["required"] = True
        example = _example_value(prop)
        if example is not None:
            arg["example"] = example
        args.append(arg)
    return args


def _type_hint(schema: dict[str, Any]) -> str | None:
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        return "|".join(str(item) for item in schema_type)
    if isinstance(schema_type, str):
        schema_format = schema.get("format")
        if schema_format:
            return f"{schema_type}:{schema_format}"
        return schema_type
    if "anyOf" in schema or "oneOf" in schema:
        return "any"
    return None


def _example_value(schema: dict[str, Any]) -> str | None:
    if "example" in schema:
        return _stringify_example(schema["example"])
    examples = schema.get("examples")
    if isinstance(examples, list) and examples:
        return _stringify_example(examples[0])
    if "default" in schema:
        return _stringify_example(schema["default"])
    return None


def _stringify_example(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _examples_from_tool(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    examples: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and "query" in item:
                entry: dict[str, Any] = {"query": str(item["query"])}
                if "callHint" in item:
                    entry["callHint"] = str(item["callHint"])
                examples.append(entry)
            elif isinstance(item, str):
                examples.append({"query": item})
    elif isinstance(value, dict) and "query" in value:
        examples.append({"query": str(value["query"])})
    elif isinstance(value, str):
        examples.append({"query": value})
    return examples
