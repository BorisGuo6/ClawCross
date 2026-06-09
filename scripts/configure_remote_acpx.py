#!/usr/bin/env python3
"""Install and smoke-check ACPX for ClawCross remote coding agents over SSH."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass


REMOTE_ENV_PATH = "~/.clawcross/remote_acpx.env"
REMOTE_ACPX_CWD = "~/.clawcross/acpx"


@dataclass
class StepResult:
    ok: bool
    target: str
    step: str
    returncode: int
    stdout: str
    stderr: str

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "target": self.target,
            "step": self.step,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def _ssh_prefix(connect_timeout: int) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        "-o",
        "ServerAliveInterval=3",
        "-o",
        "ServerAliveCountMax=1",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]


def _run_ssh(
    target: str,
    command: str,
    *,
    input_text: str | None = None,
    timeout: int = 180,
    connect_timeout: int = 8,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _ssh_prefix(connect_timeout) + [target, command],
        check=False,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _setup_command() -> str:
    return r"""
set -eu
mkdir -p "$HOME/.local/bin" "$HOME/.clawcross/acpx"
export PATH="$HOME/.local/bin:$PATH"
install_node_user() {
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) node_arch="x64" ;;
    aarch64|arm64) node_arch="arm64" ;;
    *) printf '{"ok":false,"error":"unsupported architecture for user-local Node.js","arch":"%s"}\n' "$arch"; exit 21 ;;
  esac
  node_version="${CLAWCROSS_NODE_VERSION:-}"
  if [ -z "$node_version" ] && command -v python3 >/dev/null 2>&1; then
    node_version="$(python3 - <<'PY'
import json, urllib.request
try:
    with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=20) as resp:
        versions = json.load(resp)
    print(next(item["version"] for item in versions if str(item.get("version", "")).startswith("v22.")))
except Exception:
    print("")
PY
)"
  fi
  node_version="${node_version:-v22.16.0}"
  node_dir="$HOME/.local/share/clawcross-node/$node_version"
  archive="node-${node_version}-linux-${node_arch}.tar.xz"
  url="https://nodejs.org/dist/${node_version}/${archive}"
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "$tmp_dir"' EXIT
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$tmp_dir/$archive"
  elif command -v wget >/dev/null 2>&1; then
    wget -q "$url" -O "$tmp_dir/$archive"
  else
    printf '{"ok":false,"error":"curl/wget missing; cannot install user-local Node.js"}\n'
    exit 22
  fi
  mkdir -p "$node_dir"
  tar -xJf "$tmp_dir/$archive" -C "$node_dir" --strip-components=1
  ln -sfn "$node_dir" "$HOME/.local/node"
  ln -sf "$HOME/.local/node/bin/node" "$HOME/.local/bin/node"
  ln -sf "$HOME/.local/node/bin/npm" "$HOME/.local/bin/npm"
  ln -sf "$HOME/.local/node/bin/npx" "$HOME/.local/bin/npx"
}
if ! command -v npm >/dev/null 2>&1; then
  install_node_user
else
  node_ok="no"
  if command -v node >/dev/null 2>&1; then
    if node -e 'const [maj,min]=process.versions.node.split(".").map(Number); process.exit((maj > 22 || (maj === 22 && min >= 13)) ? 0 : 1)' >/dev/null 2>&1; then
      node_ok="yes"
    fi
  fi
  if [ "$node_ok" != "yes" ]; then
    install_node_user
  fi
fi
npm config set prefix "$HOME/.local" >/dev/null 2>&1 || true
if ! command -v acpx >/dev/null 2>&1; then
  npm install -g acpx@latest
fi
codex_ok="no"
if command -v codex >/dev/null 2>&1 && codex --version >/dev/null 2>&1; then
  codex_ok="yes"
fi
if [ "$codex_ok" != "yes" ]; then
  npm install -g @openai/codex@latest >/dev/null 2>&1 || true
  hash -r 2>/dev/null || true
fi
acpx_path="$(command -v acpx || true)"
node_path="$(command -v node || true)"
npm_path="$(command -v npm || true)"
codex_path="$(command -v codex || true)"
printf '{"ok":true,"acpx":"%s","codex":"%s","node":"%s","npm":"%s"}\n' "$acpx_path" "$codex_path" "$node_path" "$npm_path"
"""


def _write_env_command() -> str:
    return (
        "mkdir -p \"$HOME/.clawcross\" && "
        "umask 077 && "
        "cat > \"$HOME/.clawcross/remote_acpx.env\" && "
        "chmod 600 \"$HOME/.clawcross/remote_acpx.env\""
    )


def _smoke_command(tool: str, session: str, *, prompt_smoke: bool) -> str:
    quoted_tool = shlex.quote(tool)
    quoted_session = shlex.quote(session)
    prompt = "Reply exactly: clawcross-acpx-ok"
    prompt_json = json.dumps([{"type": "text", "text": prompt}], ensure_ascii=False)
    base = f"""
set -eu
export PATH="$HOME/.local/bin:$PATH"
ENV_FILE="$HOME/.clawcross/remote_acpx.env"
ACPX_DIR="$HOME/.clawcross/acpx"
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi
if command -v codex >/dev/null 2>&1 && [ -n "${{OPENAI_API_KEY:-}}" ]; then
  printf '%s\n' "$OPENAI_API_KEY" | codex login --with-api-key >/dev/null
fi
mkdir -p "$ACPX_DIR"
acpx --version
acpx --cwd "$ACPX_DIR" --ttl 120 --format json --json-strict {quoted_tool} sessions ensure --name {quoted_session}
acpx --cwd "$ACPX_DIR" --ttl 120 --format json --json-strict {quoted_tool} sessions list
"""
    if not prompt_smoke:
        return base
    return base + f"""
tmp="$(mktemp)"
cat > "$tmp" <<'EOF'
{prompt_json}
EOF
acpx --cwd "$ACPX_DIR" --ttl 120 --approve-all --format json --json-strict {quoted_tool} prompt -s {quoted_session} --file "$tmp"
rm -f "$tmp"
"""


def _secret_env(openai_key: str) -> str:
    lines = [
        "# Written by ClawCross configure_remote_acpx.py.",
        "# Keep this file mode 600; do not commit it.",
        f"OPENAI_API_KEY={openai_key.strip()}",
        f"CODEX_API_KEY={openai_key.strip()}",
        "OPENAI_BASE_URL=https://api.openai.com/v1",
        "ACPX_APPROVE_ALL=1",
    ]
    return "\n".join(lines) + "\n"


def configure_target(args: argparse.Namespace, target: str, openai_key: str | None) -> list[StepResult]:
    results: list[StepResult] = []

    proc = _run_ssh(target, _setup_command(), timeout=args.timeout, connect_timeout=args.connect_timeout)
    results.append(StepResult(proc.returncode == 0, target, "install_acpx", proc.returncode, proc.stdout, proc.stderr))
    if proc.returncode != 0:
        return results

    if openai_key:
        proc = _run_ssh(
            target,
            _write_env_command(),
            input_text=_secret_env(openai_key),
            timeout=45,
            connect_timeout=args.connect_timeout,
        )
        results.append(StepResult(proc.returncode == 0, target, "write_env", proc.returncode, proc.stdout, proc.stderr))
        if proc.returncode != 0:
            return results

    proc = _run_ssh(
        target,
        _smoke_command(args.tool, args.session, prompt_smoke=args.prompt_smoke),
        timeout=args.timeout,
        connect_timeout=args.connect_timeout,
    )
    results.append(StepResult(proc.returncode == 0, target, "smoke", proc.returncode, proc.stdout, proc.stderr))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", nargs="+", help="SSH targets, for example boris@100.101.68.35")
    parser.add_argument("--openai-key-env", default="OPENAI_API_KEY", help="Local env var containing the OpenAI key")
    parser.add_argument("--skip-key", action="store_true", help="Do not write ~/.clawcross/remote_acpx.env")
    parser.add_argument("--tool", default="codex", help="ACPX tool to smoke-check")
    parser.add_argument("--session", default="clawcross-smoke", help="ACPX session name to create/use")
    parser.add_argument("--prompt-smoke", action="store_true", help="Run a real agent prompt after sessions list")
    parser.add_argument("--connect-timeout", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()

    openai_key = None if args.skip_key else os.environ.get(args.openai_key_env, "").strip()
    if not args.skip_key and not openai_key:
        print(f"Missing {args.openai_key_env}; pass --skip-key or export the key locally.", file=sys.stderr)
        return 2

    all_results: list[dict[str, object]] = []
    ok = True
    for target in args.targets:
        target_results = configure_target(args, target, openai_key)
        all_results.extend(item.as_dict() for item in target_results)
        ok = ok and all(item.ok for item in target_results)

    if args.json:
        print(json.dumps({"ok": ok, "results": all_results}, ensure_ascii=False, indent=2))
    else:
        for item in all_results:
            status = "OK" if item["ok"] else "FAIL"
            detail = (str(item["stdout"]) or str(item["stderr"])).strip().splitlines()
            preview = detail[-1] if detail else ""
            print(f"[{status}] {item['target']} {item['step']} rc={item['returncode']} {preview}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
