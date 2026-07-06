# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""Materialize declarative harness tools into runtime tool manifests."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from integrations.acpx_harness.schema import HarnessAgentSpec, HarnessToolSpec
from integrations.acpx_harness.tool_inheritance import resolve_declared_subagent_tools


_SECRET_KEY_RE = re.compile(r"(authorization|api[_-]?key|password|secret|token)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class MaterializedToolManifest:
    owner: str
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def _transport_for_config(config: dict[str, Any]) -> str:
    url = str(config.get("url") or config.get("server_url") or config.get("endpoint") or "").strip()
    if url.startswith(("http://", "https://")):
        return "http"
    if config.get("command") or config.get("args"):
        return "stdio"
    return str(config.get("transport") or "declared").strip() or "declared"


def _redact_config(value: Any, *, path: tuple[str, ...], warnings: list[str]) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            item_path = (*path, key_text)
            if _SECRET_KEY_RE.search(key_text):
                redacted[key_text] = "<redacted>"
                warnings.append(f"redacted secret-like config at {'.'.join(item_path)}")
                continue
            redacted[key_text] = _redact_config(item, path=item_path, warnings=warnings)
        return redacted
    if isinstance(value, list):
        return [
            _redact_config(item, path=(*path, str(index)), warnings=warnings)
            for index, item in enumerate(value)
        ]
    return value


def _declared_tool_names(name: str, config: dict[str, Any]) -> list[str]:
    raw = config.get("tools") or config.get("tool_names") or config.get("functions")
    if not isinstance(raw, list):
        return [name]
    names: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, dict):
            value = item.get("name") or item.get("tool") or item.get("function")
            if isinstance(value, str) and value.strip():
                names.append(value.strip())
    return names or [name]


def _manifest_name(server_id: str, tool_name: str) -> str:
    clean_server = server_id.strip()
    clean_tool = tool_name.strip()
    if clean_tool == clean_server:
        return clean_server
    return f"{clean_server}.{clean_tool}"


def materialize_tool_scope(
    tools: dict[str, HarnessToolSpec],
    *,
    owner: str,
) -> MaterializedToolManifest:
    warnings: list[str] = []
    manifest: dict[str, dict[str, Any]] = {}
    for name, tool in sorted(tools.items()):
        if tool.kind == "inherit":
            warnings.append(f"{owner}.{name} remained unresolved inherit")
            continue
        clean_config = _redact_config(tool.config, path=(owner, name), warnings=warnings)
        if tool.kind == "mcp":
            transport = _transport_for_config(tool.config)
            for declared_tool in _declared_tool_names(name, tool.config):
                manifest_name = _manifest_name(name, declared_tool)
                manifest[manifest_name] = {
                    "name": manifest_name,
                    "server_id": name,
                    "source_tool": declared_tool,
                    "kind": "mcp",
                    "transport": transport,
                    "inherited": tool.inherited,
                    "config": clean_config,
                }
            continue
        manifest[name] = {
            "name": name,
            "kind": tool.kind,
            "transport": "local" if tool.kind == "function" else "agent" if tool.kind == "agent" else "declared",
            "inherited": tool.inherited,
            "config": clean_config,
        }
    return MaterializedToolManifest(owner=owner, tools=manifest, warnings=tuple(dict.fromkeys(warnings)))


def materialized_tool_manifest_to_dict(manifest: MaterializedToolManifest) -> dict[str, Any]:
    return {
        "owner": manifest.owner,
        "tools": manifest.tools,
        "warnings": list(manifest.warnings),
        "counts": {"tools": len(manifest.tools), "warnings": len(manifest.warnings)},
    }


