"""Trusted Hermes runtime bridge for public group projection.

This module deliberately keeps platform routing out of model/tool arguments:

* native Feishu/Telegram routes are captured from Hermes ``MessageEvent``
  objects in ``pre_gateway_dispatch``;
* capability checks use the already-authenticated live platform adapter; and
* projection sends call the live adapter directly, bypassing
  ``tools.send_message_tool`` and therefore its transcript mirror.

The bridge is intentionally small and feature-detected.  Hermes does not yet
publish a stable gateway-runtime accessor, so cold-start lookup temporarily
uses the same private ``gateway.run._gateway_runner_ref`` compatibility seam
as Hermes' own ``send_message_tool``.  Missing or incompatible runtime state
always fails closed.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

_SUPPORTED_PROJECTION_PLATFORMS = frozenset({"feishu", "telegram"})
_GROUP_CHAT_TYPES = frozenset({"group", "supergroup", "channel"})
_ROUTE_CACHE_MAX_ENTRIES = 2048
_ROUTE_CACHE_TTL_SECONDS = 60 * 60

_runtime_lock = threading.RLock()
_gateway_ref: Callable[[], Any | None] | None = None
_delivery_router_ref: Callable[[], Any | None] | None = None
_captured_routes: "OrderedDict[tuple[str, str, str, str], CapturedProjectionRoute]" = OrderedDict()

# Feishu rich-text mentions and legacy Hermes/Lark mention tokens.
_FEISHU_AT_ELEMENT_RE = re.compile(
    r"<at\b[^>]*>(.*?)</at\s*>",
    re.IGNORECASE | re.DOTALL,
)
_FEISHU_AT_SELF_CLOSING_RE = re.compile(r"<at\b[^>]*/\s*>", re.IGNORECASE)
_FEISHU_USER_TOKEN_RE = re.compile(r"@_user_\d+\b", re.IGNORECASE)
# Remove the active mention sigil while preserving the visible label.  The
# negative lookbehind keeps ordinary email addresses intact.
_VISIBLE_MENTION_SIGIL_RE = re.compile(r"(?<![\w.+-])@(?=[\w])", re.UNICODE)


@dataclass(frozen=True)
class CapturedProjectionRoute:
    """Immutable native route captured from a Hermes gateway event."""

    platform: str
    chat_id: str
    chat_type: str
    origin_message_id: str
    thread_id: str | None = None
    profile: str | None = None
    origin_public_text: str | None = None
    captured_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return the wire-friendly camelCase representation."""

        return {
            "platform": self.platform,
            "chatId": self.chat_id,
            "chatType": self.chat_type,
            "threadId": self.thread_id,
            "originMessageId": self.origin_message_id,
            "originPublicText": self.origin_public_text,
            "profile": self.profile,
            "capturedAt": self.captured_at,
        }


def pre_gateway_dispatch(
    event: Any = None,
    gateway: Any = None,
    session_store: Any = None,
    **_: Any,
) -> None:
    """Hermes hook: cache runtime handles and an exact native group route.

    Hermes invokes this hook before authorization.  Capturing a route does not
    grant any authority by itself: only a turn that subsequently passes Hermes
    authorization can look up its own exact ``HERMES_SESSION_MESSAGE_ID``.
    Route fields always come from the platform adapter's ``SessionSource`` and
    never from model text or tool arguments.
    """

    del session_store  # Reserved for a future public runtime service.
    _cache_runtime(gateway)

    if event is None or bool(getattr(event, "internal", False)):
        return None
    source = getattr(event, "source", None)
    if source is None:
        return None

    platform = _platform_value(getattr(source, "platform", ""))
    chat_type = _text(getattr(source, "chat_type", "")).lower()
    if platform == "telegram" and chat_type == "forum":
        chat_type = "supergroup"
    chat_id = _text(getattr(source, "chat_id", ""))
    message_id = _text(
        getattr(source, "message_id", None) or getattr(event, "message_id", None)
    )
    if (
        platform not in _SUPPORTED_PROJECTION_PLATFORMS
        or chat_type not in _GROUP_CHAT_TYPES
        or not chat_id
        or not message_id
    ):
        return None

    now = time.time()
    route = CapturedProjectionRoute(
        platform=platform,
        chat_id=chat_id,
        chat_type=chat_type,
        origin_message_id=message_id,
        thread_id=_optional_text(getattr(source, "thread_id", None)),
        profile=_captured_profile(gateway, getattr(source, "profile", None)),
        origin_public_text=_optional_text(getattr(event, "text", None)),
        captured_at=now,
    )
    key = _route_key(
        route.profile,
        route.platform,
        route.chat_id,
        route.origin_message_id,
    )
    with _runtime_lock:
        _purge_expired_routes_locked(now)
        _captured_routes[key] = route
        _captured_routes.move_to_end(key)
        while len(_captured_routes) > _ROUTE_CACHE_MAX_ENTRIES:
            _captured_routes.popitem(last=False)
    return None


