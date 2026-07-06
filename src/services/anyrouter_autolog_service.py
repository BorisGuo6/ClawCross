"""AnyRouter / NewAPI check-in support for ClawCross.

The upstream anyrouter-autolog project is GitHub Actions oriented. This module
keeps the same request model but makes it safe to call from the local Web UI.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx

from utils.runtime_paths import USER_FILES_DIR

CONFIG_FILENAME = "anyrouter_autolog.json"
QUOTA_UNIT = 500000


@dataclass
class ProviderConfig:
    name: str
    domain: str
    login_path: str = "/login"
    sign_in_path: str | None = "/api/user/sign_in"
    user_info_path: str = "/api/user/self"
    api_user_key: str = "new-api-user"
    waf_cookie_names: list[str] | None = None

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "ProviderConfig":
        domain = str(data.get("domain") or "").strip().rstrip("/")
        if not domain:
            raise ValueError(f'provider "{name}" missing domain')
        if not domain.startswith(("http://", "https://")):
            domain = "https://" + domain
        sign_in_path = data.get("sign_in_path", "/api/user/sign_in")
        if sign_in_path in ("", None, False):
            sign_in_path = None
        waf_cookie_names = data.get("waf_cookie_names") or []
        if not isinstance(waf_cookie_names, list):
            waf_cookie_names = []
        return cls(
            name=name,
            domain=domain,
            login_path=str(data.get("login_path") or "/login"),
            sign_in_path=str(sign_in_path) if sign_in_path is not None else None,
            user_info_path=str(data.get("user_info_path") or "/api/user/self"),
            api_user_key=str(data.get("api_user_key") or "new-api-user"),
            waf_cookie_names=[str(item).strip() for item in waf_cookie_names if str(item).strip()],
        )

    def manual_check_in(self) -> bool:
        return bool(self.sign_in_path)


@dataclass
class AccountConfig:
    cookies: dict[str, str] | str
    api_user: str
    provider: str = "anyrouter"
    name: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], index: int) -> "AccountConfig":
        if not isinstance(data, dict):
            raise ValueError(f"account {index + 1} must be an object")
        if "cookies" not in data:
            raise ValueError(f"account {index + 1} missing cookies")
        api_user = str(data.get("api_user") or "").strip()
        if not api_user:
            raise ValueError(f"account {index + 1} missing api_user")
        name = str(data.get("name") or "").strip() or None
        return cls(
            cookies=data["cookies"],
            api_user=api_user,
            provider=str(data.get("provider") or "anyrouter").strip() or "anyrouter",
            name=name,
        )

    def display_name(self, index: int) -> str:
        return self.name or f"Account {index + 1}"


DEFAULT_PROVIDERS: dict[str, ProviderConfig] = {
    "anyrouter": ProviderConfig(
        name="anyrouter",
        domain="https://anyrouter.top",
        login_path="/login",
        sign_in_path="/api/user/sign_in",
        user_info_path="/api/user/self",
        api_user_key="new-api-user",
        waf_cookie_names=["acw_tc", "cdn_sec_tc", "acw_sc__v2"],
    ),
    "agentrouter": ProviderConfig(
        name="agentrouter",
        domain="https://agentrouter.org",
        login_path="/login",
        sign_in_path=None,
        user_info_path="/api/user/self",
        api_user_key="new-api-user",
        waf_cookie_names=["acw_tc"],
    ),
}


def _safe_owner(owner_id: str) -> str:
    value = str(owner_id or "").strip()
    if not value:
        value = "default"
    return re.sub(r"[^A-Za-z0-9_.@-]+", "_", value)[:80] or "default"


def config_path_for_user(owner_id: str) -> Path:
    return USER_FILES_DIR / _safe_owner(owner_id) / CONFIG_FILENAME


def load_saved_config(owner_id: str) -> dict[str, Any]:
    path = config_path_for_user(owner_id)
    if not path.exists():
        return {"accounts": [], "providers": {}, "updated_at": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"accounts": [], "providers": {}, "updated_at": None, "error": f"Failed to read config: {exc}"}
    if not isinstance(data, dict):
        return {"accounts": [], "providers": {}, "updated_at": None, "error": "Saved config is not an object"}
    data.setdefault("accounts", [])
    data.setdefault("providers", {})
    return data


def save_config(owner_id: str, config: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_config(config)
    path = config_path_for_user(owner_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "accounts": normalized["raw_accounts"],
        "providers": normalized["raw_providers"],
        "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except Exception:
        pass
    return payload


def mask_secret(value: Any) -> str:
    text = str(value or "")
    if len(text) <= 8:
        return "*" * len(text) if text else ""
    return text[:4] + "****" + text[-4:]


def masked_config(config: dict[str, Any]) -> dict[str, Any]:
    accounts = config.get("accounts") if isinstance(config.get("accounts"), list) else []
    masked_accounts = []
    for index, raw in enumerate(accounts):
        item = dict(raw) if isinstance(raw, dict) else {}
        cookies = parse_cookies(item.get("cookies", {}))
        item["cookies"] = {key: mask_secret(value) for key, value in cookies.items()}
        item["api_user"] = mask_secret(item.get("api_user", ""))
        item.setdefault("name", f"Account {index + 1}")
        item.setdefault("provider", "anyrouter")
        masked_accounts.append(item)
    return {
        "accounts": masked_accounts,
        "providers": config.get("providers") if isinstance(config.get("providers"), dict) else {},
        "updated_at": config.get("updated_at"),
        "error": config.get("error"),
    }


def parse_cookies(cookies_data: Any) -> dict[str, str]:
    if isinstance(cookies_data, dict):
        return {str(key): str(value) for key, value in cookies_data.items() if str(key)}
    if isinstance(cookies_data, str):
        result: dict[str, str] = {}
        for part in cookies_data.split(";"):
            if "=" in part:
                key, value = part.strip().split("=", 1)
                if key:
                    result[key] = value
        return result
    return {}


def normalize_provider_name(name: str, providers: dict[str, ProviderConfig]) -> str:
    raw = str(name or "anyrouter").strip().lower().rstrip("/")
    raw_no_proto = raw
    for prefix in ("https://", "http://"):
        if raw_no_proto.startswith(prefix):
            raw_no_proto = raw_no_proto[len(prefix):]
            break
    if raw in providers:
        return raw
    for provider_name, provider in providers.items():
        domain = provider.domain.lower().rstrip("/")
        domain_no_proto = domain
        for prefix in ("https://", "http://"):
            if domain_no_proto.startswith(prefix):
                domain_no_proto = domain_no_proto[len(prefix):]
                break
        if raw in {provider_name.lower(), domain, domain_no_proto} or raw_no_proto in {domain, domain_no_proto}:
            return provider_name
    return raw


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    raw_accounts = config.get("accounts", [])
    raw_providers = config.get("providers", {})
    if not isinstance(raw_accounts, list):
        raise ValueError("accounts must be an array")
    if not isinstance(raw_providers, dict):
        raise ValueError("providers must be an object")

    providers = dict(DEFAULT_PROVIDERS)
    for name, raw in raw_providers.items():
        if not isinstance(raw, dict):
            raise ValueError(f'provider "{name}" must be an object')
        providers[str(name)] = ProviderConfig.from_dict(str(name), raw)

    accounts = [AccountConfig.from_dict(item, index) for index, item in enumerate(raw_accounts)]
    for account in accounts:
        account.provider = normalize_provider_name(account.provider, providers)

    return {
        "accounts": accounts,
        "providers": providers,
        "raw_accounts": raw_accounts,
        "raw_providers": raw_providers,
    }


def _headers(provider: ProviderConfig, account: AccountConfig) -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": provider.domain,
        "Origin": provider.domain,
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        provider.api_user_key: account.api_user,
    }


def _user_info(client: httpx.Client, provider: ProviderConfig, headers: dict[str, str]) -> dict[str, Any]:
    url = f"{provider.domain}{provider.user_info_path}"
    response = client.get(url, headers=headers)
    if response.status_code != 200:
        return {"success": False, "error": f"HTTP {response.status_code}", "status_code": response.status_code}
    try:
        payload = response.json()
    except Exception:
        return {"success": False, "error": "Invalid JSON response", "status_code": response.status_code}
    if not payload.get("success"):
        return {
            "success": False,
            "error": str(payload.get("message") or payload.get("msg") or "User info request failed"),
            "status_code": response.status_code,
        }
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    quota = round(float(data.get("quota", 0) or 0) / QUOTA_UNIT, 2)
    used_quota = round(float(data.get("used_quota", 0) or 0) / QUOTA_UNIT, 2)
    return {"success": True, "quota": quota, "used_quota": used_quota, "status_code": response.status_code}


def _execute_check_in(client: httpx.Client, provider: ProviderConfig, headers: dict[str, str]) -> tuple[bool, str]:
    if not provider.sign_in_path:
        return True, "user_info_triggered"
    url = f"{provider.domain}{provider.sign_in_path}"
    checkin_headers = dict(headers)
    checkin_headers.update({"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"})
    response = client.post(url, headers=checkin_headers)
    if response.status_code != 200:
        return False, f"HTTP {response.status_code}"
    try:
        payload = response.json()
    except Exception:
        text = response.text.lower()
        return ("success" in text), "non_json_success" if "success" in text else "Invalid JSON response"
    if payload.get("ret") == 1 or payload.get("code") == 0 or payload.get("success"):
        return True, str(payload.get("msg") or payload.get("message") or "check_in_ok")
    message = str(payload.get("msg") or payload.get("message") or "Unknown error")
    already_keywords = ("已经签到", "已签到", "重复签到", "already checked", "already signed")
    if any(keyword in message.lower() for keyword in already_keywords):
        return True, message
    return False, message


def _account_result(
    *,
    index: int,
    account: AccountConfig,
    provider: ProviderConfig,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    success: bool,
    message: str,
    missing_waf_cookies: list[str],
) -> dict[str, Any]:
    before_ok = before if before and before.get("success") else None
    after_ok = after if after and after.get("success") else None
    detail: dict[str, Any] = {
        "name": account.display_name(index),
        "provider": provider.name,
        "domain": provider.domain,
        "success": success,
        "message": message,
        "missing_waf_cookies": missing_waf_cookies,
        "before": before,
        "after": after,
    }
    if before_ok and after_ok:
        before_quota = float(before_ok["quota"])
        before_used = float(before_ok["used_quota"])
        after_quota = float(after_ok["quota"])
        after_used = float(after_ok["used_quota"])
        detail["delta"] = {
            "check_in_reward": round((after_quota + after_used) - (before_quota + before_used), 2),
            "usage_increase": round(after_used - before_used, 2),
            "balance_change": round(after_quota - before_quota, 2),
        }
    return detail


def run_check_in(
    config: dict[str, Any],
    *,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> dict[str, Any]:
    normalized = normalize_config(config)
    accounts: list[AccountConfig] = normalized["accounts"]
    providers: dict[str, ProviderConfig] = normalized["providers"]
    if not accounts:
        raise ValueError("no accounts configured")

    results: list[dict[str, Any]] = []
    success_count = 0
    for index, account in enumerate(accounts):
        provider = providers.get(account.provider)
        if not provider:
            results.append({
                "name": account.display_name(index),
                "provider": account.provider,
                "success": False,
                "message": f'Provider "{account.provider}" not found',
            })
            continue
        cookies = parse_cookies(account.cookies)
        missing_waf = [name for name in (provider.waf_cookie_names or []) if name not in cookies]
        if not cookies:
            results.append(_account_result(
                index=index,
                account=account,
                provider=provider,
                before=None,
                after=None,
                success=False,
                message="Invalid or empty cookies",
                missing_waf_cookies=missing_waf,
            ))
            continue

        client = client_factory() if client_factory else httpx.Client(http2=True, timeout=30.0)
        try:
            client.cookies.update(cookies)
            headers = _headers(provider, account)
            before = _user_info(client, provider, headers)
            success, message = _execute_check_in(client, provider, headers) if provider.manual_check_in() else (True, "user_info_triggered")
            after = _user_info(client, provider, headers)
            if success:
                success_count += 1
            if not before.get("success") and not after.get("success") and success:
                success = False
                message = after.get("error") or before.get("error") or "User info request failed"
                success_count = max(0, success_count - 1)
            results.append(_account_result(
                index=index,
                account=account,
                provider=provider,
                before=before,
                after=after,
                success=success,
                message=message,
                missing_waf_cookies=missing_waf,
            ))
        except Exception as exc:
            results.append(_account_result(
                index=index,
                account=account,
                provider=provider,
                before=None,
                after=None,
                success=False,
                message=str(exc),
                missing_waf_cookies=missing_waf,
            ))
        finally:
            try:
                client.close()
            except Exception:
                pass

    return {
        "ok": success_count > 0,
        "success_count": success_count,
        "total_count": len(accounts),
        "results": results,
        "ran_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }

