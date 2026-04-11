#!/usr/bin/env python3
"""
QQ 机器人 - 处理私聊和群聊消息，支持文字/图片/语音多模态交互

功能说明：
- 接收 QQ 私聊和群聊消息
- 支持图片和语音（Silk/AMR 格式）处理
- 调用 Agent (OpenAI 兼容接口) 进行 AI 对话
"""

import os
import io
import wave
import base64
import httpx
import pysilk
import aiohttp
import asyncio
from functools import wraps
from aiohttp_socks import ProxyConnector
from pydub import AudioSegment
import botpy
from botpy.message import C2CMessage, GroupMessage

# 加载 .env 配置文件
_chatbot_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_chatbot_dir)

# QQ Bot 配置
QQ_BOT_CONFIG = {
    "appid": os.getenv("QQ_APP_ID"),
    "secret": os.getenv("QQ_BOT_SECRET"),
}

# 认证信息：INTERNAL_TOKEN:用户名:QQ
# QQ_BOT_USERNAME 指定该 Bot 以哪个系统用户身份调用 Agent
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "")
QQ_BOT_USERNAME = os.getenv("QQ_BOT_USERNAME", "qquser")

# AI 接口配置（OpenAI 兼容格式）
AI_CONFIG = {
    "api_key": f"{INTERNAL_TOKEN}:{QQ_BOT_USERNAME}:QQ",
    "url": os.getenv("AI_API_URL", f"http://127.0.0.1:{os.getenv('PORT_AGENT', '51200')}/v1/chat/completions"),
    "model": os.getenv("AI_MODEL_QQ") or os.getenv("LLM_MODEL", ""),
}

# SSH 隧道代理地址（用于连接腾讯服务器）
PROXY_URL = "socks5://127.0.0.1:1080"


def create_proxy_session():
    """创建使用代理的 aiohttp 会话（解决腾讯服务器白名单问题）"""
    return aiohttp.ClientSession(connector=ProxyConnector.from_url(PROXY_URL))


class QQBotClient(botpy.Client):
    """QQ 机器人客户端，处理多模态消息"""

    async def process_attachment_to_base64(self, url: str, is_silk_format: bool = False) -> str | None:
        """
        处理附件（图片/语音）为 Base64 字符串

        Args:
            url: 附件下载 URL
            is_silk_format: 是否为 Silk 语音格式

        Returns:
            Base64 编码字符串，失败返回 None
        """
        try:
            # 1. 通过代理下载附件
            async with create_proxy_session() as session:
                async with session.get(url, timeout=15.0) as response:
                    if response.status != 200:
                        print(f"附件下载失败: HTTP {response.status}")
                        return None
                    raw_data = await response.read()

            # 2. 图片直接转 Base64
            if not is_silk_format:
                return base64.b64encode(raw_data).decode('utf-8')

            # 3. 语音格式处理：定位 Silk 头部并解码为 PCM
            silk_header_index = raw_data.find(b"#!SILK")
            if silk_header_index == -1:
                print("未找到 SILK 头部，无法处理语音")
                return None

            silk_data = raw_data[silk_header_index:]

            # 解码 Silk 为 PCM（采样率 24000 是 QQ 语音标准）
            input_buffer = io.BytesIO(silk_data)
            output_pcm = io.BytesIO()
            pysilk.decode(input_buffer, output_pcm, 24000)

            pcm_data = output_pcm.getvalue()
            if not pcm_data:
                print("PCM 数据为空，解码失败")
                return None

            # 4. 将 PCM 封装为 WAV 格式
            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, 'wb') as wav_file:
                wav_file.setnchannels(1)   # 单声道
                wav_file.setsampwidth(2)   # 16-bit
                wav_file.setframerate(24000)
                wav_file.writeframes(pcm_data)
            wav_bytes = wav_buffer.getvalue()

            # 返回纯净 Base64（移除换行符）
            return base64.b64encode(wav_bytes).decode('utf-8').replace("\n", "").replace("\r", "")

        except Exception as e:
            print(f"附件处理异常: {e}")
            return None

    async def call_ai_service(self, content_list: list) -> str:
        """
        调用 AI 服务（OpenAI 兼容接口）

        Args:
            content_list: OpenAI 格式的 content 列表

        Returns:
            AI 回复文本
        """
        # 过滤空数据的 audio 字段
        filtered_content = [
            item for item in content_list
            if not (isinstance(item.get("input_audio"), dict) and not item["input_audio"].get("data"))
        ]

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60.0)) as session:
                async with session.post(
                    AI_CONFIG["url"],
                    headers={"Authorization": f"Bearer {AI_CONFIG['api_key']}"},
                    json={
                        "model": AI_CONFIG["model"],
                        "messages": [{"role": "user", "content": filtered_content}]
                    }
                ) as response:
                    response_data = await response.json()
                    if "choices" in response_data:
                        return response_data["choices"][0]["message"]["content"]
                    return f"AI 接口返回异常: {response_data.get('error', {}).get('message', '未知错误')}"
        except Exception as e:
            return f"网络请求失败: {str(e)}"

    async def handle_message(self, message) -> None:
        """
        统一处理私聊和群聊消息

        Args:
            message: QQ 消息对象
        """
        # 1. 清洗文本（去除机器人 @ 提及）
        raw_text = message.content.strip()
        clean_text = raw_text.replace(f"<@!{QQ_BOT_CONFIG['appid']}>", "").strip()

        # 2. 构建多模态 content 列表
        content_list = [{"type": "text", "text": clean_text or "请分析内容"}]

        # 3. 处理附件（图片/语音）
        if hasattr(message, 'attachments') and message.attachments:
            for attachment in message.attachments:
                # 判断是否为语音格式
                is_silk = (
                    attachment.content_type == "voice" or
                    attachment.filename.endswith(".silk") or
                    attachment.filename.endswith(".amr")
                )

                # 转换为 Base64
                b64_data = await self.process_attachment_to_base64(attachment.url, is_silk_format=is_silk)
                if not b64_data:
                    continue

                if is_silk:
                    # 语音格式：使用 input_audio 类型
                    content_list.append({
                        "type": "input_audio",
                        "input_audio": {
                            "data": b64_data,
                            "format": "wav"
                        }
                    })
                else:
                    # 图片格式：使用 image_url 类型
                    content_list.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64_data}"}
                    })

        # 4. 兜底：确保至少有一个 text 类型元素
        if not any(item['type'] == 'text' for item in content_list):
            content_list.insert(0, {"type": "text", "text": "请分析这段内容"})

        # 5. 调用 AI 并回复
        ai_response = await self.call_ai_service(content_list)
        await message.reply(content=ai_response)

    async def on_c2c_message_create(self, message: C2CMessage) -> None:
        """处理私聊消息"""
        print(f"收到私聊: {message.author.user_openid}")
        await self.handle_message(message)

    async def on_group_at_message_create(self, message: GroupMessage) -> None:
        """处理群聊 @ 消息"""
        print(f"收到群聊 @ 消息")
        await self.handle_message(message)


if __name__ == "__main__":
    # 启用私聊 (1<<30) 和 频道/群聊 (1<<25) 权限
    intents = botpy.Intents.none()
    intents.value = (1 << 25) | (1 << 30)

    client = QQBotClient(intents=intents)
    print(f"QQ 机器人已启动！请确保外部 SSH 隧道 (1080) 正在运行...")
    client.run(appid=QQ_BOT_CONFIG["appid"], secret=QQ_BOT_CONFIG["secret"])
