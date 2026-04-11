"""
Telegram 机器人 - 处理文字/图片/语音消息，支持多模态 AI 对话

功能说明：
- 接收并处理 Telegram 文字、图片、语音消息
- 支持 /tunnel 命令查询公网隧道状态
- 调用 Agent (OpenAI 兼容接口) 进行 AI 对话
- 白名单机制：每个 TG 用户映射到独立系统用户身份
"""

import os
import json
import time
import logging
import httpx
import base64
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters

# 加载 .env 配置文件
_chatbot_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_chatbot_dir)
load_dotenv(dotenv_path=os.path.join(_project_root, "config", ".env"))

# ==================== 配置区 ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "")

# Agent 接口配置
_AGENT_PORT = os.getenv("PORT_AGENT", "51200")
AI_API_URL = os.getenv("AI_API_URL", f"http://127.0.0.1:{_AGENT_PORT}/v1/chat/completions")
AI_MODEL = os.getenv("AI_MODEL_TG") or os.getenv("LLM_MODEL", "")

# OASIS 服务地址（用于查询隧道状态）
OASIS_BASE_URL = os.getenv("OASIS_BASE_URL", "http://127.0.0.1:51202")

# ==================== 白名单管理 ====================
# 白名单文件路径
WHITELIST_FILE_PATH = os.path.join(_project_root, "data", "telegram_whitelist.json")
WHITELIST_RELOAD_INTERVAL = 30  # 白名单重新加载间隔（秒）

# 白名单缓存结构: {entries: {chat_id: entry}, tg_name_map: {tg_username: entry}, loaded_at: timestamp}
_whitelist_cache: dict = {"entries": {}, "tg_name_map": {}, "loaded_at": 0}


def reload_whitelist() -> None:
    """
    从白名单文件加载用户映射

    白名单文件格式: {"allowed": [{"username": "系统用户名", "chat_id": "TG数字ID", "tg_username": "TG用户名"}]}
    缓存机制：每 30 秒最多重新读取一次
    """
    current_time = time.time()
    if current_time - _whitelist_cache["loaded_at"] < WHITELIST_RELOAD_INTERVAL:
        return

    # chat_id(int) -> entry 映射
    chat_id_map: dict[int, dict] = {}
    # tg_username(小写) -> entry 映射
    tg_username_map: dict[str, dict] = {}

    if os.path.exists(WHITELIST_FILE_PATH):
        try:
            with open(WHITELIST_FILE_PATH, "r", encoding="utf-8") as f:
                whitelist_data = json.load(f)

            for entry in whitelist_data.get("allowed", []):
                chat_id_str = entry.get("chat_id", "")
                if chat_id_str:
                    try:
                        chat_id_map[int(chat_id_str)] = entry
                    except ValueError:
                        pass

                tg_username = entry.get("tg_username", "")
                if tg_username:
                    tg_username_map[tg_username.lower()] = entry

        except (json.JSONDecodeError, OSError) as e:
            print(f"白名单加载失败: {e}")

    _whitelist_cache["entries"] = chat_id_map
    _whitelist_cache["tg_name_map"] = tg_username_map
    _whitelist_cache["loaded_at"] = current_time


def lookup_whitelist_entry(update: Update) -> dict | None:
    """
    根据 Telegram 消息查找对应的白名单条目

    Args:
        update: Telegram Update 对象

    Returns:
        白名单条目（包含 username），未找到返回 None
    """
    reload_whitelist()
    user = update.effective_user
    if not user:
        return None

    # 优先按 chat_id 匹配
    entry = _whitelist_cache["entries"].get(user.id)
    if entry:
        return entry

    # 其次按 tg_username 匹配
    if user.username:
        entry = _whitelist_cache["tg_name_map"].get(user.username.lower())
        if entry:
            return entry

    return None


# ==================== 工具函数 ====================

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)


