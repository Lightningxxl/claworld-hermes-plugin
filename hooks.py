"""Hermes lifecycle hooks for Claworld working memory."""

from __future__ import annotations

import json
from typing import Any

from .config import ClaworldConfig
from .working_memory import append_journal, build_prompt_context, record_owner_route_from_context


def pre_llm_call(**kwargs):
    cfg = ClaworldConfig.load()
    root = cfg.memory_root_path()
    platform = kwargs.get("platform") or _session_env("HERMES_SESSION_PLATFORM")
    chat_id = _session_env("HERMES_SESSION_CHAT_ID")

    if platform and platform != "claworld":
        record_owner_route_from_context(root)

    context = build_prompt_context(root, platform=platform or "", chat_id=chat_id or "")
    if not context.strip():
        return None
    return {"context": context}


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
