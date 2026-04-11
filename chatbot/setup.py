#!/usr/bin/env python3
"""
Chatbot 设置与启动器

功能说明：
- 交互式配置 .env 中的 Telegram Bot / QQ Bot 信息
- 启动聊天机器人进程
- 查看当前配置状态
"""

import os
import subprocess
import sys

# 目录路径
CHATBOT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CHATBOT_DIR)
ENV_FILE = os.path.join(PROJECT_ROOT, "config", ".env")

# 虚拟环境 Python 解释器路径
if sys.platform == "win32":
    VENV_PYTHON = os.path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")
else:
    VENV_PYTHON = os.path.join(PROJECT_ROOT, ".venv", "bin", "python")

# Telegram Bot 配置项
ENV_KEYS_TELEGRAM = {
    "TELEGRAM_BOT_TOKEN": ("Telegram Bot Token（从 @BotFather 获取）", ""),
    "AI_MODEL_TG": ("Telegram Bot 使用的 AI 模型（留空则复用 LLM_MODEL）", ""),
}

# QQ Bot 配置项
ENV_KEYS_QQ = {
    "QQ_APP_ID": ("QQ Bot AppID（QQ 开放平台获取）", ""),
    "QQ_BOT_SECRET": ("QQ Bot Secret", ""),
    "QQ_BOT_USERNAME": ("QQ Bot 以哪个系统用户身份调用 Agent", "qquser"),
    "AI_MODEL_QQ": ("QQ Bot 使用的 AI 模型（留空则复用 LLM_MODEL）", ""),
}

# 通用配置项
ENV_KEYS_COMMON = {
    "AI_API_URL": ("Agent OpenAI 兼容接口地址（留空则按 PORT_AGENT 推导）", ""),
}


def read_env_file() -> dict[str, str]:
    """
    读取 .env 文件为字典

    Returns:
        环境变量名到值的映射字典
    """
    env_vars = {}
    if not os.path.exists(ENV_FILE):
        return env_vars

    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip()
    return env_vars


def read_env_lines() -> list[str]:
    """读取 .env 文件的所有原始行"""
    if not os.path.exists(ENV_FILE):
        return []
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        return f.readlines()


def update_env_key(key: str, value: str) -> None:
    """
    更新 .env 文件中的指定配置项

    Args:
        key: 环境变量名
        value: 新的值
    """
    lines = read_env_lines()
    key_found = False
    updated_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            if k.strip() == key:
                updated_lines.append(f"{key}={value}\n")
                key_found = True
                continue
        updated_lines.append(line)

    if not key_found:
        updated_lines.append(f"{key}={value}\n")

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(updated_lines)


def mask_sensitive_value(value: str, visible_chars: int = 6) -> str:
    """
    遮掩敏感值，只显示前几个字符

    Args:
        value: 原始值
        visible_chars: 显示的字符数

    Returns:
        遮掩后的字符串
    """
    if not value or len(value) <= visible_chars:
        return value or "(空)"
    return value[:visible_chars] + "****"


def configure_env_group(title: str, env_keys: dict[str, tuple[str, str]]) -> None:
    """
    交互式配置一组 .env 变量

    Args:
        title: 配置组标题
        env_keys: 环境变量配置字典 {变量名: (描述, 默认值)}
    """
    env_vars = read_env_file()
    print(f"\n{'=' * 40}")
    print(f"  {title}")
    print(f"{'=' * 40}")

    for key, (description, default_value) in env_keys.items():
        current_value = env_vars.get(key, "")
        # 判断是否为有效值（非 placeholder）
        is_placeholder = current_value.startswith("your_") or not current_value
        display_value = mask_sensitive_value(current_value) if current_value and not is_placeholder else "(未设置)"

        print(f"\n  {description}")
        print(f"  环境变量: {key}")
        print(f"  当前值:   {display_value}")

        if default_value:
            prompt = f"  输入新值（回车保留当前, 'd' 使用默认 {default_value}）: "
        else:
            prompt = "  输入新值（回车保留当前）: "

        new_value = input(prompt).strip()

        if new_value == "d" and default_value:
            new_value = default_value
        elif not new_value:
            if is_placeholder and not current_value:
                print(f"  跳过（仍未设置）")
            else:
                print(f"  保留当前值")
            continue

        update_env_key(key, new_value)
        print(f"  已更新 {key}")


