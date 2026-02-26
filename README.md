# mcpflow-router

Smart tool routing for OpenCode. Reduces tool context sent to the LLM by selecting only relevant tools per query.

## The Problem

When you configure many MCP servers in OpenCode, every tool definition is sent to the LLM on every turn — leaving less room for your actual conversation and code.

## How It Works

mcpflow-router runs a lightweight gateway between OpenCode and its backend. On each message, the gateway scores all available tools against your query and enables only the relevant ones. OpenCode and the LLM never see the tools that don't match.

- 33 tools configured → 3-10 tools sent per message (typical)
- Transparent — no workflow changes, no extra commands
- Works with both OpenCode native tools and MCP server tools
- Supports Korean and English queries

## Quick Start

```bash
npm install -g mcpflow-router@latest
npx mcpflow-router opencode install
```

Then just run `opencode` as usual. The installer automatically:
- Configures built-in MCP servers (context7, grep_app, websearch)
- Sets up the gateway launcher
- Handles all wiring — no manual config needed
- Migrates existing configurations on update

### Install from Source

```bash
git clone https://github.com/effortprogrammer/mcpflow-router.git
cd mcpflow-router
npm install && npm run build
pip install -e mcp-server/
npx mcpflow-router opencode install
```

## Requirements

- OpenCode 1.2.10+
- Python 3.10+ (with httpx, pyyaml)
- Node.js 18+

## Configuration

For environment variables and advanced options, see the [Configuration Guide](docs/configuration.md).

## License

MIT
