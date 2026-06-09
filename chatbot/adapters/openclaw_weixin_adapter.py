"""
OpenClaw Weixin account adapter for ClawCross.

This adapter reuses the Weixin login credential written by Tencent's
``@tencent-weixin/openclaw-weixin`` plugin, but it does not route messages
through an OpenClaw agent. ClawCross owns the long-poll loop, calls its own
Agent API, then sends replies through the same Weixin backend API.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from src.utils.runtime_paths import ENV_FILE

load_dotenv(dotenv_path=ENV_FILE)

from .base import ChannelAdapter, ChatMessage

logger = logging.getLogger("chatbot.openclaw_weixin")

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_STATE_DIR = "~/.openclaw/openclaw-weixin"
DEFAULT_PACKAGE_JSON = "~/.openclaw/extensions/openclaw-weixin/package.json"

MESSAGE_TYPE_USER = 1
MESSAGE_ITEM_TEXT = 1
MESSAGE_ITEM_IMAGE = 2
MESSAGE_ITEM_VOICE = 3
MESSAGE_ITEM_FILE = 4
MESSAGE_ITEM_VIDEO = 5


@dataclass
class WeixinAccount:
    account_id: str
    token: str
    base_url: str
    user_id: str | None = None


def _expand_path(value: str) -> Path:
    return Path(value).expanduser()


def _client_version(version: str) -> int:
    parts = []
    for raw in (version or "0.0.0").split(".")[:3]:
        try:
            parts.append(int(raw))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[:3]
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


def _wechat_uin_header() -> str:
    value = str(random.getrandbits(32)).encode("utf-8")
    return base64.b64encode(value).decode("ascii")


def _extract_text_from_items(items: list[dict[str, Any]] | None) -> str:
    parts: list[str] = []
    for item in items or []:
        item_type = item.get("type")
        if item_type == MESSAGE_ITEM_TEXT:
            text = ((item.get("text_item") or {}).get("text") or "").strip()
            if text:
                parts.append(text)
        elif item_type == MESSAGE_ITEM_VOICE:
            text = ((item.get("voice_item") or {}).get("text") or "").strip()
            parts.append(text or "[语音消息]")
        elif item_type == MESSAGE_ITEM_IMAGE:
            parts.append("[图片消息]")
        elif item_type == MESSAGE_ITEM_FILE:
            file_item = item.get("file_item") or {}
            name = file_item.get("file_name") or "file"
            parts.append(f"[文件消息: {name}]")
        elif item_type == MESSAGE_ITEM_VIDEO:
            parts.append("[视频消息]")
    return "\n".join(part for part in parts if part)


class OpenClawWeixinAdapter(ChannelAdapter):
    """Direct Weixin backend adapter using OpenClaw plugin login credentials."""

    channel = "openclaw-weixin"

    def __init__(self):
        super().__init__()
        self._state_dir = _expand_path(os.getenv("OPENCLAW_WEIXIN_STATE_DIR", DEFAULT_STATE_DIR))
        self._account_id = os.getenv("OPENCLAW_WEIXIN_ACCOUNT_ID", "").strip()
        self._username = os.getenv("OPENCLAW_WEIXIN_USERNAME", "default").strip() or "default"
        self._default_allow = os.getenv("OPENCLAW_WEIXIN_DEFAULT_ALLOW", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._poll_timeout_ms = int(os.getenv("OPENCLAW_WEIXIN_POLL_TIMEOUT_MS", "35000"))
        self._api_timeout_ms = int(os.getenv("OPENCLAW_WEIXIN_API_TIMEOUT_MS", "15000"))
        self._idle_sleep_ms = int(os.getenv("OPENCLAW_WEIXIN_IDLE_SLEEP_MS", "1000"))
        self._bot_agent = os.getenv("OPENCLAW_WEIXIN_BOT_AGENT", "ClawCross/0.1.0").strip() or "ClawCross/0.1.0"
        self._package_json = _expand_path(os.getenv("OPENCLAW_WEIXIN_PACKAGE_JSON", DEFAULT_PACKAGE_JSON))
        self._package_name = "@tencent-weixin/openclaw-weixin"
        self._channel_version = "unknown"
        self._ilink_app_id = "bot"
        self._ilink_client_version = _client_version("0.0.0")
        self._load_package_metadata()

    def _load_package_metadata(self) -> None:
        try:
            with self._package_json.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        self._package_name = str(data.get("name") or self._package_name)
        self._channel_version = str(data.get("version") or self._channel_version)
        self._ilink_app_id = str(data.get("ilink_appid") or self._ilink_app_id)
        self._ilink_client_version = _client_version(self._channel_version)

    def _accounts_index_path(self) -> Path:
        return self._state_dir / "accounts.json"

    def _accounts_dir(self) -> Path:
        return self._state_dir / "accounts"

    def _account_path(self, account_id: str) -> Path:
        return self._accounts_dir() / f"{account_id}.json"

    def _sync_path(self, account_id: str) -> Path:
        return self._accounts_dir() / f"{account_id}.sync.json"

    def _load_account_ids(self) -> list[str]:
        if self._account_id:
            return [self._account_id]
        try:
            with self._accounts_index_path().open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [str(item) for item in data if str(item).strip()]
        except Exception:
            pass
        return []

    def _load_account(self) -> WeixinAccount | None:
        for account_id in self._load_account_ids():
            try:
                with self._account_path(account_id).open("r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as exc:
                logger.warning("读取 OpenClaw Weixin account 失败 account=%s: %s", account_id, exc)
                continue
            token = str(data.get("token") or "").strip()
            if not token:
                continue
            base_url = str(data.get("baseUrl") or data.get("base_url") or DEFAULT_BASE_URL).strip()
            return WeixinAccount(
                account_id=account_id,
                token=token,
                base_url=base_url or DEFAULT_BASE_URL,
                user_id=str(data.get("userId") or data.get("user_id") or "").strip() or None,
            )
        return None

    def _load_sync_buf(self, account_id: str) -> str:
        try:
            with self._sync_path(account_id).open("r", encoding="utf-8") as f:
                data = json.load(f)
            return str(data.get("get_updates_buf") or data.get("sync_buf") or "")
        except Exception:
            return ""

    def _save_sync_buf(self, account_id: str, value: str) -> None:
        path = self._sync_path(account_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump({"get_updates_buf": value}, f, ensure_ascii=False, indent=2)
            f.write("\n")
        tmp.replace(path)

    def _base_info(self) -> dict[str, str]:
        return {
            "channel_version": self._channel_version,
            "bot_agent": self._bot_agent,
        }

    def _headers(self, account: WeixinAccount) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {account.token}",
            "X-WECHAT-UIN": _wechat_uin_header(),
            "iLink-App-Id": self._ilink_app_id,
            "iLink-App-ClientVersion": str(self._ilink_client_version),
        }

    async def _post_json(
        self,
        account: WeixinAccount,
        endpoint: str,
        payload: dict[str, Any],
        *,
        timeout_ms: int,
    ) -> dict[str, Any]:
        base = account.base_url.rstrip("/") + "/"
        url = base + endpoint.lstrip("/")
        body = {**payload, "base_info": self._base_info()}
        timeout_s = max(1.0, timeout_ms / 1000.0)
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(url, headers=self._headers(account), json=body)
        resp.raise_for_status()
        if not resp.text.strip():
            return {}
        return resp.json()

    async def _get_updates(self, account: WeixinAccount, sync_buf: str) -> dict[str, Any]:
        try:
            return await self._post_json(
                account,
                "ilink/bot/getupdates",
                {"get_updates_buf": sync_buf},
                timeout_ms=self._poll_timeout_ms + 5000,
            )
        except (httpx.TimeoutException, httpx.ReadTimeout):
            return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}

    async def _send_text(self, account: WeixinAccount, to_user_id: str, text: str, context_token: str | None) -> None:
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"clawcross-{int(time.time() * 1000)}",
                "message_type": 2,
                "message_state": 2,
                "item_list": [{"type": MESSAGE_ITEM_TEXT, "text_item": {"text": text}}],
                "context_token": context_token or None,
            }
        }
        await self._post_json(
            account,
            "ilink/bot/sendmessage",
            payload,
            timeout_ms=self._api_timeout_ms,
        )

    async def verify_permission(self, raw_message: Any) -> tuple[bool, str | None]:
        from_user_id = str((raw_message or {}).get("from_user_id") or "")
        entry = self._find_whitelist_entry(from_user_id, None, channel=self.channel)
        if entry:
            return True, entry.get("username") or self._username
        if self._default_allow:
            return True, self._username
        return False, None

    async def build_content(self, raw_message: Any) -> list[dict]:
        text = _extract_text_from_items((raw_message or {}).get("item_list"))
        if not text:
            text = json.dumps(raw_message, ensure_ascii=False)
        return [{"type": "text", "text": text}]

    async def handle_message(self, raw_message: Any) -> str | None:
        if int((raw_message or {}).get("message_type") or 0) != MESSAGE_TYPE_USER:
            return None
        allowed, username = await self.verify_permission(raw_message)
        if not allowed or not username:
            logger.info("OpenClaw Weixin sender not allowed: %s", (raw_message or {}).get("from_user_id"))
            return None

        content_list = await self.build_content(raw_message)
        text = self.extract_text(content_list)
        from_user_id = str(raw_message.get("from_user_id") or "")

        if self.is_front_command(text):
            link = await self.generate_magic_link(username)
            return self.format_cross_reply(link)

        handled, cli_reply = await self.handle_cli_mode(
            text=text,
            channel=self.channel,
            user_id=from_user_id,
            username=username,
        )
        if handled:
            return cli_reply or ""

        api_key = self.build_api_key(username)
        result = await self.call_ai(content_list, api_key)
        return result.content if result.ok else f"发生错误: {result.error}"

    async def run(self) -> None:
        if not self._internal_token:
            logger.error("INTERNAL_TOKEN 未配置，OpenClaw Weixin 无法以用户身份调 agent")
            return

        account = self._load_account()
        if not account:
            logger.error(
                "未找到 OpenClaw Weixin 登录账号。先运行: openclaw channels login --channel openclaw-weixin"
            )
            return

        logger.info(
            "启动 OpenClaw Weixin -> ClawCross 通道 account=%s base=%s user=%s",
            account.account_id,
            account.base_url,
            self._username,
        )

        sync_buf = self._load_sync_buf(account.account_id)
        while True:
            try:
                resp = await self._get_updates(account, sync_buf)
                ret = int(resp.get("ret") or resp.get("errcode") or 0)
                if ret != 0:
                    logger.warning("OpenClaw Weixin getupdates failed: %s", resp)
                    await asyncio.sleep(5)
                    continue

                next_buf = resp.get("get_updates_buf")
                if isinstance(next_buf, str) and next_buf:
                    sync_buf = next_buf
                    self._save_sync_buf(account.account_id, sync_buf)

                for msg in resp.get("msgs") or []:
                    if not isinstance(msg, dict):
                        continue
                    reply = await self.handle_message(msg)
                    if not reply:
                        continue
                    to_user_id = str(msg.get("from_user_id") or "")
                    if not to_user_id:
                        continue
                    await self._send_text(account, to_user_id, reply, msg.get("context_token"))

                await asyncio.sleep(max(0, self._idle_sleep_ms) / 1000.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("OpenClaw Weixin loop error: %s", exc, exc_info=True)
                await asyncio.sleep(5)
