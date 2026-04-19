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
    # This example can run from any directory as long as the required imports
    # are already available via PYTHONPATH, custom sys.path logic, a wrapper,
    # or CLAWCROSS_PYTHONPATH / CLAWCROSS_PROJECT_ROOT.
    await ctx.publish(f"workflow started for user: {ctx.user_id}", author="example")

    # 获取所有 agent
    agents = ctx.list_agents()
    await ctx.publish(f"found {len(agents)} agents", author="example")

    # 发送问题给创意专家
    reply = await ctx.send_persona("creative", ctx.question)
    await ctx.publish(f"creative reply: {reply.content}", author="example")

    # 发送问题给批判专家
    reply2 = await ctx.send_persona("critical", ctx.question)
    await ctx.publish(f"critical reply: {reply2.content}", author="example")

    # 综合结果
    result = {
        "creative": reply.content or "",
        "critical": reply2.content or "",
        "agent_count": len(agents),
    }
    ctx.set_result(result)
    ctx.set_conclusion("example workflow finished")


if __name__ == "__main__":
    raise SystemExit(run_cli(main))
