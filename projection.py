"""Trusted group-projection primitives.

This module deliberately has no dependency on the Hermes gateway or the
Claworld relay client.  It owns the security boundary between backend-authored
projection bindings and the local, durable projection outbox.  Runtime wiring
belongs in the adapter and relay client.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import threading
import unicodedata
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Mapping


PROJECTION_BINDING_SCHEMA = "claworld.projection-binding.v1"
PROJECTION_REQUEST_SCHEMA = "claworld.projection-request.v1"
PROJECTION_OUTBOX_SCHEMA = "claworld.projection-outbox.v1"

SUPPORTED_PROJECTION_PLATFORMS = frozenset({"feishu", "telegram"})
SUPPORTED_PROJECTION_CHAT_TYPES = frozenset({"group", "supergroup", "channel"})
BINDING_STATES = frozenset({"pending_capability", "active", "paused", "ended", "expired"})
REQUEST_STATES = frozenset({"pending", "bound", "failed", "expired"})
OUTBOX_STATES = frozenset({"accepted", "sending", "sent", "receipt_pending", "complete", "failed"})

_BINDING_TRANSITIONS = {
    "pending_capability": frozenset({"active", "paused", "ended", "expired"}),
    "active": frozenset({"paused", "ended", "expired"}),
    "paused": frozenset({"active", "ended", "expired"}),
    "ended": frozenset(),
    "expired": frozenset(),
}
_REQUEST_TRANSITIONS = {
    "pending": frozenset({"bound", "failed", "expired"}),
    "bound": frozenset(),
    "failed": frozenset(),
    "expired": frozenset(),
}
_OUTBOX_TRANSITIONS = {
    "accepted": frozenset({"sending", "failed"}),
    "sending": frozenset({"sent", "failed"}),
    "sent": frozenset({"receipt_pending"}),
    "receipt_pending": frozenset({"complete"}),
    "complete": frozenset(),
    "failed": frozenset({"sending"}),
}

_CONTROL_MARKER_RE = re.compile(
    r"\[\[\s*(?:request_conversation_end|like|dislike|thumbs_up|thumbs_down)\s*\]\]",
    flags=re.IGNORECASE,
)
_OPERATIONAL_WHOLE_MESSAGE_PATTERNS = (
    re.compile(r"^\s*NO_REPLY\s*$"),
    re.compile(r"^\s*Sent the (?:reply|opener|Claworld reply)\.?\s*$", re.IGNORECASE),
    re.compile(r"^\s*\N{BROOM}\s*Auto-compaction complete(?:\s*\(count \d+\))?\.\s*$", re.IGNORECASE),
    re.compile(r"^\s*\N{WARNING SIGN}\ufe0f?\s*Agent failed before reply:", re.IGNORECASE),
    re.compile(r"^\s*LLM request (?:failed|timed out|unauthorized)\b", re.IGNORECASE),
)
_NO_REPLY_LINE_RE = re.compile(r"(?m)^\s*NO_REPLY\s*$")
_OPERATIONAL_SUFFIX_RE = re.compile(
    r"(?im)^\s*Usage:\s+.+\s+in\s*/\s*.+\s+out(?:\s*\N{MIDDLE DOT}\s*est\s+.+)?\s*$"
)
_HIGH_RISK_PATTERNS = (
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    ),
    (
        "bearer_credential",
        re.compile(r"\bAuthorization\s*:\s*Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    ),
    (
        "assigned_credential",
        re.compile(
            r"\b(?:api[_ -]?key|app[_ -]?token|access[_ -]?token|refresh[_ -]?token|password|client[_ -]?secret)"
            r"\b\s*[:=]\s*[\"']?[^\s\"']{6,}",
            re.IGNORECASE,
        ),
    ),
    (
        "provider_credential",
        re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}\b|\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ),
    (
        "jwt_credential",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    (
        "private_working_memory",
        re.compile(
            r"(?:\.claworld/context/(?:NOW|MEMORY|PROFILE)\.md|#\s*Claworld\s+(?:Now|Memory|Profile)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "internal_route",
        re.compile(
            r"(?:\bprojectionBindingId\b|\bHERMES_SESSION_[A-Z0-9_]+\b|"
            r"[\"'](?:chatId|threadId|relaySessionKey)[\"']\s*:)",
            re.IGNORECASE,
        ),
    ),
    (
        "tool_payload",
        re.compile(
            r"(?:<\/?(?:tool_call|tool_result)>|\b(?:tool_call|tool_result|function_call)\s*[:=]|"
            r"[\"'](?:tool|toolName|toolCall|toolResult)[\"']\s*:)",
            re.IGNORECASE,
        ),
    ),
    (
        "internal_media",
        re.compile(r"(?im)^\s*(?:MEDIA:|\[\[as_document\]\]|\[\[audio_as_voice\]\])"),
    ),
)


class ProjectionError(RuntimeError):
    """Base error for projection security and persistence failures."""


class ProjectionBindingError(ProjectionError, ValueError):
    """Raised when relay-authored binding metadata violates the contract."""


class ProjectionStateError(ProjectionError, ValueError):
    """Raised when immutable state changes or a state transition regresses."""


class ProjectionPersistenceError(ProjectionError):
    """Raised when durable projection state cannot be decoded safely."""


class ProjectionContentBlocked(ProjectionError, ValueError):
    """Raised when text is not safe to expose to an origin group."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"projection text blocked: {reason}")


@dataclass(frozen=True)
class ProjectionRoute:
    platform: str
    chat_id: str
    chat_type: str
    thread_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "chatId": self.chat_id,
            "chatType": self.chat_type,
            **({"threadId": self.thread_id} if self.thread_id else {}),
        }


