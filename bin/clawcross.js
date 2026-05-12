#!/usr/bin/env node
"use strict";

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const script = path.join(root, "scripts", "clawcross.py");
const runScript = process.platform === "win32"
  ? path.join(root, "selfskill", "scripts", "run.ps1")
  : path.join(root, "selfskill", "scripts", "run.sh");

const runCommands = new Set([
  "dev",
  "start",
  "start-foreground",
  "start-fg",
  "stop",
  "restart",
  "setup",
  "status",
  "logs",
  "doctor",
  "configure",
  "auto-model",
  "check-openclaw",
  "start-tunnel",
  "stop-tunnel",
  "evolve-skill",
]);

function getClawcrossHome() {
  return process.env.CLAWCROSS_HOME || path.join(os.homedir(), ".clawcross");
}

function isLegacyMode() {
  const value = (process.env.CLAWCROSS_USE_LEGACY_PATHS || "").trim().toLowerCase();
  return ["1", "true", "yes", "on"].includes(value);
}

function applyRuntimeEnv() {
  const legacy = isLegacyMode();
  const home = legacy ? root : getClawcrossHome();
  process.env.CLAWCROSS_HOME = home;
  process.env.CLAWCROSS_VENV_DIR = process.env.CLAWCROSS_VENV_DIR || (legacy ? path.join(root, ".venv") : path.join(home, "venv"));
  process.env.CLAWCROSS_DATA_DIR = process.env.CLAWCROSS_DATA_DIR || (legacy ? path.join(root, "data") : path.join(home, "data"));
  process.env.CLAWCROSS_LOG_DIR = process.env.CLAWCROSS_LOG_DIR || (legacy ? path.join(root, "logs") : path.join(home, "logs"));
  process.env.CLAWCROSS_CONFIG_DIR = process.env.CLAWCROSS_CONFIG_DIR || (legacy ? path.join(root, "config") : path.join(home, "config"));
  process.env.CLAWCROSS_RUN_DIR = process.env.CLAWCROSS_RUN_DIR || (legacy ? root : path.join(home, "run"));
  process.env.CLAWCROSS_BIN_DIR = process.env.CLAWCROSS_BIN_DIR || (legacy ? path.join(root, "bin") : path.join(home, "bin"));
  process.env.CLAWCROSS_WORKSPACE_DIR = process.env.CLAWCROSS_WORKSPACE_DIR || (legacy ? root : path.join(home, "workspace"));
  process.env.CLAWCROSS_STATE_DIR = process.env.CLAWCROSS_STATE_DIR || home;
  process.env.PYTHONDONTWRITEBYTECODE = process.env.PYTHONDONTWRITEBYTECODE || "1";
}

function loadDotEnv(filePath) {
  if (!fs.existsSync(filePath)) {
    return {};
  }
  const values = {};
  for (const rawLine of fs.readFileSync(filePath, "utf8").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) {
      continue;
    }
    const index = line.indexOf("=");
    const key = line.slice(0, index).trim();
    let value = line.slice(index + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    if (key) {
      values[key] = value;
    }
  }
  return values;
}

function firstExisting(candidates) {
  for (const candidate of candidates) {
    if (candidate && fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return null;
}

function isAgentRunning(port) {
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    return Promise.resolve(false);
  }
  return new Promise((resolve) => {
    const req = http.get(
      {
        hostname: "127.0.0.1",
        port,
        path: "/v1/models",
        timeout: 700,
      },
      (res) => {
        res.resume();
        resolve(res.statusCode >= 200 && res.statusCode < 500);
      },
    );
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
    req.on("error", () => resolve(false));
  });
}

async function main() {
  let args = process.argv.slice(2);
  if (args[0] === "dev") {
    process.env.CLAWCROSS_HOME = path.join(root, ".clawcross-dev");
    args = ["start", ...args.slice(1)];
  }
  applyRuntimeEnv();
  const venvDir = process.env.CLAWCROSS_VENV_DIR;
  const python = firstExisting([
    process.platform === "win32" ? path.join(venvDir, "Scripts", "python.exe") : null,
    path.join(venvDir, "bin", "python"),
    process.platform === "win32" ? path.join(root, ".venv", "Scripts", "python.exe") : null,
    path.join(root, ".venv", "bin", "python"),
  ]) || process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
  const env = loadDotEnv(firstExisting([
    path.join(process.env.CLAWCROSS_CONFIG_DIR, ".env"),
    path.join(root, "config", ".env"),
  ]) || path.join(process.env.CLAWCROSS_CONFIG_DIR, ".env"));
  const agentPort = Number.parseInt(process.env.PORT_AGENT || env.PORT_AGENT || "51200", 10);
  // No-args behaviour: drop straight into the interactive REPL. The REPL
  // itself works without LLM credentials — users can configure via /model
  // before sending any prompt. The full backend (which validates
  // LLM_MODEL) is opt-in via `clawcross start`. If the backend is
  // already running we still go to REPL; backend startup is never
  // forced from a bare `clawcross` invocation.
  const command = args[0];
  const launcherArgs = args;
  const useRunScript = runCommands.has(command);
  const launcher = useRunScript
    ? (process.platform === "win32"
      ? ["powershell", ["-ExecutionPolicy", "Bypass", "-File", runScript, ...launcherArgs]]
      : ["bash", [runScript, ...launcherArgs]])
    : [python, [script, ...args]];

  const result = spawnSync(launcher[0], launcher[1], {
    stdio: "inherit",
    env: process.env,
    cwd: useRunScript ? root : process.cwd(),
  });

  if (result.error) {
    console.error(`Failed to launch ClawCross with ${launcher[0]}: ${result.error.message}`);
    process.exit(1);
  }

  process.exit(result.status === null ? 1 : result.status);
}

main().catch((error) => {
  console.error(`Failed to launch ClawCross: ${error.message}`);
  process.exit(1);
});
