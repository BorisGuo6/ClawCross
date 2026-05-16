"""Shared chatbot channel catalog loader.

The source of truth is ``config/chatbot_channels.json`` so the mobile UI,
settings API, and CLI all describe the same channel-specific fields.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    from utils.runtime_paths import PROJECT_ROOT
except ModuleNotFoundError:  # CLI imports this module as src.utils.*
    from src.utils.runtime_paths import PROJECT_ROOT


CATALOG_PATH = PROJECT_ROOT / "config" / "chatbot_channels.json"


@lru_cache(maxsize=1)
def load_chatbot_channel_catalog() -> dict[str, Any]:
    try:
        with open(CATALOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    channels = data.get("channels")
    if not isinstance(channels, list):
        channels = []
    common_keys = data.get("common_keys")
    if not isinstance(common_keys, list):
        common_keys = ["NONEBOT_ADAPTERS", "NONEBOT_HOST", "NONEBOT_PORT", "WHITELIST_FILE"]
    return {
        "common_keys": [str(k) for k in common_keys if k],
        "channels": [ch for ch in channels if isinstance(ch, dict) and ch.get("id")],
    }


def get_chatbot_channels() -> list[dict[str, Any]]:
    return list(load_chatbot_channel_catalog()["channels"])


def get_chatbot_common_keys() -> list[str]:
    return list(load_chatbot_channel_catalog()["common_keys"])


def get_chatbot_env_keys() -> list[str]:
    keys: set[str] = set(get_chatbot_common_keys())
    for channel in get_chatbot_channels():
        env_key = str(channel.get("env_key") or "").strip()
        if env_key:
            keys.add(env_key)
        for field in channel.get("fields") or []:
            if not isinstance(field, dict):
                continue
            target = str(field.get("target") or "").strip()
            key = str(field.get("env_key") or field.get("name") or "").strip()
            if target == "env" and key:
                keys.add(key)
    return sorted(keys)
