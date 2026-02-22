#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync, type SpawnSyncReturns } from "node:child_process";
import { fileURLToPath } from "node:url";

function main(): void {
  const configPath = resolveConfigPath();
  if (!fs.existsSync(configPath)) {
    console.warn(
      `[mcpflow] OpenCode config not found at ${configPath}. ` +
        "Install OpenCode first or set OPENCODE_CONFIG to the correct path.",
    );
    return;
  }

  ensurePythonPackage();

  const result = spawnSync(
    process.execPath,
    [
      path.join("dist", "index.js"),
      "opencode",
      "install",
      "--config",
      configPath,
    ],
    { stdio: "inherit" },
  );
  if (result.status && result.status !== 0) {
    process.exit(result.status);
  }
}

function ensurePythonPackage(): void {
  const python = findPython();
  if (!python) {
    console.warn(
      "[mcpflow] python3 not found. Install Python 3.10+ to use mcpflow.",
    );
    return;
  }

  if (isPackageImportable(python)) {
    return;
  }

  const thisDir = path.dirname(fileURLToPath(import.meta.url));
  const mcpServerDir = path.join(thisDir, "..", "mcp-server");

  if (!fs.existsSync(mcpServerDir)) {
    console.warn(
      "[mcpflow] Could not find bundled mcp-server directory. " +
        "Please reinstall mcpflow or install manually:\n" +
        "  pip install git+https://github.com/effortprogrammer/mcpflow.git#subdirectory=mcp-server",
    );
    return;
  }

  console.log("[mcpflow] Installing Python package from bundled directory...");

  if (tryPipInstall(python, [mcpServerDir])) {
    return;
  }

  if (tryPipInstall("uv", ["pip", "install", mcpServerDir])) {
    return;
  }

  console.warn(
    "[mcpflow] Could not auto-install the Python package. Install manually:\n" +
      `  pip install ${mcpServerDir}`,
  );
}

function findPython(): string | null {
  for (const cmd of ["python3", "python"]) {
    const r = spawnSync(cmd, ["--version"], { stdio: "pipe" });
    if (r.status === 0) {
      return cmd;
    }
  }
  return null;
}

function isPackageImportable(python: string): boolean {
  const r = spawnSync(python, ["-c", "import mcp_tool_router"], {
    stdio: "pipe",
  });
  return r.status === 0;
}

function tryPipInstall(python: string, args: string[]): boolean {
  const strategies: [string, string[]][] = [
    [python, ["-m", "pip", "install", ...args]],
    ["uv", ["pip", "install", ...args]],
  ];
  for (const [cmd, rest] of strategies) {
    const r = spawnSync(cmd, rest, { stdio: "pipe" });
    if (r.status === 0) {
      console.log(`[mcpflow] Python package installed via ${cmd}.`);
      return true;
    }
  }
  return false;
}

function resolveConfigPath(): string {
  if (process.env.OPENCODE_CONFIG) {
    return process.env.OPENCODE_CONFIG;
  }
  const base =
    process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config");
  return path.join(base, "opencode/opencode.json");
}

main();
