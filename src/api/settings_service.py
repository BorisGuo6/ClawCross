"""
设置管理服务模块

提供系统配置的管理功能：
- 读取/更新白名单配置
- 读取/更新完整配置（含敏感信息自动脱敏）
- 重启服务信号
"""

import os
from typing import Callable

from fastapi import HTTPException

from utils.env_settings import (
    SETTINGS_WHITELIST,
    filter_updates_skip_mask,
    filter_whitelisted_updates,
    mask_all_sensitive,
    mask_sensitive,
    read_env_all,
    read_env_settings,
    write_env_settings,
)
from api.settings_models import SettingsUpdateRequest


class SettingsService:
    def __init__(
        self,
        *,
        env_path: str,
        verify_auth_or_token: Callable[[str, str, str | None], None],
    ):
        self.env_path = env_path
        self.verify_auth_or_token = verify_auth_or_token
        self.restart_flag = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".restart_flag")

    async def get_settings(self, user_id: str, password: str, x_internal_token: str | None):
        self.verify_auth_or_token(user_id, password, x_internal_token)
        raw = read_env_settings(self.env_path, SETTINGS_WHITELIST)
        masked = mask_sensitive(raw)
        return {"status": "success", "settings": masked}

    async def update_settings(self, req: SettingsUpdateRequest, x_internal_token: str | None):
        self.verify_auth_or_token(req.user_id, req.password, x_internal_token)
        filtered = filter_whitelisted_updates(req.settings, SETTINGS_WHITELIST)

        if filtered:
            write_env_settings(self.env_path, filtered)
            return {
                "status": "success",
                "updated": list(filtered.keys()),
            }

        return {"status": "success", "updated": []}

    async def get_settings_full(self, user_id: str, password: str, x_internal_token: str | None):
        self.verify_auth_or_token(user_id, password, x_internal_token)
        raw = read_env_all(self.env_path)
        masked = mask_all_sensitive(raw)
        return {"status": "success", "settings": masked}

    async def update_settings_full(self, req: SettingsUpdateRequest, x_internal_token: str | None):
        self.verify_auth_or_token(req.user_id, req.password, x_internal_token)
        updates = filter_updates_skip_mask(req.settings)

        if updates:
            write_env_settings(self.env_path, updates)
            return {
                "status": "success",
                "updated": list(updates.keys()),
            }

        return {"status": "success", "updated": []}

    async def restart_services(self, req: SettingsUpdateRequest, x_internal_token: str | None):
        self.verify_auth_or_token(req.user_id, req.password, x_internal_token)
        try:
            with open(self.restart_flag, "w") as f:
                f.write("restart")
            return {"status": "success", "message": "重启信号已发送，服务将在数秒内重启"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"写入重启信号失败: {e}")
