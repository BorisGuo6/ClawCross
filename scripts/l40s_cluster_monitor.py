#!/usr/bin/env python3
"""Publish L40S cluster health into the ClawCross harness state.

The monitor only connects to the configured jump host from this machine. All
per-L40S SSH probes run on that jump host, which keeps non-routable cluster
access behind the operator-approved hop.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from harness.store import apply_harness_event, get_harness_state  # noqa: E402


REMOTE_PROBE = r'''
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time


def run(cmd, timeout=12):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as exc:
        return 124, "", str(exc)


def disk_info(path):
    code, out, err = run(["df", "-Pk", path], timeout=8)
    if code != 0:
        return {"path": path, "ok": False, "error": err or out}
    lines = [line for line in out.splitlines() if line.strip()]
    if len(lines) < 2:
        return {"path": path, "ok": False, "error": out}
    parts = lines[-1].split()
    if len(parts) < 6:
        return {"path": path, "ok": False, "error": lines[-1]}
    total_kb = int(parts[1])
    used_kb = int(parts[2])
    avail_kb = int(parts[3])
    pct = parts[4].rstrip("%")
    return {
        "path": path,
        "ok": True,
        "filesystem": parts[0],
        "total_gib": round(total_kb / 1024 / 1024, 1),
        "used_gib": round(used_kb / 1024 / 1024, 1),
        "available_gib": round(avail_kb / 1024 / 1024, 1),
        "used_percent": int(pct) if pct.isdigit() else None,
        "mount": parts[5],
    }


def ps_user(pid):
    code, out, _ = run(["ps", "-o", "user=", "-p", str(pid)], timeout=4)
    if code == 0 and out.strip():
        return out.strip().splitlines()[0].strip()
    return ""


def gpu_info():
    code, out, err = run(
        [
            "nvidia-smi",
            "--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        timeout=15,
    )
    if code != 0:
        return {"ok": False, "error": err or out, "gpus": [], "processes": []}
    gpus = []
    uuid_to_index = {}
    for raw in out.splitlines():
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) < 6:
            continue
        index, uuid, name, total, used, util = parts[:6]
        item = {
            "index": int(index),
            "uuid": uuid,
            "name": name,
            "memory_total_mib": int(float(total)),
            "memory_used_mib": int(float(used)),
            "utilization_gpu_percent": int(float(util)),
        }
        item["free"] = item["memory_used_mib"] <= 512 and item["utilization_gpu_percent"] <= 10
        uuid_to_index[uuid] = item["index"]
        gpus.append(item)

    proc_code, proc_out, _ = run(
        [
            "nvidia-smi",
            "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
            "--format=csv,noheader,nounits",
        ],
        timeout=15,
    )
    processes = []
    if proc_code == 0:
        for raw in proc_out.splitlines():
            parts = [part.strip() for part in raw.split(",")]
            if len(parts) < 4:
                continue
            uuid, pid, name, memory = parts[:4]
            processes.append(
                {
                    "gpu_index": uuid_to_index.get(uuid),
                    "pid": int(pid) if pid.isdigit() else pid,
                    "user": ps_user(pid),
                    "process_name": name,
                    "used_memory_mib": int(float(memory)) if memory else None,
                }
            )
    return {
        "ok": True,
        "gpus": gpus,
        "processes": sorted(processes, key=lambda item: (item.get("gpu_index") is None, item.get("gpu_index") or 0, str(item.get("pid")))),
        "free_gpu_count": sum(1 for item in gpus if item.get("free")),
        "total_gpu_count": len(gpus),
    }


def tmux_sessions():
    code, out, err = run(["tmux", "list-sessions", "-F", "#{session_name}"], timeout=6)
    if code != 0:
        return {"ok": False, "sessions": [], "error": err or out}
    return {"ok": True, "sessions": [line.strip() for line in out.splitlines() if line.strip()]}


def count_files(root):
    path = Path(root)
    if not path.exists():
        return None
    count = 0
    for _, _, files in os.walk(path):
        count += len(files)
    return count


def metadata_rows(path):
    target = Path(path)
    if not target.exists():
        return None
    with target.open("r", encoding="utf-8", errors="replace") as fh:
        rows = sum(1 for _ in fh)
    return max(0, rows - 1)


def checkpoint_count(root):
    path = Path(root)
    if not path.exists():
        return None
    patterns = ("*step*", "*.safetensors", "*.pt", "*.pth", "*.bin")
    matches = set()
    for pattern in patterns:
        matches.update(str(item) for item in path.rglob(pattern))
    return len(matches)


def tail_file(path, max_bytes=5000, max_lines=18):
    target = Path(path)
    if not target.exists():
        return []
    try:
        size = target.stat().st_size
        with target.open("rb") as fh:
            if size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
            text = fh.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return [f"<tail failed: {exc}>"]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-max_lines:]


def progress_from_tail(lines):
    progress = []
    pattern = re.compile(r"(\d+)\s*/\s*(\d+)")
    for line in lines:
        match = pattern.search(line)
        if match:
            progress.append({"current": int(match.group(1)), "total": int(match.group(2)), "line": line[-240:]})
    return progress[-4:]


config = json.loads(os.environ.get("CLAWCROSS_L40S_PROBE_CONFIG", "{}") or "{}")
run_root = config.get("training_run_root", "")
cache_dir = config.get("training_cache_dir", "")
output_dir = config.get("training_output_dir", "")
log_path = config.get("training_log_path", "")
metadata_path = config.get("training_metadata_path", "")
session_name = config.get("training_session", "")

tail = tail_file(log_path) if log_path else []
tmux = tmux_sessions()
training_session_active = bool(session_name and session_name in tmux.get("sessions", []))
paths = {
    "venv": config.get("venv_path", ""),
    "code": config.get("code_path", ""),
    "weights": config.get("weights_path", ""),
    "data": config.get("data_path", ""),
    "run_root": run_root,
    "cache": cache_dir,
    "output": output_dir,
    "log": log_path,
    "metadata": metadata_path,
}

payload = {
    "ok": True,
    "hostname": socket.gethostname(),
    "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "disk": disk_info(config.get("disk_path", "/home")),
    "gpu": gpu_info(),
    "tmux": tmux,
    "paths": {key: {"path": value, "exists": bool(value and Path(value).exists())} for key, value in paths.items()},
    "training": {
        "session_name": session_name,
        "session_active": training_session_active,
        "cache_count": count_files(cache_dir) if cache_dir else None,
        "checkpoint_count": checkpoint_count(output_dir) if output_dir else None,
        "metadata_rows": metadata_rows(metadata_path) if metadata_path else None,
        "log_path": log_path,
        "log_tail": tail,
        "progress": progress_from_tail(tail),
    },
}
print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
'''


REMOTE_COLLECTOR = r'''
import base64
import json
import os
import shlex
import subprocess
import sys
import time


payload = json.loads(base64.b64decode(os.environ["CLAWCROSS_CLUSTER_MONITOR_PAYLOAD"]).decode("utf-8"))
probe_code = payload["probe_code"]
ssh_options = [
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", f"ConnectTimeout={int(payload.get('connect_timeout', 8))}",
]
remote_user = payload["remote_user"]
probe_config = json.dumps(payload.get("probe_config", {}), separators=(",", ":"))
results = []

for host in payload["hosts"]:
    target = f"{remote_user}@{host['address']}"
    started = time.time()
    env_prefix = f"CLAWCROSS_L40S_PROBE_CONFIG={shlex.quote(probe_config)}"
    cmd = ["ssh", *ssh_options, target, env_prefix + " python3 -"]
    try:
        proc = subprocess.run(
            cmd,
            input=probe_code,
            capture_output=True,
            text=True,
            timeout=float(payload.get("probe_timeout", 45)),
        )
    except Exception as exc:
        results.append({
            "host_id": host["id"],
            "address": host["address"],
            "ok": False,
            "error": str(exc),
            "elapsed_seconds": round(time.time() - started, 2),
        })
        continue
    item = {
        "host_id": host["id"],
        "address": host["address"],
        "ok": proc.returncode == 0,
        "elapsed_seconds": round(time.time() - started, 2),
    }
    if proc.returncode == 0:
        try:
            item.update(json.loads(proc.stdout.strip().splitlines()[-1]))
        except Exception as exc:
            item["ok"] = False
            item["error"] = f"invalid probe json: {exc}"
            item["stdout_tail"] = proc.stdout[-2000:]
    else:
        item["error"] = (proc.stderr or proc.stdout or f"ssh exited {proc.returncode}").strip()[-2000:]
    results.append(item)

print(json.dumps({"ok": True, "results": results}, ensure_ascii=False, separators=(",", ":")))
'''


def parse_host(value: str) -> dict[str, str]:
    if "=" in value:
        host_id, address = value.split("=", 1)
    else:
        address = value
        host_id = address.split(".", 1)[0]
    host_id = host_id.strip()
    address = address.strip()
    if not host_id or not address:
        raise argparse.ArgumentTypeError(f"invalid host mapping: {value!r}")
    return {"id": host_id, "address": address}


def format_gib(value: Any) -> str:
    try:
        return f"{float(value):.0f}GiB"
    except Exception:
        return "unknown"


def compact_users(processes: list[dict[str, Any]]) -> str:
    users = []
    seen = set()
    for proc in processes:
        user = str(proc.get("user") or "").strip()
        if not user or user in seen:
            continue
        seen.add(user)
        users.append(user)
    return ",".join(users[:4])


def classify(scan: dict[str, Any], training_session: str, required_free_gpus: int) -> str:
    if not scan.get("ok"):
        return "error"
    training = scan.get("training") if isinstance(scan.get("training"), dict) else {}
    if training_session and training.get("session_active"):
        return "running"
    disk = scan.get("disk") if isinstance(scan.get("disk"), dict) else {}
    if disk.get("ok") and float(disk.get("available_gib") or 0) < 50:
        return "blocked"
    gpu = scan.get("gpu") if isinstance(scan.get("gpu"), dict) else {}
    free = int(gpu.get("free_gpu_count") or 0)
    total = int(gpu.get("total_gpu_count") or 0)
    if total and required_free_gpus > 0 and free < required_free_gpus:
        return "blocked"
    if total and free <= 0:
        return "blocked"
    return "idle"


def build_message(scan: dict[str, Any], training_session: str) -> str:
    host_id = scan.get("host_id") or scan.get("address") or "host"
    if not scan.get("ok"):
        return f"{host_id}: probe failed: {str(scan.get('error') or 'unknown error')[:160]}"
    disk = scan.get("disk") if isinstance(scan.get("disk"), dict) else {}
    gpu = scan.get("gpu") if isinstance(scan.get("gpu"), dict) else {}
    training = scan.get("training") if isinstance(scan.get("training"), dict) else {}
    free = gpu.get("free_gpu_count", "?")
    total = gpu.get("total_gpu_count", "?")
    owners = compact_users(gpu.get("processes") or [])
    parts = [
        f"{host_id}: GPUs free {free}/{total}",
        f"/home free {format_gib(disk.get('available_gib'))}" if disk.get("ok") else "disk unknown",
    ]
    if owners:
        parts.append(f"GPU users {owners}")
    if training_session and training.get("session_active"):
        cache = training.get("cache_count")
        rows = training.get("metadata_rows")
        progress = training.get("progress") or []
        progress_text = ""
        if progress:
            latest = progress[-1]
            progress_text = f" progress {latest.get('current')}/{latest.get('total')}"
        cache_text = f" cache {cache}" if cache is not None else ""
        rows_text = f" rows {rows}" if rows is not None else ""
        parts.append(f"{training_session} active{cache_text}{rows_text}{progress_text}")
    elif training_session:
        tmux = scan.get("tmux") if isinstance(scan.get("tmux"), dict) else {}
        if training_session in (tmux.get("sessions") or []):
            parts.append(f"{training_session} listed")
    return "; ".join(parts)


def host_project_id(args: argparse.Namespace, host_id: str) -> str:
    if not args.per_host_projects:
        return args.project_id
    return f"{args.host_project_prefix}-{host_id}"


def host_task_id(args: argparse.Namespace, host_id: str) -> str:
    if not args.per_host_projects:
        return args.task_id
    return f"{args.task_id}-{host_id}"


def host_project_title(args: argparse.Namespace, scan: dict[str, Any], host_id: str) -> str:
    if not args.per_host_projects:
        return args.project_title
    gpu = scan.get("gpu") if isinstance(scan.get("gpu"), dict) else {}
    gpus = gpu.get("gpus") if isinstance(gpu.get("gpus"), list) else []
    total = int(gpu.get("total_gpu_count") or len(gpus) or 8)
    gpu_name = str((gpus[0] or {}).get("name") or "NVIDIA L40S") if gpus else "NVIDIA L40S"
    return f"L40S {host_id} ({total}x {gpu_name})"


def host_project_summary(scan: dict[str, Any], training_session: str) -> str:
    host_id = scan.get("host_id") or scan.get("address") or "host"
    if not scan.get("ok"):
        return f"{host_id} monitor: SSH/probe error."
    gpu = scan.get("gpu") if isinstance(scan.get("gpu"), dict) else {}
    disk = scan.get("disk") if isinstance(scan.get("disk"), dict) else {}
    training = scan.get("training") if isinstance(scan.get("training"), dict) else {}
    total = gpu.get("total_gpu_count", 8)
    free = gpu.get("free_gpu_count", "?")
    parts = [f"{host_id} is an {total}-GPU L40S computer; free GPUs {free}/{total}."]
    if disk.get("ok"):
        parts.append(f"/home free {format_gib(disk.get('available_gib'))}.")
    if training_session and training.get("session_active"):
        parts.append(f"{training_session} is active.")
    return " ".join(parts)


def post_harness_events(args: argparse.Namespace, scans: list[dict[str, Any]]) -> None:
    host_count = len(scans)
    active_training = [
        scan for scan in scans
        if isinstance(scan.get("training"), dict) and scan["training"].get("session_active")
    ]
    user_ids = args.user_id or [os.getenv("CLAWCROSS_HARNESS_USER") or os.getenv("CLAWCROSS_USER_ID") or os.getenv("USER") or "default"]
    for user_id in user_ids:
        for scan in scans:
            host_id = str(scan.get("host_id") or "unknown").replace("@", "-")
            project_id = host_project_id(args, host_id)
            task_id = host_task_id(args, host_id)
            gpu = scan.get("gpu") if isinstance(scan.get("gpu"), dict) else {}
            project_metadata = {
                "project": {
                    "bucket": "engineering",
                    "dashboard_bucket": "engineering",
                    "cluster": "l40s",
                    "host_id": host_id,
                    "host_count": host_count,
                    "gpu_count": gpu.get("total_gpu_count") or 8,
                    "active_training_hosts": [item.get("host_id") for item in active_training],
                    "updated_by": "l40s_cluster_monitor",
                }
            }
            apply_harness_event(
                user_id,
                {
                    "action": "project_upsert",
                    "project_id": project_id,
                    "project_title": host_project_title(args, scan, host_id),
                    "project_summary": host_project_summary(scan, args.training_session),
                    "status": "active",
                    "metadata": project_metadata,
                },
            )
            apply_harness_event(
                user_id,
                {
                    "action": "task_upsert",
                    "project_id": project_id,
                    "task_id": task_id,
                    "title": f"{host_id} realtime 8-GPU computer monitor",
                    "description": "Continuously monitor GPU occupancy, disk pressure, SSH reachability, and Wan2.2 fine-tune progress for this L40S computer.",
                    "status": "done",
                    "priority": "high",
                    "metadata": {
                        "source": "l40s_cluster_monitor",
                        "host_id": host_id,
                        "completion_semantics": "monitor-configured; live availability is reported by the machine-monitor agent heartbeat",
                    },
                },
            )
            status = classify(scan, args.training_session, args.required_free_gpus)
            apply_harness_event(
                user_id,
                {
                    "action": "heartbeat",
                    "agent_id": f"l40s-{host_id}.cluster",
                    "agent_type": "machine-monitor",
                    "project_id": project_id,
                    "current_task_id": task_id,
                    "status": status,
                    "message": build_message(scan, args.training_session),
                    "capabilities": ["l40s", "gpu-monitor", "ssh-via-jump", "wan22-training"],
                    "session_ref": f"monitor::l40s-{host_id}",
                    "remote_host": f"{args.remote_user}@{scan.get('address', '')} via {args.jump}",
                    "worktree": args.training_run_root,
                    "metadata": {
                        "monitor": "l40s_cluster_monitor",
                        "host_id": host_id,
                        "address": scan.get("address", ""),
                        "jump_host": args.jump,
                        "scan": scan,
                    },
                },
            )
        if args.print_state:
            state = get_harness_state(user_id)
            project_ids = {host_project_id(args, str(scan.get("host_id") or "unknown").replace("@", "-")) for scan in scans}
            agents = [agent for agent in state.get("agents", []) if agent.get("project_id") in project_ids]
            print(json.dumps({"user_id": user_id, "project_ids": sorted(project_ids), "agents": agents}, ensure_ascii=False, indent=2))


def collect_from_jump(args: argparse.Namespace) -> list[dict[str, Any]]:
    probe_config = {
        "disk_path": args.disk_path,
        "venv_path": args.venv_path,
        "code_path": args.code_path,
        "weights_path": args.weights_path,
        "data_path": args.data_path,
        "training_run_root": args.training_run_root,
        "training_cache_dir": args.training_cache_dir,
        "training_output_dir": args.training_output_dir,
        "training_log_path": args.training_log_path,
        "training_metadata_path": args.training_metadata_path,
        "training_session": args.training_session,
    }
    payload = {
        "hosts": args.host,
        "remote_user": args.remote_user,
        "connect_timeout": args.connect_timeout,
        "probe_timeout": args.probe_timeout,
        "probe_config": probe_config,
        "probe_code": REMOTE_PROBE,
    }
    encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    remote_cmd = f"CLAWCROSS_CLUSTER_MONITOR_PAYLOAD={shlex.quote(encoded)} python3 -"
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={args.connect_timeout}",
        args.jump,
        remote_cmd,
    ]
    proc = subprocess.run(
        cmd,
        input=REMOTE_COLLECTOR,
        capture_output=True,
        text=True,
        timeout=max(args.probe_timeout * max(1, len(args.host)) + 30, 60),
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"jump ssh exited {proc.returncode}").strip())
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:
        raise RuntimeError(f"invalid collector json: {exc}; tail={proc.stdout[-2000:]}") from exc
    return list(payload.get("results") or [])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish L40S cluster status into ClawCross Project Harness.")
    parser.add_argument("--jump", required=True, help="Jump host, for example user@tailscale-ip.")
    parser.add_argument("--remote-user", default="borisguo")
    parser.add_argument("--host", action="append", type=parse_host, required=True, help="Host mapping as id=dns-or-ip.")
    parser.add_argument("--user-id", action="append", default=[], help="ClawCross user id to update. Repeat to mirror into multiple logged-in users.")
    parser.add_argument("--project-id", default="l40s-gpu-cluster")
    parser.add_argument("--project-title", default="L40S GPU Cluster")
    parser.add_argument("--per-host-projects", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--host-project-prefix", default="l40s")
    parser.add_argument("--task-id", default="task_l40s_cluster_monitor")
    parser.add_argument("--task-title", default="Realtime L40S cluster and Wan2.2 training monitor")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--print-state", action="store_true")
    parser.add_argument("--connect-timeout", type=int, default=8)
    parser.add_argument("--probe-timeout", type=float, default=45.0)
    parser.add_argument("--required-free-gpus", type=int, default=8)
    parser.add_argument("--disk-path", default="/home")
    parser.add_argument("--venv-path", default="")
    parser.add_argument("--code-path", default="")
    parser.add_argument("--weights-path", default="")
    parser.add_argument("--data-path", default="")
    parser.add_argument("--training-run-root", default="")
    parser.add_argument("--training-cache-dir", default="")
    parser.add_argument("--training-output-dir", default="")
    parser.add_argument("--training-log-path", default="")
    parser.add_argument("--training-metadata-path", default="")
    parser.add_argument("--training-session", default="")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    while True:
        started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            scans = collect_from_jump(args)
            post_harness_events(args, scans)
            summary = ", ".join(f"{scan.get('host_id')}={classify(scan, args.training_session, args.required_free_gpus)}" for scan in scans)
            print(f"[{started_at}] updated {len(scans)} hosts: {summary}", flush=True)
        except Exception as exc:
            print(f"[{started_at}] monitor error: {exc}", file=sys.stderr, flush=True)
            if args.once:
                raise
        if args.once:
            break
        time.sleep(max(10.0, args.interval))


if __name__ == "__main__":
    main()
