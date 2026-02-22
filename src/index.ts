#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

type JsonObject = Record<string, unknown>;

const ROUTER_MODULE = "mcp_tool_router.router_mcp_server";
const REQUIRED_PACKAGES = ["httpx", "pyyaml"];

const WELL_KNOWN_REMOTE_MCPS: Record<string, JsonObject> = {
  context7: {
    type: "remote",
    url: "https://mcp.context7.com/mcp",
    enabled: false,
  },
  grep_app: {
    type: "remote",
    url: "https://mcp.grep.app",
    enabled: false,
  },
  websearch: {
    type: "remote",
    url: "https://mcp.exa.ai/mcp?tools=web_search_exa",
    enabled: false,
  },
};

function main(): void {
  const args = process.argv.slice(2);
  if (
    args.length === 0 ||
    args[0] === "help" ||
    args[0] === "--help" ||
    args[0] === "-h"
  ) {
    printHelp();
    return;
  }

  if (args[0] === "opencode" && args[1] === "install") {
    opencodeInstall(args.slice(2));
    return;
  }

  console.error(`Unknown command: ${args.join(" ")}`);
  printHelp();
  process.exit(1);
}

function opencodeInstall(args: string[]): void {
  const options = parseInstallArgs(args);
  const configPath = expandHome(
    options.config ||
      process.env.OPENCODE_CONFIG ||
      "~/.config/opencode/opencode.json",
  );
  const payload = loadConfig(configPath);
  const mcp = ensureMcp(payload);

  const routerId = options.routerId;
  const monorepoRoot = findMonorepoRoot();
  const resolved =
    options.routerCommand.length > 0
      ? { command: options.routerCommand, env: {} as Record<string, string> }
      : resolveRouterCommand(monorepoRoot);

  const existing = mcp[routerId];
  const routerEntry =
    typeof existing === "object" && existing !== null
      ? { ...(existing as JsonObject) }
      : {};
  routerEntry.type = "local";
  routerEntry.enabled = true;
  routerEntry.command = resolved.command;

  if (Object.keys(resolved.env).length > 0) {
    const environment =
      typeof routerEntry.environment === "object" &&
      routerEntry.environment !== null
        ? { ...(routerEntry.environment as JsonObject) }
        : {};
    Object.assign(environment, resolved.env);
    routerEntry.environment = environment;
  }

  mcp[routerId] = routerEntry;

  for (const [id, entry] of Object.entries(WELL_KNOWN_REMOTE_MCPS)) {
    if (id in mcp) {
      continue;
    }
    mcp[id] = { ...entry };
  }

  if (options.disableOthers) {
    for (const [serverId, entry] of Object.entries(mcp)) {
      if (serverId === routerId) {
        continue;
      }
      if (typeof entry === "object" && entry !== null) {
        (entry as JsonObject).enabled = false;
      }
    }
  }

  if (options.dryRun) {
    printJson(payload);
    return;
  }

  writeConfig(configPath, payload, options.createBackup);
  console.log(`Updated OpenCode config at ${configPath}`);

  disableOhMyOpencodeMcps(configPath, options.createBackup);
}

function parseInstallArgs(args: string[]): {
  config: string | null;
  routerId: string;
  routerCommand: string[];
  disableOthers: boolean;
  createBackup: boolean;
  dryRun: boolean;
} {
  let config: string | null = null;
  let routerId = "router";
  let disableOthers = true;
  let createBackup = true;
  let dryRun = false;
  let routerCommand: string[] = [];

  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (arg === "--config") {
      const value = args[i + 1];
      if (!value) {
        throw new Error("--config requires a value");
      }
      config = value;
      i += 1;
      continue;
    }
    if (arg === "--router-id") {
      const value = args[i + 1];
      if (!value) {
        throw new Error("--router-id requires a value");
      }
      routerId = value;
      i += 1;
      continue;
    }
    if (arg === "--router-command") {
      const result = readCommandArgs(args, i + 1);
      routerCommand = result.command;
      i = result.nextIndex;
      continue;
    }
    if (arg === "--keep-others") {
      disableOthers = false;
      continue;
    }
    if (arg === "--disable-others") {
      disableOthers = true;
      continue;
    }
    if (arg === "--no-backup") {
      createBackup = false;
      continue;
    }
    if (arg === "--dry-run") {
      dryRun = true;
      continue;
    }
    if (arg && arg.startsWith("--")) {
      console.error(`Unknown option: ${arg}`);
      process.exit(1);
    }
  }

  return {
    config,
    routerId,
    routerCommand,
    disableOthers,
    createBackup,
    dryRun,
  };
}

