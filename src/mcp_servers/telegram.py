import sys as _sys
import os as _os
_src_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _src_dir not in _sys.path:
    _sys.path.insert(0, _src_dir)

#!/usr/bin/env python3

# -*- coding: utf-8 -*-
"""
MCP Telegram 推送通知服务

功能说明：
- Agent 可通过此工具向用户的 Telegram 发送消息
- 用户的 chat_id 存储在 data/user_files/<username>/tg_chat_id.txt
- 设置 chat_id 时自动同步到全局白名单 data/telegram_whitelist.json
- 使用 .env 中的 TELEGRAM_BOT_TOKEN 发送
"""

import os
import json
import httpx
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

mcp = FastMCP("TelegramPush")

current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(dotenv_path=os.path.join(root_dir, "config", ".env"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
USER_DATA_DIR = os.path.join(root_dir, "data", "user_files")
WHITELIST_FILE = os.path.join(root_dir, "data", "telegram_whitelist.json")

def _get_chat_id_path(username: str) -> str:
    """获取用户 chat_id 文件路径"""
    return os.path.join(USER_DATA_DIR, username, "tg_chat_id.txt")

def _read_chat_id(username: str) -> str | None:
    """读取用户的 Telegram chat_id"""
    chat_id_path = _get_chat_id_path(username)
    if os.path.exists(chat_id_path):
        with open(chat_id_path, "r", encoding="utf-8") as f:
            chat_id_val = f.read().strip()
            return chat_id_val if chat_id_val else None
    return None

# ── 白名单管理 ──

def _load_whitelist() -> dict:
    """加载白名单文件。

    :return: 白名单字典，格式 {"allowed": [{"username": "...", "chat_id": "...", "tg_username": ""}]}
    """
    if os.path.exists(WHITELIST_FILE):
        with open(WHITELIST_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                pass
    return {"allowed": []}

def _save_whitelist(whitelist: dict):
    """保存白名单到磁盘。

    :param whitelist: 白名单字典
    """
    os.makedirs(os.path.dirname(WHITELIST_FILE), exist_ok=True)
    with open(WHITELIST_FILE, "w", encoding="utf-8") as f:
        json.dump(whitelist, f, ensure_ascii=False, indent=2)

def _sync_to_whitelist(username: str, chat_id: str, tg_username: str = ""):
    """将用户 chat_id 同步到全局白名单表。若已存在则更新，否则新增。

    :param username: 用户名
    :param chat_id: Telegram chat_id
    :param tg_username: Telegram 用户名（可选）
    """
    whitelist = _load_whitelist()
    found = False
    for entry in whitelist["allowed"]:
        if entry.get("username") == username:
            entry["chat_id"] = chat_id
            if tg_username:
                entry["tg_username"] = tg_username
            found = True
            break
    if not found:
        whitelist["allowed"].append({
            "username": username,
            "chat_id": chat_id,
            "tg_username": tg_username,
        })
    _save_whitelist(whitelist)

def _remove_from_whitelist(username: str):
    """从白名单中移除用户。

    :param username: 要移除的用户名
    """
    whitelist = _load_whitelist()
    whitelist["allowed"] = [entry for entry in whitelist["allowed"] if entry.get("username") != username]
    _save_whitelist(whitelist)

@mcp.tool()
async def set_telegram_chat_id(username: str, chat_id: str, tg_username: str = "") -> str:
    """
    保存用户的 Telegram chat_id 用于推送通知。
    同时会自动将用户加入 Telegram bot 白名单。
    用户可以通过向 bot 发送 /start 或使用 @userinfobot 获取自己的 chat_id。

    :param username: 用户标识符（系统自动注入，无需手动传递）
    :param chat_id: Telegram chat ID（数字字符串，如 "123456789"）
    :param tg_username: 可选的 Telegram @用户名（不要加 @，如 "my_username"）
    :return: 操作结果描述
    """
    if not chat_id or not chat_id.strip():
        return "❌ chat_id 不能为空。"
    chat_id = chat_id.strip()

    user_dir = os.path.join(USER_DATA_DIR, username)
    os.makedirs(user_dir, exist_ok=True)

    with open(_get_chat_id_path(username), "w", encoding="utf-8") as f:
        f.write(chat_id)

    # 自动同步到全局白名单
    _sync_to_whitelist(username, chat_id, tg_username.strip().lstrip("@") if tg_username else "")

    return (
        f"✅ Telegram chat_id 已保存：{chat_id}，后续可通过 Telegram 接收通知。\n"
        f"✅ 已自动加入 Telegram Bot 白名单。"
    )

@mcp.tool()
async def remove_telegram_config(username: str) -> str:
    """
    移除用户的 Telegram 配置并撤销白名单访问权限。

    :param username: 用户标识符（系统自动注入，无需手动传递）
    :return: 操作结果描述
    """
    chat_id_path = _get_chat_id_path(username)
    removed_chat_id = False
    if os.path.exists(chat_id_path):
        os.remove(chat_id_path)
        removed_chat_id = True

    _remove_from_whitelist(username)

    if removed_chat_id:
        return "✅ 已移除 Telegram chat_id 并从白名单中删除。"
    else:
        return "ℹ️ 该用户未配置 Telegram chat_id，已确保从白名单中移除。"

@mcp.tool()
async def send_telegram_message(
    username: str, text: str, source_session: str = "", parse_mode: str = "Markdown"
) -> str:
    """
    通过 Telegram Bot 向用户发送文本消息。
    用于主动通知用户任务结果、提醒或重要更新。
    消息会自动标注来源会话。

    :param username: 用户标识符（系统自动注入，无需手动传递）
    :param text: 要发送的消息内容，支持 Markdown 格式
    :param source_session: （自动注入）触发此通知的会话 ID，请勿手动设置
    :param parse_mode: 文本格式模式："Markdown"、"HTML" 或 ""（纯文本），默认："Markdown"
    :return: 发送结果描述
    """
    if not TELEGRAM_BOT_TOKEN:
        return "❌ 未配置 TELEGRAM_BOT_TOKEN，无法发送 Telegram 消息。请在 .env 中设置。"

    chat_id = _read_chat_id(username)
    if not chat_id:
        return (
            "❌ 尚未配置 Telegram chat_id，无法发送消息。\n"
            "请让用户提供 Telegram chat_id（可通过 @userinfobot 获取）。"
        )

    # 自动在消息前标注来源 session
    if source_session and source_session != "tg":
        session_tag = f"[来自会话: {source_session}]\n"
        text = session_tag + text

    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{TELEGRAM_API}/sendMessage",
                json=payload,
                timeout=15.0,
            )
            response_data = resp.json()
            if response_data.get("ok"):
                return f"✅ Telegram 消息已发送！"
            else:
                error_desc = response_data.get("description", "未知错误")
                # Markdown 解析失败时自动降级为纯文本重试
                if "parse" in error_desc.lower() and parse_mode:
                    payload["parse_mode"] = ""
                    retry_resp = await client.post(
                        f"{TELEGRAM_API}/sendMessage",
                        json=payload,
                        timeout=15.0,
                    )
                    retry_data = retry_resp.json()
                    if retry_data.get("ok"):
                        return f"✅ Telegram 消息已发送（降级为纯文本格式）。"
                return f"❌ Telegram 发送失败: {error_desc}"
        except httpx.ConnectError:
            return "❌ 无法连接 Telegram API，请检查网络。"
        except Exception as e:
            return f"⚠️ Telegram 发送异常: {str(e)}"

@mcp.tool()
async def get_telegram_status(username: str) -> str:
    """
    查询用户的 Telegram 推送通知配置状态。

    :param username: 用户标识符（系统自动注入，无需手动传递）
    :return: 配置状态的详细描述
    """
    chat_id = _read_chat_id(username)
    status_lines = ["📱 Telegram 推送配置状态："]

    if chat_id:
        status_lines.append(f"  ✅ Chat ID: {chat_id}")
    else:
        status_lines.append("  ❌ Chat ID: 未配置")

    if TELEGRAM_BOT_TOKEN:
        masked_token = TELEGRAM_BOT_TOKEN[:8] + "****" if len(TELEGRAM_BOT_TOKEN) > 8 else "****"
        status_lines.append(f"  ✅ Bot Token: {masked_token}")
    else:
        status_lines.append("  ❌ Bot Token: 未配置（.env 中缺少 TELEGRAM_BOT_TOKEN）")

    if chat_id and TELEGRAM_BOT_TOKEN:
        status_lines.append("  ✅ 可正常发送 Telegram 通知")
    else:
        status_lines.append("  ⚠️ 配置不完整，无法发送通知")

    # 白名单状态
    whitelist = _load_whitelist()
    in_whitelist = any(entry.get("username") == username for entry in whitelist.get("allowed", []))
    if in_whitelist:
        status_lines.append("  ✅ 已在 Telegram Bot 白名单中")
    else:
        status_lines.append("  ⚠️ 未在 Telegram Bot 白名单中（设置 chat_id 后自动加入）")

    return "\n".join(status_lines)

if __name__ == "__main__":
    mcp.run()
