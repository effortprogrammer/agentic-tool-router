from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
from typing import Any, Iterable

from .mcp_http import HttpMcpClient
from .mcp_stdio import StdioMcpClient
from .registry import ServerRegistry, ServerSpec
from .router import ToolRouter


@dataclass
class HostToolSpec:
    provider_id: str
    name: str
    command: list[str] | None = None
    timeout_sec: float = 60.0
    env: dict[str, str] | None = None
    execution: str = "command"
    opencode_tool_id: str | None = None


class ToolRouterHub:
    def __init__(
        self,
        registry: ServerRegistry,
        router: ToolRouter,
        auto_sync: bool = True,
        include_disabled: bool = False,
        host_tools: dict[str, dict[str, HostToolSpec]] | None = None,
        host_tool_definitions: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._registry = registry
        self._router = router
        self._auto_sync = auto_sync
        self._include_disabled = include_disabled
        self._clients: dict[str, StdioMcpClient | HttpMcpClient] = {}
        self._synced: set[str] = set()
        self._host_tools: dict[str, dict[str, HostToolSpec]] = host_tools or {}
        self._host_tool_definitions: dict[str, list[dict[str, Any]]] = (
            host_tool_definitions or {}
        )

    @classmethod
    def from_yaml(
        cls, path: str, routerd_path: str | None = None, auto_sync: bool = True
    ) -> "ToolRouterHub":
        registry = ServerRegistry.from_yaml(path)
        router = ToolRouter(routerd_path=routerd_path)
        return cls(registry, router, auto_sync=auto_sync)

    @classmethod
    def from_opencode_config(
        cls,
        path: str,
        routerd_path: str | None = None,
        auto_sync: bool = True,
        include_disabled: bool = False,
        ignore_ids: Iterable[str] | None = None,
    ) -> "ToolRouterHub":
        registry, host_tools, host_tool_definitions = cls.load_opencode_runtime(
            path,
            include_disabled=include_disabled,
            ignore_ids=ignore_ids,
        )
        router = ToolRouter(routerd_path=routerd_path)
        return cls(
            registry,
            router,
            auto_sync=auto_sync,
            include_disabled=include_disabled,
            host_tools=host_tools,
            host_tool_definitions=host_tool_definitions,
        )

    @classmethod
    def load_opencode_runtime(
        cls,
        path: str,
        include_disabled: bool = False,
        ignore_ids: Iterable[str] | None = None,
    ) -> tuple[
        ServerRegistry,
        dict[str, dict[str, HostToolSpec]],
        dict[str, list[dict[str, Any]]],
    ]:
        registry = ServerRegistry.from_opencode_config(
            path,
            include_disabled=include_disabled,
            ignore_ids=ignore_ids,
        )
        host_tools, host_tool_definitions = _load_opencode_host_tools(path)
        native_tools, native_definitions = _load_opencode_native_tools(path)
        host_tools = _merge_tool_specs(host_tools, native_tools)
        host_tool_definitions = _merge_tool_definitions(
            host_tool_definitions, native_definitions
        )
        server_ids = {server.id for server in registry.list()}
        for provider_id in list(host_tools.keys()):
            if provider_id in server_ids:
                print(
                    "[mcp-tool-router] Warning: routerHostTools providerId "
                    f"'{provider_id}' conflicts with MCP server id. Skipping host tools for this provider.",
                    file=sys.stderr,
                )
                host_tools.pop(provider_id, None)
                host_tool_definitions.pop(provider_id, None)
        return registry, host_tools, host_tool_definitions

    @property
    def registry(self) -> ServerRegistry:
        return self._registry

    @property
    def router(self) -> ToolRouter:
        return self._router

    def list_servers(self) -> list[ServerSpec]:
        return self._registry.list()

    def sync_all(self) -> None:
        self._sync_host_tools()
        servers = (
            self._registry.list()
            if self._include_disabled
            else self._registry.enabled()
        )
        for server in servers:
            self.sync_server(server.id, raise_on_error=False)

    def reload_registry(
        self,
        registry: ServerRegistry,
        *,
        host_tools: dict[str, dict[str, HostToolSpec]] | None = None,
        host_tool_definitions: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        current_ids = {server.id for server in self._registry.list()}
        next_ids = {server.id for server in registry.list()}

        removed = current_ids - next_ids
        for server_id in removed:
            client = self._clients.pop(server_id, None)
            if client is not None:
                client.close()

        self._registry = registry
        if host_tools is not None:
            self._host_tools = host_tools
        if host_tool_definitions is not None:
            self._host_tool_definitions = host_tool_definitions
        self._synced.clear()
        self._router.reset_catalog()
        self.sync_all()

    def sync_missing(self) -> None:
        self._sync_host_tools()
        servers = (
            self._registry.list()
            if self._include_disabled
            else self._registry.enabled()
        )
        for server in servers:
            if server.id not in self._synced:
                self.sync_server(server.id, raise_on_error=False)

    def sync_server(self, server_id: str, *, raise_on_error: bool = True) -> None:
        server = self._require_server(server_id)
        if not server.enabled and not self._include_disabled:
            raise ValueError(f"Server '{server_id}' is disabled.")
        try:
            client = self._ensure_client(server)
            self._router.sync_from_mcp(server_id, client)
        except Exception as exc:
            if server.transport == "http" and not raise_on_error:
                print(
                    f"[mcp-tool-router] Warning: failed to sync remote server "
                    f"'{server_id}': {exc}",
                    file=sys.stderr,
                )
                return
            raise
        self._synced.add(server_id)

    def select_tools(
        self,
        session_id: str,
        query: str,
        top_k: int = 20,
        budget_tokens: int = 1500,
        sync: bool | None = None,
        mode: str | None = None,
    ) -> list[str]:
        if sync is None:
            sync = self._auto_sync
        if sync:
            self.sync_missing()
        return self._router.select_tools(
            session_id,
            query,
            top_k=top_k,
            budget_tokens=budget_tokens,
            mode=mode,
        )

    def call_tool(self, tool_id: str, arguments: dict | None = None) -> dict:
        server_id, tool_name = _split_tool_id(tool_id)
        host_provider = self._host_tools.get(server_id)
        if host_provider is not None:
            host_tool = host_provider.get(tool_name)
            if host_tool is None:
                raise KeyError(f"Unknown host tool '{tool_id}'.")
            return self._call_host_tool(host_tool, arguments)
        server = self._require_server(server_id)
        client = self._ensure_client(server)
        return client.tools_call(tool_name, arguments)

    def call_tool_name(
        self, server_id: str, tool_name: str, arguments: dict | None = None
    ) -> dict:
        host_provider = self._host_tools.get(server_id)
        if host_provider is not None:
            host_tool = host_provider.get(tool_name)
            if host_tool is None:
                raise KeyError(f"Unknown host tool '{server_id}:{tool_name}'.")
            return self._call_host_tool(host_tool, arguments)
        server = self._require_server(server_id)
        client = self._ensure_client(server)
        return client.tools_call(tool_name, arguments)

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()
        self._router.close()

    def _sync_host_tools(self) -> None:
        for provider_id, definitions in self._host_tool_definitions.items():
            self._router.sync_from_tool_definitions(
                provider_id=provider_id,
                tools=definitions,
                source_type="host",
                source_platform="opencode",
            )

    def _call_host_tool(
        self, host_tool: HostToolSpec, arguments: dict | None = None
    ) -> dict:
        if host_tool.execution == "opencode":
            return _call_opencode_native_tool(host_tool, arguments)

        command = host_tool.command or []
        if not command:
            raise RuntimeError(
                f"Host tool '{host_tool.provider_id}:{host_tool.name}' has no command configured"
            )
        args_payload = arguments if isinstance(arguments, dict) else {}
        args_json = json.dumps(args_payload, separators=(",", ":"), sort_keys=True)

        rendered_command = [
            part.replace("{args_json}", args_json).replace(
                "{tool_name}", host_tool.name
            )
            for part in command
        ]
        env = dict(os.environ)
        if host_tool.env:
            env.update(host_tool.env)

        try:
            result = subprocess.run(
                rendered_command,
                capture_output=True,
                text=True,
                env=env,
                timeout=host_tool.timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Host tool '{host_tool.provider_id}:{host_tool.name}' timed out after {host_tool.timeout_sec}s"
            ) from exc

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise RuntimeError(
                f"Host tool '{host_tool.provider_id}:{host_tool.name}' failed"
                + (f": {stderr}" if stderr else "")
            )

        stdout = (result.stdout or "").strip()
        if not stdout:
            return {"ok": True}
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            return {"text": stdout}
        if isinstance(parsed, dict):
            return parsed
        return {"result": parsed}

    def _require_server(self, server_id: str) -> ServerSpec:
        server = self._registry.get(server_id)
        if not server:
            raise KeyError(f"Unknown server '{server_id}'.")
        return server

    def _ensure_client(self, server: ServerSpec) -> StdioMcpClient | HttpMcpClient:
        existing = self._clients.get(server.id)
        if existing is not None:
            return existing

        client: StdioMcpClient | HttpMcpClient
        if server.transport == "http":
            if not server.url:
                raise ValueError(f"Server '{server.id}' is missing url.")
            client = HttpMcpClient(server.url, headers=server.headers or None)
        elif server.transport == "stdio":
            if not server.cmd:
                raise ValueError(f"Server '{server.id}' is missing cmd.")
            client = StdioMcpClient(
                server.cmd,
                init_payload=server.init,
                send_initialized=server.send_initialized,
                env=server.env,
            )
        else:
            raise ValueError(
                f"Unsupported transport '{server.transport}' for server '{server.id}'."
            )

        self._clients[server.id] = client
        return client


def _split_tool_id(tool_id: str) -> tuple[str, str]:
    if ":" not in tool_id:
        raise ValueError(
            f"Invalid toolId '{tool_id}'. Expected '{{serverId}}:{{toolName}}'."
        )
    server_id, tool_name = tool_id.split(":", 1)
    if not server_id or not tool_name:
        raise ValueError(
            f"Invalid toolId '{tool_id}'. Expected '{{serverId}}:{{toolName}}'."
        )
    return server_id, tool_name


def _load_opencode_host_tools(
    config_path: str,
) -> tuple[dict[str, dict[str, HostToolSpec]], dict[str, list[dict[str, Any]]]]:
    expanded = os.path.expanduser(config_path)
    if not os.path.exists(expanded):
        return {}, {}

    try:
        with open(expanded, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}, {}

    entries = payload.get("routerHostTools") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return {}, {}

    host_tools: dict[str, dict[str, HostToolSpec]] = {}
    definitions: dict[str, list[dict[str, Any]]] = {}

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if not bool(entry.get("enabled", True)):
            continue

        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        provider_id = entry.get("providerId")
        if not isinstance(provider_id, str) or not provider_id.strip():
            provider_id = "opencode"
        provider_id = provider_id.strip()
        tool_name = name.strip()

        command = _parse_host_command(entry.get("command"))
        if not command:
            continue
        timeout_raw = entry.get("timeoutSec")
        timeout_sec = (
            float(timeout_raw)
            if isinstance(timeout_raw, (int, float)) and timeout_raw > 0
            else 60.0
        )
        env_value = entry.get("env")
        env = (
            {str(key): str(value) for key, value in env_value.items()}
            if isinstance(env_value, dict)
            else None
        )

        spec = HostToolSpec(
            provider_id=provider_id,
            name=tool_name,
            command=command,
            timeout_sec=timeout_sec,
            env=env,
            execution="command",
        )
        host_tools.setdefault(provider_id, {})[tool_name] = spec

        definition: dict[str, Any] = {
            "name": tool_name,
            "description": str(
                entry.get("description") or f"OpenCode host tool: {tool_name}"
            ),
            "inputSchema": (
                entry.get("inputSchema")
                if isinstance(entry.get("inputSchema"), dict)
                else {"type": "object", "properties": {}}
            ),
            "tags": entry.get("tags")
            if isinstance(entry.get("tags"), list)
            else ["opencode", "host"],
            "synonyms": entry.get("synonyms")
            if isinstance(entry.get("synonyms"), list)
            else [],
            "annotations": {
                "readOnlyHint": bool(entry.get("readOnlyHint", False)),
                "idempotentHint": bool(entry.get("idempotentHint", False)),
            },
        }
        definitions.setdefault(provider_id, []).append(definition)

    return host_tools, definitions


def _parse_host_command(value: Any) -> list[str]:
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return parts
    if isinstance(value, str) and value.strip():
        return shlex.split(value)
    return []


def _merge_tool_specs(
    left: dict[str, dict[str, HostToolSpec]],
    right: dict[str, dict[str, HostToolSpec]],
) -> dict[str, dict[str, HostToolSpec]]:
    merged: dict[str, dict[str, HostToolSpec]] = {
        provider: dict(specs) for provider, specs in left.items()
    }
    for provider_id, specs in right.items():
        provider_bucket = merged.setdefault(provider_id, {})
        for tool_name, spec in specs.items():
            provider_bucket[tool_name] = spec
    return merged


def _merge_tool_definitions(
    left: dict[str, list[dict[str, Any]]],
    right: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    merged: dict[str, list[dict[str, Any]]] = {
        provider: list(definitions) for provider, definitions in left.items()
    }
    for provider_id, definitions in right.items():
        existing = {str(item.get("name")): item for item in merged.get(provider_id, [])}
        for definition in definitions:
            existing[str(definition.get("name"))] = definition
        merged[provider_id] = sorted(
            existing.values(), key=lambda item: str(item.get("name") or "")
        )
    return merged


_NATIVE_TOOL_ENRICHMENTS: dict[str, dict[str, list[str]]] = {
    "bash": {
        "tags": [
            "execute",
            "command",
            "shell",
            "run",
            "terminal",
            "list",
            "files",
            "directory",
            "install",
            "build",
            "test",
            "script",
        ],
        "synonyms": [
            "shell",
            "terminal",
            "command line",
            "cli",
            "exec",
            "run command",
            "execute command",
        ],
    },
    "read": {
        "tags": [
            "read",
            "file",
            "content",
            "view",
            "open",
            "cat",
            "display",
            "show",
            "text",
            "source",
            "code",
            "inspect",
            "peek",
            "head",
            "tail",
        ],
        "synonyms": ["cat", "view file", "open file", "read file", "show file"],
    },
    "write": {
        "tags": [
            "write",
            "file",
            "create",
            "save",
            "output",
            "overwrite",
            "new file",
            "content",
        ],
        "synonyms": ["create file", "save file", "write file"],
    },
    "edit": {
        "tags": [
            "edit",
            "modify",
            "change",
            "update",
            "patch",
            "replace",
            "insert",
            "delete",
            "refactor",
            "fix",
            "line",
        ],
        "synonyms": ["modify file", "change file", "update file", "patch file"],
    },
    "glob": {
        "tags": [
            "glob",
            "find",
            "files",
            "search",
            "pattern",
            "list",
            "directory",
            "match",
            "wildcard",
            "path",
            "locate",
            "discover",
        ],
        "synonyms": ["find files", "list files", "search files", "file search"],
    },
    "grep": {
        "tags": [
            "grep",
            "search",
            "find",
            "pattern",
            "regex",
            "match",
            "content",
            "text",
            "code",
            "occurrences",
            "ripgrep",
        ],
        "synonyms": ["search content", "find in files", "text search", "code search"],
    },
    "lsp_diagnostics": {
        "tags": [
            "diagnostics",
            "errors",
            "warnings",
            "lint",
            "check",
            "problems",
            "issues",
            "typescript",
            "type",
        ],
        "synonyms": ["check errors", "find problems", "lint"],
    },
    "interactive_bash": {
        "tags": ["interactive", "bash", "terminal", "shell", "vim", "htop", "tui", "tmux", "repl"],
        "synonyms": ["interactive shell", "terminal app", "tui app"],
    },
    "ast_grep_search": {
        "tags": ["ast", "grep", "search", "pattern", "code", "syntax", "tree", "structural", "match", "find"],
        "synonyms": ["ast search", "structural search", "code pattern search"],
    },
    "ast_grep_replace": {
        "tags": ["ast", "grep", "replace", "refactor", "pattern", "code", "rewrite", "transform"],
        "synonyms": ["ast replace", "structural replace", "code refactor"],
    },
    "webfetch": {
        "tags": ["web", "fetch", "url", "http", "download", "page", "content", "scrape", "browse"],
        "synonyms": ["fetch url", "web page", "http request", "download page"],
    },
    "codesearch": {
        "tags": ["code", "search", "find", "source", "repository", "codebase", "semantic"],
        "synonyms": ["code search", "search code", "find code"],
    },
    "task": {
        "tags": ["task", "delegate", "agent", "subagent", "background", "spawn", "parallel"],
        "synonyms": ["delegate task", "spawn agent", "background task"],
    },
    "todowrite": {
        "tags": ["todo", "task", "list", "plan", "track", "progress", "checklist"],
        "synonyms": ["todo list", "task list", "create todo"],
    },
    "apply_patch": {
        "tags": ["patch", "apply", "diff", "change", "merge", "unified"],
        "synonyms": ["apply patch", "apply diff"],
    },
    "lsp_goto_definition": {
        "tags": ["lsp", "goto", "definition", "navigate", "jump", "symbol", "source", "declaration"],
        "synonyms": ["go to definition", "find definition", "jump to source"],
    },
    "lsp_find_references": {
        "tags": ["lsp", "references", "find", "usage", "where", "used", "callers"],
        "synonyms": ["find references", "find usages", "who calls"],
    },
    "lsp_symbols": {
        "tags": ["lsp", "symbols", "outline", "structure", "functions", "classes", "workspace"],
        "synonyms": ["list symbols", "file outline", "find symbol"],
    },
    "lsp_prepare_rename": {
        "tags": ["lsp", "rename", "prepare", "check", "symbol", "refactor"],
        "synonyms": ["prepare rename", "check rename"],
    },
    "lsp_rename": {
        "tags": ["lsp", "rename", "symbol", "refactor", "variable", "function", "class"],
        "synonyms": ["rename symbol", "rename variable", "rename function"],
    },
    "session_search": {
        "tags": ["session", "search", "find", "query", "messages", "history"],
        "synonyms": ["search sessions", "find in history"],
    },
    "session_info": {
        "tags": ["session", "info", "metadata", "details", "statistics"],
        "synonyms": ["session info", "session details"],
    },
    "look_at": {
        "tags": ["look", "image", "screenshot", "visual", "pdf", "diagram", "analyze", "picture"],
        "synonyms": ["analyze image", "look at screenshot", "visual analysis"],
    },
    "question": {
        "tags": ["question", "ask", "user", "input", "prompt", "confirm", "choice"],
        "synonyms": ["ask user", "get input", "confirm action"],
    },
    "skill": {
        "tags": ["skill", "load", "activate", "capability", "plugin", "extension"],
        "synonyms": ["load skill", "activate skill"],
    },
    "skill_mcp": {
        "tags": ["skill", "mcp", "server", "invoke", "tool", "resource"],
        "synonyms": ["invoke mcp", "skill mcp tool"],
    },
    "websearch": {
        "tags": ["web", "search", "internet", "online", "google", "query", "browse"],
        "synonyms": ["web search", "search internet", "google"],
    },
    "background_output": {
        "tags": ["background", "output", "result", "task", "async", "retrieve"],
        "synonyms": ["get background result", "task output"],
    },
    "background_cancel": {
        "tags": ["background", "cancel", "stop", "abort", "task", "kill"],
        "synonyms": ["cancel task", "stop background"],
    },
    "session_list": {
        "tags": ["session", "list", "sessions", "history", "previous", "past"],
        "synonyms": ["list sessions", "show sessions"],
    },
    "session_read": {
        "tags": ["session", "read", "messages", "conversation", "history", "chat"],
        "synonyms": ["read session", "show conversation"],
    },
}


def _load_opencode_native_tools(
    config_path: str,
) -> tuple[dict[str, dict[str, HostToolSpec]], dict[str, list[dict[str, Any]]]]:
    enabled = os.environ.get("ROUTER_OPENCODE_NATIVE_ENABLED", "true").lower()
    if enabled in {"0", "false", "no"}:
        return {}, {}

    base_url = _resolve_opencode_server_url()
    timeout = _float_env("ROUTER_OPENCODE_NATIVE_TIMEOUT", 5.0)
    directory = os.environ.get("ROUTER_OPENCODE_DIRECTORY") or None

    try:
        ids_payload = _http_json(
            f"{base_url}/experimental/tool/ids",
            timeout=timeout,
            query={"directory": directory} if directory else None,
        )
    except Exception as exc:
        print(
            f"[mcp-tool-router] Warning: failed to load OpenCode native tool IDs: {exc}",
            file=sys.stderr,
        )
        return {}, {}
    tool_ids = [str(item) for item in ids_payload if isinstance(item, str)]
    tool_ids = [tool_id for tool_id in tool_ids if not tool_id.startswith("router_")]
    if not tool_ids:
        return {}, {}

    try:
        list_payload = _load_opencode_tool_list(
            base_url, timeout=timeout, directory=directory
        )
    except Exception as exc:
        print(
            f"[mcp-tool-router] Warning: failed to load OpenCode native tool details: {exc}",
            file=sys.stderr,
        )
        list_payload = []
    definitions_by_id: dict[str, dict[str, Any]] = {}
    for item in list_payload:
        if not isinstance(item, dict):
            continue
        tool_id = item.get("id")
        if not isinstance(tool_id, str) or not tool_id:
            continue
        definitions_by_id[tool_id] = item

    provider_id = "opencode-native"
    specs: dict[str, HostToolSpec] = {}
    definitions: list[dict[str, Any]] = []
    for tool_id in sorted(set(tool_ids)):
        source = definitions_by_id.get(tool_id, {})
        description = source.get("description")
        parameters = source.get("parameters")
        input_schema = (
            parameters
            if isinstance(parameters, dict)
            else {"type": "object", "properties": {}}
        )
        specs[tool_id] = HostToolSpec(
            provider_id=provider_id,
            name=tool_id,
            command=None,
            timeout_sec=timeout,
            env=None,
            execution="opencode",
            opencode_tool_id=tool_id,
        )
        enrichment = _NATIVE_TOOL_ENRICHMENTS.get(tool_id, {})
        base_tags = ["opencode", "native"]
        extra_tags = enrichment.get("tags", [])
        base_synonyms = [tool_id]
        extra_synonyms = enrichment.get("synonyms", [])
        definitions.append(
            {
                "id": tool_id,
                "name": tool_id,
                "description": str(description or f"OpenCode native tool: {tool_id}"),
                "inputSchema": input_schema,
                "tags": base_tags + extra_tags,
                "synonyms": base_synonyms + extra_synonyms,
                "annotations": {"idempotentHint": False},
            }
        )

    return {provider_id: specs}, {provider_id: definitions}


def _load_opencode_tool_list(
    base_url: str,
    *,
    timeout: float,
    directory: str | None,
) -> list[dict[str, Any]]:
    provider, model = _load_opencode_provider_model(
        base_url, timeout=timeout, directory=directory
    )
    if not provider or not model:
        return []
    payload = _http_json(
        f"{base_url}/experimental/tool",
        timeout=timeout,
        query={"provider": provider, "model": model, "directory": directory},
    )
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _load_opencode_provider_model(
    base_url: str,
    *,
    timeout: float,
    directory: str | None,
) -> tuple[str | None, str | None]:
    payload = _http_json(
        f"{base_url}/config/providers",
        timeout=timeout,
        query={"directory": directory} if directory else None,
    )
    if not isinstance(payload, dict):
        return None, None
    default_map = payload.get("default")
    if isinstance(default_map, dict):
        for provider_id, model_id in default_map.items():
            if isinstance(provider_id, str) and isinstance(model_id, str):
                return provider_id, model_id
    providers = payload.get("providers")
    if isinstance(providers, list):
        for item in providers:
            if not isinstance(item, dict):
                continue
            pid = item.get("id")
            models = item.get("models")
            if not isinstance(pid, str) or not isinstance(models, dict):
                continue
            for model_id in models.keys():
                if isinstance(model_id, str):
                    return pid, model_id
    return None, None


def _call_opencode_native_tool(
    host_tool: HostToolSpec,
    arguments: dict | None,
) -> dict:
    tool_id = host_tool.opencode_tool_id or host_tool.name
    base_url = _resolve_opencode_server_url()
    configured_timeout = _float_env("ROUTER_OPENCODE_NATIVE_TIMEOUT", 120.0)
    timeout = configured_timeout
    if host_tool.timeout_sec and host_tool.timeout_sec > timeout:
        timeout = host_tool.timeout_sec
    directory = os.environ.get("ROUTER_OPENCODE_DIRECTORY") or None
    session_id = _create_opencode_session(
        base_url, timeout=timeout, directory=directory
    )
    args_payload = arguments if isinstance(arguments, dict) else {}

    prompt_text = (
        f"Use tool '{tool_id}' with arguments JSON below. Return only the tool output. "
        f"Arguments: {json.dumps(args_payload, separators=(',', ':'), sort_keys=True)}"
    )
    payload = {
        "parts": [{"type": "text", "text": prompt_text}],
        "tools": {tool_id: True},
    }
    response = _http_json(
        f"{base_url}/session/{urlparse.quote(session_id, safe='')}/message",
        method="POST",
        timeout=timeout,
        query={"directory": directory} if directory else None,
        body=payload,
    )
    if isinstance(response, dict):
        return {
            "nativeToolId": tool_id,
            "opencodeResponse": response,
        }
    return {"nativeToolId": tool_id, "opencodeResponse": {"raw": response}}


def _create_opencode_session(
    base_url: str,
    *,
    timeout: float,
    directory: str | None,
) -> str:
    payload = _http_json(
        f"{base_url}/session",
        method="POST",
        timeout=timeout,
        query={"directory": directory} if directory else None,
        body={},
    )
    if isinstance(payload, dict):
        session_id = payload.get("id")
        if isinstance(session_id, str) and session_id.startswith("ses"):
            return session_id
    raise RuntimeError("Failed to create OpenCode session for native tool execution")


def _http_json(
    url: str,
    *,
    method: str = "GET",
    timeout: float = 5.0,
    query: dict[str, str | None] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    full_url = url
    if query:
        pairs = [(key, value) for key, value in query.items() if value is not None]
        if pairs:
            separator = "&" if "?" in full_url else "?"
            full_url = f"{full_url}{separator}{urlparse.urlencode(pairs)}"

    data: bytes | None = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urlrequest.Request(
        full_url, data=data, method=method.upper(), headers=headers
    )
    timeout_value: float | None = None if timeout <= 0 else timeout
    try:
        with urlrequest.urlopen(req, timeout=timeout_value) as resp:
            raw = resp.read().decode("utf-8")
    except (urlerror.URLError, TimeoutError) as exc:
        raise RuntimeError(
            f"Failed OpenCode request {method} {full_url}: {exc}"
        ) from exc

    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"OpenCode response is not valid JSON for {full_url}"
        ) from exc


def _float_env(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = float(value)
        if parsed <= 0:
            return default
        return parsed
    except ValueError:
        return default


def _resolve_opencode_server_url() -> str:
    upstream = os.environ.get("ROUTER_OPENCODE_UPSTREAM_URL")
    if upstream and upstream.strip():
        upstream_url = upstream.strip().rstrip("/")
        if _opencode_url_reachable(upstream_url):
            return upstream_url

    explicit = os.environ.get("OPENCODE_SERVER_URL")
    if explicit and explicit.strip():
        explicit_url = explicit.strip().rstrip("/")
        if _opencode_url_reachable(explicit_url):
            return explicit_url

    for discovered in _discover_opencode_server_urls_from_processes():
        if _opencode_url_reachable(discovered):
            return discovered

    for discovered in _discover_opencode_server_urls_from_logs():
        if _opencode_url_reachable(discovered):
            return discovered

    fallback = "http://127.0.0.1:4096"
    if _opencode_url_reachable(fallback):
        return fallback
    return "http://localhost:4096"


def _discover_opencode_server_urls_from_logs() -> list[str]:
    log_dir = Path.home() / ".local" / "share" / "opencode" / "log"
    if not log_dir.exists() or not log_dir.is_dir():
        return []

    pattern = re.compile(r"https?://(?:localhost|127\.0\.0\.1):\d+")
    try:
        log_files = sorted(
            [path for path in log_dir.glob("*.log") if path.is_file()],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []

    candidates: list[str] = []
    seen: set[str] = set()

    for path in log_files[:5]:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for line in reversed(text.splitlines()):
            if "service=server url=" not in line and "server listening on" not in line:
                continue
            match = pattern.search(line)
            if match:
                url = match.group(0).rstrip("/")
                if url not in seen:
                    seen.add(url)
                    candidates.append(url)

    return candidates


def _discover_opencode_server_urls_from_processes() -> list[str]:
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return []

    if result.returncode != 0:
        return []

    urls: list[str] = []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        if not line.startswith("opencode"):
            continue
        match = re.search(r"(?:127\.0\.0\.1|localhost):(\d+)\s+\(LISTEN\)", line)
        if not match:
            continue
        port = match.group(1)
        url = f"http://127.0.0.1:{port}"
        if url not in seen:
            seen.add(url)
            urls.append(url)

    return urls


def _opencode_url_reachable(base_url: str) -> bool:
    try:
        payload = _http_json(
            f"{base_url.rstrip('/')}/experimental/tool/ids", timeout=1.5
        )
    except Exception:
        return False
    return isinstance(payload, list)
