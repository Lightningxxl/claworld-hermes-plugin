"""Claworld relay protocol helpers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit


BRIDGE_PROTOCOL = "claworld.delivery_reply.v1"


def normalize_ws_url(server_url: str) -> str:
    parts = urlsplit(server_url)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    path = (parts.path or "/").rstrip("/")
    if not path:
        path = "/ws"
    elif not path.endswith("/ws"):
        path = f"{path}/ws"
    return urlunsplit((scheme, parts.netloc, path, parts.query, parts.fragment))


def normalize_http_base_url(server_url: str) -> str:
    parts = urlsplit(server_url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    return urlunsplit((scheme, parts.netloc, "", "", "")).rstrip("/")


def text(value: Any, default: str | None = None) -> str | None:
    if value is None:
        return default
    value = str(value).strip()
    return value or default


def obj(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def stable_hash(value: str, length: int = 20) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


@dataclass(frozen=True)
class InboundEnvelope:
    event_type: str
    event_name: str | None
    delivery_id: str
    session_key: str
    target_agent_id: str | None
    chat_request_id: str | None
    conversation_key: str | None
    world_id: str | None
    created_at: str | None
    updated_at: str | None
    turn_created_at: str | None
    payload: dict
    metadata: dict
    raw: dict

    @property
    def inbound_text(self) -> str:
        candidates = [
            self.payload.get("commandText"),
            self.payload.get("text"),
            self.payload.get("body"),
            self.payload.get("message"),
            self.payload.get("contextText"),
        ]
        for candidate in candidates:
            normalized = text(candidate)
            if normalized:
                return normalized
        return json.dumps(self.payload, ensure_ascii=False, sort_keys=True)


def build_inbound_envelope(message: dict) -> InboundEnvelope | None:
    data = obj(message.get("data"))
    direct_payload = obj(data.get("payload"))
    payload = dict(direct_payload) if direct_payload else dict(data)
    if direct_payload:
        for key in (
            "eventType",
            "eventName",
            "sessionKind",
            "sessionKey",
            "targetSessionKey",
            "targetAgentId",
            "text",
            "body",
            "message",
            "notification",
            "conversationKey",
            "worldId",
        ):
            if payload.get(key) is None and data.get(key) is not None:
                payload[key] = data[key]
    metadata = obj(data.get("metadata")) or obj(payload.get("metadata")) or obj(data.get("meta"))
    notification = obj(payload.get("notification")) or obj(data.get("notification"))
    relay_event = text(message.get("event"), text(message.get("type")))
    event_type = first_text(
        data.get("eventType"),
        payload.get("eventType"),
        "delivery" if relay_event == "delivery" else relay_event,
    )

    delivery_id = first_text(
        data.get("deliveryId"),
        data.get("inboxItemId"),
        data.get("messageId"),
        data.get("eventId"),
        data.get("notificationId"),
        payload.get("deliveryId"),
        payload.get("inboxItemId"),
        payload.get("messageId"),
        payload.get("eventId"),
        payload.get("notificationId"),
        metadata.get("messageId"),
        metadata.get("eventId"),
        metadata.get("notificationId"),
        notification.get("notificationId"),
    )
    session_key = first_text(
        data.get("sessionKey"),
        payload.get("sessionKey"),
        data.get("targetSessionKey"),
        payload.get("targetSessionKey"),
        notification.get("targetSessionKey"),
        metadata.get("sessionKey"),
        metadata.get("targetSessionKey"),
    )
    if not event_type or not session_key:
        return None
    if event_type == "delivery" and not delivery_id:
        return None

    related = obj(notification.get("relatedObjects"))
    chat_request_id = extract_chat_request_id(data, payload, metadata, notification, related)
    return InboundEnvelope(
        event_type=event_type,
        event_name=first_text(data.get("eventName"), payload.get("eventName"), None if relay_event == "delivery" else relay_event),
        delivery_id=delivery_id or stable_hash(f"{event_type}:{session_key}:{json.dumps(payload, sort_keys=True, default=str)}"),
        session_key=session_key,
        target_agent_id=first_text(data.get("targetAgentId"), payload.get("targetAgentId"), notification.get("targetAgentId"), metadata.get("targetAgentId")),
        chat_request_id=chat_request_id,
        conversation_key=first_text(data.get("conversationKey"), payload.get("conversationKey"), related.get("conversationKey")),
        world_id=first_text(data.get("worldId"), payload.get("worldId"), related.get("worldId")),
        created_at=first_text(data.get("createdAt"), payload.get("createdAt"), data.get("availableAt"), payload.get("availableAt"), notification.get("createdAt")),
        updated_at=first_text(data.get("updatedAt"), payload.get("updatedAt"), notification.get("updatedAt")),
        turn_created_at=first_text(data.get("turnCreatedAt"), payload.get("turnCreatedAt")),
        payload=payload,
        metadata={**metadata, "relayEvent": relay_event, "inboxItemId": first_text(data.get("inboxItemId"), payload.get("inboxItemId"))},
        raw=message,
    )


def first_text(*values: Any) -> str | None:
    for value in values:
        normalized = text(value)
        if normalized:
            return normalized
    return None


CHAT_REQUEST_ID_KEYS = (
    "chatRequestId",
    "chat_request_id",
    "intentId",
    "intent_id",
)


def extract_chat_request_id(*payloads: dict) -> str | None:
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in CHAT_REQUEST_ID_KEYS:
            value = text(payload.get(key))
            if value:
                return value
        nested = obj(payload.get("payload"))
        for key in CHAT_REQUEST_ID_KEYS:
            value = text(nested.get(key))
            if value:
                return value
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in ("commandText", "contextText", "text", "message", "body"):
            value = text(payload.get(key))
            if not value:
                continue
            parsed = _extract_chat_request_id_from_text(value)
            if parsed:
                return parsed
    return None


def _extract_chat_request_id_from_text(value: str) -> str | None:
    patterns = (
        r"(?im)^\s*-?\s*Intent ID:\s*`?([^`\n]+?)`?\s*$",
        r"(?im)^\s*-?\s*Chat Request ID:\s*`?([^`\n]+?)`?\s*$",
        r"(?im)\b(?:chatRequestId|chat_request_id|intentId|intent_id)\b\s*[:=]\s*[\"`']?([A-Za-z0-9][A-Za-z0-9_.:-]{2,})",
        r"(?im)[\"'](?:chatRequestId|chat_request_id|intentId|intent_id)[\"']\s*:\s*[\"']([^\"']+)[\"']",
    )
    for pattern in patterns:
        match = re.search(pattern, str(value or ""))
        parsed = text(match.group(1) if match else None)
        if parsed:
            return parsed
    return None


def build_agent_text(envelope: InboundEnvelope) -> str:
    context_text = text(envelope.payload.get("contextText"))
    untrusted_context = text(envelope.payload.get("untrustedContext"))
    if context_text:
        incoming_text = text(envelope.payload.get("commandText"))
    else:
        incoming_text = text(envelope.payload.get("commandText")) or text(envelope.payload.get("text"), text(envelope.payload.get("body"), text(envelope.payload.get("message"))))
    parts = [p for p in (context_text, untrusted_context, incoming_text) if p]
    if not parts:
        parts = [envelope.inbound_text] if envelope.inbound_text else []
    return "\n\n".join(parts) if parts else ""


def auth_message(agent_id: str, credential: str, client_version: str, client: str | None = None) -> dict:
    return {
        "type": "auth",
        "agentId": agent_id,
        "credential": {"type": "agent_token", "token": credential},
        **({"client": client} if client else {}),
        "clientVersion": client_version,
        "bridgeProtocol": BRIDGE_PROTOCOL,
    }


def accepted_message(delivery_id: str, session_key: str | None) -> dict:
    return {
        "type": "accepted",
        "deliveryId": delivery_id,
        "sessionKey": session_key,
        "payload": {"source": "hermes_gateway_dispatch"},
    }


def reply_message(delivery_id: str, session_key: str | None, reply_text: str) -> dict:
    return {
        "type": "reply",
        "deliveryId": delivery_id,
        "sessionKey": session_key,
        "payload": {"text": reply_text, "source": "hermes_agent"},
    }


def kept_silent_message(delivery_id: str, session_key: str | None, reason: str) -> dict:
    return {
        "type": "kept_silent",
        "deliveryId": delivery_id,
        "sessionKey": session_key,
        "payload": {"reason": reason, "source": "hermes_gateway"},
    }
