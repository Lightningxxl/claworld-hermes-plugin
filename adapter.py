"""Hermes Gateway Platform Adapter for Claworld."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, ProcessingOutcome, SendResult

from .config import ClaworldConfig
from .protocol import build_agent_guidance, build_agent_text, classify_reply_content
from .relay_client import RelayClient
from .session_router import build_hermes_session_key, build_session_source, route_envelope
from .working_memory import (
    append_journal,
    build_prompt_context,
    claim_inbound_notification,
    complete_inbound_notification,
    ensure_working_memory,
    read_session_index,
    record_claworld_route,
    record_outbound_reply,
    release_inbound_notification,
)

logger = logging.getLogger(__name__)


@dataclass
class DeliveryRecord:
    delivery_id: str
    relay_session_key: str
    chat_id: str
    event_type: str = "delivery"
    replyable: bool = True
    replied: bool = False
    delivery_type: str | None = None
    chat_request_id: str | None = None
    retried: bool = False
    saw_operational_notice: bool = False


_HERMES_TRANSIENT_STATUS_PATTERNS = (
    re.compile("^\u23f3\\s*Working\\s+\u2014\\s+\\d+\\s+min(?:\\s+\u2014\\s+.*)?$", re.IGNORECASE),
    re.compile("^\U0001f504\\s*Primary model failed\\s+\u2014\\s+switching to fallback:", re.IGNORECASE),
)


def _management_notification_key(envelope, route) -> str | None:
    if envelope.event_type == "delivery" or route.session_kind != "management":
        return None
    candidates = (
        envelope.metadata.get("notificationId"),
        envelope.metadata.get("inboxItemId"),
        envelope.delivery_id,
    )
    for candidate in candidates:
        normalized = str(candidate or "").strip()
        if normalized:
            return normalized
    return None


class ClaworldPlatformAdapter(BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = False
    supports_async_delivery = True
    supports_code_blocks = False

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform("claworld"))
        self.claworld_config = ClaworldConfig.from_platform_config(config)
        self.memory_root = self.claworld_config.memory_root_path()
        self.client: RelayClient | None = None
        self._deliveries_by_id: dict[str, DeliveryRecord] = {}
        self._latest_by_chat: dict[str, str] = {}
        self._kickoff_retry_context: dict[str, dict] = {}

    @property
    def name(self) -> str:
        return "Claworld"

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.claworld_config.server_url or not self.claworld_config.app_token:
            self._set_fatal_error(
                "config_missing",
                "CLAWORLD_APP_TOKEN must be set",
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
        if _is_hermes_transient_status_notice(content):
            logger.info("suppressed Hermes transient status for Claworld chat_id=%s", chat_id)
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
            classification = classify_reply_content(content)
            if classification.silence_reason:
                record.saw_operational_notice = True
                logger.info(
                    "deferring operational notice for delivery_id=%s reason=%s",
                    record.delivery_id, classification.silence_reason,
                )
            else:
                await self.client.send_reply(record.delivery_id, record.relay_session_key, classification.text)
                record.replied = True
                try:
                    record_outbound_reply(
                        self.memory_root,
                        chat_request_id=record.chat_request_id,
                        delivery_id=record.delivery_id,
                        from_agent_id=self.claworld_config.agent_id,
                        command_text=classification.text,
                    )
                except Exception as exc:
                    logger.warning("failed to index acknowledged Claworld reply: %s", exc)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        return SendResult(success=True, message_id=record.delivery_id)

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        record = self._deliveries_by_id.get(str(event.message_id or ""))
        if record is None or record.replied or self.client is None:
            return
        if record.event_type != "delivery":
            record.replied = True
            return
        if not record.replyable:
            record.replied = True
            return

        if (
            record.delivery_type == "kickoff"
            and not record.retried
            and record.saw_operational_notice
            and outcome == ProcessingOutcome.SUCCESS
        ):
            retry_context = self._kickoff_retry_context.pop(record.delivery_id, None)
            if retry_context is not None:
                record.retried = True
                record.saw_operational_notice = False
                logger.info(
                    "retrying kickoff delivery_id=%s — first attempt produced only operational notices",
                    record.delivery_id,
                )
                retry_event = MessageEvent(
                    text=build_agent_text(retry_context["envelope"]),
                    message_type=MessageType.TEXT,
                    source=retry_context["source"],
                    raw_message=retry_context["envelope"].raw,
                    message_id=record.delivery_id,
                    channel_prompt=retry_context["channel_prompt"],
                    internal=True,
                )
                try:
                    await self.handle_message(retry_event)
                except Exception:
                    try:
                        await self._mark_record_kept_silent(record, "runtime_failed_before_reply")
                    except Exception as exc:
                        logger.warning("failed to mark retried kickoff delivery kept_silent: %s", exc)
                return

        try:
            await self._mark_record_kept_silent(record, _completion_silence_reason(outcome, record))
        except Exception as exc:
            logger.warning("failed to mark Claworld delivery kept_silent: %s", exc)

    async def get_chat_info(self, chat_id: str) -> dict:
        chat_id_text = str(chat_id)
        return {"name": chat_id_text, "type": "dm", "chat_id": chat_id_text}

    async def _on_delivery(self, envelope) -> None:
        ensure_working_memory(self.memory_root)
        existing_episode = None
        if envelope.chat_request_id:
            index = read_session_index(self.memory_root)
            episodes = index.get("conversationEpisodes") if isinstance(index.get("conversationEpisodes"), dict) else {}
            candidate = episodes.get(envelope.chat_request_id)
            existing_episode = candidate if isinstance(candidate, dict) else None
        route = route_envelope(envelope, self.claworld_config, existing_episode=existing_episode)
        notification_key = _management_notification_key(envelope, route)
        notification_claim = None
        if notification_key:
            notification_claim = claim_inbound_notification(self.memory_root, notification_key)
            if not notification_claim["claimed"]:
                logger.info(
                    "suppressed duplicate Claworld Management notification delivery_id=%s reason=%s",
                    envelope.delivery_id,
                    notification_claim["reason"],
                )
                return
        try:
            await self._dispatch_inbound_envelope(envelope, route)
            if notification_claim is not None:
                complete_inbound_notification(notification_claim)
        except Exception:
            if notification_claim is not None:
                release_inbound_notification(notification_claim)
            raise

    async def _dispatch_inbound_envelope(self, envelope, route) -> None:
        source = build_session_source(route, envelope)
        hermes_session_key = build_hermes_session_key(route)
        record = DeliveryRecord(
            delivery_id=envelope.delivery_id,
            relay_session_key=envelope.session_key,
            chat_id=route.chat_id,
            event_type=envelope.event_type,
            replyable=_is_replyable_delivery(envelope),
            delivery_type=envelope.metadata.get("deliveryType"),
            chat_request_id=envelope.chat_request_id,
        )
        self._deliveries_by_id[record.delivery_id] = record
        self._latest_by_chat[route.chat_id] = record.delivery_id

        record_claworld_route(self.memory_root, route, hermes_session_key, envelope)
        append_journal(
            self.memory_root,
            {
                "kind": "inbound_delivery",
                "sessionKind": route.session_kind,
                "deliveryId": envelope.delivery_id,
                "eventType": envelope.event_type,
                "eventName": envelope.event_name,
                "chatRequestId": envelope.chat_request_id,
                "relaySessionKey": envelope.session_key,
                "hermesSessionKey": hermes_session_key,
                "conversationKey": envelope.conversation_key,
                "worldId": envelope.world_id,
                "createdAt": envelope.created_at,
                "updatedAt": envelope.updated_at,
            },
        )

        if self.client is not None and _requires_acceptance_delivery(envelope):
            try:
                await self.client.send_accepted(envelope.delivery_id, envelope.session_key)
            except Exception as exc:
                logger.warning("failed to acknowledge Claworld delivery acceptance: %s", exc)

        channel_prompt = None
        try:
            channel_prompt = build_prompt_context(
                self.memory_root,
                platform="claworld",
                chat_id=route.chat_id,
            )
        except Exception as exc:
            logger.warning("failed to build Claworld channel prompt: %s", exc)
        channel_prompt = _append_prompt_guidance(channel_prompt, build_agent_guidance(envelope))

        event = MessageEvent(
            text=build_agent_text(envelope),
            message_type=MessageType.TEXT,
            source=source,
            raw_message=envelope.raw,
            message_id=envelope.delivery_id,
            channel_prompt=channel_prompt,
            internal=True,
        )

        if record.delivery_type == "kickoff" and record.replyable:
            self._kickoff_retry_context[record.delivery_id] = {
                "envelope": envelope,
                "source": source,
                "channel_prompt": channel_prompt,
            }

        try:
            await self.handle_message(event)
        except Exception:
            if record.event_type == "delivery" and record.replyable and not record.replied and self.client is not None:
                try:
                    await self._mark_record_kept_silent(record, "runtime_failed_before_reply")
                except Exception as exc:
                    logger.warning("failed to mark failed Claworld delivery kept_silent: %s", exc)
            raise

    def _record_for_send(self, chat_id: str, reply_to: str | None) -> DeliveryRecord | None:
        if reply_to:
            record = self._deliveries_by_id.get(str(reply_to))
            if record is not None:
                return record
        latest_id = self._latest_by_chat.get(str(chat_id))
        if latest_id:
            return self._deliveries_by_id.get(latest_id)
        return None

    async def _mark_record_kept_silent(self, record: DeliveryRecord, reason: str) -> None:
        if self.client is None:
            raise RuntimeError("Claworld relay is not connected")
        await self.client.send_kept_silent(record.delivery_id, record.relay_session_key, reason)
        record.replied = True


def _is_hermes_home_channel_notice(content: str) -> bool:
    text = str(content or "").strip()
    return (
        "No home channel is set for Claworld." in text
        and "A home channel is where Hermes delivers cron job results" in text
        and "make this chat your home channel" in text
    )


def _is_hermes_transient_status_notice(content: str) -> bool:
    text = str(content or "").strip()
    return bool(text) and _matches_any(_HERMES_TRANSIENT_STATUS_PATTERNS, text)


def _matches_any(patterns, text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _append_prompt_guidance(prompt: str | None, guidance: str | None) -> str | None:
    sections = [str(section).strip() for section in (prompt, guidance) if str(section or "").strip()]
    return "\n\n".join(sections) if sections else None


def _completion_silence_reason(outcome: ProcessingOutcome, record: DeliveryRecord) -> str:
    if outcome != ProcessingOutcome.SUCCESS:
        return "runtime_failed_before_reply"
    if not record.replyable:
        return "non_replyable_delivery"
    if record.saw_operational_notice:
        return "operational_notice_only"
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