function readCommandArgs(
  args: string[],
  startIndex: number,
): { command: string[]; nextIndex: number } {
  const command: string[] = [];
  let i = startIndex;
  for (; i < args.length; i += 1) {
    const current = args[i];
    if (!current) {
      continue;
    }
    if (current.startsWith("--")) {
      i -= 1;
      break;
    }
    command.push(current);
  }
  const first = command[0];
  if (command.length === 1 && first && first.includes(" ")) {
    command.splice(0, 1, ...first.split(" ").filter(Boolean));
  }
  return { command, nextIndex: i };
}

function loadConfig(configPath: string): JsonObject {
  if (!fs.existsSync(configPath)) {
    return {};
  }
  const raw = fs.readFileSync(configPath, "utf-8");
  const payload = JSON.parse(raw);
  if (
    typeof payload !== "object" ||
    payload === null ||
    Array.isArray(payload)
  ) {
    throw new Error("OpenCode config must be a JSON object.");
  }
  return payload as JsonObject;
}

function ensureMcp(payload: JsonObject): Record<string, JsonObject> {
  if (!("mcp" in payload) || payload.mcp == null) {
    payload.mcp = {};
  }
  if (
    typeof payload.mcp !== "object" ||
    payload.mcp === null ||
    Array.isArray(payload.mcp)
  ) {
    throw new Error("OpenCode config 'mcp' field must be an object.");
  }
  return payload.mcp as Record<string, JsonObject>;
}

function writeConfig(
  configPath: string,
  payload: JsonObject,
  createBackup: boolean,
): void {
  const dir = path.dirname(configPath);
  fs.mkdirSync(dir, { recursive: true });
  if (createBackup && fs.existsSync(configPath)) {
    fs.copyFileSync(configPath, `${configPath}.bak`);
  }
  fs.writeFileSync(configPath, JSON.stringify(payload, null, 2));
}

const OH_MY_OPENCODE_BUILTIN_MCPS = ["context7", "grep_app", "websearch"];

function disableOhMyOpencodeMcps(
  opencodeConfigPath: string,
  createBackup: boolean,
): void {
  const configDir = path.dirname(opencodeConfigPath);
  const omoPath = path.join(configDir, "oh-my-opencode.json");

  let omoPayload: JsonObject = {};
  if (fs.existsSync(omoPath)) {
    try {
      const raw = fs.readFileSync(omoPath, "utf-8");
      const parsed = JSON.parse(raw);
      if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
        omoPayload = parsed as JsonObject;
      }
    } catch {
      return;
    }
  }

  const existing = Array.isArray(omoPayload.disabled_mcps)
    ? (omoPayload.disabled_mcps as string[])
    : [];
  const merged = Array.from(new Set([...existing, ...OH_MY_OPENCODE_BUILTIN_MCPS]));

  if (
    merged.length === existing.length &&
    merged.every((v) => existing.includes(v))
  ) {
    return;
  }

  omoPayload.disabled_mcps = merged;
  if (createBackup && fs.existsSync(omoPath)) {
    fs.copyFileSync(omoPath, `${omoPath}.bak`);
  }
  fs.mkdirSync(configDir, { recursive: true });
  fs.writeFileSync(omoPath, JSON.stringify(omoPayload, null, 2));
  console.log(
    `Disabled oh-my-opencode built-in MCPs (${OH_MY_OPENCODE_BUILTIN_MCPS.join(", ")}) — now routed through the router.`,
  );
}

