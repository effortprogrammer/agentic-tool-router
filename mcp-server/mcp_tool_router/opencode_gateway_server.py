from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import traceback
from typing import Any
import sys
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from .hub import ToolRouterHub, _resolve_opencode_server_url


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("mcpflow-gateway")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    log_path = os.environ.get(
        "ROUTER_GATEWAY_LOG",
        os.path.join(os.environ.get("TMPDIR", "/tmp"), "mcpflow-gateway.log"),
    )
    try:
        handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    except Exception:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


_log = _setup_logging()


@dataclass
class GatewayConfig:
    bind_host: str
    bind_port: int
    upstream_url: str
    request_timeout_sec: float
    stream_timeout_sec: float
    select_timeout_sec: float
    select_top_k: int
    select_budget_tokens: int
    default_session_id: str
    max_runtime_ids_cache_sec: float


@dataclass
class _RuntimeIdsCache:
    expires_at: float
    ids: set[str]


@dataclass
class _GatewayState:
    hub: ToolRouterHub
    config: GatewayConfig
    lock: threading.Lock
    runtime_ids_cache: _RuntimeIdsCache | None = None
    session_permission_hashes: dict[str, str] | None = None


_STATE: _GatewayState | None = None


class OpenCodeGatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self._proxy_request("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._proxy_request("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._proxy_request("DELETE")

    def do_PUT(self) -> None:  # noqa: N802
        self._proxy_request("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._proxy_request("PATCH")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return None

    def _proxy_request(self, method: str) -> None:
        state = _STATE
        if state is None:
            self._write_json(500, {"error": "gateway state not initialized"})
            return

        content_length = int(self.headers.get("Content-Length") or "0")
        raw_body = self.rfile.read(content_length) if content_length > 0 else b""

        parsed_url = urlparse.urlparse(self.path)
        path = parsed_url.path
        query = urlparse.parse_qs(parsed_url.query)

        body_bytes = raw_body
        if method.upper() == "POST" and _is_session_message_path(path):
            try:
                body_bytes = _inject_tools_allowlist(
                    state,
                    path,
                    query,
                    raw_body,
                )
            except Exception:
                body_bytes = raw_body

        upstream = state.config.upstream_url.rstrip("/") + self.path
        headers: dict[str, str] = {}
        for key, value in self.headers.items():
            low = key.lower()
            if low in {"host", "content-length", "connection"}:
                continue
            headers[key] = value

        request_obj = urlrequest.Request(
            upstream,
            data=body_bytes if method.upper() in {"POST", "PUT", "PATCH"} else None,
            method=method.upper(),
            headers=headers,
        )

        is_stream = _should_stream_proxy(path, headers, method)
        is_message_post = method.upper() == "POST" and _is_session_message_path(path)
        _log.debug(
            "%s %s -> upstream %s (stream=%s msg_post=%s)",
            method,
            self.path,
            upstream,
            is_stream,
            is_message_post,
        )

        try:
            timeout = (
                state.config.stream_timeout_sec
                if is_stream
                else (
                    max(state.config.request_timeout_sec, 180.0)
                    if is_message_post
                    else state.config.request_timeout_sec
                )
            )
            timeout_value = None if timeout <= 0 else timeout
            with urlrequest.urlopen(
                request_obj,
                timeout=timeout_value,
            ) as response:
                resp_ct = response.headers.get("Content-Type", "")
                do_stream = is_stream or "text/event-stream" in resp_ct.lower()
                _log.debug(
                    "%s %s <- status=%d ct=%s do_stream=%s",
                    method,
                    self.path,
                    response.status,
                    resp_ct,
                    do_stream,
                )
                if do_stream:
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        low = key.lower()
                        if low in {
                            "transfer-encoding",
                            "connection",
                            "content-length",
                            "date",
                            "server",
                        }:
                            continue
                        self.send_header(key, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    # urllib's addinfourl wrapper lacks read1(); unwrap to
                    # the underlying http.client.HTTPResponse which has it.
                    # read1() is critical: read(n) with chunked encoding
                    # blocks until n bytes accumulate, starving SSE events.
                    _reader = response
                    try:
                        _fp = response.fp
                        if hasattr(_fp, "read1"):
                            _reader = _fp
                    except AttributeError:
                        pass
                    _chunk_n = 0
                    while True:
                        try:
                            chunk = _reader.read1(8192)
                        except AttributeError:
                            chunk = response.read(8192)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        _chunk_n += 1
                        if _chunk_n == 1:
                            _log.debug(
                                "%s %s first chunk (%d bytes)",
                                method, self.path, len(chunk),
                            )
                    _log.debug(
                        "%s %s stream ended, %d chunks", method, self.path, _chunk_n
                    )
                else:
                    payload = response.read()
                    _log.debug(
                        "%s %s <- status=%d size=%d",
                        method,
                        self.path,
                        response.status,
                        len(payload),
                    )
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        low = key.lower()
                        if low in {
                            "transfer-encoding",
                            "connection",
                            "content-length",
                            "date",
                            "server",
                        }:
                            continue
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    if payload:
                        self.wfile.write(payload)
                    self.wfile.flush()
        except urlerror.HTTPError as exc:
            payload = exc.read() if exc.fp is not None else b""
            self.send_response(exc.code)
            for key, value in exc.headers.items() if exc.headers else []:
                low = key.lower()
                if low in {
                    "transfer-encoding",
                    "connection",
                    "content-length",
                    "date",
                    "server",
                }:
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)
            self.wfile.flush()
        except BrokenPipeError:
            return
        except Exception as exc:
            _log.error(
                "proxy failed: %s %s -> %s", method, self.path, exc, exc_info=True
            )
            self._write_json(502, {"error": f"gateway proxy failed: {exc}"})

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        self.wfile.flush()


def _is_session_message_path(path: str) -> bool:
    parts = [segment for segment in path.split("/") if segment]
    return (
        len(parts) == 3 and parts[0] == "session" and parts[2] in {"message", "prompt"}
    )


def _should_stream_proxy(
    path: str, headers: dict[str, str], method: str = "GET"
) -> bool:
    if method.upper() == "POST" and _is_session_message_path(path):
        return False
    if path == "/event" or path.endswith("/event"):
        return True
    accept = headers.get("Accept") or headers.get("accept") or ""
    if "text/event-stream" in accept.lower():
        return True
    return False


def _session_id_from_path(path: str) -> str | None:
    parts = [segment for segment in path.split("/") if segment]
    if len(parts) == 3 and parts[0] == "session" and parts[2] in {"message", "prompt"}:
        return parts[1]
    return None


def _inject_tools_allowlist(
    state: _GatewayState,
    path: str,
    query: dict[str, list[str]],
    raw_body: bytes,
) -> bytes:
    if not raw_body:
        return raw_body

    payload = json.loads(raw_body.decode("utf-8"))
    if not isinstance(payload, dict):
        return raw_body

    query_text = _query_text_from_parts(payload.get("parts"))
    if not query_text:
        # opencode uses {"prompt": "..."} for /session/{id}/prompt
        prompt_val = payload.get("prompt")
        if isinstance(prompt_val, str) and prompt_val.strip():
            query_text = prompt_val.strip()
    if not query_text:
        return raw_body

    session_id = _session_id_from_path(path) or state.config.default_session_id
    selected = _select_tools_with_timeout(state, session_id, query_text)
    if selected is None:
        return raw_body

    directory = _first(query.get("directory"))
    runtime_all_ids = _runtime_tool_ids(state, directory)

    if not selected:
        payload["tools"] = {tool_id: False for tool_id in sorted(runtime_all_ids)}
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )

    runtime_ids = _map_selected_to_runtime_ids(
        selected,
        runtime_all_ids,
    )
    if not runtime_ids:
        payload["tools"] = {tool_id: False for tool_id in sorted(runtime_all_ids)}
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )

    existing = payload.get("tools")
    if isinstance(existing, dict):
        existing_allowed = {
            str(tool_id) for tool_id, enabled in existing.items() if bool(enabled)
        }
        runtime_ids = [
            tool_id for tool_id in runtime_ids if tool_id in existing_allowed
        ]
        if not runtime_ids:
            payload["tools"] = {tool_id: False for tool_id in sorted(runtime_all_ids)}
            return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )

    tools_map = {tool_id: False for tool_id in sorted(runtime_all_ids)}
    for tool_id in runtime_ids:
        tools_map[tool_id] = True
    payload["tools"] = tools_map
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _update_session_permissions(
    state: _GatewayState,
    *,
    session_id: str,
    directory: str | None,
    runtime_all_ids: set[str],
    allowed_ids: set[str],
) -> None:
    if not runtime_all_ids:
        return

    rules = [
        {
            "permission": tool_id,
            "pattern": "*",
            "action": "allow" if tool_id in allowed_ids else "deny",
        }
        for tool_id in sorted(runtime_all_ids)
    ]
    body_bytes = json.dumps(
        {"permission": rules}, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    digest = hashlib.sha256(body_bytes).hexdigest()

    with state.lock:
        cache = state.session_permission_hashes
        if cache is None:
            cache = {}
            state.session_permission_hashes = cache
        if cache.get(session_id) == digest:
            return

    query = ""
    if directory:
        query = "?" + urlparse.urlencode({"directory": directory})
    request_obj = urlrequest.Request(
        f"{state.config.upstream_url.rstrip('/')}/session/{urlparse.quote(session_id, safe='')}{query}",
        method="PATCH",
        data=body_bytes,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        timeout_value = (
            None
            if state.config.request_timeout_sec <= 0
            else state.config.request_timeout_sec
        )
        with urlrequest.urlopen(request_obj, timeout=timeout_value):
            pass
    except Exception:
        return

    with state.lock:
        cache = state.session_permission_hashes
        if cache is None:
            cache = {}
            state.session_permission_hashes = cache
        cache[session_id] = digest


def _select_tools_with_timeout(
    state: _GatewayState,
    session_id: str,
    query_text: str,
) -> list[str] | None:
    timeout = state.config.select_timeout_sec
    if timeout <= 0:
        try:
            result = _call_select_tools_prefer_sync(
                state.hub,
                session_id=session_id,
                query=query_text,
                top_k=state.config.select_top_k,
                budget_tokens=state.config.select_budget_tokens,
            )
        except Exception:
            return None
        return list(result) if isinstance(result, list) else []

    result_box: dict[str, Any] = {}
    error_box: dict[str, BaseException] = {}
    done = threading.Event()

    def _run_select() -> None:
        try:
            result_box["result"] = _call_select_tools_prefer_sync(
                state.hub,
                session_id=session_id,
                query=query_text,
                top_k=state.config.select_top_k,
                budget_tokens=state.config.select_budget_tokens,
            )
        except BaseException as exc:  # noqa: BLE001
            error_box["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=_run_select, daemon=True)
    worker.start()
    if not done.wait(timeout=timeout):
        return None
    if error_box:
        return None
    result = result_box.get("result")
    return list(result) if isinstance(result, list) else []


def _call_select_tools_prefer_sync(
    hub: ToolRouterHub,
    *,
    session_id: str,
    query: str,
    top_k: int,
    budget_tokens: int,
) -> list[str]:
    try:
        return hub.select_tools(
            session_id=session_id,
            query=query,
            top_k=top_k,
            budget_tokens=budget_tokens,
            sync=False,
        )
    except TypeError:
        return hub.select_tools(
            session_id=session_id,
            query=query,
            top_k=top_k,
            budget_tokens=budget_tokens,
        )
    except Exception:
        pass

    try:
        return hub.select_tools(
            session_id=session_id,
            query=query,
            top_k=top_k,
            budget_tokens=budget_tokens,
            sync=True,
        )
    except TypeError:
        try:
            return hub.select_tools(
                session_id=session_id,
                query=query,
                top_k=top_k,
                budget_tokens=budget_tokens,
                sync=False,
            )
        except TypeError:
            return hub.select_tools(
                session_id=session_id,
                query=query,
                top_k=top_k,
                budget_tokens=budget_tokens,
            )


def _query_text_from_parts(parts: Any) -> str:
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for item in parts:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "text":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            chunks.append(text.strip())
    return "\n".join(chunks).strip()


def _map_selected_to_runtime_ids(
    selected_tool_ids: list[str],
    runtime_ids: set[str],
) -> list[str]:
    mapped: list[str] = []
    seen: set[str] = set()
    for selected in selected_tool_ids:
        candidates = [selected]
        if ":" in selected:
            provider, tool_name = selected.split(":", 1)
            candidates.append(tool_name)
            candidates.append(f"{provider}_{tool_name}")
            if provider == "opencode-native":
                candidates.insert(0, tool_name)
        for candidate in candidates:
            if candidate in runtime_ids and candidate not in seen:
                seen.add(candidate)
                mapped.append(candidate)
                break
    return mapped


def _runtime_tool_ids(state: _GatewayState, directory: str | None) -> set[str]:
    now = time.time()
    with state.lock:
        cached = state.runtime_ids_cache
        if cached and cached.expires_at > now:
            return set(cached.ids)

    ids = _fetch_runtime_tool_ids(
        state.config.upstream_url,
        state.config.request_timeout_sec,
        directory,
    )

    with state.lock:
        state.runtime_ids_cache = _RuntimeIdsCache(
            expires_at=now + state.config.max_runtime_ids_cache_sec,
            ids=set(ids),
        )
    return set(ids)


def _fetch_runtime_tool_ids(
    upstream_url: str,
    timeout_sec: float,
    directory: str | None,
) -> set[str]:
    query: list[tuple[str, str]] = []
    if directory:
        query.append(("directory", directory))
    suffix = ""
    if query:
        suffix = "?" + urlparse.urlencode(query)
    request_obj = urlrequest.Request(
        f"{upstream_url.rstrip('/')}/experimental/tool/ids{suffix}",
        method="GET",
        headers={"Accept": "application/json"},
    )
    with urlrequest.urlopen(request_obj, timeout=timeout_sec) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        return set()
    return {str(item) for item in payload if isinstance(item, str)}


def _first(values: list[str] | None) -> str | None:
    if not values:
        return None
    value = values[0]
    return value if value else None


def _load_gateway_hub() -> ToolRouterHub:
    config_path = os.environ.get("OPENCODE_CONFIG", "~/.config/opencode/opencode.json")
    include_disabled = os.environ.get(
        "ROUTER_INCLUDE_DISABLED", "true"
    ).lower() not in {
        "0",
        "false",
        "no",
    }
    ignore_ids = _parse_id_list(os.environ.get("ROUTER_IGNORE_IDS"))
    router_id = os.environ.get("ROUTER_MCP_ID")
    if router_id:
        ignore_ids.add(router_id)
    if not ignore_ids:
        ignore_ids.add("router")
    routerd_cmd = os.environ.get("ROUTERD")
    return ToolRouterHub.from_opencode_config(
        config_path,
        routerd_path=routerd_cmd,
        auto_sync=True,
        include_disabled=include_disabled,
        ignore_ids=sorted(ignore_ids),
    )


def _parse_id_list(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def _load_gateway_config() -> GatewayConfig:
    bind_host = os.environ.get("ROUTER_GATEWAY_BIND", "127.0.0.1")
    bind_port = int(os.environ.get("ROUTER_GATEWAY_PORT", "4141"))
    upstream_url = (
        os.environ.get("ROUTER_OPENCODE_UPSTREAM_URL")
        or os.environ.get("OPENCODE_UPSTREAM_URL")
        or _resolve_opencode_server_url()
    )
    return GatewayConfig(
        bind_host=bind_host,
        bind_port=bind_port,
        upstream_url=upstream_url.rstrip("/"),
        request_timeout_sec=float(os.environ.get("ROUTER_GATEWAY_TIMEOUT_SEC", "15")),
        stream_timeout_sec=float(
            os.environ.get("ROUTER_GATEWAY_STREAM_TIMEOUT_SEC", "0")
        ),
        select_timeout_sec=float(
            os.environ.get("ROUTER_GATEWAY_SELECT_TIMEOUT_SEC", "2")
        ),
        select_top_k=int(os.environ.get("ROUTER_GATEWAY_TOP_K", "20")),
        select_budget_tokens=int(
            os.environ.get("ROUTER_GATEWAY_BUDGET_TOKENS", "1500")
        ),
        default_session_id=os.environ.get("ROUTER_SESSION_ID", "default"),
        max_runtime_ids_cache_sec=float(
            os.environ.get("ROUTER_GATEWAY_IDS_TTL_SEC", "3")
        ),
    )


def main() -> int:
    global _STATE
    config = _load_gateway_config()
    hub = _load_gateway_hub()
    _STATE = _GatewayState(
        hub=hub,
        config=config,
        lock=threading.Lock(),
        session_permission_hashes={},
    )
    server = ThreadingHTTPServer(
        (config.bind_host, config.bind_port), OpenCodeGatewayHandler
    )
    print(
        f"[mcp-tool-router] OpenCode gateway listening on http://{config.bind_host}:{config.bind_port} "
        f"-> upstream {config.upstream_url}",
        file=sys.stderr,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        hub.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
