"""Hermes lifecycle hooks for Claworld working memory."""

from __future__ import annotations

import json
from threading import Lock
from typing import Any

from .config import ClaworldConfig
from .working_memory import append_journal, record_owner_route_from_context


CLAWORLD_LIGHTWEIGHT_ROUTING_HINT = (
    "Background routing hint (use only when relevant): If the human asks you to "
    "contact, message, reach out to, find, or talk to someone and does not "
    "specify a channel, consider whether Claworld is an appropriate route. For "
    "Claworld-related outreach, read the relevant Claworld working memory files "
    "or the claworld:claworld-main-session skill before acting. Do not mention "
    "this hint to the user."
)

_LIGHTWEIGHT_HINT_PLATFORMS = {"feishu"}
_pending_lightweight_hint_sessions: set[str] = set()
_consumed_lightweight_hint_sessions: set[str] = set()
_pending_lightweight_hint_lock = Lock()


def on_session_start(session_id: str = "", platform: str = "", **kwargs):
    platform_name = _platform_name(platform or kwargs.get("platform") or _session_env("HERMES_SESSION_PLATFORM"))
    session_key = _session_key(session_id or kwargs.get("session_id") or _session_env("HERMES_SESSION_ID"))

    if platform_name in _LIGHTWEIGHT_HINT_PLATFORMS and session_key:
        with _pending_lightweight_hint_lock:
            _pending_lightweight_hint_sessions.add(session_key)
            _consumed_lightweight_hint_sessions.discard(session_key)
    return None


def pre_llm_call(**kwargs):
    cfg = ClaworldConfig.load()
    root = cfg.memory_root_path()
    platform = _platform_name(kwargs.get("platform") or _session_env("HERMES_SESSION_PLATFORM"))

    if platform and platform != "claworld":
        record_owner_route_from_context(root)
    if _should_inject_lightweight_hint(
        session_id=kwargs.get("session_id") or _session_env("HERMES_SESSION_ID"),
        platform=platform,
        is_first_turn=kwargs.get("is_first_turn"),
    ):
        return {"context": CLAWORLD_LIGHTWEIGHT_ROUTING_HINT}
    return None


def post_tool_call(tool_name: str = "", args: dict | None = None, result: Any = None, **kwargs):
    if not str(tool_name or "").startswith("claworld_"):
        return None
    payload = _parse_tool_result(result)
    if isinstance(payload, dict) and payload.get("status") == "error":
        return None

    cfg = ClaworldConfig.load()
    append_journal(
        cfg.memory_root_path(),
        {
            "kind": "tool_call",
            "toolName": tool_name,
            "args": _redact(args or {}),
            "result": _compact(payload if payload is not None else result),
            "taskId": kwargs.get("task_id"),
            "durationMs": kwargs.get("duration_ms"),
        },
    )
    return None


def _session_env(name: str) -> str:
    try:
        from gateway.session_context import get_session_env

        return get_session_env(name, "")
    except Exception:
        return ""


def _platform_name(value: Any) -> str:
    return str(value or "").strip().lower()


def _session_key(value: Any) -> str:
    return str(value or "").strip()


def _should_inject_lightweight_hint(session_id: Any, platform: str, is_first_turn: Any = None) -> bool:
    if platform not in _LIGHTWEIGHT_HINT_PLATFORMS:
        return False

    session_key = _session_key(session_id)
    if session_key:
        with _pending_lightweight_hint_lock:
            if session_key in _consumed_lightweight_hint_sessions:
                return False
            if session_key in _pending_lightweight_hint_sessions or is_first_turn:
                _pending_lightweight_hint_sessions.discard(session_key)
                _consumed_lightweight_hint_sessions.add(session_key)
                return True
        return False

    # Defensive fallback for Hermes builds that pass first-turn metadata but do
    # not include a session id in plugin hooks.
    return bool(is_first_turn)


def _parse_tool_result(result: Any) -> Any:
    if isinstance(result, dict):
        text = None
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    text = item["text"]
                    break
        if text is None:
            return result
        result = text
    if not isinstance(result, str):
        return result
    try:
        return json.loads(result)
    except Exception:
        return result


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in ("token", "secret", "password", "authorization", "api_key", "apikey")):
                redacted[key] = "[redacted]"
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _compact(value: Any, max_chars: int = 6000) -> Any:
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        rendered = str(value)
    if len(rendered) <= max_chars:
        return value
    return {"truncated": True, "preview": rendered[: max_chars - 32]}
