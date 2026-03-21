from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import shlex
from typing import Any, Iterable

import yaml

from .jsonc import load_jsonc_file


@dataclass
class ServerSpec:
    id: str
    cmd: str | None = None
    url: str | None = None
    enabled: bool = True
    init: dict | None = None
    send_initialized: bool = False
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    transport: str = "stdio"


class ServerRegistry:
    def __init__(self, servers: Iterable[ServerSpec]) -> None:
        self._servers = {server.id: server for server in servers}

    @classmethod
    def from_yaml(cls, path: str) -> "ServerRegistry":
        expanded = os.path.expanduser(path)
        with open(expanded, "r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)
        servers = _parse_registry_payload(payload)
        return cls(servers)

    @classmethod
    def from_opencode_config(
        cls,
        path: str,
        include_disabled: bool = False,
        ignore_ids: Iterable[str] | None = None,
    ) -> "ServerRegistry":
        expanded = os.path.expanduser(path)
        payload = load_jsonc_file(expanded)
        servers = _parse_opencode_payload(
            payload,
            include_disabled=include_disabled,
            ignore_ids=set(ignore_ids or []),
        )
        return cls(servers)

    def list(self) -> list[ServerSpec]:
        return list(self._servers.values())

    def enabled(self) -> list[ServerSpec]:
        return [server for server in self._servers.values() if server.enabled]

    def get(self, server_id: str) -> ServerSpec | None:
        return self._servers.get(server_id)


def _parse_registry_payload(payload: Any) -> list[ServerSpec]:
    if payload is None:
        return []
    if isinstance(payload, dict):
        servers_payload = payload.get("servers")
        if servers_payload is None:
            raise ValueError("Registry YAML must contain a top-level 'servers' list.")
    else:
        servers_payload = payload

    if not isinstance(servers_payload, list):
        raise ValueError("Registry 'servers' must be a list.")

    servers: list[ServerSpec] = []
    for entry in servers_payload:
        if not isinstance(entry, dict):
            raise ValueError("Each server entry must be a mapping.")
        server_id = _string_value(entry, ["id", "serverId", "server_id"])
        cmd = _string_value(entry, ["cmd", "command"])
        if not server_id:
            raise ValueError("Each server entry requires 'id'.")
        transport = str(entry.get("transport") or "stdio")
        if transport != "stdio":
            raise ValueError(
                f"Unsupported transport '{transport}' for server '{server_id}'. stdio only."
            )
        if not cmd:
            raise ValueError(
                f"Server '{server_id}' requires 'cmd' for stdio transport."
            )
        init_payload = _expand_env(entry.get("init"))
        send_initialized = bool(
            entry.get("send_initialized")
            or entry.get("sendInitialized")
            or entry.get("initialized")
            or False
        )
        enabled = bool(entry.get("enabled", True))
        tags = _string_list(entry.get("tags"))
        env = _string_dict(_expand_env(entry.get("env")))
        metadata = {
            key: value
            for key, value in entry.items()
            if key
            not in {
                "id",
                "serverId",
                "server_id",
                "cmd",
                "command",
                "init",
                "send_initialized",
                "sendInitialized",
                "initialized",
                "env",
                "enabled",
                "tags",
                "transport",
            }
        }
        servers.append(
            ServerSpec(
                id=server_id,
                cmd=os.path.expandvars(cmd) if cmd else None,
                enabled=enabled,
                init=init_payload if isinstance(init_payload, dict) else None,
                send_initialized=send_initialized,
                env=env,
                tags=tags,
                metadata=metadata,
                transport=transport,
            )
        )
    return servers


def _parse_opencode_payload(
    payload: Any,
    include_disabled: bool,
    ignore_ids: set[str],
) -> list[ServerSpec]:
    if not isinstance(payload, dict):
        return []
    mcp = payload.get("mcp") or payload.get("mcpServers") or {}
    if not isinstance(mcp, dict):
        return []
    servers: list[ServerSpec] = []
    for server_id, entry in mcp.items():
        if not isinstance(entry, dict):
            continue
        if str(server_id) in ignore_ids:
            continue
        enabled = bool(entry.get("enabled", True))
        if not include_disabled and not enabled:
            continue
        server_type = str(entry.get("type") or "local")

        if server_type == "remote":
            url = entry.get("url")
            if not url or not isinstance(url, str):
                continue
            headers = _string_dict(entry.get("headers"))
            servers.append(
                ServerSpec(
                    id=str(server_id),
                    url=url.strip(),
                    enabled=enabled,
                    headers=headers,
                    tags=[],
                    metadata={},
                    transport="http",
                )
            )
            continue

        if server_type != "local":
            continue
        cmd = _command_from_opencode(entry)
        if not cmd:
            continue
        init_payload = _expand_env(entry.get("init"))
        send_initialized = bool(
            entry.get("send_initialized")
            or entry.get("sendInitialized")
            or entry.get("initialized")
            or False
        )
        env = _string_dict(_expand_env(entry.get("env")))
        servers.append(
            ServerSpec(
                id=str(server_id),
                cmd=cmd,
                enabled=enabled,
                init=init_payload if isinstance(init_payload, dict) else None,
                send_initialized=send_initialized,
                env=env,
                tags=[],
                metadata={},
                transport="stdio",
            )
        )
    return servers


def _string_value(entry: dict[str, Any], keys: Iterable[str]) -> str | None:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _string_dict(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    return {}


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _command_from_opencode(entry: dict[str, Any]) -> str | None:
    command = entry.get("command")
    args = entry.get("args")
    parts: list[str] = []
    if isinstance(command, list):
        parts.extend(str(item) for item in command if item)
    elif isinstance(command, str) and command.strip():
        parts.append(command.strip())
    if isinstance(args, list):
        parts.extend(str(item) for item in args if item)
    if not parts:
        return None
    return " ".join(shlex.quote(part) for part in parts)