def show_current_config() -> None:
    """显示当前 chatbot 相关配置"""
    env_vars = read_env_file()
    print(f"\n{'=' * 40}")
    print("  当前 Chatbot 配置概览")
    print(f"{'=' * 40}")

    config_sections = [
        ("Telegram Bot", ENV_KEYS_TELEGRAM),
        ("QQ Bot", ENV_KEYS_QQ),
        ("通用配置", ENV_KEYS_COMMON),
    ]

    for section_name, keys in config_sections:
        print(f"\n  [{section_name}]")
        for key, (description, _) in keys.items():
            value = env_vars.get(key, "")
            is_sensitive = "token" in key.lower() or "secret" in key.lower() or "key" in key.lower()
            display_value = mask_sensitive_value(value) if is_sensitive else value or "(未设置)"
            print(f"    {key} = {display_value}")

    # 显示白名单信息
    whitelist_path = os.path.join(PROJECT_ROOT, "data", "telegram_whitelist.json")
    if os.path.exists(whitelist_path):
        import json
        with open(whitelist_path, "r", encoding="utf-8") as f:
            whitelist_data = json.load(f)
        user_count = len(whitelist_data.get("allowed", []))
        print(f"\n  [Telegram 白名单]")
        print(f"    允许用户数: {user_count}")
        for entry in whitelist_data.get("allowed", []):
            print(f"    - {entry.get('username', '?')} → chat_id: {entry.get('chat_id', '?')}")
    else:
        print(f"\n  [Telegram 白名单]")
        print(f"    白名单文件不存在（将在 Agent 设置 chat_id 时自动创建）")


def launch_bots() -> None:
    """启动聊天机器人进程"""
    # 检查虚拟环境
    if not os.path.exists(VENV_PYTHON):
        print(f"错误: 未找到虚拟环境 {VENV_PYTHON}")
        return

    print("\n" + "-" * 30)
    print("选择要启动的机器人：")
    print("1. QQ 机器人 (QQbot.py)")
    print("2. Telegram 机器人 (telegrambot.py)")
    print("3. 全部启动")
    print("4. 跳过")

    choice = input("\n请选择 (1/2/3/4): ").strip()

    # 确保日志目录存在
    log_dir = os.path.join(CHATBOT_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)

    if choice == "1":
        print("\n启动 QQ 机器人...")
        qq_log = open(os.path.join(log_dir, "qqbot.log"), "a", encoding="utf-8")
        subprocess.Popen(
            [VENV_PYTHON, os.path.join(CHATBOT_DIR, "QQbot.py")],
            stdout=qq_log, stderr=qq_log,
        )
        print("日志: chatbot/logs/qqbot.log")

    elif choice == "2":
        print("\n启动 Telegram 机器人...")
        tg_log = open(os.path.join(log_dir, "telegrambot.log"), "a", encoding="utf-8")
        subprocess.Popen(
            [VENV_PYTHON, os.path.join(CHATBOT_DIR, "telegrambot.py")],
            stdout=tg_log, stderr=tg_log,
        )
        print("日志: chatbot/logs/telegrambot.log")

    elif choice == "3":
        print("\n启动所有机器人...")
        qq_log = open(os.path.join(log_dir, "qqbot.log"), "a", encoding="utf-8")
        tg_log = open(os.path.join(log_dir, "telegrambot.log"), "a", encoding="utf-8")
        subprocess.Popen(
            [VENV_PYTHON, os.path.join(CHATBOT_DIR, "QQbot.py")],
            stdout=qq_log, stderr=qq_log,
        )
        subprocess.Popen(
            [VENV_PYTHON, os.path.join(CHATBOT_DIR, "telegrambot.py")],
            stdout=tg_log, stderr=tg_log,
        )
        print("日志: chatbot/logs/qqbot.log, chatbot/logs/telegrambot.log")

    else:
        print("\n跳过启动。")

    print("-" * 30)


def main() -> None:
    """主入口函数"""
    # Headless 模式：跳过交互菜单（避免后台运行时出现 EOFError）
    if os.getenv("WEBOT_HEADLESS", "0") == "1":
        print("=== Chatbot 设置与启动器 (headless 模式) ===")
        return

    print("=== Chatbot 设置与启动器 ===")

    # 检查 .env 文件是否存在
    if not os.path.exists(ENV_FILE):
        print(f"错误: .env 配置文件不存在: {ENV_FILE}")
        print("请先从 config/.env.example 复制为 config/.env")
        return

    while True:
        print("\n" + "=" * 40)
        print("  主菜单")
        print("=" * 40)
        print("  1. 配置 Telegram Bot")
        print("  2. 配置 QQ Bot")
        print("  3. 配置通用设置")
        print("  4. 查看当前配置")
        print("  5. 启动机器人")
        print("  0. 退出")

        choice = input("\n请选择 (0-5): ").strip()

        if choice == "1":
            configure_env_group("Telegram Bot 配置", ENV_KEYS_TELEGRAM)
        elif choice == "2":
            configure_env_group("QQ Bot 配置", ENV_KEYS_QQ)
        elif choice == "3":
            configure_env_group("通用配置", ENV_KEYS_COMMON)
        elif choice == "4":
            show_current_config()
        elif choice == "5":
            launch_bots()
        elif choice == "0":
            print("\n再见！")
            break
        else:
            print("无效选择，请重新输入。")


if __name__ == "__main__":
    main()
