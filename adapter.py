"""Hermes Gateway Platform Adapter for Claworld."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, ProcessingOutcome, SendResult

from .config import ClaworldConfig
from .protocol import build_agent_text
from .relay_client import RelayClient
from .session_router import build_hermes_session_key, build_session_source, route_envelope
from .working_memory import append_journal, ensure_working_memory, record_claworld_route

logger = logging.getLogger(__name__)


@dataclass
class DeliveryRecord:
    delivery_id: str
    relay_session_key: str
    chat_id: str
    event_type: str = "delivery"
    replyable: bool = True
    replied: bool = False


class ClaworldPlatformAdapter(BasePlatformAdapter):
    supports_async_delivery = True
    supports_code_blocks = False

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform("claworld"))
        self.claworld_config = ClaworldConfig.from_platform_config(config)
        self.memory_root = self.claworld_config.memory_root_path()
        self.client: RelayClient | None = None
        self._deliveries_by_id: dict[str, DeliveryRecord] = {}
        self._latest_by_chat: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "Claworld"

    async def connect(self) -> bool:
        if not self.claworld_config.server_url or not self.claworld_config.app_token:
            self._set_fatal_error(
                "config_missing",
                "CLAWORLD_SERVER_URL and CLAWORLD_APP_TOKEN must be set",
                retryable=False,
            )
            return False
        ensure_working_memory(self.memory_root)
        self.client = RelayClient(self.claworld_config, on_delivery=self._on_delivery, logger=logger)
        await self.client.connect()
        self._mark_connected()
        logger.info("Claworld adapter connected")
        return True

    async def disconnect(self) -> None:
        if self.client is not None:
            await self.client.close()
        self.client = None
        self._mark_disconnected()

    async def send(self, chat_id: str, content: str, reply_to: str | None = None, metadata: dict | None = None) -> SendResult:
        if self.client is None:
            return SendResult(success=False, error="Claworld relay is not connected", retryable=True)
        if _is_hermes_home_channel_notice(content):
            logger.info("suppressed Hermes home-channel notice for Claworld chat_id=%s", chat_id)
            return SendResult(success=True)
        record = self._record_for_send(chat_id, reply_to)
        if record is None:
            return SendResult(success=False, error=f"No Claworld delivery is known for chat_id={chat_id}")
        if record.event_type != "delivery":
            record.replied = True
            return SendResult(success=True, message_id=record.delivery_id)
        try:
            if not record.replyable:
                record.replied = True
                return SendResult(success=True, message_id=record.delivery_id)
            elif not str(content or "").strip():
                await self.client.send_kept_silent(record.delivery_id, record.relay_session_key, "empty_reply")
            elif _is_no_reply(content):
                await self.client.send_kept_silent(record.delivery_id, record.relay_session_key, "no_reply")
            else:
                await self.client.send_reply(record.delivery_id, record.relay_session_key, content)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        record.replied = True
        return SendResult(success=True, message_id=record.delivery_id)

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        record = self._deliveries_by_id.get(str(event.message_id or ""))
        if record is None or record.replied or self.client is None:
            return
        if record.event_type == "delivery" and record.replyable:
            try:
                await self.client.send_kept_silent(record.delivery_id, record.relay_session_key, _completion_silence_reason(outcome, record))
                record.replied = True
            except Exception as exc:
                logger.warning("failed to mark Claworld delivery kept_silent: %s", exc)
        elif record.event_type == "delivery":
            record.replied = True

    async def get_chat_info(self, chat_id: str) -> dict:
        chat_id_text = str(chat_id)
        return {"name": chat_id_text, "type": "dm", "chat_id": chat_id_text}

    async def _on_delivery(self, envelope) -> None:
        route = route_envelope(envelope, self.claworld_config)
        source = build_session_source(route, envelope)
        hermes_session_key = build_hermes_session_key(route)
        record = DeliveryRecord(
            delivery_id=envelope.delivery_id,
            relay_session_key=envelope.session_key,
            chat_id=route.chat_id,
            event_type=envelope.event_type,
            replyable=_is_replyable_delivery(envelope),
        )
        self._deliveries_by_id[record.delivery_id] = record
        self._latest_by_chat[route.chat_id] = record.delivery_id

        ensure_working_memory(self.memory_root)
        record_claworld_route(self.memory_root, route, hermes_session_key, envelope)
        append_journal(
            self.memory_root,
            {
                "kind": "inbound_delivery",
                "sessionKind": route.session_kind,
                "deliveryId": envelope.delivery_id,
                "eventType": envelope.event_type,
                "eventName": envelope.event_name,
                "relaySessionKey": envelope.session_key,
                "hermesSessionKey": hermes_session_key,
                "conversationKey": envelope.conversation_key,
                "worldId": envelope.world_id,
                "createdAt": envelope.created_at,
                "updatedAt": envelope.updated_at,
            },
        )

        if self.client is not None and _requires_acceptance_delivery(envelope):
            await self.client.send_accepted(envelope.delivery_id, envelope.session_key)

        event = MessageEvent(
            text=build_agent_text(envelope, route.session_kind),
            message_type=MessageType.TEXT,
            source=source,
            raw_message=envelope.raw,
            message_id=envelope.delivery_id,
            internal=True,
        )
        await self.handle_message(event)

    def _record_for_send(self, chat_id: str, reply_to: str | None) -> DeliveryRecord | None:
        if reply_to:
            record = self._deliveries_by_id.get(str(reply_to))
            if record is not None:
                return record
        latest_id = self._latest_by_chat.get(str(chat_id))
        if latest_id:
            return self._deliveries_by_id.get(latest_id)
        return None


def _is_no_reply(content: str) -> bool:
    return str(content or "").strip() == "NO_REPLY"


def _is_hermes_home_channel_notice(content: str) -> bool:
    text = str(content or "").strip()
    return (
        "No home channel is set for Claworld." in text
        and "A home channel is where Hermes delivers cron job results" in text
        and "make this chat your home channel" in text
    )


def _completion_silence_reason(outcome: ProcessingOutcome, record: DeliveryRecord) -> str:
    if outcome != ProcessingOutcome.SUCCESS:
        return "runtime_failed_before_reply"
    if not record.replyable:
        return "non_replyable_delivery"
    return "no_renderable_reply"


def _is_replyable_delivery(envelope) -> bool:
    if envelope.event_type != "delivery":
        return False
    for source in (envelope.metadata, envelope.payload):
        if isinstance(source, dict) and source.get("allowReply") is False:
            return False
    return True


def _requires_acceptance_delivery(envelope) -> bool:
    if envelope.event_type != "delivery":
        return False
    for source in (envelope.metadata, envelope.payload):
        if isinstance(source, dict) and source.get("acceptanceRequired") is False:
            return False
    return True
