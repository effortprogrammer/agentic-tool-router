# Configuration Guide

## Adding New MCP Servers

```bash
# 1. Edit your OpenCode config (~/.config/opencode/opencode.json)
{
  "mcp": {
    "your-server": {
      "type": "local",
      "enabled": true,
      "command": ["..."],
      "env": {}
    }
  }
}

# 2. Restart OpenCode
# (router reloads config on OpenCode restart)
```

## Manual Config

If you prefer to configure manually or need custom settings, edit `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "router": {
      "type": "local",
      "enabled": true,
      "command": ["python3", "-m", "mcp_tool_router.router_mcp_server"]
    },
    "slack": {
      "type": "local",
      "enabled": false
    },
    "github": {
      "type": "local",
      "enabled": false,
      "command": ["npx", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_TOKEN": "$GITHUB_TOKEN" }
    }
  }
}
```

## Auto-configure Options

```bash
npx @mcpflow/cli opencode install --help
```

| Option | Description |
|---------|-------------|
| `--config <path>` | Path to OpenCode config (default: `~/.config/opencode/opencode.json`) |
| `--router-id <name>` | Router MCP server ID (default: `router`) |
| `--router-command <cmd>` | Override router daemon command |
| `--keep-others` | Don't disable existing MCP servers |
| `--dry-run` | Show changes without applying |

## Environment Variables

| Variable                  | Default                            | Description                                     |
| ------------------------- | ---------------------------------- | ----------------------------------------------- |
| `OPENCODE_CONFIG`         | `~/.config/opencode/opencode.json` | Path to OpenCode config                         |
| `ROUTERD`                 | auto-detect                        | Override the router daemon command              |
| `ROUTER_IGNORE_IDS`       | _(empty)_                          | Comma-separated MCP server IDs to skip          |
| `ROUTER_INCLUDE_DISABLED` | `true`                             | Include disabled MCP entries from config        |
| `ROUTER_MCP_ID`           | _(empty)_                          | Router's own MCP ID (auto-added to ignore list) |
| `ROUTER_SESSION_ID`       | `default`                          | Session ID for working-set tracking             |

## Minimum MCP Tool Fields

The router only requires MCP-standard fields:

- `name`
- `description` (optional but recommended)
- `inputSchema` (or `input_schema`)

Missing `tags`, `synonyms`, and `examples` are derived automatically from the
tool name and description.

## Repository Layout

```
packages/
  shared/    # Shared types (ToolCard, SearchQueryInput, etc.)
  core/      # Search engines, tokenizer, working set, result reducer
  daemon/    # tool-routerd JSON-RPC server
  cli/       # CLI helper (opencode install)
python/
  mcp_tool_router/  # Python MCP server + hub + registry
examples/
```
