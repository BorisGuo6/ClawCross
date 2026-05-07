#!/usr/bin/env node
"use strict";

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const script = path.join(root, "scripts", "clawcross.py");
const runScript = process.platform === "win32"
  ? path.join(root, "selfskill", "scripts", "run.ps1")
  : path.join(root, "selfskill", "scripts", "run.sh");

const runCommands = new Set([
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

function firstExisting(candidates) {
  for (const candidate of candidates) {
    if (candidate && fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return null;
}

const python = firstExisting([
  process.platform === "win32" ? path.join(root, ".venv", "Scripts", "python.exe") : null,
  path.join(root, ".venv", "bin", "python"),
]) || process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");

const args = process.argv.slice(2);
const command = args[0];
const launcher = runCommands.has(command)
  ? (process.platform === "win32"
    ? ["powershell", ["-ExecutionPolicy", "Bypass", "-File", runScript, ...args]]
    : ["bash", [runScript, ...args]])
  : [python, [script, ...args]];

const result = spawnSync(launcher[0], launcher[1], {
  stdio: "inherit",
  env: process.env,
  cwd: runCommands.has(command) ? root : process.cwd(),
});

if (result.error) {
  console.error(`Failed to launch ClawCross with ${launcher[0]}: ${result.error.message}`);
  process.exit(1);
}

process.exit(result.status === null ? 1 : result.status);
