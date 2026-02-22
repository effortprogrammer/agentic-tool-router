# mcpflow-router

Smart tool routing for [OpenCode](https://opencode.ai). Reduces MCP tool context usage
through smart search, working-set management, and on-demand tool loading.

## The Problem

When you configure many MCP servers in OpenCode, every tool definition is sent to
the LLM on every turn — leaving less context for your actual conversation and code.

## The Solution

mcpflow-router sits between OpenCode and your MCP servers, exposing only **3 meta-tools**:

| Tool                  | Purpose                                            |
| --------------------- | -------------------------------------------------- |
| `router_select_tools` | Search for relevant tools by query |
| `router_call_tool`    | Call a tool by `{serverId}:{toolName}`             |
| `router_tool_info`    | Inspect a tool's full schema before calling it     |

```
User: "Create a GitHub PR"
  → OpenCode calls: router_select_tools({ query: "github pull request" })
  → Router returns: [{ toolId: "github:create_pull_request", ... }]
  → OpenCode calls: router_call_tool({ toolId: "github:create_pull_request", arguments: {...} })
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

### Install from Source

```bash
git clone https://github.com/effortprogrammer/mcpflow-router.git
cd mcpflow-router
npm install && npm run build
pip install -e mcp-server/
npx mcpflow-router opencode install
```

For manual configuration and advanced options, see the [Configuration Guide](docs/configuration.md).

## License

MIT
