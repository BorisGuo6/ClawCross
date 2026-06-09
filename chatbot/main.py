"""
Chatbot 入口 - 多渠道机器人管理器

用法：
    python chatbot/main.py             # 启动所有已配置的渠道
    python chatbot/main.py --list      # 列出所有渠道状态
    python chatbot/main.py --webhook   # 只启动通用 Webhook
    python chatbot/main.py --nonebot   # 只启动 NoneBot 桥接
    python chatbot/main.py --clawcross_wechat    # 只启动 ClawCross WeChat 微信桥
    python chatbot/main.py --openclaw-weixin  # 只启动 OpenClaw Weixin 登录态桥
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

_chatbot_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_chatbot_dir)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("chatbot.main")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


async def run_channel(channel_name: str):
    from adapters import WebhookAdapter, NoneBotBridgeAdapter, ClawCrossWeChatAdapter, OpenClawWeixinAdapter

    adapters = {
        "webhook": WebhookAdapter,
        "nonebot": NoneBotBridgeAdapter,
        "clawcross_wechat": ClawCrossWeChatAdapter,
        "openclaw-weixin": OpenClawWeixinAdapter,
    }

    if channel_name not in adapters:
        logger.error(f"未知渠道: {channel_name}")
        return

    adapter = adapters[channel_name]()

    checks = {
        "webhook": lambda a: True,
        "nonebot": lambda a: bool(a._adapter_names),
        "clawcross_wechat": lambda a: bool(a._internal_token),
        "openclaw-weixin": lambda a: bool(a._internal_token),
    }

    if not checks[channel_name](adapter):
        logger.error(f"{channel_name} 配置不完整")
        return

    logger.info(f"启动 {channel_name} 渠道...")
    await adapter.run()


async def run_all():
    from adapters import create_manager_from_env

    manager = create_manager_from_env()

    enabled = [(s, a) for s, a in manager._adapters if s == "enabled"]
    if not enabled:
        logger.warning("没有已启用的渠道")
        return

    logger.info(f"启动 {len(enabled)} 个渠道...")
    await manager.run_all()


def list_channels():
    from adapters import create_manager_from_env

    manager = create_manager_from_env()

    print("=== Chatbot 渠道状态 ===\n")

    if not manager._adapters:
        print("(无已配置的渠道)")
        print()
        print("配置方法:")
        print("  - NoneBot 桥接: 在 .env 设置 NONEBOT_ADAPTERS=telegram,qq,discord,...")
        print("  - 通用 Webhook: 在 .env 设置 <NAME>_WEBHOOK_URL=https://...")
        print("  - ClawCross WeChat 微信:  在 .env 设置 CLAWCROSS_WECHAT_ENABLED=true (需先安装 clawcross_wechat 二进制)")
        print("  - OpenClaw Weixin: 在 .env 设置 OPENCLAW_WEIXIN_ENABLED=true (需先扫码登录 openclaw-weixin)")
        return

    for status, adapter in manager._adapters:
        icon = "[on]" if status == "enabled" else "[off]"
        print(f"{icon} {adapter.channel}")

        if adapter.channel == "webhook":
            print(f"   Webhook URL: {'已配置' if adapter._webhook_url else '未配置'}")
        elif adapter.channel == "nonebot":
            ns = ", ".join(adapter._adapter_names) if adapter._adapter_names else "(无)"
            print(f"   NoneBot 适配器: {ns}")
            print(f"   监听: {adapter._host}:{adapter._port}")
        elif adapter.channel == "clawcross_wechat":
            print(f"   clawcross_wechat 二进制: {adapter._bin}")
            print(f"   配置文件: {adapter._config_path}")
            print(f"   username: {adapter._username}")
        elif adapter.channel == "openclaw-weixin":
            print(f"   state dir: {adapter._state_dir}")
            print(f"   account: {adapter._account_id or '(accounts.json first)'}")
            print(f"   username: {adapter._username}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Chatbot 多渠道机器人")
    parser.add_argument("--list", action="store_true", help="列出渠道状态")
    parser.add_argument("--webhook", action="store_true", help="只启动通用 Webhook")
    parser.add_argument("--nonebot", action="store_true", help="只启动 NoneBot 桥接")
    parser.add_argument("--clawcross_wechat", action="store_true", help="只启动 ClawCross WeChat 微信桥")
    parser.add_argument("--openclaw-weixin", action="store_true", help="只启动 OpenClaw Weixin 登录态桥")
    args = parser.parse_args()

    if args.list:
        list_channels()
        return

    if args.webhook:
        asyncio.run(run_channel("webhook"))
    elif args.nonebot:
        asyncio.run(run_channel("nonebot"))
    elif args.clawcross_wechat:
        asyncio.run(run_channel("clawcross_wechat"))
    elif args.openclaw_weixin:
        asyncio.run(run_channel("openclaw-weixin"))
    else:
        asyncio.run(run_all())


if __name__ == "__main__":
    main()