def subagent_lifecycle_tool_manifest() -> dict[str, dict[str, Any]]:
    return {
        "sys_session_send": {
            "name": "sys_session_send",
            "server_id": "sys",
            "source_tool": "session_send",
            "kind": "function",
            "transport": "local",
            "inherited": False,
            "config": {
                "description": "Send a task to a materialized subagent or reviewer session.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "agent_name": {"type": "string"},
                        "role": {"type": "string", "enum": ["", "subagent", "reviewer"]},
                        "session_id": {"type": "string"},
                        "purpose": {"type": "string"},
                        "title": {"type": "string"},
                        "prompt": {"type": "string"},
                        "model": {"type": "string"},
                        "dry_run": {"type": "boolean"},
                    },
                    "required": ["agent_name", "prompt"],
                    "additionalProperties": True,
                },
            },
        },
        "sys_read_inbox": {
            "name": "sys_read_inbox",
            "server_id": "sys",
            "source_tool": "read_inbox",
            "kind": "function",
            "transport": "local",
            "inherited": False,
            "config": {
                "description": "Drain async tool completions, or read materialized child-session tasks when child filters are provided.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "agent_name": {"type": "string"},
                        "role": {"type": "string", "enum": ["", "subagent", "reviewer"]},
                        "session_id": {"type": "string"},
                        "title": {"type": "string"},
                        "status": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "additionalProperties": False,
                },
            },
        },
        "sys_call_async": {
            "name": "sys_call_async",
            "server_id": "sys",
            "source_tool": "call_async",
            "kind": "function",
            "transport": "local",
            "inherited": False,
            "config": {
                "description": "Dispatch a local system tool call and deliver its completion through sys_read_inbox.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string"},
                        "name": {"type": "string"},
                        "arguments": {"type": "object"},
                        "args": {"type": "object"},
                        "handle_id": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
        },
        "sys_cancel_async": {
            "name": "sys_cancel_async",
            "server_id": "sys",
            "source_tool": "cancel_async",
            "kind": "function",
            "transport": "local",
            "inherited": False,
            "config": {
                "description": "Record cancellation for an async handle and deliver the result through sys_read_inbox.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "handle_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["handle_id"],
                    "additionalProperties": False,
                },
            },
        },
        "sys_session_list": {
            "name": "sys_session_list",
            "server_id": "sys",
            "source_tool": "session_list",
            "kind": "function",
            "transport": "local",
            "inherited": False,
            "config": {
                "description": "List direct child/reviewer sessions and named task instances.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "agent_name": {"type": "string"},
                        "role": {"type": "string", "enum": ["", "subagent", "reviewer"]},
                        "session_id": {"type": "string"},
                        "title": {"type": "string"},
                        "status": {"type": "string"},
                        "include_events": {"type": "boolean"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "additionalProperties": False,
                },
            },
        },
        "sys_list_models": {
            "name": "sys_list_models",
            "server_id": "sys",
            "source_tool": "list_models",
            "kind": "function",
            "transport": "local",
            "inherited": False,
            "config": {
                "description": (
                    "List explicit model bindings for each materialized child worker and this root session."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
        },
        "sys_advise_models": {
            "name": "sys_advise_models",
            "server_id": "sys",
            "source_tool": "advise_models",
            "kind": "function",
            "transport": "local",
            "inherited": False,
            "config": {
                "description": (
                    "Advise model overrides for planned subagent fan-out tasks. "
                    "Currently reports router_on=false until a ClawCross routing advisor is configured."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "tasks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "agents": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "agent": {"type": "string"},
                                                "models": {
                                                    "oneOf": [
                                                        {"type": "array", "items": {"type": "string"}},
                                                        {"type": "null"},
                                                    ]
                                                },
                                            },
                                            "required": ["agent"],
                                            "additionalProperties": False,
                                        },
                                    },
                                    "task": {"type": "string"},
                                },
                                "required": ["title", "agents", "task"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["tasks"],
                    "additionalProperties": False,
                },
            },
        },
        "sys_session_get_history": {
            "name": "sys_session_get_history",
            "server_id": "sys",
            "source_tool": "session_get_history",
            "kind": "function",
            "transport": "local",
            "inherited": False,
            "config": {
                "description": "Read a bounded compact history from the parent session or one direct child session.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "conversation_id": {"type": "string"},
                        "session_id": {"type": "string"},
                        "tail_items": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "additionalProperties": False,
                },
            },
        },
        "sys_session_get_info": {
            "name": "sys_session_get_info",
            "server_id": "sys",
            "source_tool": "session_get_info",
            "kind": "function",
            "transport": "local",
            "inherited": False,
            "config": {
                "description": "Read metadata for the parent session or one direct child session without returning transcript items.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
        },
        "sys_agent_list": {
            "name": "sys_agent_list",
            "server_id": "sys",
            "source_tool": "agent_list",
            "kind": "function",
            "transport": "local",
            "inherited": False,
            "config": {
                "description": (
                    "List bounded agent metadata for the current parent session tree."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        },
        "sys_agent_get": {
            "name": "sys_agent_get",
            "server_id": "sys",
            "source_tool": "agent_get",
            "kind": "function",
            "transport": "local",
            "inherited": False,
            "config": {
                "description": (
                    "Return bounded metadata for the agent bound to a parent or direct child session."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                    },
                    "required": ["session_id"],
                    "additionalProperties": False,
                },
            },
        },
        "sys_agent_download": {
            "name": "sys_agent_download",
            "server_id": "sys",
            "source_tool": "agent_download",
            "kind": "function",
            "transport": "local",
            "inherited": False,
            "config": {
                "description": "Download a bounded, redacted ClawCross agent metadata bundle for inspection.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "dest_filename": {"type": "string"},
                    },
                    "required": ["session_id"],
                    "additionalProperties": False,
                },
            },
        },
        "sys_session_create": {
            "name": "sys_session_create",
            "server_id": "sys",
            "source_tool": "session_create",
            "kind": "function",
            "transport": "local",
            "inherited": False,
            "config": {
                "description": "Create a direct child subagent/reviewer session from a launchable agent_id.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        "config_path": {"type": "string"},
                        "title": {"type": "string"},
                        "message": {"type": "string"},
                    },
                    "oneOf": [{"required": ["agent_id"]}, {"required": ["config_path"]}],
                    "additionalProperties": False,
                },
            },
        },
        "sys_session_close": {
            "name": "sys_session_close",
            "server_id": "sys",
            "source_tool": "session_close",
            "kind": "function",
            "transport": "local",
            "inherited": False,
            "config": {
                "description": "Tombstone a direct named child session so the same agent/title can start fresh later.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "conversation_id": {"type": "string"},
                        "session_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "anyOf": [{"required": ["conversation_id"]}, {"required": ["session_id"]}],
                    "additionalProperties": False,
                },
            },
        },
        "sys_session_share": {
            "name": "sys_session_share",
            "server_id": "sys",
            "source_tool": "session_share",
            "kind": "function",
            "transport": "local",
            "inherited": False,
            "config": {
                "description": "Grant read/edit/manage access to the caller session or one direct child session.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "session_id": {"type": "string"},
                        "conversation_id": {"type": "string"},
                        "level": {"type": "string", "enum": ["read", "edit", "manage"]},
                    },
                    "required": ["user_id"],
                    "additionalProperties": False,
                },
            },
        },
        "sys_cancel_task": {
            "name": "sys_cancel_task",
            "server_id": "sys",
            "source_tool": "cancel_task",
            "kind": "function",
            "transport": "local",
            "inherited": False,
            "config": {
                "description": "Request cancellation for a materialized child-session task.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "agent_name": {"type": "string"},
                        "role": {"type": "string", "enum": ["", "subagent", "reviewer"]},
                        "session_id": {"type": "string"},
                        "title": {"type": "string"},
                        "child_task_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
        },
    }