def get_captured_projection_route(
    *,
    platform: str,
    chat_id: str,
    origin_message_id: str,
    profile: str | None = None,
) -> CapturedProjectionRoute | None:
    """Return only the exact route captured for this chat message/profile."""

    platform_name = _platform_value(platform)
    normalized_chat_id = _text(chat_id)
    message_id = _text(origin_message_id)
    if (
        platform_name not in _SUPPORTED_PROJECTION_PLATFORMS
        or not normalized_chat_id
        or not message_id
    ):
        return None
    key = _route_key(profile, platform_name, normalized_chat_id, message_id)
    now = time.time()
    with _runtime_lock:
        _purge_expired_routes_locked(now)
        route = _captured_routes.get(key)
        if route is not None:
            _captured_routes.move_to_end(key)
        return route


def get_current_projection_route() -> CapturedProjectionRoute | None:
    """Resolve the current Hermes turn to its exact captured native route.

    No latest-route fallback is allowed.  If Hermes lacks an exact current
    message id (or ``chat_type`` was not captured by the gateway hook), this
    returns ``None`` so callers can decline projection safely.
    """

    try:
        from gateway.session_context import get_session_env
    except Exception:
        return None

    return get_captured_projection_route(
        platform=get_session_env("HERMES_SESSION_PLATFORM", ""),
        chat_id=get_session_env("HERMES_SESSION_CHAT_ID", ""),
        origin_message_id=get_session_env("HERMES_SESSION_MESSAGE_ID", ""),
        profile=get_session_env("HERMES_SESSION_PROFILE", "") or None,
    )


def get_current_or_default_projection_profile(*, platform: str) -> str | None:
    """Return an explicit local profile for a peer-side binding.

    This is a local credential choice only.  It must never be added to the
    relay route or trusted metadata.  In multiplex mode the active profile is
    selected; a router-only legacy runtime is explicitly treated as default.
    """

    if _platform_value(platform) not in _SUPPORTED_PROJECTION_PLATFORMS:
        return None
    gateway, router = _resolve_runtime()
    if gateway is not None:
        return _active_profile_name(gateway)
    if router is not None:
        return "default"
    return None


