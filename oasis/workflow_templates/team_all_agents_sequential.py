import asyncio
import os
import sys

try:
    from oasis.python_workflow_cli import StandaloneWorkflowContext, run_cli
except ModuleNotFoundError:
    extra_paths = [
        p for p in os.environ.get("CLAWCROSS_PYTHONPATH", "").split(os.pathsep) if p
    ]
    project_root = os.environ.get("CLAWCROSS_PROJECT_ROOT", "").strip()
    if project_root:
        extra_paths.append(project_root)
    for path_entry in extra_paths:
        if path_entry and path_entry not in sys.path:
            sys.path.insert(0, path_entry)
    from oasis.python_workflow_cli import StandaloneWorkflowContext, run_cli


async def main(ctx: StandaloneWorkflowContext):
    agents = [a for a in ctx.list_agents() if a.get("id")]
    if not agents:
        ctx.set_conclusion("No agents available.")
        ctx.set_result({"ok": False, "error": "no agents available", "team": ctx.team, "question": ctx.question})
        return

    ordered_agents = sorted(
        agents,
        key=lambda a: (
            str(a.get("kind", "")),
            str(a.get("tag", "")),
            str(a.get("name", "")),
            str(a.get("id", "")),
        ),
    )

    await ctx.publish(
        f"Sequential workflow started with {len(ordered_agents)} agents in scope '{ctx.team or 'default'}'.",
        author="workflowpy",
    )

    transcript = []
    results = []
    for index, agent in enumerate(ordered_agents, start=1):
        agent_name = str(agent.get("name") or agent.get("id") or f"agent_{index}")
        prompt = ctx.question
        if transcript:
            prompt = (
                f"Original task:\n{ctx.question}\n\n"
                "Previous agent outputs:\n"
                + "\n\n".join(transcript)
                + "\n\nPlease continue from the previous outputs and add your own response."
            )

        reply = await ctx.send_agent(agent["id"], prompt)
        item = {
            "index": index,
            "agent_id": agent["id"],
            "agent_name": agent_name,
            "agent_tag": agent.get("tag", ""),
            "ok": reply.ok,
            "content": (reply.content or "").strip(),
            "error": reply.error,
        }
        results.append(item)

        if item["ok"]:
            transcript.append(f"{agent_name}:\n{item['content']}")
            await ctx.publish(item["content"] or "(empty reply)", author=agent_name[:80])
        else:
            await ctx.publish(
                f"FAILED: {item['error'] or 'unknown error'}",
                author=agent_name[:80],
            )

    success_count = sum(1 for item in results if item["ok"])
    ctx.set_conclusion(
        f"Sequential workflow completed: {success_count}/{len(ordered_agents)} agents succeeded."
    )
    ctx.set_result({
        "ok": True,
        "mode": "sequential",
        "team": ctx.team,
        "question": ctx.question,
        "agent_count": len(ordered_agents),
        "results": results,
    })


if __name__ == "__main__":
    raise SystemExit(run_cli(main))
