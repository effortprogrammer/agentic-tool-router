import test from "node:test";
import assert from "node:assert/strict";

import { buildOpencodeShim, shouldBypassOpencodeShim } from "../dist/index.js";

test("bypasses only explicit passthrough commands and help/version", () => {
  for (const arg of [
    "attach",
    "serve",
    "run",
    "github",
    "help",
    "version",
    "--help",
    "-h",
    "--version",
    "-v",
  ]) {
    assert.equal(shouldBypassOpencodeShim(arg), true, `${arg} should bypass`);
  }
});

test("keeps router startup for option-only attach arguments", () => {
  for (const arg of [
    undefined,
    "--port",
    "--hostname",
    "--mdns",
    "--cors",
    "--agent",
    "-m",
    "--model",
    "--prompt",
  ]) {
    assert.equal(shouldBypassOpencodeShim(arg), false, `${arg} should not bypass`);
  }
});

test("generated shim keeps help/version bypass but routes option-only args", () => {
  const shim = buildOpencodeShim("/tmp/opencode-real", {
    command: ["python3", "-m", "mcp_tool_router.opencode_gateway_server"],
    env: { PYTHONPATH: "/tmp/mcp-server" },
  });
  const caseBlock = shim.match(/case "\$1" in[\s\S]*?esac/);

  assert.ok(caseBlock, "expected case block in generated shim");
  assert.match(shim, /\n    -h\|--help\|-v\|--version\|help\|version\)\n/);
  assert.doesNotMatch(
    caseBlock[0],
    /--port|--hostname|--mdns|--cors|--prompt/,
  );
  assert.match(shim, /"\$REAL_OPENCODE" attach "http:\/\/\$\{GATEWAY_HOST\}:\$\{GATEWAY_PORT\}" "\$@"/);
});
