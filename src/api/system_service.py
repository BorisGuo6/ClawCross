"""
系统触发服务模块

处理来自定时任务、外部系统等触发源的请求：
- 系统触发消息处理
- 多模态附件（图片/音频/文件）处理
"""

import asyncio
import base64
from typing import Any, Callable

from langchain_core.messages import HumanMessage, ToolMessage

from utils.logging_utils import get_logger
from services.message_builder import build_human_message
from api.system_models import SystemTriggerRequest

logger = get_logger("system_service")

# 可以直接 base64 解码为文本的 MIME 类型（前缀匹配）
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_EXACT = {
    "application/json", "application/xml", "application/javascript",
    "application/typescript", "application/x-yaml", "application/yaml",
    "application/toml", "application/x-toml",
    "application/sql", "application/graphql",
    "application/x-sh", "application/x-python",
    "application/csv", "application/x-csv",
    "application/ld+json", "application/manifest+json",
    "application/x-httpd-php",
}


def _is_text_mime(mime_type: str) -> bool:
    """判断 MIME 类型是否为文本类（可以 base64 解码为可读文本）。"""
    mime = mime_type.lower().strip()
    if any(mime.startswith(p) for p in _TEXT_MIME_PREFIXES):
        return True
    if mime in _TEXT_MIME_EXACT:
        return True
    # 常见文本后缀的通配：application/*+json, application/*+xml
    if mime.endswith("+json") or mime.endswith("+xml"):
        return True
    return False


def _try_decode_base64_text(data: str, max_chars: int = 50000) -> str | None:
    """尝试将 base64 数据解码为 UTF-8 文本。失败返回 None。"""
    try:
        raw = base64.b64decode(data)
        text = raw.decode("utf-8")
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n... (文件过长，已截断，共 {len(raw)} 字节)"
        return text
    except Exception:
        return None


class SystemService:
    def __init__(
        self,
        *,
        agent: Any,
        verify_internal_token: Callable[[str | None], None],
    ):
        self.agent = agent
        self.verify_internal_token = verify_internal_token

    def _build_message_from_trigger(self, req: SystemTriggerRequest) -> HumanMessage:
        """将 SystemTriggerRequest 转为 HumanMessage，支持多模态附件。"""
        if not req.attachments:
            return HumanMessage(content=req.text)

        images: list[str] = []
        audios: list[dict] = []
        files: list[dict] = []

        for att in req.attachments:
            if att.type == "image":
                # build_human_message 期望 data URI 格式
                data_uri = f"data:{att.mime_type};base64,{att.data}"
                images.append(data_uri)
            elif att.type == "audio":
                # 提取格式后缀 (e.g. "audio/mp3" → "mp3")
                fmt = att.mime_type.split("/")[-1] if "/" in att.mime_type else "wav"
                audios.append({
                    "base64": att.data,
                    "format": fmt,
                    "name": att.name,
                })
            else:
                # file 类型 — 按 MIME 分类处理
                if att.mime_type == "application/pdf":
                    # PDF: 传 base64 给 build_human_message，它会调用 _extract_pdf_text
                    files.append({
                        "name": att.name,
                        "type": "pdf",
                        "content": att.data,
                    })
                elif _is_text_mime(att.mime_type):
                    # 文本类文件 (json/txt/csv/xml/yaml/...): 解码 base64 为可读文本
                    decoded = _try_decode_base64_text(att.data)
                    if decoded is not None:
                        files.append({
                            "name": att.name,
                            "type": "text",
                            "content": decoded,
                        })
                    else:
                        files.append({
                            "name": att.name,
                            "type": "text",
                            "content": f"(文件 {att.name} 解码失败，MIME: {att.mime_type})",
                        })
                else:
                    # 真正的二进制文件 (zip/docx/xlsx 等): 降级为描述
                    files.append({
                        "name": att.name,
                        "type": "text",
                        "content": f"(二进制文件: {att.name}, 类型: {att.mime_type}，无法直接展示内容)",
                    })

        return build_human_message(
            text=req.text,
            images=images or None,
            files=files or None,
            audios=audios or None,
        )

    async def system_trigger(self, req: SystemTriggerRequest, x_internal_token: str | None):
        self.verify_internal_token(x_internal_token)
        thread_id = f"{req.user_id}#{req.session_id}"
        config = {"configurable": {"thread_id": thread_id}}

        human_msg = self._build_message_from_trigger(req)
        logger.info("system_trigger for %s, has_attachments=%s, content_type=%s",
                     thread_id, bool(req.attachments),
                     type(human_msg.content).__name__)

        system_input = {
            "messages": [human_msg],
            "trigger_source": "system",
            "enabled_tools": None,
            "user_id": req.user_id,
            "session_id": req.session_id,
            "max_turns": None,
            "turn_count": 0,
        }

        async def wait_and_invoke():
            """在 thread 锁上排队执行：不 preempt 用户对话；仅在拿到锁后注册 cancel 目标。"""
            task_key = f"{req.user_id}#{req.session_id}"
            lock = await self.agent.get_thread_lock(thread_id)
            logger.info(
                "system_trigger waiting for lock on %s (will not cancel in-flight user run)",
                thread_id,
            )
            async with lock:
                # 与用户 openai 流式任务共用 task_key：必须在持有锁后才 register，
                # 否则会在等锁阶段覆盖 registry 中仍活跃的用户任务。
                self.agent.register_task(task_key, asyncio.current_task())
                self.agent.set_thread_busy_source(thread_id, "system")
                logger.info("Acquired lock on %s, invoking graph ...", thread_id)
                try:
                    async for _ in self.agent.agent_app.astream_events(system_input, config, version="v2", durability="exit"):
                        pass
                    self.agent.add_pending_system_message(thread_id)
                    logger.info("Done for %s", thread_id)
                except asyncio.CancelledError:
                    logger.info("Cancelled for %s", thread_id)
                    try:
                        snapshot = await self.agent.agent_app.aget_state(config)
                        last_msgs = snapshot.values.get("messages", [])
                        if last_msgs:
                            last_msg = last_msgs[-1]
                            if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                                tool_messages = [
                                    ToolMessage(
                                        content="⚠️ 工具调用被用户终止",
                                        tool_call_id=tc["id"],
                                    )
                                    for tc in last_msg.tool_calls
                                ]
                                await self.agent.agent_app.aupdate_state(config, {"messages": tool_messages})
                    except Exception:
                        pass
                except Exception as e:
                    logger.exception("Error for %s: %s", thread_id, e)
                finally:
                    self.agent.clear_thread_busy_source(thread_id)
                    await self.agent.purge_checkpoints(thread_id)
                    self.agent.unregister_task(task_key)

        asyncio.create_task(wait_and_invoke())
        return {"status": "received", "message": f"系统触发已收到，用户 {req.user_id}"}