async def can_send_to(
    *,
    platform: str,
    chat_id: str,
    thread_id: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Check whether this Hermes profile's bot can project to a native group.

    Feishu uses the official ``im.v1.chat_members.is_in_chat`` endpoint.
    Telegram verifies the chat plus the current bot's membership and relevant
    send/post permissions.  Results never include credentials or raw platform
    exception text.
    """

    target = _target_fields(platform, chat_id, thread_id, profile)
    if target["platform"] not in _SUPPORTED_PROJECTION_PLATFORMS:
        return _capability_result(
            target,
            success=False,
            can_send=False,
            reason="unsupported_platform",
        )
    if not target["chatId"]:
        return _capability_result(target, success=False, can_send=False, reason="invalid_chat_id")
    if not target["profile"]:
        return _capability_result(target, success=False, can_send=False, reason="profile_required")

    gateway, router = _resolve_runtime()
    if gateway is None and router is None:
        return _capability_result(
            target,
            success=False,
            can_send=False,
            reason="gateway_unavailable",
        )
    adapter = _resolve_adapter(
        gateway=gateway,
        router=router,
        platform=target["platform"],
        profile=target["profile"],
    )
    if adapter is None:
        return _capability_result(
            target,
            success=False,
            can_send=False,
            reason="adapter_unavailable",
        )

    if target["platform"] == "feishu":
        return await _can_send_to_feishu(adapter, target)
    return await _can_send_to_telegram(adapter, target)


async def send_without_mirror(
    *,
    platform: str,
    chat_id: str,
    content: str,
    thread_id: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Send one projection through the live adapter without transcript mirror.

    This function intentionally does **not** call ``send_message_tool`` or
    ``gateway.mirror.mirror_to_session``.  Only the adapter's normal outbound
    method runs, so the human Main transcript is not polluted.  Mention syntax
    is neutralized at this final choke point to prevent a projected message
    from waking another bot and creating a second native-channel A2A loop.
    """

    target = _target_fields(platform, chat_id, thread_id, profile)
    if target["platform"] not in _SUPPORTED_PROJECTION_PLATFORMS:
        return _send_result(target, success=False, reason="unsupported_platform")
    if not target["chatId"]:
        return _send_result(target, success=False, reason="invalid_chat_id")
    if not target["profile"]:
        return _send_result(target, success=False, reason="profile_required")

    cleaned_content, mentions_stripped = sanitize_projection_content(content)
    if not cleaned_content:
        return _send_result(
            target,
            success=False,
            reason="empty_content",
            mentions_stripped=mentions_stripped,
        )

    gateway, router = _resolve_runtime()
    if gateway is None and router is None:
        return _send_result(target, success=False, reason="gateway_unavailable")
    adapter = _resolve_adapter(
        gateway=gateway,
        router=router,
        platform=target["platform"],
        profile=target["profile"],
    )
    if adapter is None:
        return _send_result(target, success=False, reason="adapter_unavailable")

    metadata = {"thread_id": target["threadId"]} if target["threadId"] else None
    try:
        # Direct BasePlatformAdapter.send is the intentional no-mirror seam.
        # The public model tool adds mirroring *after* this call; bypassing that
        # wrapper leaves the native platform message as the only side effect.
        result = await adapter.send(
            chat_id=target["chatId"],
            content=cleaned_content,
            metadata=metadata,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "Projection send failed for %s adapter (%s)",
            target["platform"],
            type(exc).__name__,
        )
        return _send_result(
            target,
            success=False,
            reason="send_failed",
            mentions_stripped=mentions_stripped,
        )

    if not _result_succeeded(result):
        return _send_result(
            target,
            success=False,
            reason="adapter_rejected",
            mentions_stripped=mentions_stripped,
            error_kind=_result_error_kind(result),
        )

    message_id = _result_message_id(result)
    return _send_result(
        target,
        success=True,
        reason="sent" if message_id else "missing_message_id",
        message_id=message_id,
        mentions_stripped=mentions_stripped,
        receipt_ready=bool(message_id),
    )


def sanitize_projection_content(content: Any) -> tuple[str, bool]:
    """Neutralize native mention syntax and return ``(content, changed)``."""

    original = str(content or "")
    cleaned = _FEISHU_AT_ELEMENT_RE.sub(lambda match: _text(match.group(1)), original)
    cleaned = _FEISHU_AT_SELF_CLOSING_RE.sub("", cleaned)
    cleaned = _FEISHU_USER_TOKEN_RE.sub("", cleaned)
    cleaned = _VISIBLE_MENTION_SIGIL_RE.sub("", cleaned)
    cleaned = cleaned.strip()
    return cleaned, cleaned != original.strip()


async def _can_send_to_feishu(adapter: Any, target: Mapping[str, Any]) -> dict[str, Any]:
    bot_open_id = _optional_text(getattr(adapter, "_bot_open_id", None))
    app_id = _optional_text(getattr(adapter, "_app_id", None))
    bot_name = _optional_text(getattr(adapter, "_bot_name", None))
    identity = {
        "external_bot_id": bot_open_id or app_id,
        "bot_mention": bot_open_id or bot_name,
    }
    if not identity["external_bot_id"]:
        return _capability_result(
            target,
            success=False,
            can_send=False,
            reason="bot_identity_unavailable",
        )
    client = getattr(adapter, "_client", None)
    chat_members = getattr(getattr(getattr(client, "im", None), "v1", None), "chat_members", None)
    is_in_chat_fn = getattr(chat_members, "is_in_chat", None)
    if client is None or not callable(is_in_chat_fn):
        return _capability_result(
            target,
            success=False,
            can_send=False,
            reason="adapter_not_connected",
            **identity,
        )

    chat_info: dict[str, Any] = {}
    get_chat_info = getattr(adapter, "get_chat_info", None)
    if callable(get_chat_info):
        try:
            maybe_info = await get_chat_info(target["chatId"])
            if isinstance(maybe_info, dict):
                chat_info = maybe_info
        except asyncio.CancelledError:
            raise
        except Exception:
            chat_info = {}

    try:
        request = _build_feishu_is_in_chat_request(target["chatId"])
        run_blocking = getattr(adapter, "_run_blocking", None)
        if callable(run_blocking):
            response = await run_blocking(is_in_chat_fn, request)
        else:
            response = await asyncio.to_thread(is_in_chat_fn, request)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Feishu projection capability check failed (%s)", type(exc).__name__)
        return _capability_result(
            target,
            success=False,
            can_send=False,
            reason="preflight_failed",
            **identity,
        )

    response_success = getattr(response, "success", None)
    if not response or (
        callable(response_success) and not response_success()
    ) or response_success is False:
        return _capability_result(
            target,
            success=False,
            can_send=False,
            reason="platform_rejected",
            platform_code=_optional_text(getattr(response, "code", None)),
            **identity,
        )
    data = getattr(response, "data", None)
    in_chat = getattr(data, "is_in_chat", None)
    if in_chat is not True:
        return _capability_result(
            target,
            success=True,
            can_send=False,
            reason="bot_not_member",
            chat_type=_optional_text(chat_info.get("type")),
            **identity,
        )

    normalized_chat_type = _optional_text(chat_info.get("type"))
    if normalized_chat_type not in _GROUP_CHAT_TYPES:
        return _capability_result(
            target,
            success=True,
            can_send=False,
            reason="not_group_chat",
            chat_type=normalized_chat_type,
            **identity,
        )
    return _capability_result(
        target,
        success=True,
        can_send=True,
        reason="member_verified",
        chat_type=normalized_chat_type or "group",
        permission_basis="feishu_is_in_chat",
        **identity,
    )


async def _can_send_to_telegram(adapter: Any, target: Mapping[str, Any]) -> dict[str, Any]:
    bot = getattr(adapter, "_bot", None)
    if bot is None:
        return _capability_result(
            target,
            success=False,
            can_send=False,
            reason="adapter_not_connected",
        )

    chat_id = _normalize_telegram_chat_id(target["chatId"])
    try:
        chat = await bot.get_chat(chat_id)
        bot_id = getattr(bot, "id", None)
        bot_username = _optional_text(getattr(bot, "username", None))
        if bot_id is None or not bot_username:
            me = await bot.get_me()
            bot_id = bot_id or getattr(me, "id", None)
            bot_username = bot_username or _optional_text(getattr(me, "username", None))
        if bot_id is None:
            return _capability_result(
                target,
                success=False,
                can_send=False,
                reason="bot_identity_unavailable",
            )
        member = await bot.get_chat_member(chat_id, bot_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Telegram projection capability check failed (%s)", type(exc).__name__)
        return _capability_result(target, success=False, can_send=False, reason="preflight_failed")

    identity = {
        "external_bot_id": _text(bot_id),
        "bot_mention": _telegram_bot_mention(bot_username),
    }
    chat_type = _enum_value(getattr(chat, "type", "")).lower()
    if chat_type not in _GROUP_CHAT_TYPES:
        return _capability_result(
            target,
            success=True,
            can_send=False,
            reason="not_group_chat",
            chat_type=chat_type or None,
            bot_username=bot_username,
            **identity,
        )
    if target.get("threadId") and not bool(getattr(chat, "is_forum", False)):
        return _capability_result(
            target,
            success=True,
            can_send=False,
            reason="thread_not_supported",
            chat_type=chat_type,
            bot_username=bot_username,
            **identity,
        )

    status = _enum_value(getattr(member, "status", "")).lower()
    if status not in {
        "creator",
        "administrator",
        "member",
        "restricted",
        "left",
        "kicked",
        "banned",
    }:
        return _capability_result(
            target,
            success=False,
            can_send=False,
            reason="membership_unknown",
            chat_type=chat_type,
            bot_username=bot_username,
            **identity,
        )
    if status in {"left", "kicked", "banned"}:
        return _capability_result(
            target,
            success=True,
            can_send=False,
            reason="bot_not_member",
            chat_type=chat_type,
            member_status=status,
            bot_username=bot_username,
            **identity,
        )
    if chat_type == "channel" and (
        status != "creator"
        and (
            status != "administrator"
            or getattr(member, "can_post_messages", None) is not True
        )
    ):
        return _capability_result(
            target,
            success=True,
            can_send=False,
            reason="permission_denied",
            chat_type=chat_type,
            member_status=status,
            bot_username=bot_username,
            **identity,
        )
    if status == "restricted" and getattr(member, "can_send_messages", None) is not True:
        return _capability_result(
            target,
            success=True,
            can_send=False,
            reason="permission_denied",
            chat_type=chat_type,
            member_status=status,
            bot_username=bot_username,
            **identity,
        )
    default_permissions = getattr(chat, "permissions", None)
    if (
        status not in {"creator", "administrator"}
        and default_permissions is not None
        and getattr(default_permissions, "can_send_messages", None) is False
    ):
        return _capability_result(
            target,
            success=True,
            can_send=False,
            reason="permission_denied",
            chat_type=chat_type,
            member_status=status,
            bot_username=bot_username,
            **identity,
        )
    return _capability_result(
        target,
        success=True,
        can_send=True,
        reason="member_verified",
        chat_type=chat_type,
        member_status=status or None,
        bot_username=bot_username,
        permission_basis="telegram_chat_member",
        thread_verified=not bool(target.get("threadId")),
        **identity,
    )


def _build_feishu_is_in_chat_request(chat_id: str) -> Any:
    from lark_oapi.api.im.v1.model.is_in_chat_chat_members_request import (
        IsInChatChatMembersRequest,
    )

    return IsInChatChatMembersRequest.builder().chat_id(chat_id).build()


def _cache_runtime(gateway: Any) -> None:
    if gateway is None:
        return
    router = getattr(gateway, "delivery_router", None)
    with _runtime_lock:
        global _gateway_ref, _delivery_router_ref
        _gateway_ref = _make_ref(gateway)
        if router is not None:
            _delivery_router_ref = _make_ref(router)


def _resolve_runtime() -> tuple[Any | None, Any | None]:
    with _runtime_lock:
        gateway = _gateway_ref() if _gateway_ref is not None else None
        router = _delivery_router_ref() if _delivery_router_ref is not None else None
    if gateway is not None:
        return gateway, router

    # Temporary Hermes compatibility seam.  Hermes' own send_message_tool uses
    # this weakref for live-adapter lookup.  Feature-detect every attribute and
    # fail closed if upstream removes/changes it; never instantiate an adapter
    # with arbitrary credentials here.
    try:
        from gateway.run import _gateway_runner_ref
    except Exception:
        return None, router
    if not callable(_gateway_runner_ref):
        return None, router
    try:
        gateway = _gateway_runner_ref()
    except Exception:
        return None, router
    if gateway is None:
        return None, router
    _cache_runtime(gateway)
    return gateway, getattr(gateway, "delivery_router", None)


def _resolve_adapter(
    *,
    gateway: Any,
    router: Any,
    platform: str,
    profile: str | None,
) -> Any | None:
    profile_name = _text(profile)
    if not profile_name:
        return None
    if gateway is not None:
        secondary_maps = getattr(gateway, "_profile_adapters", None)
        if isinstance(secondary_maps, Mapping) and profile_name in secondary_maps:
            adapter = _adapter_from_map(secondary_maps.get(profile_name), platform)
            if adapter is not None:
                return adapter

        if profile_name != _active_profile_name(gateway):
            return None
        adapter = _adapter_from_map(getattr(gateway, "adapters", None), platform)
        if adapter is not None:
            return adapter
        return _adapter_from_map(getattr(router, "adapters", None), platform)
    if profile_name != "default":
        return None
    return _adapter_from_map(getattr(router, "adapters", None), platform)


def _adapter_from_map(adapters: Any, platform: str) -> Any | None:
    if not isinstance(adapters, Mapping):
        return None
    for key, adapter in adapters.items():
        if _platform_value(key) == platform:
            return adapter
    return None


def _make_ref(value: Any) -> Callable[[], Any | None]:
    try:
        return weakref.ref(value)
    except TypeError:
        # A few test/fake runtimes are not weak-referenceable.  Real
        # GatewayRunner/DeliveryRouter instances are; the bounded strong
        # fallback keeps feature detection deterministic without affecting the
        # production lifetime (one gateway singleton per process).
        return lambda value=value: value


def _route_key(
    profile: str | None,
    platform: str,
    chat_id: str,
    message_id: str,
) -> tuple[str, str, str, str]:
    return (
        _profile_name(profile),
        _platform_value(platform),
        _text(chat_id),
        _text(message_id),
    )


def _captured_profile(gateway: Any, source_profile: Any) -> str:
    explicit = _text(source_profile)
    return explicit or _active_profile_name(gateway)


def _active_profile_name(gateway: Any) -> str:
    active_profile_fn = getattr(gateway, "_active_profile_name", None)
    try:
        active = _text(active_profile_fn()) if callable(active_profile_fn) else ""
    except Exception:
        active = ""
    return active or "default"


def _profile_name(value: Any) -> str:
    return _text(value) or "default"


def _purge_expired_routes_locked(now: float) -> None:
    cutoff = now - _ROUTE_CACHE_TTL_SECONDS
    for key, route in list(_captured_routes.items()):
        if route.captured_at < cutoff:
            _captured_routes.pop(key, None)


def _target_fields(
    platform: str,
    chat_id: str,
    thread_id: str | None,
    profile: str | None,
) -> dict[str, Any]:
    return {
        "platform": _platform_value(platform),
        "chatId": _text(chat_id),
        "threadId": _optional_text(thread_id),
        "profile": _optional_text(profile),
    }


def _capability_result(
    target: Mapping[str, Any],
    *,
    success: bool,
    can_send: bool,
    reason: str,
    **extra: Any,
) -> dict[str, Any]:
    result = dict(target)
    result.update({"success": success, "canSend": can_send, "reason": reason})
    result.update(
        {
            _camel_case(key): value
            for key, value in extra.items()
            if value is not None
        }
    )
    return result


def _send_result(
    target: Mapping[str, Any],
    *,
    success: bool,
    reason: str,
    message_id: str | None = None,
    mentions_stripped: bool = False,
    receipt_ready: bool = False,
    error_kind: str | None = None,
) -> dict[str, Any]:
    result = dict(target)
    result.update(
        {
            "success": success,
            "reason": reason,
            "messageId": message_id,
            "mentionsStripped": mentions_stripped,
            "receiptReady": receipt_ready,
        }
    )
    if error_kind:
        result["errorKind"] = error_kind
    return result


def _result_succeeded(result: Any) -> bool:
    if isinstance(result, Mapping):
        return bool(result.get("success"))
    return bool(getattr(result, "success", False))


def _result_message_id(result: Any) -> str | None:
    if isinstance(result, Mapping):
        value = result.get("message_id") or result.get("messageId")
    else:
        value = getattr(result, "message_id", None)
    return _optional_text(value)


def _result_error_kind(result: Any) -> str | None:
    if isinstance(result, Mapping):
        value = result.get("error_kind") or result.get("errorKind")
    else:
        value = getattr(result, "error_kind", None)
    return _optional_text(value)


def _normalize_telegram_chat_id(chat_id: str) -> Any:
    try:
        from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id

        return normalize_telegram_chat_id(chat_id)
    except Exception:
        try:
            return int(chat_id)
        except (TypeError, ValueError):
            return chat_id


def _telegram_bot_mention(username: str | None) -> str | None:
    normalized = _optional_text(username)
    if not normalized:
        return None
    return f"@{normalized.lstrip('@')}"


def _platform_value(value: Any) -> str:
    return _text(getattr(value, "value", value)).lower()


def _enum_value(value: Any) -> str:
    return _text(getattr(value, "value", value))


def _optional_text(value: Any) -> str | None:
    normalized = _text(value)
    return normalized or None


def _camel_case(value: str) -> str:
    head, *tail = str(value).split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _reset_projection_runtime_for_tests() -> None:
    """Clear process-local caches.  Test-only; not part of the plugin API."""

    with _runtime_lock:
        global _gateway_ref, _delivery_router_ref
        _gateway_ref = None
        _delivery_router_ref = None
        _captured_routes.clear()


__all__ = [
    "CapturedProjectionRoute",
    "can_send_to",
    "get_captured_projection_route",
    "get_current_projection_route",
    "get_current_or_default_projection_profile",
    "pre_gateway_dispatch",
    "sanitize_projection_content",
    "send_without_mirror",
]
