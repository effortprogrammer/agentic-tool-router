#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

type JsonObject = Record<string, unknown>;
type InstallTarget = "opencode" | "claude" | "codex" | "openclaw";

interface JsonInstallProfile {
  target: InstallTarget;
  kind: "json";
  displayName: string;
  envVar: string;
  defaultConfigPath: string;
  mcpField: "mcp" | "mcpServers";
  wellKnownRemoteMcps?: Record<string, JsonObject>;
  postWriteHook?: (configPath: string, createBackup: boolean) => void;
  postInstallHook?: (
    createBackup: boolean,
    gatewayRuntime: { command: string[]; env: Record<string, string> },
  ) => void;
}

interface UnsupportedInstallProfile {
  target: InstallTarget;
  kind: "unsupported";
  displayName: string;
  reason: string;
}

type InstallProfile = JsonInstallProfile | UnsupportedInstallProfile;

const ROUTER_MODULE = "mcp_tool_router.router_mcp_server";
const GATEWAY_MODULE = "mcp_tool_router.opencode_gateway_server";
const REQUIRED_PACKAGES = ["httpx", "pyyaml"];

const OPENCODE_WELL_KNOWN_REMOTE_MCPS: Record<string, JsonObject> = {
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

const INSTALL_PROFILES: Record<InstallTarget, InstallProfile> = {
  opencode: {
    target: "opencode",
    kind: "json",
    displayName: "OpenCode",
    envVar: "OPENCODE_CONFIG",
    defaultConfigPath: "~/.config/opencode/opencode.json",
    mcpField: "mcp",
    wellKnownRemoteMcps: OPENCODE_WELL_KNOWN_REMOTE_MCPS,
    postWriteHook: disableOhMyOpencodeMcps,
    postInstallHook: ensureOpencodeGatewayShim,
  },
  claude: {
    target: "claude",
    kind: "json",
    displayName: "Claude Code",
    envVar: "CLAUDE_CONFIG",
    defaultConfigPath: "~/.claude.json",
    mcpField: "mcpServers",
  },
  codex: {
    target: "codex",
    kind: "unsupported",
    displayName: "Codex",
    reason:
      "Codex currently uses TOML config (~/.codex/config.toml). JSON-based auto-install is not enabled yet.",
  },
  openclaw: {
    target: "openclaw",
    kind: "json",
    displayName: "OpenClaw",
    envVar: "OPENCLAW_CONFIG",
    defaultConfigPath: "~/.config/openclaw/openclaw.json",
    mcpField: "mcpServers",
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

  const target = args[0];
  if (target && args.length >= 2 && args[1] === "install" && isInstallTarget(target)) {
    installTarget(target, args.slice(2));
    return;
  }

  console.error(`Unknown command: ${args.join(" ")}`);
  printHelp();
  process.exit(1);
}

function installTarget(target: InstallTarget, args: string[]): void {
  const profile = INSTALL_PROFILES[target];
  if (profile.kind === "unsupported") {
    console.error(
      `${profile.displayName} install is not supported yet. ${profile.reason}`,
    );
    process.exit(1);
  }

  const options = parseInstallArgs(args);
  const configPath = expandHome(options.config || process.env[profile.envVar] || profile.defaultConfigPath);
  const payload = loadConfig(configPath);
  const mcp = ensureMcpField(payload, profile.mcpField, profile.displayName);

  const routerId = options.routerId;
  const monorepoRoot = findMonorepoRoot();
  const resolved =
    options.routerCommand.length > 0
      ? { command: options.routerCommand, env: {} as Record<string, string> }
      : resolveRouterCommand(monorepoRoot);
  const gatewayResolved = resolveGatewayCommand(monorepoRoot);

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

  if (profile.wellKnownRemoteMcps) {
    for (const [id, entry] of Object.entries(profile.wellKnownRemoteMcps)) {
      if (id in mcp) {
        continue;
      }
      mcp[id] = { ...entry };
    }
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
  console.log(`Updated ${profile.displayName} config at ${configPath}`);
  if (profile.postWriteHook) {
    profile.postWriteHook(configPath, options.createBackup);
  }
  if (profile.postInstallHook) {
    profile.postInstallHook(options.createBackup, gatewayResolved);
  }

  if (target === "opencode") {
    if (isRunningAsRootViaSudo()) {
      console.warn(
        "[mcpflow] Detected sudo install. For normal OpenCode usage, run `opencode` without sudo.",
      );
    }
    console.log(
      "[mcpflow] OpenCode is ready. Just run `opencode` (no extra gateway/shim command needed).",
    );
  }
}

function isInstallTarget(value: string): value is InstallTarget {
  return value in INSTALL_PROFILES;
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
    throw new Error("Config file must be a JSON object.");
  }
  return payload as JsonObject;
}

function ensureMcpField(
  payload: JsonObject,
  field: "mcp" | "mcpServers",
  displayName: string,
): Record<string, JsonObject> {
  if (!(field in payload) || payload[field] == null) {
    payload[field] = {};
  }
  if (
    typeof payload[field] !== "object" ||
    payload[field] === null ||
    Array.isArray(payload[field])
  ) {
    throw new Error(
      `${displayName} config '${field}' field must be an object.`,
    );
  }
  return payload[field] as Record<string, JsonObject>;
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

const OPENCODE_SHIM_MARKER = "# mcpflow-router managed opencode launcher";
const OPENCODE_REAL_SUFFIX = ".mcpflow-real";

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

function ensureOpencodeGatewayShim(
  createBackup: boolean,
  gatewayRuntime: { command: string[]; env: Record<string, string> },
): void {
  const opencodePath = findCommand("opencode");
  if (!opencodePath) {
    console.warn(
      "[mcpflow] Could not find 'opencode' binary in PATH; skipping automatic gateway launcher shim.",
    );
    return;
  }

  let stat: fs.Stats;
  try {
    stat = fs.statSync(opencodePath);
  } catch {
    console.warn(
      `[mcpflow] Could not inspect ${opencodePath}; skipping automatic gateway launcher shim.`,
    );
    return;
  }

  if (!stat.isFile()) {
    console.warn(
      `[mcpflow] '${opencodePath}' is not a regular file; skipping automatic gateway launcher shim.`,
    );
    return;
  }

  const existingContent = safeReadText(opencodePath);
  const hasManagedShim =
    existingContent !== null && existingContent.includes(OPENCODE_SHIM_MARKER);

  const realBinaryPath = `${opencodePath}${OPENCODE_REAL_SUFFIX}`;
  if (hasManagedShim) {
    if (!fs.existsSync(realBinaryPath)) {
      console.warn(
        `[mcpflow] Found managed OpenCode shim but missing backup binary at ${realBinaryPath}. Skipping shim update.`,
      );
      return;
    }
  } else {
    if (createBackup) {
      fs.copyFileSync(opencodePath, `${opencodePath}.bak`);
    }
    if (fs.existsSync(realBinaryPath)) {
      fs.copyFileSync(opencodePath, realBinaryPath);
    } else {
      fs.renameSync(opencodePath, realBinaryPath);
    }
  }

  // Re-sign the binary after rename/copy so macOS doesn't SIGKILL it.
  try {
    spawnSync("codesign", ["--force", "--sign", "-", realBinaryPath], {
      stdio: "pipe",
    });
  } catch {
    // codesign may not exist on non-macOS; ignore.
  }

  const shim = buildOpencodeShim(realBinaryPath, gatewayRuntime);
  fs.writeFileSync(opencodePath, shim, { encoding: "utf-8", mode: 0o755 });
  fs.chmodSync(opencodePath, 0o755);

  console.log(
    `[mcpflow] Installed automatic OpenCode gateway launcher at ${opencodePath}.`,
  );
}

function safeReadText(filePath: string): string | null {
  try {
    return fs.readFileSync(filePath, "utf-8");
  } catch {
    return null;
  }
}

function buildOpencodeShim(
  realBinaryPath: string,
  gatewayRuntime: { command: string[]; env: Record<string, string> },
): string {
  const quotedReal = shSingleQuote(realBinaryPath);
  const marker = OPENCODE_SHIM_MARKER;
  const gatewayEnvParts = Object.entries(gatewayRuntime.env).map(
    ([k, v]) => `${k}=${shSingleQuote(v)}`,
  );
  const gatewayCommandParts = gatewayRuntime.command.map((part) =>
    shSingleQuote(part),
  );
  const gatewayLaunch =
    gatewayEnvParts.length > 0
      ? `env ${gatewayEnvParts.join(" ")} ${gatewayCommandParts.join(" ")}`
      : gatewayCommandParts.join(" ");
  return [
    "#!/usr/bin/env bash",
    marker,
    "set -euo pipefail",
    "if [[ \"${MCPFLOW_NOSUDO_REEXEC:-0}\" != \"1\" && ${EUID:-$(id -u)} -eq 0 && -n \"${SUDO_USER:-}\" ]]; then",
    "  if command -v sudo >/dev/null 2>&1; then",
    "    exec env MCPFLOW_NOSUDO_REEXEC=1 sudo -u \"$SUDO_USER\" -H \"$0\" \"$@\"",
    "  fi",
    "fi",
    "REAL_OPENCODE=" + quotedReal,
    "GATEWAY_HOST=${ROUTER_GATEWAY_BIND:-127.0.0.1}",
    "GATEWAY_PORT=${ROUTER_GATEWAY_PORT:-4141}",
    "UPSTREAM_URL=${ROUTER_OPENCODE_UPSTREAM_URL:-http://127.0.0.1:4096}",
    "export ROUTER_OPENCODE_UPSTREAM_URL=\"$UPSTREAM_URL\"",
    "started_server=0",
    "started_gateway=0",
    "server_pid=",
    "gateway_pid=",
    "cleanup() {",
    "  if [[ \"$started_gateway\" -eq 1 && -n \"${gateway_pid}\" ]]; then",
    "    kill \"$gateway_pid\" >/dev/null 2>&1 || true",
    "  fi",
    "  if [[ \"$started_server\" -eq 1 && -n \"${server_pid}\" ]]; then",
    "    kill \"$server_pid\" >/dev/null 2>&1 || true",
    "  fi",
    "}",
    "trap cleanup EXIT INT TERM",
    "if [[ $# -gt 0 ]]; then",
    "  case \"$1\" in",
    "    attach|serve|web|acp|completion|mcp|run|debug|auth|agent|upgrade|uninstall|models|stats|export|import|github|pr|session|db)",
    "      exec \"$REAL_OPENCODE\" \"$@\"",
    "      ;;",
    "    -h|--help|-v|--version|--print-logs|--log-level|--port|--hostname|--mdns|--mdns-domain|--cors|-m|--model|-c|--continue|-s|--session|--fork|--prompt|--agent)",
    "      exec \"$REAL_OPENCODE\" \"$@\"",
    "      ;;",
    "    *)",
    "      if [[ \"$1\" != -* ]]; then",
    "        first_project=$1",
    "        shift",
    "        set -- --dir=\"$first_project\" \"$@\"",
    "      fi",
    "      ;;",
    "  esac",
    "fi",
    "if ! lsof -nP -iTCP:4096 -sTCP:LISTEN >/dev/null 2>&1; then",
    "  \"$REAL_OPENCODE\" serve --hostname=127.0.0.1 --port=4096 >/dev/null 2>&1 &",
    "  server_pid=$!",
    "  started_server=1",
    "  for _ in {1..80}; do",
    "    if lsof -nP -iTCP:4096 -sTCP:LISTEN >/dev/null 2>&1; then",
    "      break",
    "    fi",
    "    sleep 0.05",
    "  done",
    "fi",
    "existing_gateway_pids=$(lsof -tiTCP:\"$GATEWAY_PORT\" -sTCP:LISTEN 2>/dev/null || true)",
    "if [[ -n \"$existing_gateway_pids\" ]]; then",
    "  kill $existing_gateway_pids >/dev/null 2>&1 || true",
    "  sleep 0.1",
    "fi",
    ` ${gatewayLaunch} >/dev/null 2>&1 &`,
    "gateway_pid=$!",
    "started_gateway=1",
    "for _ in {1..80}; do",
    "  if lsof -nP -iTCP:\"$GATEWAY_PORT\" -sTCP:LISTEN >/dev/null 2>&1; then",
    "    break",
    "  fi",
    "  sleep 0.05",
    "done",
    "exec \"$REAL_OPENCODE\" attach \"http://${GATEWAY_HOST}:${GATEWAY_PORT}\" \"$@\"",
    "",
  ].join("\n");
}

function shSingleQuote(value: string): string {
  return `'${value.replace(/'/g, `'"'"'`)}'`;
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
  return resolvePythonModuleCommand(monorepoRoot, ROUTER_MODULE);
}

function resolveGatewayCommand(
  monorepoRoot: string | null,
): { command: string[]; env: Record<string, string> } {
  return resolvePythonModuleCommand(monorepoRoot, GATEWAY_MODULE);
}

function resolvePythonModuleCommand(
  monorepoRoot: string | null,
  moduleName: string,
): { command: string[]; env: Record<string, string> } {
  const env: Record<string, string> = {};
  const defaultCommand = ["python3", "-m", moduleName];

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
      return { command: [venvPython, "-m", moduleName], env };
    }
  }

  // 2. System python3 — if deps already available
  const systemPython = findPython();
  if (
    systemPython !== null &&
    canImport(systemPython, "httpx", env)
  ) {
    return { command: [systemPython, "-m", moduleName], env };
  }

  // 3. uv run — auto-installs deps in ephemeral env
  const uv = findCommand("uv");
  if (uv !== null) {
    const withArgs = REQUIRED_PACKAGES.flatMap((pkg) => ["--with", pkg]);
    return {
      command: [uv, "run", ...withArgs, "python3", "-m", moduleName],
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

function isRunningAsRootViaSudo(): boolean {
  return typeof process.getuid === "function" && process.getuid() === 0 && !!process.env.SUDO_USER;
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
      "mcpflow-router <target> install [options]",
      "",
      "Targets:",
      "  opencode   OpenCode JSON config (~/.config/opencode/opencode.json)",
      "  claude     Claude Code JSON config (~/.claude.json)",
      "  codex      Reserved target (TOML installer not yet enabled)",
      "  openclaw   OpenClaw JSON config (~/.config/openclaw/openclaw.json)",
      "",
      "Options:",
      "  --config <path>           Override target config path",
      "  --router-id <id>          MCP server id for the router (default: router)",
      "  --router-command <cmd..>  Router command (default: python3 -m mcp_tool_router.router_mcp_server)",
      "  --keep-others             Keep existing enabled flags for other MCP entries",
      "  --disable-others          Disable all other MCP entries (default)",
      "  --no-backup               Do not create a .bak backup",
      "  --dry-run                 Print changes without writing",
      "",
      "OpenCode note:",
      "  After `opencode install`, just run `opencode` (without sudo).",
    ].join("\n"),
  );
}

main();