function expandHome(value: string): string {
  if (value.startsWith("~")) {
    return path.join(os.homedir(), value.slice(1));
  }
  return value;
}

function printJson(payload: JsonObject): void {
  console.log(JSON.stringify(payload, null, 2));
}

function resolveRouterCommand(
  monorepoRoot: string | null,
): { command: string[]; env: Record<string, string> } {
  const env: Record<string, string> = {};
  const defaultCommand = ["python3", "-m", ROUTER_MODULE];

  if (monorepoRoot) {
    const pythonDir = path.join(monorepoRoot, "mcp-server");
    if (
      fs.existsSync(pythonDir) &&
      fs.existsSync(path.join(pythonDir, "mcp_tool_router"))
    ) {
      env.PYTHONPATH = pythonDir;
    }

    const daemonCli = path.join(monorepoRoot, "dist", "daemon", "cli.js");
    if (fs.existsSync(daemonCli)) {
      env.ROUTERD = `node ${daemonCli}`;
    }
  }

  // 1. Project .venv python — has all deps installed
  if (monorepoRoot) {
    const venvPython = path.join(monorepoRoot, ".venv", "bin", "python3");
    if (
      fs.existsSync(venvPython) &&
      canImport(venvPython, "httpx", env)
    ) {
      return { command: [venvPython, "-m", ROUTER_MODULE], env };
    }
  }

  // 2. System python3 — if deps already available
  const systemPython = findPython();
  if (
    systemPython !== null &&
    canImport(systemPython, "httpx", env)
  ) {
    return { command: [systemPython, "-m", ROUTER_MODULE], env };
  }

  // 3. uv run — auto-installs deps in ephemeral env
  const uv = findCommand("uv");
  if (uv !== null) {
    const withArgs = REQUIRED_PACKAGES.flatMap((pkg) => ["--with", pkg]);
    return {
      command: [uv, "run", ...withArgs, "python3", "-m", ROUTER_MODULE],
      env,
    };
  }

  // 4. Fallback — bare python3 (may fail if deps missing)
  return { command: defaultCommand, env };
}

function canImport(
  python: string,
  pkg: string,
  env: Record<string, string>,
): boolean {
  const r = spawnSync(python, ["-c", `import ${pkg}`], {
    stdio: "pipe",
    env: { ...process.env, ...env },
  });
  return r.status === 0;
}

function findCommand(name: string): string | null {
  const r = spawnSync("which", [name], { stdio: "pipe" });
  if (r.status === 0) {
    const out = r.stdout?.toString().trim();
    return out || null;
  }
  return null;
}

function findMonorepoRoot(): string | null {
  const thisDir = path.dirname(fileURLToPath(import.meta.url));
  let dir = thisDir;
  const root = path.parse(dir).root;
  for (let i = 0; i < 8 && dir !== root; i++) {
    dir = path.dirname(dir);
    if (
      fs.existsSync(path.join(dir, "mcp-server", "mcp_tool_router")) &&
      fs.existsSync(path.join(dir, "dist", "index.js"))
    ) {
      return dir;
    }
  }
  return null;
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

function printHelp(): void {
  console.log(
    [
      "mcpflow opencode install [options]",
      "",
      "Options:",
      "  --config <path>           OpenCode config path (default: ~/.config/opencode/opencode.json)",
      "  --router-id <id>          MCP server id for the router (default: router)",
      "  --router-command <cmd..>  Router command (default: python3 -m mcp_tool_router.router_mcp_server)",
      "  --keep-others             Keep existing enabled flags for other MCP entries",
      "  --disable-others          Disable all other MCP entries (default)",
      "  --no-backup               Do not create a .bak backup",
      "  --dry-run                 Print changes without writing",
    ].join("\n"),
  );
}

main();
