"""Hermes Gateway Platform Adapter for Claworld."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, ProcessingOutcome, SendResult

from .config import ClaworldConfig
from .projection import (
    ProjectionBindingError,
    ProjectionContentBlocked,
    ProjectionStateError,
    ProjectionStore,
    extract_trusted_projection_binding,
    projection_attempt_id,
    projection_idempotency_key,
)
from .projection_profiles import (
    ProjectionProfileSelectionError,
    ProjectionProfileStore,
)
from .projection_runtime import (
    can_send_to,
    get_current_or_default_projection_profile,
    send_without_mirror,
)
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
        self.projection_store = ProjectionStore(self.memory_root)
        self.projection_profile_store = ProjectionProfileStore(self.memory_root)

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
        self.client = RelayClient(
            self.claworld_config,
            on_delivery=self._on_delivery,
            on_control_event=self._on_projection_control_event,
            logger=logger,
        )
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
                public_group=_is_public_group_delivery(envelope),
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

    async def _on_projection_control_event(self, message: dict) -> None:
        event = str(message.get("event") or message.get("type") or "").strip()
        if event == "projection.binding.verify":
            await self._verify_projection_binding(message)
            return
        expected_state = {
            "projection.binding.ready": "active",
            "projection.binding.paused": "paused",
            "projection.binding.ended": "ended",
            "projection.binding.expired": "expired",
        }.get(event)
        if expected_state is not None:
            await self._persist_projection_binding_event(
                message,
                event=event,
                expected_state=expected_state,
            )
            return
        if event == "projection.turn.requested":
            await self._project_turn(message)
            return
        if event == "projection.receipt.accepted":
            await self._complete_projection_receipt(message)

    async def _persist_projection_binding_event(
        self,
        message: dict,
        *,
        event: str,
        expected_state: str,
    ) -> None:
        local_agent_id = self._projection_agent_id()
        binding = extract_trusted_projection_binding(
            message,
            local_agent_id=local_agent_id,
            allow_expired=expected_state in {"ended", "expired"},
        )
        if binding is None:
            raise ProjectionBindingError(f"{event} omitted trusted binding")
        if binding.state != expected_state:
            raise ProjectionBindingError(
                f"{event} requires projection binding state {expected_state}"
            )
        async with self.projection_store.hold_binding(binding.projection_binding_id):
            existing = self.projection_store.load_binding(binding.projection_binding_id)
            if existing is not None:
                _assert_same_projection_authority(existing, binding)
                if existing.state in {"ended", "expired"}:
                    return
                if existing.turn_seq > binding.turn_seq:
                    return
                # A delayed initial-ready event must never silently reactivate
                # a binding the relay has already paused or terminated.
                if binding.state == "active" and existing.state in {
                    "paused",
                    "ended",
                    "expired",
                }:
                    return
            self.projection_store.save_binding(binding)
            if binding.state == "active":
                self._projection_profile_for_binding(binding)

    async def _verify_projection_binding(self, message: dict) -> None:
        if self.client is None:
            raise RuntimeError("Claworld relay is not connected")
        local_agent_id = self._projection_agent_id()
        binding = extract_trusted_projection_binding(
            message,
            local_agent_id=local_agent_id,
        )
        if binding is None:
            raise ProjectionBindingError("projection binding verify event omitted trusted binding")
        if binding.state != "pending_capability" or binding.turn_seq != 0:
            raise ProjectionBindingError(
                "projection binding verify requires pending_capability with turnSeq 0"
            )
        async with self.projection_store.hold_binding(binding.projection_binding_id):
            existing = self.projection_store.load_binding(binding.projection_binding_id)
            if existing is None:
                self.projection_store.save_binding(binding)
            else:
                _assert_same_projection_authority(existing, binding)
                # A replayed verify may arrive after ready.  Re-report the
                # capability idempotently without regressing durable state.
                if existing.state == "pending_capability":
                    self.projection_store.save_binding(binding)

            profile = self._projection_profile_for_binding(binding)

            result = await can_send_to(
                platform=binding.route.platform,
                chat_id=binding.route.chat_id,
                thread_id=binding.route.thread_id,
                profile=profile,
                # This value is accepted only from relay-controlled binding
                # metadata validated above, never from model/tool arguments.
                trusted_chat_type=binding.route.chat_type,
            )
            can_send = result.get("canSend") is True
            reason = str(result.get("reason") or "projection_preflight_failed").strip()
            external_bot_id = str(result.get("externalBotId") or "").strip() or None
            runtime_chat_type = str(result.get("chatType") or "").strip().lower()
            if can_send and not _projection_chat_types_match(
                binding.route.chat_type,
                runtime_chat_type,
            ):
                can_send = False
                reason = "projection_chat_type_mismatch"
            if can_send and not external_bot_id:
                can_send = False
                reason = "projection_bot_identity_unavailable"
            evidence = {
                key: result[key]
                for key in (
                    "chatType",
                    "memberStatus",
                    "permissionBasis",
                    "threadVerified",
                )
                if result.get(key) is not None
            }
            await self.client.send_projection_capability(
                binding.projection_binding_id,
                can_send=can_send,
                external_bot_id=external_bot_id,
                bot_mention=str(result.get("botMention") or "").strip() or None,
                reason=None if can_send else reason,
                evidence=evidence,
            )

    async def _project_turn(self, message: dict) -> None:
        if self.client is None:
            raise RuntimeError("Claworld relay is not connected")
        local_agent_id = self._projection_agent_id()
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        trusted = data.get("trustedMetadata") if isinstance(data.get("trustedMetadata"), dict) else {}
        attempt = (
            trusted.get("projectionAttempt")
            if isinstance(trusted.get("projectionAttempt"), dict)
            else {}
        )
        chat_request_id = str(attempt.get("chatRequestId") or "").strip()
        binding = extract_trusted_projection_binding(
            message,
            expected_chat_request_id=chat_request_id or None,
            local_agent_id=local_agent_id,
        )
        if binding is None:
            raise ProjectionBindingError("projection turn event omitted trusted binding")
        if binding.state != "active":
            raise ProjectionBindingError("projection turn requires an active binding")
        normalized_attempt = _validate_projection_attempt(
            attempt,
            binding=binding,
            local_agent_id=local_agent_id,
        )
        projection_attempt_id_value = normalized_attempt["projectionAttemptId"]
        delivery_id = normalized_attempt["sourceDeliveryId"]
        turn_seq = normalized_attempt["turnSeq"]
        idempotency_key = normalized_attempt["idempotencyKey"]
        public_text = data.get("publicText")
        if not isinstance(public_text, str):
            raise ProjectionBindingError("projection turn publicText must be text")

        async with self.projection_store.hold_binding(binding.projection_binding_id):
            existing_binding = self.projection_store.load_binding(binding.projection_binding_id)
            if existing_binding is None:
                self.projection_store.save_binding(binding)
            else:
                _assert_same_projection_authority(existing_binding, binding)
                if existing_binding.state != "active":
                    raise ProjectionStateError(
                        "projection turn cannot reactivate a paused or terminal binding"
                    )
                # Projection turn events are addressed only to the Agent that
                # must perform that native send.  With strict relay
                # alternation, the other Agent owns the intervening global
                # turn, so one local projector normally observes 1,3,5... or
                # 2,4,6....  A stride of two is therefore contiguous for this
                # per-projector stream; anything larger means unseen local
                # projection work and remains fail-closed.
                if binding.turn_seq > existing_binding.turn_seq + 2:
                    raise ProjectionStateError("projection turnSeq has a delivery gap")
                if binding.turn_seq < existing_binding.turn_seq:
                    # An old turn may be replayed only to finish its already
                    # durable receipt.  Projecting previously unseen stale work
                    # would invert the public group's visible turn order.
                    if self.projection_store.load_outbox(idempotency_key) is None:
                        raise ProjectionStateError("projection turnSeq is stale and was never accepted")
                else:
                    self.projection_store.save_binding(binding)
            profile = self._projection_profile_for_binding(binding)
            try:
                outbox = self.projection_store.claim_outbox(
                    binding=binding,
                    delivery_id=delivery_id,
                    turn_seq=turn_seq,
                    public_text=public_text,
                )
            except ProjectionContentBlocked as exc:
                await self._send_failed_projection_receipt(
                    projection_attempt_id=projection_attempt_id_value,
                    binding_id=binding.projection_binding_id,
                    delivery_id=delivery_id,
                    idempotency_key=idempotency_key,
                    reason=f"content_blocked:{exc.reason}",
                )
                return

            if outbox.status == "complete":
                return
            if outbox.status == "receipt_pending":
                await self._send_sent_projection_receipt(projection_attempt_id_value, outbox)
                return
            if outbox.status == "sent":
                outbox = self.projection_store.transition_outbox(
                    outbox.idempotency_key,
                    status="receipt_pending",
                )
                await self._send_sent_projection_receipt(projection_attempt_id_value, outbox)
                return
            if outbox.status == "sending":
                outbox = self.projection_store.transition_outbox(
                    outbox.idempotency_key,
                    status="failed",
                    failure_code="ambiguous_send_after_restart",
                    failure_reason="projection send outcome was not durably recorded",
                    retryable=False,
                )
                await self._send_failed_projection_receipt(
                    projection_attempt_id=projection_attempt_id_value,
                    binding_id=outbox.projection_binding_id,
                    delivery_id=outbox.delivery_id,
                    idempotency_key=outbox.idempotency_key,
                    reason=outbox.failure_code or "ambiguous_send_after_restart",
                )
                return
            if outbox.status == "failed":
                await self._send_failed_projection_receipt(
                    projection_attempt_id=projection_attempt_id_value,
                    binding_id=outbox.projection_binding_id,
                    delivery_id=outbox.delivery_id,
                    idempotency_key=outbox.idempotency_key,
                    reason=outbox.failure_code or outbox.failure_reason or "projection_send_failed",
                )
                return

            outbox = self.projection_store.transition_outbox(outbox.idempotency_key, status="sending")
            send_result = await send_without_mirror(
                platform=binding.route.platform,
                chat_id=binding.route.chat_id,
                thread_id=binding.route.thread_id,
                content=outbox.public_text,
                profile=profile,
            )
            external_message_id = str(send_result.get("messageId") or "").strip() or None
            if send_result.get("success") is not True or not external_message_id:
                failure_code = str(send_result.get("reason") or "projection_send_failed").strip()
                outbox = self.projection_store.transition_outbox(
                    outbox.idempotency_key,
                    status="failed",
                    failure_code=failure_code,
                    failure_reason="native platform projection did not produce a durable message id",
                    retryable=False,
                )
                await self._send_failed_projection_receipt(
                    projection_attempt_id=projection_attempt_id_value,
                    binding_id=outbox.projection_binding_id,
                    delivery_id=outbox.delivery_id,
                    idempotency_key=outbox.idempotency_key,
                    reason=failure_code,
                )
                return

            outbox = self.projection_store.transition_outbox(
                outbox.idempotency_key,
                status="sent",
                external_message_id=external_message_id,
            )
            outbox = self.projection_store.transition_outbox(
                outbox.idempotency_key,
                status="receipt_pending",
            )
            await self._send_sent_projection_receipt(projection_attempt_id_value, outbox)

    async def _send_sent_projection_receipt(self, projection_attempt_id, outbox) -> None:
        if self.client is None:
            raise RuntimeError("Claworld relay is not connected")
        await self.client.send_projection_receipt(
            projection_attempt_id,
            projection_binding_id=outbox.projection_binding_id,
            delivery_id=outbox.delivery_id,
            idempotency_key=outbox.idempotency_key,
            status="sent",
            platform_message_id=outbox.external_message_id,
        )
        # A successful WebSocket command write or HTTP fallback only proves
        # that the relay accepted the receipt command.  Keep the durable item
        # pending until the backend-authored projection.receipt.accepted event
        # is observed (including after reconnect).

    async def _send_failed_projection_receipt(
        self,
        *,
        projection_attempt_id: str,
        binding_id: str,
        delivery_id: str,
        idempotency_key: str,
        reason: str,
    ) -> None:
        if self.client is None:
            raise RuntimeError("Claworld relay is not connected")
        await self.client.send_projection_receipt(
            projection_attempt_id,
            projection_binding_id=binding_id,
            delivery_id=delivery_id,
            idempotency_key=idempotency_key,
            status="failed",
            failure_reason=reason,
        )

    async def _complete_projection_receipt(self, message: dict) -> None:
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        projection_attempt_id_value = str(data.get("projectionAttemptId") or "").strip()
        idempotency_key = str(data.get("idempotencyKey") or "").strip()
        binding_id = str(data.get("projectionBindingId") or "").strip()
        delivery_id = str(data.get("deliveryId") or "").strip()
        if not all((projection_attempt_id_value, idempotency_key, binding_id, delivery_id)):
            raise ProjectionBindingError("projection receipt accepted identity is incomplete")
        expected_key = projection_idempotency_key(binding_id, delivery_id)
        if idempotency_key != expected_key:
            raise ProjectionBindingError("projection receipt accepted idempotency mismatch")
        if projection_attempt_id_value != projection_attempt_id(binding_id, delivery_id):
            raise ProjectionBindingError("projection receipt accepted attempt identity mismatch")
        outbox = self.projection_store.load_outbox(idempotency_key)
        if outbox is None or outbox.status == "complete":
            return
        if (
            outbox.projection_binding_id != binding_id
            or outbox.delivery_id != delivery_id
        ):
            raise ProjectionBindingError("projection receipt accepted outbox identity mismatch")
        async with self.projection_store.hold_binding(outbox.projection_binding_id):
            outbox = self.projection_store.load_outbox(idempotency_key)
            if outbox is not None and outbox.status == "receipt_pending":
                self.projection_store.transition_outbox(idempotency_key, status="complete")

    def _record_for_send(self, chat_id: str, reply_to: str | None) -> DeliveryRecord | None:
        if reply_to:
            record = self._deliveries_by_id.get(str(reply_to))
            if record is not None:
                return record
        latest_id = self._latest_by_chat.get(str(chat_id))
        if latest_id:
            return self._deliveries_by_id.get(latest_id)
        return None

    def _projection_agent_id(self) -> str:
        resolved = str(getattr(self.client, "agent_id", "") or "").strip()
        configured = str(self.claworld_config.agent_id or "").strip()
        agent_id = resolved or configured
        if not agent_id:
            raise ProjectionBindingError("local projection Agent identity is unavailable")
        return agent_id

    def _projection_profile_for_binding(self, binding) -> str:
        local_agent_id = self._projection_agent_id()
        expected_role = (
            "initiator_exact"
            if local_agent_id == binding.initiator_agent_id
            else "peer_default"
        )
        existing = self.projection_profile_store.load_binding_profile(
            binding.projection_binding_id
        )
        if existing is not None:
            if existing.route_digest != binding.route_digest:
                raise ProjectionBindingError(
                    "local projection profile binding route digest mismatch"
                )
            if existing.role != expected_role:
                raise ProjectionBindingError(
                    "local projection profile binding participant role mismatch"
                )
            try:
                updated = self.projection_profile_store.bind_profile(
                    projection_binding_id=binding.projection_binding_id,
                    route_digest=binding.route_digest,
                    profile=existing.profile,
                    role=existing.role,
                    channel_identity_binding_id=(
                        binding.channel_identity_binding_id
                        or existing.channel_identity_binding_id
                    ),
                )
            except ProjectionProfileSelectionError as exc:
                raise ProjectionBindingError(str(exc)) from exc
            return updated.profile

        if local_agent_id == binding.initiator_agent_id:
            profile = self.projection_profile_store.load_origin_profile(
                binding.route_digest
            )
            role = "initiator_exact"
            if not profile:
                raise ProjectionBindingError(
                    "initiator projection profile was not captured for this exact origin route"
                )
        else:
            profile = get_current_or_default_projection_profile(
                platform=binding.route.platform
            )
            role = "peer_default"
            if not profile:
                raise ProjectionBindingError(
                    "peer projection profile could not be selected explicitly"
                )

        try:
            selection = self.projection_profile_store.bind_profile(
                projection_binding_id=binding.projection_binding_id,
                route_digest=binding.route_digest,
                profile=profile,
                role=role,
                channel_identity_binding_id=binding.channel_identity_binding_id,
            )
        except ProjectionProfileSelectionError as exc:
            raise ProjectionBindingError(str(exc)) from exc
        return selection.profile

    async def _mark_record_kept_silent(self, record: DeliveryRecord, reason: str) -> None:
        if self.client is None:
            raise RuntimeError("Claworld relay is not connected")
        await self.client.send_kept_silent(record.delivery_id, record.relay_session_key, reason)
        record.replied = True


def _assert_same_projection_authority(existing, incoming) -> None:
    if (
        existing.immutable_authority_fingerprint
        != incoming.immutable_authority_fingerprint
    ):
        raise ProjectionStateError("projection binding immutable authority changed")


def _projection_chat_types_match(binding_chat_type: str, runtime_chat_type: str) -> bool:
    binding_type = str(binding_chat_type or "").strip().lower()
    runtime_type = str(runtime_chat_type or "").strip().lower()
    if binding_type == runtime_type:
        return True
    # Hermes intentionally normalizes Telegram group and supergroup inbound
    # routes to "group", while Telegram's capability API returns its native
    # "supergroup" type.  They are the same projection audience boundary.
    return {binding_type, runtime_type} <= {"group", "supergroup"}


def _validate_projection_attempt(attempt: dict, *, binding, local_agent_id: str) -> dict:
    if attempt.get("schema") != "claworld.projection-attempt.v1":
        raise ProjectionBindingError(
            "projection attempt schema must be claworld.projection-attempt.v1"
        )

    binding_id = str(attempt.get("projectionBindingId") or "").strip()
    chat_request_id = str(attempt.get("chatRequestId") or "").strip()
    source_delivery_id = str(attempt.get("sourceDeliveryId") or "").strip()
    delivery_id = str(attempt.get("deliveryId") or "").strip()
    idempotency_key = str(attempt.get("idempotencyKey") or "").strip()
    attempt_id = str(attempt.get("projectionAttemptId") or "").strip()
    route_digest = str(attempt.get("routeDigest") or "").strip()
    projector_agent_id = str(attempt.get("projectorAgentId") or "").strip()
    target_agent_id = str(attempt.get("targetAgentId") or "").strip()
    conversation_key = str(attempt.get("conversationKey") or "").strip()
    turn_id = str(attempt.get("turnId") or "").strip()
    turn_seq = attempt.get("turnSeq")

    if binding_id != binding.projection_binding_id:
        raise ProjectionBindingError("projection attempt projectionBindingId mismatch")
    if chat_request_id != binding.chat_request_id:
        raise ProjectionBindingError("projection attempt chatRequestId mismatch")
    if not source_delivery_id or delivery_id != source_delivery_id:
        raise ProjectionBindingError("projection attempt delivery identity is invalid")
    expected_key = projection_idempotency_key(binding.projection_binding_id, source_delivery_id)
    if idempotency_key != expected_key:
        raise ProjectionBindingError("projection attempt idempotencyKey mismatch")
    if attempt_id != projection_attempt_id(binding.projection_binding_id, source_delivery_id):
        raise ProjectionBindingError("projection attempt projectionAttemptId mismatch")
    if route_digest != binding.route_digest:
        raise ProjectionBindingError("projection attempt routeDigest mismatch")
    if projector_agent_id != local_agent_id:
        raise ProjectionBindingError("projection attempt was assigned to another Agent")
    expected_target = (
        binding.peer_agent_id
        if local_agent_id == binding.initiator_agent_id
        else binding.initiator_agent_id
    )
    if target_agent_id != expected_target:
        raise ProjectionBindingError("projection attempt targetAgentId mismatch")
    if not conversation_key or not turn_id:
        raise ProjectionBindingError("projection attempt conversationKey and turnId are required")
    if (
        isinstance(turn_seq, bool)
        or not isinstance(turn_seq, int)
        or turn_seq <= 0
        or turn_seq != binding.turn_seq
    ):
        raise ProjectionBindingError("projection attempt turnSeq does not match binding")
    expected_projector = (
        binding.initiator_agent_id
        if turn_seq % 2 == 1
        else binding.peer_agent_id
    )
    if projector_agent_id != expected_projector:
        raise ProjectionBindingError(
            "projection attempt projector does not match turnSeq alternation"
        )
    if str(attempt.get("state") or "").strip() != "pending_receipt":
        raise ProjectionBindingError("projection attempt state must be pending_receipt")
    expires_at = _projection_timestamp(attempt.get("expiresAt"), "projection attempt expiresAt")
    if expires_at <= datetime.now(timezone.utc):
        raise ProjectionBindingError("projection attempt has expired")

    return {
        "projectionAttemptId": attempt_id,
        "idempotencyKey": idempotency_key,
        "sourceDeliveryId": source_delivery_id,
        "turnSeq": turn_seq,
        "expiresAt": expires_at,
    }


def _projection_timestamp(value, field: str) -> datetime:
    normalized = str(value or "").strip()
    if not normalized:
        raise ProjectionBindingError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectionBindingError(f"{field} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ProjectionBindingError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


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


def _is_public_group_delivery(envelope) -> bool:
    for source in (envelope.metadata, envelope.payload):
        if not isinstance(source, dict):
            continue
        if str(source.get("publicAudience") or source.get("audience") or "").strip() in {
            "origin_group",
            "public_group",
        }:
            return True
        if str(source.get("projectionBindingId") or "").strip():
            return True
    return False


def _requires_acceptance_delivery(envelope) -> bool:
    if envelope.event_type != "delivery":
        return False
    for source in (envelope.metadata, envelope.payload):
        if isinstance(source, dict) and source.get("acceptanceRequired") is False:
            return False
    return True
