import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

function writeExecutable(filePath, content) {
  fs.writeFileSync(filePath, content, { encoding: "utf-8", mode: 0o755 });
  fs.chmodSync(filePath, 0o755);
}

test("opencode install/uninstall accepts jsonc config paths and preserves backup text", async () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "mcpflow-jsonc-"));
  const binDir = path.join(tempRoot, "bin");
  const configDir = path.join(tempRoot, "config");
  fs.mkdirSync(binDir, { recursive: true });
  fs.mkdirSync(configDir, { recursive: true });

  const pythonPath = path.join(binDir, "python3");
  writeExecutable(
    pythonPath,
    `#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then
  echo "Python 3.11.0"
  exit 0
fi
if [[ "$1" == "-c" ]]; then
  exit 0
fi
exit 0
`,
  );

  const opencodePath = path.join(binDir, "opencode");
  writeExecutable(
    opencodePath,
    `#!/usr/bin/env bash
if [[ "$1" == "-v" ]]; then
  echo "1.2.3"
  exit 0
fi
echo "original opencode $@"
`,
  );

  const configPath = path.join(configDir, "opencode.jsonc");
  const originalConfig = `{
  // comment should be accepted
  "mcp": {
    "router": {
      "enabled": false,
    },
  },
}
`;
  fs.writeFileSync(configPath, originalConfig, "utf-8");

  const env = {
    ...process.env,
    PATH: `${binDir}:${process.env.PATH || ""}`,
    XDG_CONFIG_HOME: configDir,
  };

  const install = spawnSync(
    process.execPath,
    ["dist/index.js", "opencode", "install", "--config", configPath],
    {
      cwd: process.cwd(),
      env,
      encoding: "utf-8",
    },
  );

  assert.equal(install.status, 0, install.stderr);
  assert.match(install.stdout, new RegExp(configPath.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  assert.equal(fs.existsSync(path.join(configDir, "opencode.json")), false);
  assert.equal(fs.readFileSync(`${configPath}.bak`, "utf-8"), originalConfig);

  const installedPayload = JSON.parse(fs.readFileSync(configPath, "utf-8"));
  assert.ok(installedPayload.mcp);
  assert.equal(installedPayload.mcp.router, undefined);
  assert.equal(installedPayload.mcp.context7.enabled, true);
  assert.equal(fs.existsSync(`${opencodePath}.mcpflow-real`), true);

  const uninstall = spawnSync(
    process.execPath,
    ["dist/index.js", "opencode", "uninstall", "--config", configPath, "--keep-backups"],
    {
      cwd: process.cwd(),
      env,
      encoding: "utf-8",
    },
  );

  assert.equal(uninstall.status, 0, uninstall.stderr);
  assert.equal(fs.readFileSync(configPath, "utf-8"), originalConfig);
});
