# mcpflow-router

Smart tool routing for OpenCode. Reduces tool context
usage through smart search, working-set management, and on-demand tool loading.

## The Problem

When you configure many MCP servers in OpenCode, every tool definition is sent to
the LLM on every turn — leaving less context for your actual conversation and code.

## The Solution

mcpflow-router sits between OpenCode and your MCP servers, exposing only **3 meta-tools**:

| Tool                  | Purpose                                            |
| --------------------- | -------------------------------------------------- |
| `select_tools`        | Search for relevant tools by query |
| `call_tool`           | Call a tool by `{serverId}:{toolName}`             |
| `tool_info`           | Inspect a tool's full schema before calling it     |

```
User: "Create a GitHub PR"
  → OpenCode calls: select_tools({ query: "github pull request" })
  → Router returns: [{ toolId: "github:create_pull_request", ... }]
  → OpenCode calls: call_tool({ toolId: "github:create_pull_request", arguments: {...} })
```

## Quick Start

**3 commands and you're done:**

```bash
# 1. Install and configure mcpflow-router (auto-updates your OpenCode config)
npx mcpflow-router opencode install

# 2. Start OpenCode — it auto-loads mcpflow-router
# (no manual config needed!)

# 3. Verify it works
opencode mcp list
# Should see "router" with ✓ connected status
```

That's it! mcpflow-router automatically:
- ✅ Disables your existing MCP servers
- ✅ Configures itself as single MCP entry
- ✅ Starts managing all your tools via smart search

## OpenCode Native Tools (Auto-ingest)

The router auto-discovers OpenCode runtime tools through OpenCode's experimental
HTTP endpoints, indexes them as `opencode-native:*`, and executes selected tools
through OpenCode session APIs.

Requires OpenCode `1.2.10+`.

For full per-message reduction across OpenCode built-ins and MCP tools, use
gateway mode:

```bash
python -m mcp_tool_router.opencode_gateway_server
```

### Install from Source

```bash
git clone https://github.com/effortprogrammer/mcpflow-router.git
cd mcpflow-router
npm install && npm run build
pip install -e mcp-server/
npx mcpflow-router opencode install
```

For manual configuration and advanced options, see the [Configuration Guide](docs/configuration.md).
For host-native tool integration direction, see [Host Tool Routing Plan](docs/host-tool-routing.md).

## License

MIT
