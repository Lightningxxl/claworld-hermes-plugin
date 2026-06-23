"""Configuration resolution for the Claworld Hermes plugin."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CLAWORLD_SERVER_URL = "https://claworld.love"


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    value = str(value).strip()
    if not value:
        return default
    expanded = os.path.expandvars(value)
    if expanded == value and value.startswith("${") and value.endswith("}"):
        return default
    return expanded or default


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


def _int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _nonnegative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


@dataclass(frozen=True)
class ClaworldConfig:
    server_url: str = ""
    api_key: str = ""
    app_token: str = ""
    account_id: str = "default"
    agent_id: str = ""
    working_memory_root: str = ""
    heartbeat_seconds: int = 15
    reconnect: bool = True
    reply_ack_timeout_seconds: int = 5
    allow_all_users: bool = False
    allowed_users: tuple[str, ...] = ()
    http_proxy: str = ""
    use_env_proxy: bool = False
    http_retries: int = 2

    @classmethod
    def load(cls) -> "ClaworldConfig":
        file_cfg = cls.from_hermes_config()
        env_cfg = cls.from_env()
        env_account_id = _text(os.getenv("CLAWORLD_ACCOUNT_ID"))
        return cls(
            server_url=env_cfg.server_url or file_cfg.server_url or DEFAULT_CLAWORLD_SERVER_URL,
            api_key=env_cfg.api_key or file_cfg.api_key,
            app_token=env_cfg.app_token or file_cfg.app_token,
            account_id=env_account_id or file_cfg.account_id or "default",
            agent_id=env_cfg.agent_id or file_cfg.agent_id,
            working_memory_root=env_cfg.working_memory_root or file_cfg.working_memory_root,
            heartbeat_seconds=_env_int("CLAWORLD_HEARTBEAT_SECONDS", file_cfg.heartbeat_seconds),
            reconnect=_env_bool("CLAWORLD_RECONNECT", file_cfg.reconnect),
            reply_ack_timeout_seconds=_env_int("CLAWORLD_REPLY_ACK_TIMEOUT_SECONDS", file_cfg.reply_ack_timeout_seconds),
            allow_all_users=_env_bool("CLAWORLD_ALLOW_ALL_USERS", file_cfg.allow_all_users),
            allowed_users=env_cfg.allowed_users or file_cfg.allowed_users,
            http_proxy=env_cfg.http_proxy or file_cfg.http_proxy,
            use_env_proxy=_env_bool("CLAWORLD_USE_ENV_PROXY", file_cfg.use_env_proxy),
            http_retries=_env_nonnegative_int("CLAWORLD_HTTP_RETRIES", file_cfg.http_retries),
        )

    @classmethod
    def from_env(cls) -> "ClaworldConfig":
        return cls(
            server_url=_text(os.getenv("CLAWORLD_SERVER_URL")),
            api_key=_text(os.getenv("CLAWORLD_API_KEY")),
            app_token=_text(os.getenv("CLAWORLD_APP_TOKEN")),
            account_id=_text(os.getenv("CLAWORLD_ACCOUNT_ID"), "default"),
            agent_id=_text(os.getenv("CLAWORLD_AGENT_ID")),
            working_memory_root=_text(os.getenv("CLAWORLD_WORKING_MEMORY_ROOT")),
            heartbeat_seconds=_int(os.getenv("CLAWORLD_HEARTBEAT_SECONDS"), 15),
            reconnect=_bool(os.getenv("CLAWORLD_RECONNECT"), True),
            reply_ack_timeout_seconds=_int(os.getenv("CLAWORLD_REPLY_ACK_TIMEOUT_SECONDS"), 5),
            allow_all_users=_bool(os.getenv("CLAWORLD_ALLOW_ALL_USERS"), False),
            allowed_users=_csv(os.getenv("CLAWORLD_ALLOWED_USERS")),
            http_proxy=_text(os.getenv("CLAWORLD_HTTP_PROXY"), _text(os.getenv("CLAWORLD_PROXY_URL"))),
            use_env_proxy=_bool(os.getenv("CLAWORLD_USE_ENV_PROXY"), False),
            http_retries=_nonnegative_int(os.getenv("CLAWORLD_HTTP_RETRIES"), 2),
        )

    @classmethod
    def from_hermes_config(cls) -> "ClaworldConfig":
        try:
            import yaml
        except Exception:
            return cls()

        hermes_home = hermes_home_path()
        path = hermes_home / "config.yaml"
        if not path.exists():
            return cls()
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return cls()
        platforms = (((data.get("gateway") or {}).get("platforms") or {}) if isinstance(data, dict) else {})
        claworld = platforms.get("claworld") if isinstance(platforms, dict) else None
        extra = (claworld or {}).get("extra") if isinstance(claworld, dict) else None
        return cls.from_extra(extra or {})

    @classmethod
    def from_extra(cls, extra: dict) -> "ClaworldConfig":
        if not isinstance(extra, dict):
            extra = {}
        return cls(
            server_url=_text(extra.get("server_url"), _text(extra.get("serverUrl"))),
            api_key=_text(extra.get("api_key"), _text(extra.get("apiKey"))),
            app_token=_text(extra.get("app_token"), _text(extra.get("appToken"))),
            account_id=_text(extra.get("account_id"), _text(extra.get("accountId"), "default")),
            agent_id=_text(extra.get("agent_id"), _text(extra.get("agentId"))),
            working_memory_root=_text(extra.get("working_memory_root"), _text(extra.get("workingMemoryRoot"))),
            heartbeat_seconds=_int(extra.get("heartbeat_seconds", extra.get("heartbeatSeconds")), 15),
            reconnect=_bool(extra.get("reconnect"), True),
            reply_ack_timeout_seconds=_int(extra.get("reply_ack_timeout_seconds", extra.get("replyAckTimeoutSeconds")), 5),
            allow_all_users=_bool(extra.get("allow_all_users", extra.get("allowAllUsers")), False),
            allowed_users=_csv(extra.get("allowed_users", extra.get("allowedUsers"))),
            http_proxy=_text(extra.get("http_proxy"), _text(extra.get("httpProxy"), _text(extra.get("proxy_url"), _text(extra.get("proxyUrl"))))),
            use_env_proxy=_bool(extra.get("use_env_proxy", extra.get("useEnvProxy")), False),
            http_retries=_nonnegative_int(extra.get("http_retries", extra.get("httpRetries")), 2),
        )

    @classmethod
    def from_platform_config(cls, platform_config: Any) -> "ClaworldConfig":
        extra = getattr(platform_config, "extra", None) or {}
        if not isinstance(extra, dict):
            extra = {}
        env = cls.from_env()
        env_account_id = _text(os.getenv("CLAWORLD_ACCOUNT_ID"))
        file_cfg = cls.from_extra(extra)
        return cls(
            server_url=env.server_url or file_cfg.server_url or DEFAULT_CLAWORLD_SERVER_URL,
            api_key=env.api_key or file_cfg.api_key,
            app_token=env.app_token or file_cfg.app_token,
            account_id=env_account_id or file_cfg.account_id or "default",
            agent_id=env.agent_id or file_cfg.agent_id,
            working_memory_root=env.working_memory_root or file_cfg.working_memory_root,
            heartbeat_seconds=_env_int("CLAWORLD_HEARTBEAT_SECONDS", file_cfg.heartbeat_seconds),
            reconnect=_env_bool("CLAWORLD_RECONNECT", file_cfg.reconnect),
            reply_ack_timeout_seconds=_env_int("CLAWORLD_REPLY_ACK_TIMEOUT_SECONDS", file_cfg.reply_ack_timeout_seconds),
            allow_all_users=_env_bool("CLAWORLD_ALLOW_ALL_USERS", file_cfg.allow_all_users),
            allowed_users=env.allowed_users or file_cfg.allowed_users,
            http_proxy=env.http_proxy or file_cfg.http_proxy,
            use_env_proxy=_env_bool("CLAWORLD_USE_ENV_PROXY", file_cfg.use_env_proxy),
            http_retries=_env_nonnegative_int("CLAWORLD_HTTP_RETRIES", file_cfg.http_retries),
        )

    def to_platform_extra(self) -> dict:
        return {
            "server_url": self.server_url,
            "api_key": self.api_key,
            "app_token": self.app_token,
            "account_id": self.account_id,
            "agent_id": self.agent_id,
            "working_memory_root": self.working_memory_root,
            "heartbeat_seconds": self.heartbeat_seconds,
            "reconnect": self.reconnect,
            "reply_ack_timeout_seconds": self.reply_ack_timeout_seconds,
            "allow_all_users": self.allow_all_users,
            "allowed_users": list(self.allowed_users),
            "http_proxy": self.http_proxy,
            "use_env_proxy": self.use_env_proxy,
            "http_retries": self.http_retries,
        }

    def memory_root_path(self) -> Path:
        if self.working_memory_root:
            return Path(self.working_memory_root).expanduser()
        hermes_home = hermes_home_path()
        return hermes_home / ".claworld"


def _csv(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(_text(v) for v in value if _text(v))
    return tuple(part.strip() for part in str(value).split(",") if part.strip())


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return _int(raw, default) if raw not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return _bool(raw, default) if raw not in (None, "") else default


def _env_nonnegative_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return _nonnegative_int(raw, default) if raw not in (None, "") else default


def hermes_home_path() -> Path:
    """Resolve the active Hermes home while remaining importable outside Hermes."""

    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()).expanduser()
    except Exception:
        return Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()
