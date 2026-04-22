#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from api.update_manager import collect_repo_state, read_update_status, write_update_status


def _run_shell(command: str, *, step: str) -> None:
    if not command.strip():
        return
    write_update_status(step, f"执行命令: {command}")
    proc = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        shell=True,
        text=True,
        capture_output=True,
    )
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    if proc.stderr:
        print(proc.stderr, end="" if proc.stderr.endswith("\n") else "\n", file=sys.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"命令执行失败 ({proc.returncode}): {command}")


def _default_restart_command() -> str:
    if os.name == "nt":
        return r"powershell -ExecutionPolicy Bypass -File .\selfskill\scripts\run.ps1 stop; powershell -ExecutionPolicy Bypass -File .\selfskill\scripts\run.ps1 start"
    return "bash selfskill/scripts/run.sh stop && bash selfskill/scripts/run.sh start"


def _git_pull_ff_only() -> None:
    proc = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "pull", "--ff-only"],
        capture_output=True,
        text=True,
    )
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    if proc.stderr:
        print(proc.stderr, end="" if proc.stderr.endswith("\n") else "\n", file=sys.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"git pull --ff-only 失败: {proc.stderr.strip() or proc.stdout.strip()}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    status = read_update_status()
    run_id = args.run_id
    if status.get("run_id") and status.get("run_id") != run_id:
        print(f"Skip run_id={run_id}; active run is {status.get('run_id')}")
        return 0

    try:
        write_update_status("checking", "检查 Git 更新状态。", run_id=run_id)
        before = collect_repo_state(fetch_remote=True)
        write_update_status(
            "checking",
            "已同步远端引用，检查可用更新。",
            run_id=run_id,
            branch=before.get("branch", ""),
            upstream=before.get("upstream", ""),
            current_commit=before.get("current_commit", ""),
            current_short_commit=before.get("current_short_commit", ""),
            latest_commit=before.get("latest_commit", ""),
            latest_short_commit=before.get("latest_short_commit", ""),
        )

        if before.get("dirty"):
            raise RuntimeError("仓库存在未提交改动，自动更新已停止。")

        if not before.get("has_update"):
            write_update_status(
                "done",
                "已经是最新版本，无需更新。",
                run_id=run_id,
                completed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                current_commit=before.get("current_commit", ""),
                current_short_commit=before.get("current_short_commit", ""),
                latest_commit=before.get("latest_commit", ""),
                latest_short_commit=before.get("latest_short_commit", ""),
            )
            return 0

        write_update_status("updating", "拉取最新代码。", run_id=run_id)
        _git_pull_ff_only()

        post_pull = (os.getenv("CLAWCROSS_UPDATE_POST_PULL_CMD", "") or "").strip()
        if post_pull:
            _run_shell(post_pull, step="updating")

        write_update_status("restarting", "代码更新完成，正在重启服务。", run_id=run_id)
        restart_cmd = (os.getenv("CLAWCROSS_UPDATE_RESTART_CMD", "") or "").strip() or _default_restart_command()
        _run_shell(restart_cmd, step="restarting")

        after = collect_repo_state(fetch_remote=False)
        write_update_status(
            "done",
            "更新完成，服务已重启。",
            run_id=run_id,
            completed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            branch=after.get("branch", ""),
            upstream=after.get("upstream", ""),
            current_commit=after.get("current_commit", ""),
            current_short_commit=after.get("current_short_commit", ""),
            latest_commit=after.get("latest_commit", ""),
            latest_short_commit=after.get("latest_short_commit", ""),
        )
        return 0
    except Exception as exc:
        write_update_status(
            "failed",
            f"更新失败: {exc}",
            run_id=run_id,
        )
        print(f"[self-update] failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
