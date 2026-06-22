"""Map Claworld relay sessions onto Hermes SessionSource buckets."""

from __future__ import annotations

from dataclasses import dataclass

from .config import ClaworldConfig
from .protocol import InboundEnvelope, stable_hash, text


MANAGEMENT_EVENT_TYPES = {
    "notification",
    "domain_notification",
    "management_wake",
    "management_tick",
    "conversation_lifecycle",
    "platform_recommendation",
    "ops_recommendation",
}


@dataclass(frozen=True)
class ClaworldRoute:
    session_kind: str
    chat_id: str
    chat_name: str
    relay_session_key: str
    conversation_key: str | None
    management_key: str | None


def resolve_session_kind(envelope: InboundEnvelope) -> str:
    raw_key = envelope.session_key.lower()
    payload_kind = text(envelope.payload.get("sessionKind"))
    metadata_kind = text(envelope.metadata.get("sessionKind"))
    payload_event = text(envelope.payload.get("eventType"))
    metadata_event = text(envelope.metadata.get("eventType"))
    if payload_kind == "management" or metadata_kind == "management":
        return "management"
    if raw_key.startswith("management:") or ":management:" in raw_key:
        return "management"
    if payload_event in MANAGEMENT_EVENT_TYPES or metadata_event in MANAGEMENT_EVENT_TYPES:
        return "management"
    return "conversation"


def route_envelope(envelope: InboundEnvelope, config: ClaworldConfig) -> ClaworldRoute:
    session_kind = resolve_session_kind(envelope)
    if session_kind == "management":
        agent_part = envelope.target_agent_id or config.agent_id or config.account_id
        bucket = f"management-{stable_hash(agent_part or envelope.session_key)}"
        return ClaworldRoute(
            session_kind="management",
            chat_id=bucket,
            chat_name="Claworld Management",
            relay_session_key=envelope.session_key,
            conversation_key=envelope.conversation_key,
            management_key=bucket,
        )

    conversation_raw = envelope.conversation_key or envelope.session_key
    bucket = f"conversation-{stable_hash(conversation_raw)}"
    return ClaworldRoute(
        session_kind="conversation",
        chat_id=bucket,
        chat_name=f"Claworld Conversation {bucket[-8:]}",
        relay_session_key=envelope.session_key,
        conversation_key=envelope.conversation_key,
        management_key=None,
    )


def build_session_source(route: ClaworldRoute, envelope: InboundEnvelope):
    from gateway.config import Platform
    from gateway.session import SessionSource

    return SessionSource(
        platform=Platform("claworld"),
        chat_id=route.chat_id,
        chat_name=route.chat_name,
        chat_type="dm",
        user_id=envelope.target_agent_id or "claworld",
        user_name=route.session_kind,
        message_id=envelope.delivery_id,
    )


def build_hermes_session_key(route: ClaworldRoute) -> str:
    return f"agent:main:claworld:dm:{route.chat_id}"