@dataclass(frozen=True)
class ProjectionBinding:
    projection_binding_id: str
    chat_request_id: str
    route: ProjectionRoute
    initiator_agent_id: str
    peer_agent_id: str
    origin_message_id: str
    state: str
    turn_seq: int
    expires_at: str
    issued_at: str | None
    authority: dict[str, Any]
    channel_identity_binding_id: str | None = None
    bot_mentions: tuple[str, ...] = ()

    @property
    def route_digest(self) -> str:
        return projection_route_digest(self.route, self.origin_message_id)

    @property
    def immutable_fingerprint(self) -> str:
        return _sha256_json(
            {
                "projectionBindingId": self.projection_binding_id,
                "chatRequestId": self.chat_request_id,
                "route": self.route.to_dict(),
                "initiatorAgentId": self.initiator_agent_id,
                "peerAgentId": self.peer_agent_id,
                "originMessageId": self.origin_message_id,
                "issuedAt": self.issued_at,
                "authority": self.authority,
                "channelIdentityBindingId": self.channel_identity_binding_id,
            }
        )

    @property
    def immutable_authority_fingerprint(self) -> str:
        """Fingerprint fields that never change, even during handshake.

        The recipient-scoped channel identity starts empty on the initial
        verify event and may be bound once when capability negotiation
        succeeds.  ``expiresAt`` is also deliberately excluded because the
        relay renews an active binding; persistence separately enforces that
        the trusted deadline can only move forward.  ``immutable_fingerprint``
        includes the resulting channel identity; this base fingerprint exists
        only to validate that controlled append.
        """

        return _sha256_json(
            {
                "projectionBindingId": self.projection_binding_id,
                "chatRequestId": self.chat_request_id,
                "route": self.route.to_dict(),
                "initiatorAgentId": self.initiator_agent_id,
                "peerAgentId": self.peer_agent_id,
                "originMessageId": self.origin_message_id,
                "issuedAt": self.issued_at,
                "authority": self.authority,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PROJECTION_BINDING_SCHEMA,
            "projectionBindingId": self.projection_binding_id,
            "chatRequestId": self.chat_request_id,
            "route": self.route.to_dict(),
            "routeDigest": self.route_digest,
            "initiatorAgentId": self.initiator_agent_id,
            "peerAgentId": self.peer_agent_id,
            "originMessageId": self.origin_message_id,
            "state": self.state,
            "turnSeq": self.turn_seq,
            "expiresAt": self.expires_at,
            **({"issuedAt": self.issued_at} if self.issued_at else {}),
            "authority": json.loads(json.dumps(self.authority)),
            **(
                {"channelIdentityBindingId": self.channel_identity_binding_id}
                if self.channel_identity_binding_id
                else {}
            ),
            **({"botMentions": list(self.bot_mentions)} if self.bot_mentions else {}),
        }


@dataclass(frozen=True)
class ProjectionRequest:
    client_request_id: str
    route: ProjectionRoute
    origin_message_id: str
    local_agent_id: str
    state: str = "pending"
    chat_request_id: str | None = None
    projection_binding_id: str | None = None
    failure_code: str | None = None
    failure_reason: str | None = None

    @property
    def immutable_fingerprint(self) -> str:
        return _sha256_json(
            {
                "clientRequestId": self.client_request_id,
                "route": self.route.to_dict(),
                "originMessageId": self.origin_message_id,
                "localAgentId": self.local_agent_id,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PROJECTION_REQUEST_SCHEMA,
            "clientRequestId": self.client_request_id,
            "route": self.route.to_dict(),
            "routeDigest": projection_route_digest(self.route, self.origin_message_id),
            "originMessageId": self.origin_message_id,
            "localAgentId": self.local_agent_id,
            "state": self.state,
            **({"chatRequestId": self.chat_request_id} if self.chat_request_id else {}),
            **({"projectionBindingId": self.projection_binding_id} if self.projection_binding_id else {}),
            **({"failureCode": self.failure_code} if self.failure_code else {}),
            **({"failureReason": self.failure_reason} if self.failure_reason else {}),
        }


@dataclass(frozen=True)
class ProjectionOutboxRecord:
    idempotency_key: str
    projection_binding_id: str
    delivery_id: str
    turn_seq: int
    route_digest: str
    public_text: str
    body_hash: str
    status: str = "accepted"
    external_message_id: str | None = None
    failure_code: str | None = None
    failure_reason: str | None = None
    retryable: bool | None = None
    attempts: int = 0

    @property
    def immutable_fingerprint(self) -> str:
        return _sha256_json(
            {
                "idempotencyKey": self.idempotency_key,
                "projectionBindingId": self.projection_binding_id,
                "deliveryId": self.delivery_id,
                "turnSeq": self.turn_seq,
                "routeDigest": self.route_digest,
                "bodyHash": self.body_hash,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PROJECTION_OUTBOX_SCHEMA,
            "idempotencyKey": self.idempotency_key,
            "projectionBindingId": self.projection_binding_id,
            "deliveryId": self.delivery_id,
            "turnSeq": self.turn_seq,
            "routeDigest": self.route_digest,
            "publicText": self.public_text,
            "bodyHash": self.body_hash,
            "status": self.status,
            "attempts": self.attempts,
            **({"externalMessageId": self.external_message_id} if self.external_message_id else {}),
            **({"failureCode": self.failure_code} if self.failure_code else {}),
            **({"failureReason": self.failure_reason} if self.failure_reason else {}),
            **({"retryable": self.retryable} if self.retryable is not None else {}),
        }


@dataclass(frozen=True)
class ProjectionTextResult:
    text: str = ""
    suppressed_reason: str | None = None
    blocked_reason: str | None = None
    removed_markers: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return bool(self.text) and self.suppressed_reason is None and self.blocked_reason is None


def extract_trusted_projection_binding(
    envelope: Mapping[str, Any],
    *,
    expected_chat_request_id: str | None = None,
    local_agent_id: str | None = None,
    now: datetime | None = None,
    allow_expired: bool = False,
) -> ProjectionBinding | None:
    """Read a binding only from the relay-controlled metadata surface.

    Peer payload, command text, ``untrustedContext``, and ordinary ``metadata``
    are intentionally ignored.  The caller must only invoke this on an
    authenticated Claworld relay envelope.
    """

    if not isinstance(envelope, Mapping):
        raise ProjectionBindingError("relay envelope must be an object")
    data = envelope.get("data")
    if not isinstance(data, Mapping):
        return None
    trusted = data.get("trustedMetadata")
    if not isinstance(trusted, Mapping):
        return None
    raw_binding = trusted.get("projectionBinding")
    if raw_binding is None:
        return None
    if not isinstance(raw_binding, Mapping):
        raise ProjectionBindingError("trusted projectionBinding must be an object")
    return validate_trusted_projection_binding(
        raw_binding,
        expected_chat_request_id=expected_chat_request_id,
        local_agent_id=local_agent_id,
        now=now,
        allow_expired=allow_expired,
    )


def validate_trusted_projection_binding(
    raw: Mapping[str, Any],
    *,
    expected_chat_request_id: str | None = None,
    local_agent_id: str | None = None,
    now: datetime | None = None,
    allow_expired: bool = False,
) -> ProjectionBinding:
    """Validate structurally trusted binding metadata and its local scope."""

    if not isinstance(raw, Mapping):
        raise ProjectionBindingError("projection binding must be an object")
    if raw.get("schema") != PROJECTION_BINDING_SCHEMA:
        raise ProjectionBindingError(f"projection binding schema must be {PROJECTION_BINDING_SCHEMA}")

    binding_id = _identifier(raw.get("projectionBindingId"), "projectionBindingId")
    chat_request_id = _identifier(raw.get("chatRequestId"), "chatRequestId")
    expected_request = _optional_identifier(expected_chat_request_id, "expectedChatRequestId")
    if expected_request and chat_request_id != expected_request:
        raise ProjectionBindingError("projection binding chatRequestId does not match the delivery")

    route_raw = raw.get("route")
    if route_raw is not None and not isinstance(route_raw, Mapping):
        raise ProjectionBindingError("projection binding route must be an object")
    route_raw = route_raw if isinstance(route_raw, Mapping) else {}
    platform = _coalesced_route_text(raw, route_raw, "platform", "platform", lowercase=True)
    if platform not in SUPPORTED_PROJECTION_PLATFORMS:
        raise ProjectionBindingError(f"unsupported projection platform: {platform}")
    chat_id = _coalesced_route_text(raw, route_raw, "chatId", "chatId", max_chars=512)
    thread_id = _coalesced_optional_route_text(raw, route_raw, "threadId", "threadId", max_chars=512)
    chat_type = _coalesced_route_text(raw, route_raw, "chatType", "chatType", lowercase=True)
    if chat_type not in SUPPORTED_PROJECTION_CHAT_TYPES:
        raise ProjectionBindingError(f"unsupported projection chatType: {chat_type}")
    route = ProjectionRoute(
        platform=platform,
        chat_id=chat_id,
        chat_type=chat_type,
        thread_id=thread_id,
    )
    origin_message_id = _plain_text(raw.get("originMessageId"), "originMessageId", max_chars=512)
    advertised_route_digest = _projection_digest(raw.get("routeDigest"), "routeDigest")
    if advertised_route_digest != projection_route_digest(route, origin_message_id):
        raise ProjectionBindingError("projection binding routeDigest does not match its route")

    initiator_agent_id = _identifier(raw.get("initiatorAgentId"), "initiatorAgentId")
    peer_agent_id = _identifier(raw.get("peerAgentId"), "peerAgentId")
    if initiator_agent_id == peer_agent_id:
        raise ProjectionBindingError("projection binding participants must be distinct")
    normalized_local_agent = _optional_identifier(local_agent_id, "localAgentId")
    if normalized_local_agent and normalized_local_agent not in {initiator_agent_id, peer_agent_id}:
        raise ProjectionBindingError("local agent is not a participant in the projection binding")

    state = _enum(raw.get("state"), "state", BINDING_STATES)
    turn_seq = _nonnegative_int(raw.get("turnSeq"), "turnSeq")
    expires_at_value = _parse_timestamp(raw.get("expiresAt"), "expiresAt")
    issued_at_raw = raw.get("issuedAt")
    issued_at_value = _parse_timestamp(issued_at_raw, "issuedAt") if issued_at_raw is not None else None
    if issued_at_value and issued_at_value >= expires_at_value:
        raise ProjectionBindingError("projection binding expiresAt must be after issuedAt")
    current = _as_utc(now or datetime.now(timezone.utc))
    if expires_at_value <= current and not allow_expired and state not in {"ended", "expired"}:
        raise ProjectionBindingError("projection binding has expired")

    authority_raw = raw.get("authority")
    if not isinstance(authority_raw, Mapping):
        raise ProjectionBindingError("projection binding authority must be an object")
    authority = json.loads(json.dumps(dict(authority_raw), ensure_ascii=False, sort_keys=True))
    if authority.get("source") != "claworld_relay":
        raise ProjectionBindingError("projection binding authority source must be claworld_relay")
    authority_kind = _plain_text(authority.get("kind"), "authority.kind", max_chars=64)
    if authority_kind not in {"relay_control_plane", "signed_binding"}:
        raise ProjectionBindingError("projection binding authority kind is unsupported")
    if authority_kind == "signed_binding" and not _optional_plain_text(
        authority.get("proof"), "authority.proof", max_chars=8192
    ):
        raise ProjectionBindingError("signed projection binding requires authority.proof")

    raw_mentions = raw.get("botMentions", [])
    if raw_mentions is None:
        raw_mentions = []
    if not isinstance(raw_mentions, list):
        raise ProjectionBindingError("projection binding botMentions must be an array")
    bot_mentions: list[str] = []
    for index, value in enumerate(raw_mentions):
        mention = _plain_text(value, f"botMentions[{index}]", max_chars=512)
        if mention not in bot_mentions:
            bot_mentions.append(mention)
    if len(bot_mentions) > 16:
        raise ProjectionBindingError("projection binding has too many bot mentions")
    channel_identity_binding_id = _optional_identifier(
        raw.get("channelIdentityBindingId"),
        "channelIdentityBindingId",
    )

    return ProjectionBinding(
        projection_binding_id=binding_id,
        chat_request_id=chat_request_id,
        route=route,
        initiator_agent_id=initiator_agent_id,
        peer_agent_id=peer_agent_id,
        origin_message_id=origin_message_id,
        state=state,
        turn_seq=turn_seq,
        expires_at=_format_timestamp(expires_at_value),
        issued_at=_format_timestamp(issued_at_value) if issued_at_value else None,
        authority=authority,
        channel_identity_binding_id=channel_identity_binding_id,
        bot_mentions=tuple(bot_mentions),
    )


def new_projection_request(
    *,
    client_request_id: str,
    platform: str,
    chat_id: str,
    chat_type: str,
    thread_id: str | None,
    origin_message_id: str,
    local_agent_id: str,
) -> ProjectionRequest:
    route = _validate_route(platform, chat_id, thread_id, chat_type)
    return ProjectionRequest(
        client_request_id=_identifier(client_request_id, "clientRequestId"),
        route=route,
        origin_message_id=_plain_text(origin_message_id, "originMessageId", max_chars=512),
        local_agent_id=_identifier(local_agent_id, "localAgentId"),
    )


def projection_route_digest(route: ProjectionRoute, origin_message_id: str) -> str:
    """Hash the backend-canonical immutable origin route.

    The exact JSON input is the compact array
    ``[platform, chatId, threadId|null, chatType, originMessageId]``.
    """

    if not isinstance(route, ProjectionRoute):
        raise TypeError("route must be a ProjectionRoute")
    validated_route = _validate_route(
        route.platform,
        route.chat_id,
        route.thread_id,
        route.chat_type,
    )
    normalized_origin_message_id = _plain_text(
        origin_message_id,
        "originMessageId",
        max_chars=512,
    )
    canonical = [
        validated_route.platform,
        validated_route.chat_id,
        validated_route.thread_id,
        validated_route.chat_type,
        normalized_origin_message_id,
    ]
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def projection_idempotency_key(projection_binding_id: str, delivery_id: str) -> str:
    binding_id = _identifier(projection_binding_id, "projectionBindingId")
    normalized_delivery_id = _identifier(delivery_id, "deliveryId")
    return f"{binding_id}:{normalized_delivery_id}:reply"


def projection_attempt_id(projection_binding_id: str, delivery_id: str) -> str:
    """Build the backend-canonical deterministic projection attempt id."""

    key = projection_idempotency_key(projection_binding_id, delivery_id)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return f"pat_{digest}"


def sanitize_public_projection_text(
    content: Any,
    *,
    bot_mentions: Iterable[str] = (),
    max_chars: int = 12000,
) -> ProjectionTextResult:
    """Return only final text that is safe to expose to the origin group."""

    raw = unicodedata.normalize("NFC", str(content or ""))
    if any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in raw):
        return ProjectionTextResult(blocked_reason="control_characters")
    normalized = raw.strip()
    if not normalized:
        return ProjectionTextResult(suppressed_reason="empty")
    # NO_REPLY is an internal silence directive, never public content.  If it
    # appears as its own line alongside visible prose, suppress the *entire*
    # projection instead of stripping the line and leaking a partial reply.
    if _NO_REPLY_LINE_RE.search(normalized):
        return ProjectionTextResult(suppressed_reason="no_reply")
    for pattern in _OPERATIONAL_WHOLE_MESSAGE_PATTERNS:
        if pattern.search(normalized):
            reason = "no_reply" if normalized == "NO_REPLY" else "operational_notice"
            return ProjectionTextResult(suppressed_reason=reason)

    removed: list[str] = []
    if _CONTROL_MARKER_RE.search(normalized):
        normalized = _CONTROL_MARKER_RE.sub("", normalized)
        removed.append("internal_control_marker")
    if _OPERATIONAL_SUFFIX_RE.search(normalized):
        normalized = _OPERATIONAL_SUFFIX_RE.sub("", normalized)
        removed.append("operational_suffix")

    for reason, pattern in _HIGH_RISK_PATTERNS:
        if pattern.search(normalized):
            return ProjectionTextResult(blocked_reason=reason, removed_markers=tuple(removed))

    normalized, mentions_removed = _remove_bot_mentions(normalized, bot_mentions)
    if mentions_removed:
        removed.append("bot_mention")
    normalized = _normalize_visible_whitespace(normalized)
    if not normalized:
        return ProjectionTextResult(suppressed_reason="control_only", removed_markers=tuple(removed))
    if len(normalized) > max_chars:
        return ProjectionTextResult(blocked_reason="message_too_long", removed_markers=tuple(removed))
    return ProjectionTextResult(text=normalized, removed_markers=tuple(removed))


def require_public_projection_text(
    content: Any,
    *,
    bot_mentions: Iterable[str] = (),
    max_chars: int = 12000,
) -> str:
    result = sanitize_public_projection_text(content, bot_mentions=bot_mentions, max_chars=max_chars)
    if result.blocked_reason:
        raise ProjectionContentBlocked(result.blocked_reason)
    if result.suppressed_reason:
        raise ProjectionContentBlocked(result.suppressed_reason)
    return result.text


class ProjectionStore:
    """Atomic, permission-restricted storage for projection runtime state."""

    def __init__(self, memory_root: Path | str) -> None:
        self.memory_root = Path(memory_root).expanduser()
        self.root = self.memory_root / "runtime" / "projections"
        self.bindings_dir = self.root / "bindings"
        self.requests_dir = self.root / "requests"
        self.outbox_dir = self.root / "outbox"
        self._state_lock = threading.RLock()
        self._locks_guard = threading.Lock()
        self._binding_locks: dict[str, asyncio.Lock] = {}
        self._ensure_directories()

    def binding_lock(self, projection_binding_id: str) -> asyncio.Lock:
        binding_id = _identifier(projection_binding_id, "projectionBindingId")
        with self._locks_guard:
            lock = self._binding_locks.get(binding_id)
            if lock is None:
                lock = asyncio.Lock()
                self._binding_locks[binding_id] = lock
            return lock

    @asynccontextmanager
    async def hold_binding(self, projection_binding_id: str) -> AsyncIterator[None]:
        lock = self.binding_lock(projection_binding_id)
        async with lock:
            yield

    def save_binding(self, binding: ProjectionBinding) -> ProjectionBinding:
        if not isinstance(binding, ProjectionBinding):
            raise TypeError("binding must be a ProjectionBinding")
        path = self._record_path(self.bindings_dir, binding.projection_binding_id)
        with self._state_lock:
            existing_payload = self._read_payload(path)
            if existing_payload is None:
                self._atomic_write(path, _with_timestamps(binding.to_dict()))
                return binding
            existing = validate_trusted_projection_binding(existing_payload, allow_expired=True)
            _assert_same_fingerprint(
                "projection binding",
                existing.immutable_authority_fingerprint,
                binding.immutable_authority_fingerprint,
            )
            _validate_expires_at_update(existing, binding)
            _validate_transition("projection binding", existing.state, binding.state, _BINDING_TRANSITIONS)
            _validate_channel_identity_update(existing, binding)
            _validate_bot_mentions_update(existing, binding)
            if binding.turn_seq < existing.turn_seq:
                raise ProjectionStateError("projection binding turnSeq cannot regress")
            if binding == existing:
                return existing
            self._atomic_write(path, _with_timestamps(binding.to_dict(), existing_payload))
            return binding

    def load_binding(self, projection_binding_id: str) -> ProjectionBinding | None:
        path = self._record_path(self.bindings_dir, projection_binding_id)
        with self._state_lock:
            payload = self._read_payload(path)
        return validate_trusted_projection_binding(payload, allow_expired=True) if payload else None

    def find_binding_by_chat_request(self, chat_request_id: str) -> ProjectionBinding | None:
        request_id = _identifier(chat_request_id, "chatRequestId")
        matches = [binding for binding in self.list_bindings() if binding.chat_request_id == request_id]
        if len(matches) > 1:
            raise ProjectionPersistenceError("multiple projection bindings exist for one chatRequestId")
        return matches[0] if matches else None

    def list_bindings(self) -> list[ProjectionBinding]:
        return [
            validate_trusted_projection_binding(payload, allow_expired=True)
            for payload in self._list_payloads(self.bindings_dir)
        ]

    def update_binding_state(
        self,
        projection_binding_id: str,
        *,
        state: str,
        turn_seq: int | None = None,
    ) -> ProjectionBinding:
        existing = self.load_binding(projection_binding_id)
        if existing is None:
            raise ProjectionStateError("projection binding was not found")
        normalized_state = _enum(state, "state", BINDING_STATES)
        normalized_turn_seq = existing.turn_seq if turn_seq is None else _nonnegative_int(turn_seq, "turnSeq")
        return self.save_binding(replace(existing, state=normalized_state, turn_seq=normalized_turn_seq))

    def save_request(self, request: ProjectionRequest) -> ProjectionRequest:
        _validate_request(request)
        path = self._record_path(self.requests_dir, request.client_request_id)
        with self._state_lock:
            existing_payload = self._read_payload(path)
            if existing_payload is None:
                self._atomic_write(path, _with_timestamps(request.to_dict()))
                return request
            existing = _request_from_payload(existing_payload)
            _assert_same_fingerprint(
                "projection request",
                existing.immutable_fingerprint,
                request.immutable_fingerprint,
            )
            _validate_transition("projection request", existing.state, request.state, _REQUEST_TRANSITIONS)
            for field, current, incoming in (
                ("chatRequestId", existing.chat_request_id, request.chat_request_id),
                ("projectionBindingId", existing.projection_binding_id, request.projection_binding_id),
            ):
                if current and incoming and current != incoming:
                    raise ProjectionStateError(f"projection request {field} is immutable once assigned")
            if request == existing:
                return existing
            self._atomic_write(path, _with_timestamps(request.to_dict(), existing_payload))
            return request

    def load_request(self, client_request_id: str) -> ProjectionRequest | None:
        path = self._record_path(self.requests_dir, client_request_id)
        with self._state_lock:
            payload = self._read_payload(path)
        return _request_from_payload(payload) if payload else None

    def update_request(
        self,
        client_request_id: str,
        *,
        state: str,
        chat_request_id: str | None = None,
        projection_binding_id: str | None = None,
        failure_code: str | None = None,
        failure_reason: str | None = None,
    ) -> ProjectionRequest:
        existing = self.load_request(client_request_id)
        if existing is None:
            raise ProjectionStateError("projection request was not found")
        updated = replace(
            existing,
            state=_enum(state, "state", REQUEST_STATES),
            chat_request_id=_optional_identifier(chat_request_id, "chatRequestId") or existing.chat_request_id,
            projection_binding_id=(
                _optional_identifier(projection_binding_id, "projectionBindingId") or existing.projection_binding_id
            ),
            failure_code=_optional_plain_text(failure_code, "failureCode", max_chars=128),
            failure_reason=_optional_plain_text(failure_reason, "failureReason", max_chars=2048),
        )
        return self.save_request(updated)

    def claim_outbox(
        self,
        *,
        binding: ProjectionBinding,
        delivery_id: str,
        turn_seq: int,
        public_text: str,
    ) -> ProjectionOutboxRecord:
        if not isinstance(binding, ProjectionBinding):
            raise TypeError("binding must be a ProjectionBinding")
        persisted_binding = self.load_binding(binding.projection_binding_id)
        if persisted_binding is None:
            raise ProjectionStateError("projection binding must be persisted before claiming outbox work")
        _assert_same_fingerprint(
            "projection binding",
            persisted_binding.immutable_fingerprint,
            binding.immutable_fingerprint,
        )
        if persisted_binding.state != "active":
            raise ProjectionStateError("projection outbox requires an active binding")
        if _parse_timestamp(persisted_binding.expires_at, "expiresAt") <= datetime.now(timezone.utc):
            raise ProjectionStateError("projection outbox cannot use an expired binding")
        binding = persisted_binding
        normalized_delivery_id = _identifier(delivery_id, "deliveryId")
        normalized_turn_seq = _positive_int(turn_seq, "turnSeq")
        normalized_text = require_public_projection_text(public_text, bot_mentions=binding.bot_mentions)
        key = projection_idempotency_key(binding.projection_binding_id, normalized_delivery_id)
        item = ProjectionOutboxRecord(
            idempotency_key=key,
            projection_binding_id=binding.projection_binding_id,
            delivery_id=normalized_delivery_id,
            turn_seq=normalized_turn_seq,
            route_digest=binding.route_digest,
            public_text=normalized_text,
            body_hash=hashlib.sha256(normalized_text.encode("utf-8")).hexdigest(),
        )
        path = self._record_path(self.outbox_dir, key)
        with self._state_lock:
            existing_payload = self._read_payload(path)
            if existing_payload is None:
                self._atomic_write(path, _with_timestamps(item.to_dict()))
                return item
            existing = _outbox_from_payload(existing_payload)
            _assert_same_fingerprint(
                "projection outbox item",
                existing.immutable_fingerprint,
                item.immutable_fingerprint,
            )
            if existing.public_text != item.public_text:
                raise ProjectionStateError("projection outbox public text is immutable")
            return existing

    def load_outbox(self, idempotency_key: str) -> ProjectionOutboxRecord | None:
        path = self._record_path(self.outbox_dir, idempotency_key)
        with self._state_lock:
            payload = self._read_payload(path)
        return _outbox_from_payload(payload) if payload else None

    def list_outbox(
        self,
        *,
        projection_binding_id: str | None = None,
        statuses: Iterable[str] | None = None,
    ) -> list[ProjectionOutboxRecord]:
        binding_id = _optional_identifier(projection_binding_id, "projectionBindingId")
        normalized_statuses = None
        if statuses is not None:
            normalized_statuses = {_enum(status, "status", OUTBOX_STATES) for status in statuses}
        items = [_outbox_from_payload(payload) for payload in self._list_payloads(self.outbox_dir)]
        return [
            item
            for item in items
            if (not binding_id or item.projection_binding_id == binding_id)
            and (normalized_statuses is None or item.status in normalized_statuses)
        ]

    def transition_outbox(
        self,
        idempotency_key: str,
        *,
        status: str,
        external_message_id: str | None = None,
        failure_code: str | None = None,
        failure_reason: str | None = None,
        retryable: bool | None = None,
    ) -> ProjectionOutboxRecord:
        existing = self.load_outbox(idempotency_key)
        if existing is None:
            raise ProjectionStateError("projection outbox item was not found")
        normalized_status = _enum(status, "status", OUTBOX_STATES)
        _validate_transition("projection outbox item", existing.status, normalized_status, _OUTBOX_TRANSITIONS)
        normalized_external_id = _optional_plain_text(external_message_id, "externalMessageId", max_chars=512)
        if (
            existing.external_message_id
            and normalized_external_id
            and existing.external_message_id != normalized_external_id
        ):
            raise ProjectionStateError("projection externalMessageId is immutable")
        resolved_external_id = existing.external_message_id or normalized_external_id
        normalized_failure_code = _optional_plain_text(failure_code, "failureCode", max_chars=128)
        normalized_failure_reason = _optional_plain_text(failure_reason, "failureReason", max_chars=2048)
        if normalized_status in {"sent", "receipt_pending", "complete"} and not resolved_external_id:
            raise ProjectionStateError(f"projection outbox status {normalized_status} requires externalMessageId")
        if normalized_status == "failed" and not (normalized_failure_code or normalized_failure_reason):
            raise ProjectionStateError("failed projection outbox item requires a failure reason")
        if existing.status == "failed" and normalized_status == "sending" and existing.retryable is not True:
            raise ProjectionStateError("non-retryable projection failure cannot be retried")

        attempts = existing.attempts
        if normalized_status == "sending" and existing.status != "sending":
            attempts += 1
        keep_failure = normalized_status in {"failed", "receipt_pending"}
        resolved_failure_code = normalized_failure_code or (existing.failure_code if keep_failure else None)
        resolved_failure_reason = normalized_failure_reason or (existing.failure_reason if keep_failure else None)
        resolved_retryable = retryable if retryable is not None else (existing.retryable if keep_failure else None)
        updated = replace(
            existing,
            status=normalized_status,
            external_message_id=resolved_external_id,
            failure_code=resolved_failure_code if keep_failure else None,
            failure_reason=resolved_failure_reason if keep_failure else None,
            retryable=resolved_retryable if keep_failure else None,
            attempts=attempts,
        )
        if updated == existing:
            return existing
        path = self._record_path(self.outbox_dir, idempotency_key)
        with self._state_lock:
            current_payload = self._read_payload(path)
            if current_payload is None:
                raise ProjectionStateError("projection outbox item disappeared")
            current = _outbox_from_payload(current_payload)
            if current != existing:
                raise ProjectionStateError("projection outbox item changed concurrently")
            self._atomic_write(path, _with_timestamps(updated.to_dict(), current_payload))
        return updated

    def _ensure_directories(self) -> None:
        for path in (self.root, self.bindings_dir, self.requests_dir, self.outbox_dir):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                path.chmod(0o700)
            except OSError as exc:
                raise ProjectionPersistenceError(f"cannot secure projection state directory: {path}") from exc

    def _record_path(self, directory: Path, key: Any) -> Path:
        normalized = _plain_text(key, "record key", max_chars=2048)
        return directory / f"{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}.json"

    def _read_payload(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ProjectionPersistenceError(f"invalid projection state file: {path}") from exc
        if not isinstance(payload, dict):
            raise ProjectionPersistenceError(f"projection state file is not an object: {path}")
        return payload

    def _list_payloads(self, directory: Path) -> list[dict[str, Any]]:
        with self._state_lock:
            return [payload for path in sorted(directory.glob("*.json")) if (payload := self._read_payload(path))]

    def _atomic_write(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            path.chmod(0o600)
            _fsync_directory(path.parent)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)


def _validate_route(platform: Any, chat_id: Any, thread_id: Any, chat_type: Any) -> ProjectionRoute:
    normalized_platform = _plain_text(platform, "platform", max_chars=32).lower()
    if normalized_platform == "lark":
        normalized_platform = "feishu"
    if normalized_platform not in SUPPORTED_PROJECTION_PLATFORMS:
        raise ProjectionBindingError(f"unsupported projection platform: {normalized_platform}")
    normalized_chat_type = _plain_text(chat_type, "chatType", max_chars=32).lower()
    if normalized_chat_type not in SUPPORTED_PROJECTION_CHAT_TYPES:
        raise ProjectionBindingError(f"unsupported projection chatType: {normalized_chat_type}")
    return ProjectionRoute(
        platform=normalized_platform,
        chat_id=_plain_text(chat_id, "chatId", max_chars=512),
        chat_type=normalized_chat_type,
        thread_id=_optional_plain_text(thread_id, "threadId", max_chars=512),
    )


def _validate_request(request: ProjectionRequest) -> None:
    if not isinstance(request, ProjectionRequest):
        raise TypeError("request must be a ProjectionRequest")
    _identifier(request.client_request_id, "clientRequestId")
    _validate_route(
        request.route.platform,
        request.route.chat_id,
        request.route.thread_id,
        request.route.chat_type,
    )
    _plain_text(request.origin_message_id, "originMessageId", max_chars=512)
    _identifier(request.local_agent_id, "localAgentId")
    _enum(request.state, "state", REQUEST_STATES)
    if request.state == "pending" and any(
        (
            request.chat_request_id,
            request.projection_binding_id,
            request.failure_code,
            request.failure_reason,
        )
    ):
        raise ProjectionStateError("pending projection request cannot contain a result")
    if request.state == "bound" and not (request.chat_request_id and request.projection_binding_id):
        raise ProjectionStateError("bound projection request requires chatRequestId and projectionBindingId")
    if request.state == "bound" and (request.failure_code or request.failure_reason):
        raise ProjectionStateError("bound projection request cannot contain a failure")
    if request.state == "failed" and not (request.failure_code or request.failure_reason):
        raise ProjectionStateError("failed projection request requires a failure reason")


def _request_from_payload(payload: Mapping[str, Any]) -> ProjectionRequest:
    if payload.get("schema") != PROJECTION_REQUEST_SCHEMA:
        raise ProjectionPersistenceError("invalid projection request schema")
    route_raw = payload.get("route")
    if not isinstance(route_raw, Mapping):
        raise ProjectionPersistenceError("projection request route is missing")
    route = _validate_route(
        route_raw.get("platform"),
        route_raw.get("chatId"),
        route_raw.get("threadId"),
        route_raw.get("chatType"),
    )
    origin_message_id = _plain_text(payload.get("originMessageId"), "originMessageId", max_chars=512)
    advertised_digest = _projection_digest(payload.get("routeDigest"), "routeDigest")
    if advertised_digest != projection_route_digest(route, origin_message_id):
        raise ProjectionPersistenceError("projection request routeDigest mismatch")
    request = ProjectionRequest(
        client_request_id=_identifier(payload.get("clientRequestId"), "clientRequestId"),
        route=route,
        origin_message_id=origin_message_id,
        local_agent_id=_identifier(payload.get("localAgentId"), "localAgentId"),
        state=_enum(payload.get("state"), "state", REQUEST_STATES),
        chat_request_id=_optional_identifier(payload.get("chatRequestId"), "chatRequestId"),
        projection_binding_id=_optional_identifier(payload.get("projectionBindingId"), "projectionBindingId"),
        failure_code=_optional_plain_text(payload.get("failureCode"), "failureCode", max_chars=128),
        failure_reason=_optional_plain_text(payload.get("failureReason"), "failureReason", max_chars=2048),
    )
    _validate_request(request)
    return request


def _outbox_from_payload(payload: Mapping[str, Any]) -> ProjectionOutboxRecord:
    if payload.get("schema") != PROJECTION_OUTBOX_SCHEMA:
        raise ProjectionPersistenceError("invalid projection outbox schema")
    public_text = _plain_text(payload.get("publicText"), "publicText", max_chars=12000)
    body_hash = _plain_text(payload.get("bodyHash"), "bodyHash", max_chars=128)
    if hashlib.sha256(public_text.encode("utf-8")).hexdigest() != body_hash:
        raise ProjectionPersistenceError("projection outbox bodyHash mismatch")
    item = ProjectionOutboxRecord(
        idempotency_key=_plain_text(payload.get("idempotencyKey"), "idempotencyKey", max_chars=2048),
        projection_binding_id=_identifier(payload.get("projectionBindingId"), "projectionBindingId"),
        delivery_id=_identifier(payload.get("deliveryId"), "deliveryId"),
        turn_seq=_positive_int(payload.get("turnSeq"), "turnSeq"),
        route_digest=_projection_digest(payload.get("routeDigest"), "routeDigest"),
        public_text=public_text,
        body_hash=body_hash,
        status=_enum(payload.get("status"), "status", OUTBOX_STATES),
        external_message_id=_optional_plain_text(payload.get("externalMessageId"), "externalMessageId", max_chars=512),
        failure_code=_optional_plain_text(payload.get("failureCode"), "failureCode", max_chars=128),
        failure_reason=_optional_plain_text(payload.get("failureReason"), "failureReason", max_chars=2048),
        retryable=payload.get("retryable") if isinstance(payload.get("retryable"), bool) else None,
        attempts=_nonnegative_int(payload.get("attempts", 0), "attempts"),
    )
    expected_key = projection_idempotency_key(item.projection_binding_id, item.delivery_id)
    if item.idempotency_key != expected_key:
        raise ProjectionPersistenceError("projection outbox idempotencyKey mismatch")
    if item.status in {"sent", "receipt_pending", "complete"} and not item.external_message_id:
        raise ProjectionPersistenceError(f"projection outbox status {item.status} requires externalMessageId")
    return item


def _coalesced_route_text(
    raw: Mapping[str, Any],
    route: Mapping[str, Any],
    top_key: str,
    route_key: str,
    *,
    lowercase: bool = False,
    max_chars: int = 64,
) -> str:
    top = _optional_plain_text(raw.get(top_key), top_key, max_chars=max_chars)
    nested = _optional_plain_text(route.get(route_key), f"route.{route_key}", max_chars=max_chars)
    normalized_top = top.lower() if top and lowercase else top
    normalized_nested = nested.lower() if nested and lowercase else nested
    values = {value for value in (normalized_top, normalized_nested) if value}
    if len(values) > 1:
        raise ProjectionBindingError(f"conflicting projection route field: {top_key}")
    if not values:
        raise ProjectionBindingError(f"projection binding requires {top_key}")
    return next(iter(values))


def _coalesced_optional_route_text(
    raw: Mapping[str, Any],
    route: Mapping[str, Any],
    top_key: str,
    route_key: str,
    *,
    max_chars: int,
) -> str | None:
    top = _optional_plain_text(raw.get(top_key), top_key, max_chars=max_chars)
    nested = _optional_plain_text(route.get(route_key), f"route.{route_key}", max_chars=max_chars)
    values = {value for value in (top, nested) if value}
    if len(values) > 1:
        raise ProjectionBindingError(f"conflicting projection route field: {top_key}")
    return next(iter(values)) if values else None


def _remove_bot_mentions(text: str, bot_mentions: Iterable[str]) -> tuple[str, bool]:
    result = text
    removed = False
    for raw_identity in bot_mentions:
        identity = str(raw_identity or "").strip()
        if not identity:
            continue
        if identity.startswith("<at") or identity.startswith("@"):
            replaced = result.replace(identity, identity.lstrip("@").strip() if identity.startswith("@") else "")
            removed = removed or replaced != result
            result = replaced
        username = identity.lstrip("@").strip()
        if username and re.fullmatch(r"[A-Za-z0-9_.-]{2,128}", username):
            pattern = re.compile(rf"(?<![\w@])@{re.escape(username)}\b", re.IGNORECASE)
            result, count = pattern.subn(username, result)
            removed = removed or bool(count)
        escaped_identity = re.escape(identity)
        feishu_pattern = re.compile(
            rf"<at\b[^>]*(?:user_id|open_id)\s*=\s*[\"']?{escaped_identity}[\"']?[^>]*>.*?</at>",
            re.IGNORECASE | re.DOTALL,
        )
        result, count = feishu_pattern.subn("", result)
        removed = removed or bool(count)
    return result, removed


def _normalize_visible_whitespace(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    rendered = "\n".join(lines)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    rendered = re.sub(r" +([,.;:!?\u3002\uff0c\uff01\uff1f])", r"\1", rendered)
    return rendered.strip()


def _validate_transition(label: str, current: str, target: str, transitions: Mapping[str, frozenset[str]]) -> None:
    if current == target:
        return
    if target not in transitions.get(current, frozenset()):
        raise ProjectionStateError(f"invalid {label} state transition: {current} -> {target}")


def _validate_bot_mentions_update(existing: ProjectionBinding, incoming: ProjectionBinding) -> None:
    """Allow backend capability discovery to append mentions, then freeze them.

    ``botMentions`` are intentionally outside the binding authority fingerprint:
    the backend learns each bot identity during the two-sided capability
    handshake.  They still must not become a generally mutable field.  A
    removal could re-enable a native-channel bot mention loop, while replacing
    an entry could silently change which visible text the sanitizer strips.

    The backend emits mentions in stable participant/capability order, so the
    only accepted mutation is a prefix-preserving append while the persisted
    binding is still ``pending_capability``.  A first write received at
    ``active`` remains valid (for reconnect recovery), but is frozen
    immediately.
    """

    if existing.bot_mentions == incoming.bot_mentions:
        return
    if existing.state != "pending_capability":
        raise ProjectionStateError(
            "projection binding botMentions are immutable after capability handshake"
        )
    prefix_length = len(existing.bot_mentions)
    if incoming.bot_mentions[:prefix_length] != existing.bot_mentions:
        raise ProjectionStateError(
            "projection binding botMentions may only be appended during capability handshake"
        )


def _validate_expires_at_update(
    existing: ProjectionBinding,
    incoming: ProjectionBinding,
) -> None:
    """Accept relay renewals without ever shortening the durable authority."""

    current = _parse_timestamp(existing.expires_at, "expiresAt")
    proposed = _parse_timestamp(incoming.expires_at, "expiresAt")
    if proposed < current:
        raise ProjectionStateError(
            "projection binding expiresAt cannot move backwards"
        )


def _validate_channel_identity_update(
    existing: ProjectionBinding,
    incoming: ProjectionBinding,
) -> None:
    """Allow one recipient identity bind during the pending handshake only."""

    current = existing.channel_identity_binding_id
    proposed = incoming.channel_identity_binding_id
    if current == proposed:
        return
    if current:
        raise ProjectionStateError(
            "projection channelIdentityBindingId is immutable once assigned"
        )
    if not proposed or existing.state != "pending_capability":
        raise ProjectionStateError(
            "projection channelIdentityBindingId may only be assigned during capability handshake"
        )


def _assert_same_fingerprint(label: str, expected: str, received: str) -> None:
    if expected != received:
        raise ProjectionStateError(f"{label} immutable fields do not match persisted state")


def _with_timestamps(payload: Mapping[str, Any], existing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    now = _format_timestamp(datetime.now(timezone.utc))
    return {
        **dict(payload),
        "createdAt": (existing or {}).get("createdAt") or now,
        "updatedAt": now,
    }


def _fsync_directory(path: Path) -> None:
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _identifier(value: Any, field: str) -> str:
    normalized = _plain_text(value, field, max_chars=255)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{1,254}", normalized):
        raise ProjectionBindingError(f"{field} is not a valid identifier")
    return normalized


def _optional_identifier(value: Any, field: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return _identifier(value, field)


def _plain_text(value: Any, field: str, *, max_chars: int) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        raise ProjectionBindingError(f"{field} must be text")
    normalized = str(value).strip()
    if not normalized:
        raise ProjectionBindingError(f"{field} is required")
    if len(normalized) > max_chars:
        raise ProjectionBindingError(f"{field} is too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ProjectionBindingError(f"{field} contains control characters")
    return normalized


def _optional_plain_text(value: Any, field: str, *, max_chars: int) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return _plain_text(value, field, max_chars=max_chars)


def _projection_digest(value: Any, field: str) -> str:
    normalized = _plain_text(value, field, max_chars=71)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", normalized):
        raise ProjectionBindingError(f"{field} must use sha256:<lowercase-hex>")
    return normalized


def _enum(value: Any, field: str, allowed: Iterable[str]) -> str:
    normalized = _plain_text(value, field, max_chars=64)
    if normalized not in allowed:
        raise ProjectionBindingError(f"unsupported {field}: {normalized}")
    return normalized


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProjectionBindingError(f"{field} must be a non-negative integer")
    return value


def _positive_int(value: Any, field: str) -> int:
    parsed = _nonnegative_int(value, field)
    if parsed < 1:
        raise ProjectionBindingError(f"{field} must be a positive integer")
    return parsed


def _parse_timestamp(value: Any, field: str) -> datetime:
    normalized = _plain_text(value, field, max_chars=128)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectionBindingError(f"{field} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProjectionBindingError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProjectionBindingError("now must include a timezone")
    return value.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
