import sys as _sys
import os as _os
_src_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _src_dir not in _sys.path:
    _sys.path.insert(0, _src_dir)

"""
MCP 定时任务调度服务

提供闹钟/定时任务管理工具：
- add_alarm: 设置定时任务
- list_alarms: 查询已设置的定时任务
- delete_alarm: 删除指定的定时任务
"""

from mcp.server.fastmcp import FastMCP
import httpx
import os
from dotenv import load_dotenv

# 初始化 MCP 服务
mcp = FastMCP("TimeMaster")

# 加载 .env 配置
current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(dotenv_path=os.path.join(root_dir, "config", ".env"))

# 调度器服务端口和地址
PORT_SCHEDULER = int(os.getenv("PORT_SCHEDULER", "51201"))
SCHEDULER_URL = f"http://127.0.0.1:{PORT_SCHEDULER}/tasks"

@mcp.tool()
async def add_alarm(username: str, cron: str, text: str, session_id: str = "default") -> str:
    """
    为用户设置一个定时任务（闹钟）。

    :param username: 用户唯一标识符（系统自动注入，无需手动传递）
    :param cron: Cron 表达式 (分 时 日 月 周)，例如 "0 1 * * *" 代表凌晨1点
    :param text: 到点时需要执行的指令内容
    :param session_id: 会话ID（系统自动注入，无需手动传递）
    :return: 操作结果的描述信息
    """
    async with httpx.AsyncClient() as client:
        try:
            payload = {"user_id": username, "cron": cron, "text": text, "session_id": session_id}
            resp = await client.post(SCHEDULER_URL, json=payload, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                return f"✅ 闹钟设置成功！任务 ID: {data['task_id']}，下次运行时间: {data.get('next_run')}"
            return f"❌ 设置失败，服务器返回: {resp.text}"
        except Exception as e:
            return f"⚠️ 无法连接到定时服务器: {str(e)}"

@mcp.tool()
async def list_alarms(username: str) -> str:
    """
    获取当前用户已设置的定时任务列表。

    :param username: 用户唯一标识符（系统自动注入，无需手动传递）
    :return: 用户所有定时任务的列表描述
    """
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(SCHEDULER_URL)
            tasks = resp.json()
            # 过滤只显示该用户的任务
            user_tasks = [t for t in tasks if t.get("user_id") == username]
            if not user_tasks:
                return "📭 您当前没有设定任何闹钟。"

            result = "📅 您的定时任务列表:\n"
            for task in user_tasks:
                result += f"- [ID: {task['task_id']}] 规则: {task['cron']}, 内容: {task['text']}\n"
            return result
        except Exception as e:
            return f"⚠️ 读取列表失败: {str(e)}"

@mcp.tool()
async def delete_alarm(username: str, task_id: str) -> str:
    """
    根据任务 ID 删除指定的定时任务（仅限本人创建的任务）。

    :param username: 用户唯一标识符（系统自动注入，无需手动传递）
    :param task_id: 之前创建任务时分配的 8 位 ID
    :return: 删除操作的结果描述
    """
    async with httpx.AsyncClient() as client:
        try:
            # 先查询任务列表，确认任务属于该用户
            resp = await client.get(SCHEDULER_URL)
            tasks = resp.json()
            target_task = next((t for t in tasks if t.get("task_id") == task_id), None)

            if not target_task:
                return f"❌ 未找到任务 {task_id}。"
            if target_task.get("user_id") != username:
                return f"❌ 无权删除任务 {task_id}，该任务不属于您。"

            # 验证通过，执行删除
            resp = await client.delete(f"{SCHEDULER_URL}/{task_id}")
            if resp.status_code == 200:
                return f"🗑️ 任务 {task_id} 已成功删除。"
            return f"❌ 删除失败: {resp.text}"
        except Exception as e:
            return f"⚠️ 连接失败: {str(e)}"

if __name__ == "__main__":
    mcp.run()