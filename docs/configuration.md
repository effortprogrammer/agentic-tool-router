# Configuration Guide

## Quick Start

```bash
npx mcpflow-router opencode install
```

This auto-configures everything. The sections below are for advanced usage only.

## How It Works

The installer sets up a launcher shim that starts three components automatically:

1. `opencode serve` — the OpenCode backend (port 4096)
2. Gateway proxy — scores tools and controls visibility (port 4141)
3. `opencode attach` — connects the TUI to the gateway

Subcommands like `opencode serve`, `opencode mcp`, and `opencode attach` pass through to the original binary unchanged.

Run `opencode` as your normal user (not `sudo`) so OpenCode uses the correct home config and auth directories.

## Gateway Environment Variables

| Variable | Default | Description |
|---|---|---|
| `ROUTER_GATEWAY_BIND` | `127.0.0.1` | Gateway bind address |
| `ROUTER_GATEWAY_PORT` | `4141` | Gateway listen port |
| `ROUTER_OPENCODE_UPSTREAM_URL` | `http://127.0.0.1:4096` | OpenCode serve upstream URL |
| `ROUTER_GATEWAY_TIMEOUT_SEC` | `15` | Proxy request timeout (seconds) |
| `ROUTER_GATEWAY_STREAM_TIMEOUT_SEC` | `0` (unlimited) | Streaming response timeout |
| `ROUTER_GATEWAY_SELECT_TIMEOUT_SEC` | `2` | Tool selection timeout |
| `ROUTER_GATEWAY_TOP_K` | `20` | Max candidate tools to score |
| `ROUTER_GATEWAY_BUDGET_TOKENS` | `4000` | Token budget for selected tools |
| `ROUTER_GATEWAY_LOG` | `$TMPDIR/mcpflow-gateway.log` | Gateway log file path |

## Other Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OPENCODE_CONFIG` | `~/.config/opencode/opencode.json` | Path to OpenCode config |
| `ROUTERD` | auto-detect | Override the tool-routerd daemon path |
| `ROUTER_IGNORE_IDS` | _(empty)_ | Comma-separated MCP server IDs to skip |
| `ROUTER_INCLUDE_DISABLED` | `true` | Include disabled MCP entries from config |
| `ROUTER_SESSION_ID` | `default` | Session ID for working-set tracking |

## Install Options

```bash
npx mcpflow-router opencode install --help
```

| Option | Description |
|---|---|
| `--config <path>` | Path to OpenCode config file |
| `--no-backup` | Skip creating config backup |
| `--dry-run` | Show changes without applying |

## Compatibility

- Tested with OpenCode 1.2.10+
- Uses OpenCode experimental endpoints: `/experimental/tool/ids`, `/experimental/tool`
- Gateway proxies all OpenCode HTTP API paths transparently

## Manual Gateway Run

For advanced use cases, the gateway can be started independently:

```bash
python -m mcp_tool_router.opencode_gateway_server
```

## Repository Layout

```
src/              # TypeScript source
  core/           # Search engine, tokenizer, working set, catalog
  daemon/         # tool-routerd JSON-RPC server
  shared/         # Shared types (ToolCard, etc.)
mcp-server/       # Python package
  mcp_tool_router/
    opencode_gateway_server.py  # Gateway proxy
    hub.py                      # Tool catalog + enrichments
    router.py                   # Tool selection (daemon RPC)
    registry.py                 # MCP server registry
    opencode_config.py          # Config management
docs/             # Documentation
```