async def download_file_as_base64(file_id: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    """
    下载 Telegram 文件并转换为 Base64 字符串

    Args:
        file_id: Telegram 文件 ID
        context: Telegram Bot 上下文

    Returns:
        Base64 编码字符串
    """
    file = await context.bot.get_file(file_id)
    async with httpx.AsyncClient() as client:
        response = await client.get(file.file_path)
        return base64.b64encode(response.content).decode('utf-8')


# ==================== 命令处理器 ====================

async def handle_tunnel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    处理 /tunnel 命令 - 查询公网隧道状态

    显示隧道运行状态和公网访问地址
    """
    entry = lookup_whitelist_entry(update)
    if entry is None:
        reload_whitelist()
        if _whitelist_cache["entries"] or _whitelist_cache["tg_name_map"]:
            await update.message.reply_text("你没有权限使用此机器人。")
            return

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{OASIS_BASE_URL}/publicnet/info")
            if response.status_code != 200:
                await update.message.reply_text(f"查询失败: HTTP {response.status_code}")
                return
            data = response.json()

        tunnel_info = data.get("tunnel", {})
        if tunnel_info.get("running"):
            public_domain = tunnel_info.get("public_domain", "")
            if public_domain:
                message_text = (
                    f"公网隧道运行中\n\n"
                    f"地址: {public_domain}\n\n"
                    f"在手机浏览器中打开即可访问前端"
                )
            else:
                message_text = "隧道运行中，但公网地址尚未就绪，请稍后再试"
        else:
            message_text = "公网隧道未运行，可通过 Agent 或前端 Settings 启动隧道"

        await update.message.reply_text(message_text)

    except httpx.ConnectError:
        await update.message.reply_text("无法连接 OASIS 服务，请确认服务已启动")
    except Exception as e:
        await update.message.reply_text(f"查询失败: {e}")


# ==================== 消息处理器 ====================

async def handle_multimodal_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    处理多模态消息（文字/图片/语音）

    核心逻辑：
    1. 验证用户权限（白名单）
    2. 构建 OpenAI 格式 content 列表
    3. 调用 Agent AI 接口
    4. 返回 AI 回复
    """
    # 1. 权限验证
    entry = lookup_whitelist_entry(update)

    if entry is None:
        reload_whitelist()
        if _whitelist_cache["entries"] or _whitelist_cache["tg_name_map"]:
            # 白名单非空但用户不在其中
            user = update.effective_user
            user_id = user.id if user else "unknown"
            username_str = f"@{user.username}" if user and user.username else ""
            logging.warning(f"未授权用户尝试访问: {user_id} {username_str}")
            await update.message.reply_text("你没有权限使用此机器人。请先在 Agent 中设置 Telegram chat_id。")
            return
        else:
            # 白名单为空，无法确定用户身份
            await update.message.reply_text("白名单未配置，请先通过 Agent 设置 Telegram chat_id。")
            return

    system_username = entry.get("username", "")
    if not system_username:
        await update.message.reply_text("白名单配置错误：缺少系统用户名。请重新通过 Agent 设置 Telegram。")
        return

    if not INTERNAL_TOKEN:
        await update.message.reply_text("系统未配置 INTERNAL_TOKEN，无法调用 Agent。")
        return

    # 2. 获取消息内容
    chat_id = update.effective_chat.id
    message_text = update.message.caption or update.message.text or "请分析此内容"

    # 3. 立即发送"正在输入"状态
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # 4. 构建 OpenAI 格式 content 列表
    content_list = [{"type": "text", "text": message_text}]

    try:
        # 5. 处理图片附件
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
            base64_image = await download_file_as_base64(file_id, context)
            content_list.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
            })

        # 6. 处理语音附件
        elif update.message.voice:
            file_id = update.message.voice.file_id
            base64_audio = await download_file_as_base64(file_id, context)
            content_list.append({
                "type": "input_audio",
                "input_audio": {
                    "data": base64_audio,
                    "format": "wav",
                }
            })

        # 7. 调用 Agent AI 接口
        # 认证格式: INTERNAL_TOKEN:username:TG（管理员级认证 + 指定用户 + session=TG）
        api_key = f"{INTERNAL_TOKEN}:{system_username}:TG"

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                AI_API_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": AI_MODEL,
                    "messages": [
                        {"role": "user", "content": content_list}
                    ]
                }
            )

            if response.status_code != 200:
                raise Exception(f"AI 接口报错 ({response.status_code}): {response.text}")

            response_json = response.json()
            ai_reply = response_json["choices"][0]["message"]["content"]

    except Exception as e:
        logging.error(f"AI 请求失败 (用户 {system_username}): {e}")
        ai_reply = f"发生错误: {str(e)}"

    # 8. 回复用户
    await update.message.reply_text(ai_reply)


# ==================== 启动入口 ====================

if __name__ == '__main__':
    if not TELEGRAM_BOT_TOKEN:
        print("错误: 未设置 TELEGRAM_BOT_TOKEN，无法启动。")
        exit(1)

    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # 注册 /tunnel 命令处理器
    application.add_handler(CommandHandler("tunnel", handle_tunnel_command))

    # 注册消息处理器（文字/图片/语音，排除命令）
    message_handler = MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.VOICE) & (~filters.COMMAND),
        handle_multimodal_message
    )
    application.add_handler(message_handler)

    # 初始加载白名单
    reload_whitelist()

    # 打印启动信息
    print("--- Telegram 机器人已启动 (轮询模式) ---")
    print("支持：文字 / 图片 / 语音 (OpenAI 多模态格式)")
    print(f"Agent 接口: {AI_API_URL}")
    print(f"认证方式: INTERNAL_TOKEN + 用户隔离（每个 TG 用户映射到独立的系统用户）")

    whitelist_entries = _whitelist_cache["entries"]
    if whitelist_entries:
        print(f"白名单已启用，{len(whitelist_entries)} 个用户:")
        for chat_id, entry in whitelist_entries.items():
            print(f"   chat_id={chat_id} → {entry.get('username', '?')}")
        print(f"   白名单每 {WHITELIST_RELOAD_INTERVAL} 秒自动重载")
    else:
        print("警告: 白名单为空（请先通过 Agent 设置用户的 Telegram chat_id）")

    application.run_polling(drop_pending_updates=True)
