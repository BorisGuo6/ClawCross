"""
Lightweight EvoSkill-style skill evolution for ClawCross.

This module upgrades the existing SKILL.md CRUD flow into a failure-driven loop:
- analyze recent trajectory failures and explicit execution errors
- synthesize a small candidate frontier of skill mutations
- persist feedback history and evolution reports
- apply the best mutation back into a skill's managed self-evolution block

It deliberately stays heuristic and benchmark-free for now. The goal is to
adapt EvoSkill's proposer/frontier/feedback loop to ClawCross's existing data
sources (skills, trajectories, insights, runtime failures) without requiring a
separate branch-based benchmark harness.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any

from webot.profiles import slugify
from webot.skills import edit_skill, get_skill, write_skill_file


PROJECT_ROOT = Path(__file__).resolve().parents[2]
USER_FILES_DIR = PROJECT_ROOT / "data" / "user_files"

EVOLUTION_BEGIN = "<!-- clawcross:self-evolution:begin -->"
EVOLUTION_END = "<!-- clawcross:self-evolution:end -->"
MAX_FAILURE_SAMPLES = 12
ERROR_TERMS = (
    "error",
    "failed",
    "failure",
    "exception",
    "traceback",
    "timeout",
    "timed out",
    "permission",
    "denied",
    "unauthorized",
    "forbidden",
    "429",
    "rate limit",
    "context",
    "token",
    "json",
    "schema",
    "parse",
    "missing",
    "not found",
)
_STOPWORDS = {
    "the", "and", "for", "that", "with", "this", "from", "into", "then", "when",
    "after", "before", "have", "has", "had", "will", "would", "should", "could",
    "your", "their", "there", "about", "into", "because", "while", "where",
    "which", "what", "just", "than", "them", "they", "were", "been", "being",
    "also", "only", "more", "less", "very", "much", "some", "many", "each",
    "over", "under", "again", "still", "need", "must", "make", "made", "using",
    "used", "user", "users", "session", "sessions", "skill", "skills", "agent",
    "agents", "clawcross", "trace", "failure", "failed", "error", "stderr",
    "stdout", "output", "input", "command", "commands", "default", "recent",
}

_SIGNAL_LIBRARY: list[dict[str, Any]] = [
    {
        "id": "verification-loop",
        "title": "Tighten verification loops",
        "patterns": [
            "assert", "regression", "broken", "incorrect", "expected", "test",
            "failed", "failure", "bug",
        ],
        "guardrails": [
            "State the exact failure mode before editing anything.",
            "Choose the narrowest reproducer first, then a broader regression command.",
            "Record pass/fail status immediately after each attempted fix.",
        ],
        "validation": [
            "Rerun the minimal reproducer before any broad test suite.",
            "When a fix passes, note the exact verifier command in the skill.",
        ],
    },
    {
        "id": "workspace-preflight",
        "title": "Add repo/workspace preflight checks",
        "patterns": [
            "no such file", "not found", "module not found", "importerror",
            "modulenotfounderror", "cwd", "path", "relative path", "missing file",
        ],
        "guardrails": [
            "Inspect the repo index and target paths before editing or running commands.",
            "Confirm cwd, interpreter, and entrypoints before assuming a runtime bug.",
            "Prefer absolute or repo-root-relative paths in instructions and validation.",
        ],
        "validation": [
            "List the files you rely on before the main command.",
            "Capture the final working path/entrypoint in the skill.",
        ],
    },
    {
        "id": "approval-auth",
        "title": "Preflight auth and approval constraints",
        "patterns": [
            "401", "403", "forbidden", "unauthorized", "permission", "approval",
            "token", "credential", "auth",
        ],
        "guardrails": [
            "Check authentication, required tokens, and approval mode before retrying.",
            "Distinguish auth failures from product logic failures.",
            "Document safe fallback behavior when credentials are missing.",
        ],
        "validation": [
            "Verify token presence or auth mode explicitly before the main action.",
            "Avoid repeated retries when the root cause is missing credentials.",
        ],
    },
    {
        "id": "bounded-execution",
        "title": "Bound long-running and flaky execution",
        "patterns": [
            "timeout", "timed out", "hang", "hung", "stuck", "deadline",
            "overloaded", "429", "rate limit",
        ],
        "guardrails": [
            "Use smaller repro steps before rerunning expensive workflows.",
            "Call out retry/backoff conditions explicitly instead of looping blindly.",
            "Capture the last known good checkpoint before restarting long flows.",
        ],
        "validation": [
            "Prefer bounded smoke tests and explicit timeouts in verifier commands.",
            "Note rate-limit or overload conditions as operational, not code, failures.",
        ],
    },
    {
        "id": "structured-output",
        "title": "Harden structured-output handling",
        "patterns": [
            "json", "yaml", "frontmatter", "parse", "schema", "typeerror",
            "valueerror", "keyerror", "syntaxerror", "decode",
        ],
        "guardrails": [
            "Validate expected structured output shape before downstream use.",
            "Call out schema constraints and fallback parsing rules explicitly.",
            "When patching text templates, preserve frontmatter and managed markers.",
        ],
        "validation": [
            "Re-run the parser or schema validator after any template change.",
            "Capture representative failing payloads in the evolution report.",
        ],
    },
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _truncate(text: str, limit: int = 240) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(40, limit - 18)].rstrip() + " ...[truncated]"


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _safe_json_load(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return data if isinstance(data, type(default)) else default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _user_root(user_id: str) -> Path:
    root = USER_FILES_DIR / (user_id or "anonymous")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _evolution_root(user_id: str) -> Path:
    root = _user_root(user_id) / "skill_evolution"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _skill_evolution_dir(user_id: str, skill_name: str) -> Path:
    root = _evolution_root(user_id) / "skills" / slugify(skill_name, "skill")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _tokenize(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9_/-]{3,}", (text or "").lower())
        if token not in _STOPWORDS
    ]


def _detect_signals(text: str) -> Counter[str]:
    normalized = (text or "").lower()
    counter: Counter[str] = Counter()
    for item in _SIGNAL_LIBRARY:
        hits = sum(1 for pattern in item["patterns"] if pattern in normalized)
        if hits:
            counter[item["id"]] += hits
    return counter


def _find_signal(signal_id: str) -> dict[str, Any]:
    for item in _SIGNAL_LIBRARY:
        if item["id"] == signal_id:
            return item
    return {
        "id": signal_id,
        "title": signal_id,
        "guardrails": ["Capture the failure precisely before changing the skill."],
        "validation": ["Re-run the narrowest reproducer after updating the skill."],
    }


def _build_frontier(
    *,
    signal_counts: Counter[str],
    failures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not signal_counts:
        signal_counts = Counter({"verification-loop": max(1, len(failures))})

    total_hits = sum(signal_counts.values()) or 1
    frontier: list[dict[str, Any]] = []
    top_items = signal_counts.most_common(3)
    for signal_id, count in top_items:
        signal = _find_signal(signal_id)
        heuristic_score = round(min(0.97, 0.35 + (count / total_hits) * 0.4 + min(len(failures), 5) * 0.04), 3)
        frontier.append(
            {
                "candidate_id": slugify(f"{signal_id}-{count}", signal_id),
                "signal_id": signal_id,
                "title": signal["title"],
                "heuristic_score": heuristic_score,
                "rationale": f"{count} matched failure signals in the recent evidence window.",
                "guardrails": list(signal["guardrails"]),
                "validation": list(signal["validation"]),
            }
        )

    if len(top_items) > 1:
        blended = [signal_id for signal_id, _ in top_items]
        heuristic_score = round(min(0.99, max(item["heuristic_score"] for item in frontier) + 0.05), 3)
        frontier.insert(
            0,
            {
                "candidate_id": slugify("blended-" + "-".join(blended), "blended"),
                "signal_id": "blended",
                "title": "Blend the strongest recent failure patterns",
                "heuristic_score": heuristic_score,
                "rationale": "Multiple signal clusters repeated recently, so the skill should capture the shared guardrails.",
                "guardrails": [
                    "Start from the narrowest reproducible failure before broad retries.",
                    "Record repo/cwd/entrypoint assumptions explicitly when failures mention paths or imports.",
                    "End every fix attempt with an explicit verifier command and observed result.",
                ],
                "validation": [
                    "Run the minimal reproducer first, then the broader regression command.",
                    "Persist the command and result summary in the evolution report.",
                ],
            },
        )

    return frontier


def _extract_failure_samples(
    *,
    user_id: str,
    session_id: str = "",
    days: int = 30,
    limit: int = MAX_FAILURE_SAMPLES,
    extra_error_text: str = "",
    extra_command: str = "",
) -> list[dict[str, Any]]:
    from webot.trajectory import list_trajectories

    cutoff = _utc_now() - timedelta(days=max(1, days))
    entries = list_trajectories(completed=None, limit=10000, user_id=user_id)
    failures: list[dict[str, Any]] = []

    for entry in entries:
        timestamp = _parse_iso(str(entry.get("timestamp") or ""))
        if timestamp is not None and timestamp < cutoff:
            continue
        if session_id and entry.get("session_id") != session_id:
            continue

        metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
        conversations = entry.get("conversations") if isinstance(entry.get("conversations"), list) else []
        convo_text = "\n".join(_normalize_text(msg.get("value")) for msg in conversations if isinstance(msg, dict))
        searchable = "\n".join(
            part for part in [
                convo_text,
                _normalize_text(metadata.get("error")),
                _normalize_text(metadata.get("stderr")),
                _normalize_text(metadata.get("stdout")),
                _normalize_text(metadata.get("command")),
            ] if part
        ).strip()
        if not searchable:
            continue

        completed = bool(entry.get("completed"))
        looks_like_failure = (not completed) or any(term in searchable.lower() for term in ERROR_TERMS)
        if not looks_like_failure:
            continue

        failures.append(
            {
                "timestamp": str(entry.get("timestamp") or ""),
                "session_id": str(entry.get("session_id") or ""),
                "completed": completed,
                "command": _truncate(_normalize_text(metadata.get("command")), 180),
                "error_excerpt": _truncate(
                    _normalize_text(metadata.get("error") or metadata.get("stderr") or searchable),
                    280,
                ),
                "searchable_text": searchable,
            }
        )

    if extra_error_text.strip():
        failures.insert(
            0,
            {
                "timestamp": _utc_now_iso(),
                "session_id": session_id or "",
                "completed": False,
                "command": _truncate(extra_command, 180),
                "error_excerpt": _truncate(extra_error_text, 280),
                "searchable_text": f"{extra_command}\n{extra_error_text}".strip(),
            },
        )

    return failures[: max(1, min(limit, MAX_FAILURE_SAMPLES))]


def analyze_skill_evolution(
    user_id: str,
    *,
    name: str,
    session_id: str = "",
    days: int = 30,
    limit: int = 8,
    error_text: str = "",
    command: str = "",
) -> dict[str, Any]:
    skill = get_skill(user_id, name=name)
    if not skill:
        return {"success": False, "error": f"Skill '{name}' not found"}

    failures = _extract_failure_samples(
        user_id=user_id,
        session_id=session_id,
        days=days,
        limit=limit,
        extra_error_text=error_text,
        extra_command=command,
    )
    skill_body = str(skill.get("body") or "")
    signal_counts: Counter[str] = Counter()
    vocabulary: Counter[str] = Counter()

    for failure in failures:
        signal_counts.update(_detect_signals(failure["searchable_text"]))
        vocabulary.update(_tokenize(failure["searchable_text"]))

    frontier = _build_frontier(signal_counts=signal_counts, failures=failures)
    top_terms = [term for term, _ in vocabulary.most_common(10)]
    top_signals = [
        {
            "signal_id": signal_id,
            "count": count,
            "title": _find_signal(signal_id)["title"],
        }
        for signal_id, count in signal_counts.most_common(5)
    ]

    summary_parts = []
    if failures:
        summary_parts.append(f"Observed {len(failures)} recent failure-like traces.")
    else:
        summary_parts.append("No recent failed trajectories were found, so the report relies on explicit execution errors and heuristic defaults.")
    if top_signals:
        summary_parts.append("Dominant signals: " + ", ".join(item["signal_id"] for item in top_signals[:3]) + ".")
    if top_terms:
        summary_parts.append("Recurring terms: " + ", ".join(top_terms[:6]) + ".")

    report = {
        "success": True,
        "generated_at": _utc_now_iso(),
        "skill_name": name,
        "session_id": session_id,
        "window_days": days,
        "failure_count": len(failures),
        "summary": " ".join(summary_parts),
        "top_terms": top_terms,
        "signals": top_signals,
        "failures": [
            {
                "timestamp": item["timestamp"],
                "session_id": item["session_id"],
                "command": item["command"],
                "error_excerpt": item["error_excerpt"],
            }
            for item in failures
        ],
        "frontier": frontier,
        "skill_has_managed_section": EVOLUTION_BEGIN in skill_body or EVOLUTION_BEGIN in str(skill.get("content") or ""),
    }
    return report


def render_evolution_report(report: dict[str, Any]) -> str:
    if not report.get("success"):
        return f"# Skill Evolution Report\n\nError: {report.get('error', 'unknown error')}\n"

    lines = [
        "# Skill Evolution Report",
        "",
        f"- Skill: `{report.get('skill_name', '')}`",
        f"- Generated at: `{report.get('generated_at', '')}`",
        f"- Session: `{report.get('session_id', '') or 'all recent sessions'}`",
        f"- Window: last `{report.get('window_days', 0)}` days",
        f"- Failure-like traces: `{report.get('failure_count', 0)}`",
        "",
        "## Summary",
        "",
        report.get("summary", "No summary available."),
        "",
    ]

    signals = report.get("signals") or []
    if signals:
        lines.extend(["## Dominant Signals", ""])
        for signal in signals:
            lines.append(
                f"- `{signal['signal_id']}` ({signal['count']} hits): {signal['title']}"
            )
        lines.append("")

    frontier = report.get("frontier") or []
    if frontier:
        lines.extend(["## Candidate Frontier", ""])
        for index, item in enumerate(frontier, start=1):
            lines.append(
                f"{index}. `{item['candidate_id']}` — score `{item['heuristic_score']}` — {item['title']}"
            )
            lines.append(f"   rationale: {item['rationale']}")
        lines.append("")

    failures = report.get("failures") or []
    if failures:
        lines.extend(["## Recent Evidence", ""])
        for item in failures[:5]:
            stamp = item.get("timestamp") or "unknown-time"
            session_id = item.get("session_id") or "unknown-session"
            command = item.get("command") or "(no command captured)"
            excerpt = item.get("error_excerpt") or "(no excerpt)"
            lines.append(f"- `{stamp}` session=`{session_id}` command=`{command}`")
            lines.append(f"  - {excerpt}")
        lines.append("")

    top_terms = report.get("top_terms") or []
    if top_terms:
        lines.extend(["## Recurring Terms", "", "- " + ", ".join(top_terms[:10]), ""])

    return "\n".join(lines).rstrip() + "\n"


def _build_managed_section(
    *,
    report: dict[str, Any],
    candidate: dict[str, Any],
    command: str = "",
    error_text: str = "",
) -> str:
    lines = [
        "## Self-Evolution Loop",
        "",
        "This block is auto-maintained by ClawCross's lightweight EvoSkill adapter.",
        "Prefer `skill_evolution_report` / `skill_evolution_apply` or `selfskill/scripts/evolve_skill.py` over manual edits here.",
        "",
        f"- Updated at: `{report.get('generated_at', _utc_now_iso())}`",
        f"- Heuristic candidate: `{candidate.get('candidate_id', 'unknown')}`",
        f"- Heuristic score: `{candidate.get('heuristic_score', 0)}`",
        "",
        "### Trigger Summary",
        "",
        report.get("summary", "No recent failure summary available."),
        "",
    ]

    if command.strip():
        lines.extend(["### Latest Trigger Command", "", f"`{command.strip()}`", ""])
    if error_text.strip():
        lines.extend(["### Latest Error Excerpt", "", "```text", _truncate(error_text, 2000), "```", ""])

    lines.extend(["### Operating Adjustments", ""])
    for index, item in enumerate(candidate.get("guardrails") or [], start=1):
        lines.append(f"{index}. {item}")
    lines.append("")

    lines.extend(["### Validation Loop", ""])
    for index, item in enumerate(candidate.get("validation") or [], start=1):
        lines.append(f"{index}. {item}")
    lines.append("")

    failures = report.get("failures") or []
    if failures:
        lines.extend(["### Recent Evidence", ""])
        for item in failures[:4]:
            lines.append(
                f"- `{item.get('timestamp') or 'unknown-time'}` `{item.get('session_id') or 'unknown-session'}` — {_truncate(item.get('error_excerpt') or '', 160)}"
            )
        lines.append("")

    lines.extend(["### Candidate Frontier Snapshot", ""])
    for item in (report.get("frontier") or [])[:4]:
        lines.append(
            f"- `{item['candidate_id']}` score `{item['heuristic_score']}` — {item['title']}"
        )

    return "\n".join(lines).strip()


def _upsert_managed_block(content: str, block: str) -> str:
    text = content.rstrip()
    replacement = f"{EVOLUTION_BEGIN}\n{block}\n{EVOLUTION_END}"
    pattern = re.compile(
        rf"{re.escape(EVOLUTION_BEGIN)}.*?{re.escape(EVOLUTION_END)}",
        re.DOTALL,
    )
    if pattern.search(text):
        return pattern.sub(replacement, text)
    return text + "\n\n" + replacement + "\n"


def _persist_skill_evolution_state(
    *,
    user_id: str,
    skill_name: str,
    report: dict[str, Any],
    applied_candidate: dict[str, Any],
    report_path: str,
    source: str,
    command: str,
    error_text: str,
) -> tuple[Path, Path]:
    state_dir = _skill_evolution_dir(user_id, skill_name)
    feedback_path = state_dir / "feedback_history.jsonl"
    frontier_path = state_dir / "frontier.json"

    entry = {
        "timestamp": _utc_now_iso(),
        "skill_name": skill_name,
        "source": source,
        "command": command,
        "error_excerpt": _truncate(error_text, 280),
        "candidate_id": applied_candidate.get("candidate_id"),
        "heuristic_score": applied_candidate.get("heuristic_score"),
        "report_path": report_path,
        "failure_count": report.get("failure_count", 0),
    }
    _append_jsonl(feedback_path, entry)

    frontier_state = _safe_json_load(frontier_path, {"history": [], "frontier": []})
    frontier_state["updated_at"] = _utc_now_iso()
    frontier_state["latest_report"] = report_path
    frontier_state["frontier"] = report.get("frontier") or []
    frontier_state.setdefault("history", []).append(entry)
    frontier_state["history"] = frontier_state["history"][-25:]
    _write_json(frontier_path, frontier_state)

    return feedback_path, frontier_path


def apply_skill_evolution(
    user_id: str,
    *,
    name: str,
    session_id: str = "",
    days: int = 30,
    limit: int = 8,
    error_text: str = "",
    command: str = "",
    source: str = "manual",
) -> dict[str, Any]:
    skill = get_skill(user_id, name=name)
    if not skill:
        return {"success": False, "error": f"Skill '{name}' not found"}

    report = analyze_skill_evolution(
        user_id,
        name=name,
        session_id=session_id,
        days=days,
        limit=limit,
        error_text=error_text,
        command=command,
    )
    if not report.get("success"):
        return report

    frontier = report.get("frontier") or []
    candidate = frontier[0] if frontier else {
        "candidate_id": "fallback",
        "heuristic_score": 0.4,
        "guardrails": [
            "Capture the exact failing command and stderr before changing the skill.",
            "State a deterministic verifier command after every change.",
        ],
        "validation": [
            "Rerun the smallest reproducer after the skill update.",
        ],
    }

    managed_block = _build_managed_section(
        report=report,
        candidate=candidate,
        command=command,
        error_text=error_text,
    )
    updated_content = _upsert_managed_block(str(skill.get("content") or ""), managed_block)
    edit_result = edit_skill(user_id, name=name, content=updated_content)
    if not edit_result.get("success"):
        return edit_result

    report_md = render_evolution_report(report)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    latest_file = f"references/evolution/latest-report.md"
    stamped_file = f"references/evolution/report-{stamp}.md"
    latest_result = write_skill_file(user_id, name=name, file_path=latest_file, file_content=report_md)
    stamped_result = write_skill_file(user_id, name=name, file_path=stamped_file, file_content=report_md)

    report_path = (
        stamped_result.get("path")
        or latest_result.get("path")
        or edit_result.get("path")
        or ""
    )
    feedback_path, frontier_path = _persist_skill_evolution_state(
        user_id=user_id,
        skill_name=name,
        report=report,
        applied_candidate=candidate,
        report_path=str(report_path),
        source=source,
        command=command,
        error_text=error_text,
    )

    return {
        "success": True,
        "message": f"Applied self-evolution block to skill '{name}'",
        "path": edit_result.get("path", ""),
        "report_path": report_path,
        "latest_report_path": latest_result.get("path", ""),
        "feedback_history_path": str(feedback_path),
        "frontier_path": str(frontier_path),
        "candidate_id": candidate.get("candidate_id"),
        "heuristic_score": candidate.get("heuristic_score"),
        "failure_count": report.get("failure_count", 0),
        "summary": report.get("summary", ""),
    }


def record_failure_feedback(
    *,
    user_id: str,
    session_id: str,
    entry: dict[str, Any],
) -> dict[str, Any]:
    """Persist a lightweight failure digest whenever a failed trajectory is saved."""
    evolution_root = _evolution_root(user_id)
    history_path = evolution_root / "runtime_failures.jsonl"
    digest_path = evolution_root / "latest_failure_digest.md"

    metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
    searchable = "\n".join(
        part for part in [
            "\n".join(_normalize_text(msg.get("value")) for msg in entry.get("conversations", []) if isinstance(msg, dict)),
            _normalize_text(metadata.get("error")),
            _normalize_text(metadata.get("stderr")),
            _normalize_text(metadata.get("command")),
        ] if part
    ).strip()
    signals = _detect_signals(searchable)
    record = {
        "timestamp": entry.get("timestamp") or _utc_now_iso(),
        "session_id": session_id,
        "command": _truncate(_normalize_text(metadata.get("command")), 180),
        "error_excerpt": _truncate(_normalize_text(metadata.get("error") or metadata.get("stderr") or searchable), 280),
        "signals": dict(signals),
    }
    _append_jsonl(history_path, record)

    recent_lines = [
        "# Latest Failure Digest",
        "",
        f"- Updated at: `{_utc_now_iso()}`",
        f"- Session: `{session_id or 'unknown'}`",
        f"- Command: `{record['command'] or '(not captured)'}`",
        "",
        "## Error Excerpt",
        "",
        record["error_excerpt"] or "(empty)",
        "",
    ]
    if signals:
        recent_lines.extend(["## Signals", ""])
        for signal_id, count in signals.most_common():
            recent_lines.append(f"- `{signal_id}`: {count} hits")
    digest_path.write_text("\n".join(recent_lines).rstrip() + "\n", encoding="utf-8")

    return {
        "success": True,
        "history_path": str(history_path),
        "digest_path": str(digest_path),
        "signals": dict(signals),
    }


def summarize_execution_failure(
    *,
    command: str,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 1,
) -> dict[str, Any]:
    text = "\n".join(part for part in [command, stderr, stdout] if part).strip()
    signal_counts = _detect_signals(text)
    frontier = _build_frontier(
        signal_counts=signal_counts,
        failures=[{"searchable_text": text, "error_excerpt": _truncate(stderr or stdout, 280)}],
    )
    summary = [
        f"Command exited with code {exit_code}.",
    ]
    if command.strip():
        summary.append(f"Command: {command.strip()}.")
    if signal_counts:
        summary.append("Signals: " + ", ".join(signal_id for signal_id, _ in signal_counts.most_common(3)) + ".")
    if stderr.strip():
        summary.append("stderr carried the strongest failure evidence.")
    elif stdout.strip():
        summary.append("stdout was used as fallback failure evidence.")

    return {
        "generated_at": _utc_now_iso(),
        "exit_code": exit_code,
        "command": command,
        "summary": " ".join(summary),
        "stderr_excerpt": _truncate(stderr, 1200),
        "stdout_excerpt": _truncate(stdout, 1200),
        "frontier": frontier,
    }


def _guess_repo_root(skill_path: Path) -> Path:
    for candidate in [skill_path.parent, *skill_path.parents]:
        if (candidate / ".git").exists():
            return candidate
    return skill_path.parent


def update_markdown_skill_document(
    *,
    skill_path: str | Path,
    command: str,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 1,
    force: bool = False,
) -> dict[str, Any]:
    """Update a repository Markdown skill file with the latest execution learnings."""
    target = Path(skill_path).expanduser().resolve()
    if not target.is_file():
        return {"success": False, "error": f"Skill document not found: {target}"}

    if exit_code == 0 and not force:
        return {
            "success": True,
            "updated": False,
            "message": "Command succeeded; no self-evolution update written.",
        }

    report = summarize_execution_failure(command=command, stdout=stdout, stderr=stderr, exit_code=exit_code)
    best_candidate = (report.get("frontier") or [{}])[0]
    managed_block = _build_managed_section(
        report={
            "generated_at": report.get("generated_at"),
            "summary": report.get("summary"),
            "frontier": report.get("frontier") or [],
            "failures": [
                {
                    "timestamp": report.get("generated_at"),
                    "session_id": "repo-skill",
                    "error_excerpt": report.get("stderr_excerpt") or report.get("stdout_excerpt") or "",
                }
            ],
        },
        candidate=best_candidate,
        command=command,
        error_text=stderr or stdout,
    )

    original = target.read_text(encoding="utf-8", errors="replace")
    updated = _upsert_managed_block(original, managed_block)
    target.write_text(updated, encoding="utf-8")

    repo_root = _guess_repo_root(target)
    reports_dir = repo_root / "docs" / "self-evolution"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report_path = reports_dir / f"{slugify(target.stem, 'skill')}-latest.md"
    report_path.write_text(
        "\n".join(
            [
                "# Repository Skill Self-Evolution Report",
                "",
                f"- Skill file: `{target}`",
                f"- Generated at: `{report['generated_at']}`",
                f"- Exit code: `{exit_code}`",
                "",
                "## Summary",
                "",
                report["summary"],
                "",
                "## stderr",
                "",
                "```text",
                report.get("stderr_excerpt") or "(empty)",
                "```",
                "",
                "## stdout",
                "",
                "```text",
                report.get("stdout_excerpt") or "(empty)",
                "```",
                "",
            ]
        ).rstrip()
        + "\n",
        encoding="utf-8",
    )
    stamped_path = reports_dir / f"{slugify(target.stem, 'skill')}-{stamp}.md"
    stamped_path.write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8")

    return {
        "success": True,
        "updated": True,
        "path": str(target),
        "report_path": str(report_path),
        "archive_report_path": str(stamped_path),
        "candidate_id": best_candidate.get("candidate_id"),
        "heuristic_score": best_candidate.get("heuristic_score"),
        "summary": report.get("summary"),
    }
