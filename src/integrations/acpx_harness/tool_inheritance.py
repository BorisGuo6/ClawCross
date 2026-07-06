# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 SubLang International <https://sublang.ai>

"""Tool-scope resolution for declarative ACPX harness agents."""

from __future__ import annotations

from dataclasses import replace

from integrations.acpx_harness.schema import HarnessAgentSpec, HarnessToolSpec


class ToolInheritanceError(ValueError):
    pass


def resolve_child_tools(parent: HarnessAgentSpec, child: HarnessAgentSpec) -> dict[str, HarnessToolSpec]:
    """Resolve a child agent's effective tool set.

    ClawCross chooses the stricter rule: no default inheritance. A child may
    explicitly request a parent tool with ``tool_name: inherit``.
    """

    resolved: dict[str, HarnessToolSpec] = {}
    for name, tool in child.tools.items():
        if tool.kind != "inherit":
            resolved[name] = tool
            continue
        parent_tool = parent.tools.get(name)
        if parent_tool is None:
            raise ToolInheritanceError(f"child agent {child.name!r} requested missing parent tool {name!r}")
        resolved[name] = replace(parent_tool, inherited=True)
    return resolved


def resolve_declared_subagent_tools(parent: HarnessAgentSpec) -> dict[str, dict[str, HarnessToolSpec]]:
    """Return effective tool scopes for every declared child/reviewer agent."""

    scopes: dict[str, dict[str, HarnessToolSpec]] = {}
    for name, child in {**parent.subagents, **parent.reviewers}.items():
        scopes[name] = resolve_child_tools(parent, child)
    return scopes


def tool_scope_to_dict(tools: dict[str, HarnessToolSpec]) -> dict[str, dict[str, object]]:
    return {
        name: {"type": tool.kind, "inherited": tool.inherited, **tool.config}
        for name, tool in tools.items()
    }
