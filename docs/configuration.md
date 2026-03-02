# Configuration Guide

## Quick Start

```bash
npx mcpflow-router opencode install
```

This auto-configures everything. The sections below are for advanced usage only.

The installer auto-bootstraps gateway Python dependencies into a user-local venv at
`$XDG_CACHE_HOME/mcpflow-router/gateway-venv` (or `~/.cache/mcpflow-router/gateway-venv`)
when your system Python does not already have `httpx` and `pyyaml`.

## How It Works

The installer sets up a launcher shim that starts three components automatically:

1. `opencode serve` — the OpenCode backend (port 4096)
2. Gateway proxy — scores tools and controls visibility (port 4141)
3. `opencode attach` — connects the TUI to the gateway

Subcommands like `opencode serve`, `opencode mcp`, and `opencode attach` pass through to the original binary unchanged.

Run `opencode` as your normal user (not `sudo`) so OpenCode uses the correct home config and auth directories.

If you need to revert everything:

```bash
npx mcpflow-router opencode uninstall
```

## Gateway Environment Variables

| Variable | Default | Description |
|---|---|---|
| `ROUTER_GATEWAY_BIND` | `127.0.0.1` | Gateway bind address |
| `ROUTER_GATEWAY_PORT` | `4141` | Gateway listen port |
| `ROUTER_OPENCODE_UPSTREAM_URL` | `http://127.0.0.1:4096` | OpenCode serve upstream URL |
| `OPENCODE_UPSTREAM_URL` | _(legacy fallback)_ | Legacy upstream URL override; used only if router var is unset |
| `ROUTER_OPENCODE_SERVER_LOG` | `$TMPDIR/mcpflow-opencode-serve.log` | Local `opencode serve` log file path |
| `ROUTER_OPENCODE_GATEWAY_LOG` | `$TMPDIR/mcpflow-opencode-gateway.log` | Gateway launcher log file path |
| `ROUTER_GATEWAY_TIMEOUT_SEC` | `15` | Proxy request timeout (seconds) |
| `ROUTER_GATEWAY_STREAM_TIMEOUT_SEC` | `0` (unlimited) | Streaming response timeout |
| `ROUTER_GATEWAY_SELECT_TIMEOUT_SEC` | `2` | Tool selection timeout |
| `ROUTER_GATEWAY_TOP_K` | `20` | Max candidate tools to score |
| `ROUTER_GATEWAY_BUDGET_TOKENS` | `4000` | Token budget for selected tools |
| `ROUTER_GATEWAY_LOG` | `$TMPDIR/mcpflow-gateway.log` | Gateway log file path |

## Context Mode v2 (Output Routing)

Context Mode reduces tool output bloat by routing outputs through tiers:

| Tier | Threshold | Action |
|---|---|---|
| L0 | < 1 KB | Pass through unchanged |
| L1 | 1-10 KB | Algorithmic summary + file link |
| L2 | > 10 KB | L1 summary (agent delegation planned) |

> **Note:** L2 currently uses the same algorithmic summary as L1. Agent-based intelligent summarization is planned for a future release when the agent interface is finalized.

### Context Mode Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CONTEXT_MODE_DISABLED` | _(unset)_ | Set to any value to disable output routing |
| `CONTEXT_MODE_DIR` | `/tmp/ctx` | Storage directory for original outputs |
| `CONTEXT_MODE_MAX_MB` | `100` | Max storage size before LRU cleanup |
| `CONTEXT_MODE_L0_THRESHOLD` | `1024` | L0/L1 boundary in bytes |
| `CONTEXT_MODE_L1_THRESHOLD` | `10240` | L1/L2 boundary in bytes |
| `CONTEXT_MODE_TOOL_OVERRIDES` | _(empty)_ | Force tools to tiers, e.g. `glob:L0,playwright:L2` |

### How Context Mode Works

When a tool produces output larger than L0 threshold:

1. Original output is saved to `/tmp/ctx/{tool}_{hash}.txt`
2. A summary is generated showing first/last lines and file link
3. The summary replaces the full output in the LLM context
4. The model can still access full output via `cat /tmp/ctx/...`

This complements mcpflow-router's tool selection (input optimization) with output optimization.

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

## Uninstall Options

```bash
npx mcpflow-router opencode uninstall --help
```

| Option | Description |
|---|---|
| `--config <path>` | Path to OpenCode config file |
| `--keep-backups` | Keep `.bak` / `.mcpflow-real` artifacts after restore |
| `--dry-run` | Show restore/remove actions without applying |

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
