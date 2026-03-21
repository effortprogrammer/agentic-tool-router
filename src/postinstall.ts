#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync, type SpawnSyncReturns } from "node:child_process";

function main(): void {
  const configPath = resolveConfigPath();
  ensurePythonRuntime();

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

function ensurePythonRuntime(): void {
  if (findPython()) {
    return;
  }
  console.warn(
    "[mcpflow] python3 not found. Install Python 3.10+ to use mcpflow.",
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

function resolveConfigPath(): string {
  if (process.env.OPENCODE_CONFIG) {
    return process.env.OPENCODE_CONFIG;
  }
  const base =
    process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config");
  const jsonPath = path.join(base, "opencode/opencode.json");
  const jsoncPath = jsonPath.replace(/\.json$/u, ".jsonc");
  return exists(jsoncPath) ? jsoncPath : jsonPath;
}

function exists(filePath: string): boolean {
  return fs.existsSync(filePath);
}

main();