def attach_subagent_lifecycle_tools(
    materialized_tools: dict[str, Any],
    materialized_agents: dict[str, Any],
    *,
    async_enabled: bool = True,
    spawn_enabled: bool = False,
    session_sharing: str = "none",
) -> dict[str, Any]:
    counts = materialized_agents.get("counts") if isinstance(materialized_agents, dict) else {}
    has_materialized_children = int((counts or {}).get("sessions") or 0) > 0
    root = dict(materialized_tools.get("root") if isinstance(materialized_tools.get("root"), dict) else {})
    tools = dict(root.get("tools") if isinstance(root.get("tools"), dict) else {})
    child_only_tools = {"sys_session_send", "sys_session_close"}
    async_tools = {"sys_call_async", "sys_read_inbox", "sys_cancel_async"}
    for name, tool in subagent_lifecycle_tool_manifest().items():
        if name in child_only_tools and not has_materialized_children:
            continue
        if name in async_tools and not async_enabled and (name != "sys_read_inbox" or not has_materialized_children):
            continue
        if name == "sys_session_create" and not spawn_enabled:
            continue
        if name == "sys_session_share" and session_sharing not in {"non-public", "public"}:
            continue
        tools.setdefault(name, tool)
    root["tools"] = tools
    root["counts"] = {
        **(root.get("counts") if isinstance(root.get("counts"), dict) else {}),
        "tools": len(tools),
        "system_tools": sum(1 for tool in tools.values() if str(tool.get("server_id") or "") == "sys"),
    }
    return {**materialized_tools, "root": root}


def materialize_agent_tool_bindings(spec: HarnessAgentSpec) -> dict[str, Any]:
    root = materialize_tool_scope(spec.tools, owner=spec.name)
    child_scopes = resolve_declared_subagent_tools(spec)
    subagents: dict[str, Any] = {}
    reviewers: dict[str, Any] = {}
    for name, child in spec.subagents.items():
        subagents[name] = materialized_tool_manifest_to_dict(
            materialize_tool_scope(child_scopes.get(name, child.tools), owner=f"{spec.name}.{name}")
        )
    for name, reviewer in spec.reviewers.items():
        reviewers[name] = materialized_tool_manifest_to_dict(
            materialize_tool_scope(child_scopes.get(name, reviewer.tools), owner=f"{spec.name}.{name}")
        )
    warnings = list(root.warnings)
    for group in (subagents, reviewers):
        for item in group.values():
            warnings.extend(item.get("warnings") or [])
    return {
        "root": materialized_tool_manifest_to_dict(root),
        "subagents": subagents,
        "reviewers": reviewers,
        "warnings": list(dict.fromkeys(warnings)),
        "counts": {
            "root_tools": len(root.tools),
            "subagents": len(subagents),
            "reviewers": len(reviewers),
            "warnings": len(dict.fromkeys(warnings)),
        },
    }
