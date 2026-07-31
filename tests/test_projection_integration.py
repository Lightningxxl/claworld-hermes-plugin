from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
import unittest
from enum import Enum
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "claworld_hermes_plugin"
pkg = sys.modules.get(PACKAGE)
if pkg is None:
    pkg = types.ModuleType(PACKAGE)
    pkg.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = pkg


def _install_gateway_shim() -> None:
    if "gateway.platforms.base" in sys.modules:
        return
    gateway = types.ModuleType("gateway")
    gateway_config = types.ModuleType("gateway.config")
    gateway_platforms = types.ModuleType("gateway.platforms")
    gateway_base = types.ModuleType("gateway.platforms.base")
    gateway_session = types.ModuleType("gateway.session")

    class Platform(str):
        pass

    class ProcessingOutcome(Enum):
        SUCCESS = "success"
        FAILURE = "failure"
        CANCELLED = "cancelled"

    class BasePlatformAdapter:
        def __init__(self, *args, **kwargs):
            pass

    class MessageEvent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class MessageType:
        TEXT = "text"

    class SendResult:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class SessionSource:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    gateway_config.Platform = Platform
    gateway_base.BasePlatformAdapter = BasePlatformAdapter
    gateway_base.MessageEvent = MessageEvent
    gateway_base.MessageType = MessageType
    gateway_base.ProcessingOutcome = ProcessingOutcome
    gateway_base.SendResult = SendResult
    gateway_session.SessionSource = SessionSource
    sys.modules["gateway"] = gateway
    sys.modules["gateway.config"] = gateway_config
    sys.modules["gateway.platforms"] = gateway_platforms
    sys.modules["gateway.platforms.base"] = gateway_base
    sys.modules["gateway.session"] = gateway_session


_install_gateway_shim()

from claworld_hermes_plugin import adapter as adapter_module
from claworld_hermes_plugin import relay_client as relay_module
from claworld_hermes_plugin import tools as tools_module
from claworld_hermes_plugin import working_memory
from claworld_hermes_plugin.config import ClaworldConfig
from claworld_hermes_plugin.projection import (
    ProjectionBindingError,
    ProjectionRoute,
    ProjectionStateError,
    ProjectionStore,
    projection_attempt_id,
    projection_idempotency_key,
    projection_route_digest,
    sanitize_public_projection_text,
)
from claworld_hermes_plugin.projection_profiles import ProjectionProfileStore
from claworld_hermes_plugin.projection_runtime import CapturedProjectionRoute


LOCAL_AGENT_ID = "agent-a"
PEER_AGENT_ID = "agent-b"
BINDING_ID = "binding-integration-1"
CHAT_REQUEST_ID = "request-integration-1"
SOURCE_DELIVERY_ID = "delivery-integration-1"
_AUTO_CHANNEL_IDENTITY = object()


class FakeRelayClient:
    def __init__(self, *, receipt_failures: list[Exception] | None = None):
        self.agent_id = LOCAL_AGENT_ID
        self.capabilities: list[tuple[str, dict]] = []
        self.receipts: list[tuple[str, dict]] = []
        self.receipt_failures = list(receipt_failures or [])

    async def send_projection_capability(self, binding_id: str, **kwargs):
        self.capabilities.append((binding_id, kwargs))
        return {"accepted": True}

    async def send_projection_receipt(self, attempt_id: str, **kwargs):
        self.receipts.append((attempt_id, kwargs))
        if self.receipt_failures:
            raise self.receipt_failures.pop(0)
        return {"accepted": True}


def _adapter(
    root: Path,
    client: FakeRelayClient | None = None,
    *,
    origin_profile: str | None = "default",
):
    instance = object.__new__(adapter_module.ClaworldPlatformAdapter)
    instance.claworld_config = types.SimpleNamespace(agent_id=LOCAL_AGENT_ID)
    instance.projection_store = ProjectionStore(root)
    instance.projection_profile_store = ProjectionProfileStore(root)
    instance.client = client or FakeRelayClient()
    if origin_profile:
        instance.projection_profile_store.remember_origin_profile(
            _binding_payload()["routeDigest"],
            origin_profile,
        )
    return instance


def _binding_payload(
    *,
    state: str = "active",
    turn_seq: int = 1,
    chat_type: str = "group",
    bot_mentions: list[str] | None = None,
    initiator_agent_id: str = LOCAL_AGENT_ID,
    peer_agent_id: str = PEER_AGENT_ID,
    channel_identity_binding_id: str | None | object = _AUTO_CHANNEL_IDENTITY,
    expires_at: str = "2099-01-01T00:00:00Z",
) -> dict:
    route = ProjectionRoute(
        platform="telegram",
        chat_id="-100123456",
        chat_type=chat_type,
        thread_id="88",
    )
    origin_message_id = "42"
    resolved_channel_identity = channel_identity_binding_id
    if resolved_channel_identity is _AUTO_CHANNEL_IDENTITY:
        resolved_channel_identity = (
            None if state == "pending_capability" else "aci-local-agent-bot"
        )
    payload = {
        "schema": "claworld.projection-binding.v1",
        "projectionBindingId": BINDING_ID,
        "chatRequestId": CHAT_REQUEST_ID,
        "route": route.to_dict(),
        "routeDigest": projection_route_digest(route, origin_message_id),
        "initiatorAgentId": initiator_agent_id,
        "peerAgentId": peer_agent_id,
        "originMessageId": origin_message_id,
        "state": state,
        "turnSeq": turn_seq,
        "issuedAt": "2098-01-01T00:00:00Z",
        "expiresAt": expires_at,
        "authority": {
            "source": "claworld_relay",
            "kind": "relay_control_plane",
        },
        # The relay learns these from the two capability reports.  Pending
        # verification may therefore be empty; ready/turn events carry the
        # stable, aggregated list, which is frozen after activation.
        "botMentions": list(
            bot_mentions
            if bot_mentions is not None
            else ["@agent_a_bot", "@agent_b_bot"]
        ),
    }
    if resolved_channel_identity:
        payload["channelIdentityBindingId"] = resolved_channel_identity
    return payload


def _attempt_payload(binding: dict, **overrides) -> dict:
    delivery_id = SOURCE_DELIVERY_ID
    payload = {
        "schema": "claworld.projection-attempt.v1",
        "projectionAttemptId": projection_attempt_id(BINDING_ID, delivery_id),
        "idempotencyKey": projection_idempotency_key(BINDING_ID, delivery_id),
        "projectionBindingId": BINDING_ID,
        "chatRequestId": CHAT_REQUEST_ID,
        "conversationKey": "conversation:integration",
        "sourceDeliveryId": delivery_id,
        "deliveryId": delivery_id,
        "turnId": "turn-integration-1",
        "turnSeq": binding["turnSeq"],
        "projectorAgentId": LOCAL_AGENT_ID,
        "targetAgentId": PEER_AGENT_ID,
        "routeDigest": binding["routeDigest"],
        "state": "pending_receipt",
        "expiresAt": "2098-06-01T00:00:00Z",
    }
    payload.update(overrides)
    return payload


def _control_event(
    event: str,
    binding: dict,
    *,
    attempt: dict | None = None,
    public_text: str | None = None,
) -> dict:
    trusted = {"projectionBinding": binding}
    if attempt is not None:
        trusted["projectionAttempt"] = attempt
    data = {"trustedMetadata": trusted}
    if public_text is not None:
        data["publicText"] = public_text
    return {"event": event, "data": data}


def _receipt_accepted_event(attempt: dict) -> dict:
    return {
        "event": "projection.receipt.accepted",
        "data": {
            "projectionAttemptId": attempt["projectionAttemptId"],
            "projectionBindingId": attempt["projectionBindingId"],
            "deliveryId": attempt["deliveryId"],
            "idempotencyKey": attempt["idempotencyKey"],
        },
    }


class ProjectionBindingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_ready_and_turn_accept_monotonic_relay_renewals(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _adapter(Path(tmp) / ".claworld")
            pending = _binding_payload(
                state="pending_capability",
                turn_seq=0,
                bot_mentions=[],
                expires_at="2098-01-02T00:00:00Z",
            )
            ready = _binding_payload(
                state="active",
                turn_seq=0,
                expires_at="2098-02-01T00:00:00Z",
            )
            renewed_turn = _binding_payload(
                state="active",
                turn_seq=1,
                expires_at="2098-03-01T00:00:00Z",
            )
            capability = AsyncMock(
                return_value={
                    "success": True,
                    "canSend": True,
                    "chatType": "supergroup",
                    "externalBotId": "123456",
                }
            )
            native_send = AsyncMock(
                return_value={"success": True, "messageId": "renewed-message"}
            )
            with (
                patch.object(adapter_module, "can_send_to", capability),
                patch.object(adapter_module, "send_without_mirror", native_send),
            ):
                await instance._on_projection_control_event(
                    _control_event("projection.binding.verify", pending)
                )
                await instance._on_projection_control_event(
                    _control_event("projection.binding.ready", ready)
                )
                await instance._on_projection_control_event(
                    _control_event(
                        "projection.turn.requested",
                        renewed_turn,
                        attempt=_attempt_payload(renewed_turn),
                        public_text="renewed public turn",
                    )
                )

            persisted = instance.projection_store.load_binding(BINDING_ID)
            self.assertEqual(persisted.state, "active")
            self.assertEqual(persisted.turn_seq, 1)
            self.assertEqual(persisted.expires_at, "2098-03-01T00:00:00Z")
            native_send.assert_awaited_once()

    async def test_binding_verify_reports_capability_and_ready_is_monotonic(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _adapter(Path(tmp) / ".claworld")
            pending = _binding_payload(
                state="pending_capability",
                turn_seq=0,
                bot_mentions=[],
            )
            ready = _binding_payload(
                state="active",
                turn_seq=0,
                bot_mentions=["@agent_a_bot", "@agent_b_bot"],
            )
            capability = AsyncMock(
                return_value={
                    "success": True,
                    "canSend": True,
                    # Hermes inbound normalizes this route to group, while the
                    # native Telegram API correctly reports supergroup.
                    "chatType": "supergroup",
                    "externalBotId": "123456",
                    "botMention": "@agent_a_bot",
                    "memberStatus": "administrator",
                    "permissionBasis": "telegram_chat_member",
                }
            )
            with patch.object(adapter_module, "can_send_to", capability):
                await instance._on_projection_control_event(
                    _control_event("projection.binding.verify", pending)
                )
                await instance._on_projection_control_event(
                    _control_event("projection.binding.ready", ready)
                )
                # A delayed/replayed verify must not regress active to pending.
                await instance._on_projection_control_event(
                    _control_event("projection.binding.verify", pending)
                )

            persisted = instance.projection_store.load_binding(BINDING_ID)
            self.assertEqual(persisted.state, "active")
            self.assertEqual(
                persisted.channel_identity_binding_id,
                "aci-local-agent-bot",
            )
            self.assertEqual(
                persisted.bot_mentions,
                ("@agent_a_bot", "@agent_b_bot"),
            )
            self.assertEqual(len(instance.client.capabilities), 2)
            binding_id, report = instance.client.capabilities[0]
            self.assertEqual(binding_id, BINDING_ID)
            self.assertTrue(report["can_send"])
            self.assertEqual(report["external_bot_id"], "123456")
            self.assertEqual(report["bot_mention"], "@agent_a_bot")
            self.assertEqual(capability.await_args.kwargs["profile"], "default")
            profile_selection = (
                instance.projection_profile_store.load_binding_profile(BINDING_ID)
            )
            self.assertEqual(
                profile_selection.channel_identity_binding_id,
                "aci-local-agent-bot",
            )

    async def test_channel_identity_binds_once_then_is_frozen(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _adapter(Path(tmp) / ".claworld")
            pending = _binding_payload(
                state="pending_capability",
                turn_seq=0,
                bot_mentions=[],
                channel_identity_binding_id=None,
            )
            ready = _binding_payload(
                state="active",
                turn_seq=0,
                channel_identity_binding_id="aci-local-agent-bot",
            )
            capability = AsyncMock(
                return_value={
                    "success": True,
                    "canSend": True,
                    "chatType": "supergroup",
                    "externalBotId": "123456",
                }
            )
            with patch.object(adapter_module, "can_send_to", capability):
                await instance._on_projection_control_event(
                    _control_event("projection.binding.verify", pending)
                )
            await instance._on_projection_control_event(
                _control_event("projection.binding.ready", ready)
            )

            forged = _binding_payload(
                state="active",
                turn_seq=0,
                channel_identity_binding_id="aci-replaced-bot",
            )
            with self.assertRaisesRegex(ProjectionStateError, "immutable once assigned"):
                await instance._on_projection_control_event(
                    _control_event("projection.binding.ready", forged)
                )

            persisted = instance.projection_store.load_binding(BINDING_ID)
            self.assertEqual(
                persisted.channel_identity_binding_id,
                "aci-local-agent-bot",
            )
            profile = instance.projection_profile_store.load_binding_profile(BINDING_ID)
            self.assertEqual(
                profile.channel_identity_binding_id,
                "aci-local-agent-bot",
            )

    async def test_initiator_profile_is_exact_persisted_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claworld"
            first = _adapter(root, origin_profile="worker")
            pending = _binding_payload(
                state="pending_capability",
                turn_seq=0,
                bot_mentions=[],
            )
            capability = AsyncMock(
                return_value={
                    "success": True,
                    "canSend": True,
                    "chatType": "supergroup",
                    "externalBotId": "123456",
                }
            )
            with patch.object(adapter_module, "can_send_to", capability):
                await first._on_projection_control_event(
                    _control_event("projection.binding.verify", pending)
                )

            capability.assert_awaited_once_with(
                platform="telegram",
                chat_id="-100123456",
                thread_id="88",
                profile="worker",
            )
            selection = first.projection_profile_store.load_binding_profile(BINDING_ID)
            self.assertEqual(selection.profile, "worker")
            self.assertEqual(selection.role, "initiator_exact")

            restarted = _adapter(root, origin_profile="worker")
            self.assertEqual(
                restarted._projection_profile_for_binding(
                    adapter_module.extract_trusted_projection_binding(
                        _control_event("projection.binding.verify", pending),
                        local_agent_id=LOCAL_AGENT_ID,
                    )
                ),
                "worker",
            )

    async def test_peer_binds_explicit_current_default_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _adapter(
                Path(tmp) / ".claworld",
                origin_profile=None,
            )
            pending = _binding_payload(
                state="pending_capability",
                turn_seq=0,
                bot_mentions=[],
                initiator_agent_id=PEER_AGENT_ID,
                peer_agent_id=LOCAL_AGENT_ID,
            )
            capability = AsyncMock(
                return_value={
                    "success": True,
                    "canSend": True,
                    "chatType": "supergroup",
                    "externalBotId": "123456",
                }
            )
            with (
                patch.object(
                    adapter_module,
                    "get_current_or_default_projection_profile",
                    return_value="default",
                ) as select_profile,
                patch.object(adapter_module, "can_send_to", capability),
            ):
                await instance._on_projection_control_event(
                    _control_event("projection.binding.verify", pending)
                )

            select_profile.assert_called_once_with(platform="telegram")
            self.assertEqual(capability.await_args.kwargs["profile"], "default")
            selection = instance.projection_profile_store.load_binding_profile(BINDING_ID)
            self.assertEqual(selection.profile, "default")
            self.assertEqual(selection.role, "peer_default")

    async def test_projection_attempt_contract_is_strict(self):
        cases = {
            "schema": {"schema": "claworld.projection-attempt.v0"},
            "attempt id": {"projectionAttemptId": "pat_forged"},
            "idempotency": {"idempotencyKey": "binding:delivery:forged"},
            "binding": {"projectionBindingId": "binding-other"},
            "delivery": {"deliveryId": "delivery-other"},
            "route": {"routeDigest": "sha256:" + "0" * 64},
            "turn sequence": {"turnSeq": 2},
            "target": {"targetAgentId": LOCAL_AGENT_ID},
            "state": {"state": "projected"},
            "expiry": {"expiresAt": "2020-01-01T00:00:00Z"},
        }
        for label, override in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                instance = _adapter(Path(tmp) / ".claworld")
                binding = _binding_payload()
                attempt = _attempt_payload(binding, **override)
                native_send = AsyncMock(
                    return_value={"success": True, "messageId": "external-1"}
                )
                with patch.object(adapter_module, "send_without_mirror", native_send):
                    with self.assertRaises(ProjectionBindingError):
                        await instance._on_projection_control_event(
                            _control_event(
                                "projection.turn.requested",
                                binding,
                                attempt=attempt,
                                public_text="public text",
                            )
                        )
                native_send.assert_not_awaited()
                self.assertEqual(instance.client.receipts, [])

    async def test_turn_cannot_reactivate_paused_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _adapter(Path(tmp) / ".claworld")
            active = _binding_payload(state="active", turn_seq=1)
            paused = _binding_payload(state="paused", turn_seq=1)
            await instance._on_projection_control_event(
                _control_event("projection.binding.ready", active)
            )
            await instance._on_projection_control_event(
                _control_event("projection.binding.paused", paused)
            )
            attempt = _attempt_payload(active)
            native_send = AsyncMock()
            with patch.object(adapter_module, "send_without_mirror", native_send):
                with self.assertRaisesRegex(ProjectionStateError, "cannot reactivate"):
                    await instance._on_projection_control_event(
                        _control_event(
                            "projection.turn.requested",
                            active,
                            attempt=attempt,
                            public_text="delayed turn",
                        )
                    )
            native_send.assert_not_awaited()

    async def test_unseen_turn_sequence_gap_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _adapter(Path(tmp) / ".claworld")
            ready = _binding_payload(state="active", turn_seq=0)
            await instance._on_projection_control_event(
                _control_event("projection.binding.ready", ready)
            )
            gap = _binding_payload(state="active", turn_seq=2)
            attempt = _attempt_payload(gap)
            native_send = AsyncMock()
            with patch.object(adapter_module, "send_without_mirror", native_send):
                with self.assertRaisesRegex(ProjectionStateError, "delivery gap"):
                    await instance._on_projection_control_event(
                        _control_event(
                            "projection.turn.requested",
                            gap,
                            attempt=attempt,
                            public_text="out of order",
                        )
                    )
            native_send.assert_not_awaited()


class ProjectionTurnIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_projects_then_reports_durable_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _adapter(Path(tmp) / ".claworld")
            binding = _binding_payload()
            attempt = _attempt_payload(binding)
            native_send = AsyncMock(
                return_value={"success": True, "messageId": "tg-message-900"}
            )
            with patch.object(adapter_module, "send_without_mirror", native_send):
                await instance._on_projection_control_event(
                    _control_event(
                        "projection.turn.requested",
                        binding,
                        attempt=attempt,
                        public_text="hello @agent_b_bot",
                    )
                )

            native_send.assert_awaited_once_with(
                platform="telegram",
                chat_id="-100123456",
                thread_id="88",
                content="hello agent_b_bot",
                profile="default",
            )
            self.assertEqual(len(instance.client.receipts), 1)
            attempt_id, receipt = instance.client.receipts[0]
            self.assertEqual(attempt_id, attempt["projectionAttemptId"])
            self.assertEqual(receipt["status"], "sent")
            self.assertEqual(receipt["platform_message_id"], "tg-message-900")
            outbox = instance.projection_store.load_outbox(attempt["idempotencyKey"])
            self.assertEqual(outbox.status, "receipt_pending")
            self.assertEqual(outbox.external_message_id, "tg-message-900")

            # Merely accepting the receipt command (including HTTP fallback)
            # is not the durable completion signal.
            await instance._on_projection_control_event(_receipt_accepted_event(attempt))
            outbox = instance.projection_store.load_outbox(attempt["idempotencyKey"])
            self.assertEqual(outbox.status, "complete")

    async def test_http_receipt_fallback_stays_pending_until_control_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ClaworldConfig(
                server_url="https://example.invalid",
                app_token="token",
                agent_id=LOCAL_AGENT_ID,
            )
            client = relay_module.RelayClient(
                cfg,
                on_delivery=AsyncMock(),
            )
            # No WebSocket connection forces the real RelayClient HTTP path.
            instance = _adapter(Path(tmp) / ".claworld", client)
            binding = _binding_payload()
            attempt = _attempt_payload(binding)
            native_send = AsyncMock(
                return_value={"success": True, "messageId": "tg-message-http"}
            )

            async def inline_to_thread(callback):
                # Python 3.14's IsolatedAsyncioTestCase can wait indefinitely
                # for its default executor inside this sandbox.  Running the
                # same fallback callback inline keeps this protocol test
                # deterministic while still exercising the ws=None branch.
                return callback()

            with (
                patch.object(adapter_module, "send_without_mirror", native_send),
                patch.object(relay_module.asyncio, "to_thread", inline_to_thread),
                patch.object(
                    relay_module,
                    "request_json",
                    return_value={"accepted": True},
                ) as http_request,
            ):
                await instance._on_projection_control_event(
                    _control_event(
                        "projection.turn.requested",
                        binding,
                        attempt=attempt,
                        public_text="HTTP fallback durability",
                    )
                )

            http_request.assert_called_once()
            self.assertIn(
                f"/v1/projection-attempts/{attempt['projectionAttemptId']}/receipt",
                http_request.call_args.args,
            )
            outbox = instance.projection_store.load_outbox(attempt["idempotencyKey"])
            self.assertEqual(outbox.status, "receipt_pending")

            await instance._on_projection_control_event(_receipt_accepted_event(attempt))
            outbox = instance.projection_store.load_outbox(attempt["idempotencyKey"])
            self.assertEqual(outbox.status, "complete")

    async def test_restart_replays_receipt_without_resending_native_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claworld"
            first_client = FakeRelayClient(
                receipt_failures=[RuntimeError("relay unavailable after native send")]
            )
            first = _adapter(root, first_client)
            binding = _binding_payload()
            attempt = _attempt_payload(binding)
            event = _control_event(
                "projection.turn.requested",
                binding,
                attempt=attempt,
                public_text="restart-safe public reply",
            )
            first_native_send = AsyncMock(
                return_value={"success": True, "messageId": "tg-message-901"}
            )
            with patch.object(adapter_module, "send_without_mirror", first_native_send):
                with self.assertRaisesRegex(RuntimeError, "relay unavailable"):
                    await first._on_projection_control_event(event)
            first_native_send.assert_awaited_once()
            pending = first.projection_store.load_outbox(attempt["idempotencyKey"])
            self.assertEqual(pending.status, "receipt_pending")

            restarted_client = FakeRelayClient()
            restarted = _adapter(root, restarted_client)
            replay_native_send = AsyncMock()
            with patch.object(adapter_module, "send_without_mirror", replay_native_send):
                await restarted._on_projection_control_event(event)

            replay_native_send.assert_not_awaited()
            self.assertEqual(len(restarted_client.receipts), 1)
            pending = restarted.projection_store.load_outbox(attempt["idempotencyKey"])
            self.assertEqual(pending.status, "receipt_pending")

            await restarted._on_projection_control_event(_receipt_accepted_event(attempt))
            await restarted._on_projection_control_event(event)
            self.assertEqual(len(restarted_client.receipts), 1)
            completed = restarted.projection_store.load_outbox(attempt["idempotencyKey"])
            self.assertEqual(completed.status, "complete")
            self.assertEqual(completed.external_message_id, "tg-message-901")

    async def test_native_send_failure_reports_failed_receipt_and_never_resends(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _adapter(Path(tmp) / ".claworld")
            binding = _binding_payload()
            attempt = _attempt_payload(binding)
            event = _control_event(
                "projection.turn.requested",
                binding,
                attempt=attempt,
                public_text="cannot send this turn",
            )
            native_send = AsyncMock(
                return_value={"success": False, "reason": "permission_denied"}
            )
            with patch.object(adapter_module, "send_without_mirror", native_send):
                await instance._on_projection_control_event(event)
                await instance._on_projection_control_event(event)

            native_send.assert_awaited_once()
            self.assertEqual(len(instance.client.receipts), 2)
            self.assertTrue(
                all(receipt[1]["status"] == "failed" for receipt in instance.client.receipts)
            )
            self.assertTrue(
                all(
                    receipt[1]["failure_reason"] == "permission_denied"
                    for receipt in instance.client.receipts
                )
            )
            failed = instance.projection_store.load_outbox(attempt["idempotencyKey"])
            self.assertEqual(failed.status, "failed")
            self.assertFalse(failed.retryable)


class ProjectionRelayAckTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_receipt_waits_for_backend_durable_ack(self):
        cfg = ClaworldConfig(
            server_url="https://example.invalid",
            app_token="token",
            agent_id=LOCAL_AGENT_ID,
        )
        client = relay_module.RelayClient(cfg, on_delivery=AsyncMock())
        attempt_id = projection_attempt_id(BINDING_ID, SOURCE_DELIVERY_ID)
        with patch.object(
            client,
            "_send_with_ack",
            new=AsyncMock(return_value={"accepted": True}),
        ) as send_with_ack:
            await client.send_projection_receipt(
                attempt_id,
                projection_binding_id=BINDING_ID,
                delivery_id=SOURCE_DELIVERY_ID,
                idempotency_key=projection_idempotency_key(
                    BINDING_ID,
                    SOURCE_DELIVERY_ID,
                ),
                status="failed",
                failure_reason="permission_denied",
            )

        kwargs = send_with_ack.await_args.kwargs
        self.assertEqual(kwargs["ack_events"], ("projection.receipt.accepted",))
        self.assertNotIn("command_names", kwargs)
        self.assertEqual(kwargs["delivery_id"], attempt_id)

    async def test_receipt_accepted_resolves_waiter_and_dispatches_control(self):
        controls = []

        async def on_control(message):
            controls.append(message)

        cfg = ClaworldConfig(
            server_url="https://example.invalid",
            app_token="token",
            agent_id=LOCAL_AGENT_ID,
        )
        client = relay_module.RelayClient(
            cfg,
            on_delivery=AsyncMock(),
            on_control_event=on_control,
        )
        attempt_id = projection_attempt_id(BINDING_ID, SOURCE_DELIVERY_ID)
        future = client._register_ack_waiter(
            ("projection.receipt.accepted",),
            attempt_id,
        )
        message = {
            "event": "projection.receipt.accepted",
            "data": {
                "projectionAttemptId": attempt_id,
                "projectionBindingId": BINDING_ID,
                "deliveryId": SOURCE_DELIVERY_ID,
                "idempotencyKey": projection_idempotency_key(
                    BINDING_ID,
                    SOURCE_DELIVERY_ID,
                ),
            },
        }

        await client._handle_raw_message(json.dumps(message))
        await asyncio.gather(*list(client._delivery_tasks), return_exceptions=True)

        self.assertTrue(future.done())
        self.assertEqual(controls, [message])

    async def test_malformed_receipt_ack_does_not_resolve_waiter(self):
        controls = []

        async def on_control(message):
            controls.append(message)

        cfg = ClaworldConfig(
            server_url="https://example.invalid",
            app_token="token",
            agent_id=LOCAL_AGENT_ID,
        )
        client = relay_module.RelayClient(
            cfg,
            on_delivery=AsyncMock(),
            on_control_event=on_control,
        )
        attempt_id = projection_attempt_id(BINDING_ID, SOURCE_DELIVERY_ID)
        future = client._register_ack_waiter(
            ("projection.receipt.accepted",),
            attempt_id,
        )
        message = _receipt_accepted_event(
            _attempt_payload(_binding_payload())
        )
        message["data"]["idempotencyKey"] = "forged:receipt:key"

        await client._handle_raw_message(json.dumps(message))
        await asyncio.gather(*list(client._delivery_tasks), return_exceptions=True)

        self.assertFalse(future.done())
        self.assertEqual(controls, [])
        client._remove_ack_waiter(
            ("projection.receipt.accepted",),
            attempt_id,
            future,
        )
        future.cancel()


class ProjectionRequestDurabilityTests(unittest.TestCase):
    def _config(self, root: Path) -> ClaworldConfig:
        return ClaworldConfig(
            server_url="https://example.invalid",
            app_token="token",
            agent_id=LOCAL_AGENT_ID,
            working_memory_root=str(root),
        )

    def _captured(self) -> CapturedProjectionRoute:
        return CapturedProjectionRoute(
            platform="telegram",
            chat_id="-100123456",
            chat_type="supergroup",
            origin_message_id="42",
            thread_id="88",
            profile="worker",
            origin_public_text="请联系 Agent B",
        )

    def _args(self, **overrides) -> dict:
        args = {
            "action": "request",
            "displayName": " Agent B ",
            "agentCode": "agent-b-code",
            "openingMessage": "请讨论这个问题",
            "projectToOriginGroup": True,
        }
        args.update(overrides)
        return args

    def _patch_route(self):
        return (
            patch.object(
                tools_module,
                "get_current_projection_route",
                return_value=self._captured(),
            ),
            patch.object(
                tools_module,
                "_current_hermes_session_context",
                return_value={},
            ),
        )

    def test_repeated_and_restarted_requests_reuse_keys_and_bind_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claworld"
            cfg = self._config(root)
            bodies = []

            def create_request(_cfg, method, path, **kwargs):
                self.assertEqual((method, path), ("POST", "/v1/chat-requests"))
                bodies.append(kwargs["body"])
                return {
                    "chatRequest": {
                        "chatRequestId": "request-created-1",
                        "projection": {
                            "projectionBindingId": "binding-created-1",
                        },
                    }
                }

            route_patch, context_patch = self._patch_route()
            with (
                route_patch,
                context_patch,
                patch.object(
                    tools_module,
                    "request_json",
                    side_effect=create_request,
                ),
            ):
                tools_module._manage_conversations(cfg, self._args())
                # A new config/store instance models process restart.
                restarted_cfg = self._config(root)
                tools_module._manage_conversations(restarted_cfg, self._args())
                explicit = self._args(
                    clientRequestId=bodies[0]["clientRequestId"],
                    idempotencyKey=bodies[0]["idempotencyKey"],
                )
                tools_module._manage_conversations(restarted_cfg, explicit)

            self.assertEqual(len(bodies), 3)
            self.assertTrue(bodies[0]["clientRequestId"].startswith("cpr_"))
            self.assertTrue(
                bodies[0]["idempotencyKey"].startswith("projection-request:")
            )
            self.assertEqual(
                {body["clientRequestId"] for body in bodies},
                {bodies[0]["clientRequestId"]},
            )
            self.assertEqual(
                {body["idempotencyKey"] for body in bodies},
                {bodies[0]["idempotencyKey"]},
            )
            self.assertNotIn("profile", json.dumps(bodies, ensure_ascii=False))
            persisted = ProjectionStore(root).load_request(
                bodies[0]["clientRequestId"]
            )
            self.assertEqual(persisted.state, "bound")
            self.assertEqual(persisted.chat_request_id, "request-created-1")
            self.assertEqual(
                persisted.projection_binding_id,
                "binding-created-1",
            )

    def test_uncertain_transport_and_server_failure_remain_pending(self):
        errors = (
            TimeoutError("response lost"),
            tools_module.ClaworldHttpError(503, {"error": "unavailable"}),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / ".claworld"
                bodies = []

                def uncertain(_cfg, _method, _path, **kwargs):
                    bodies.append(kwargs["body"])
                    raise error

                route_patch, context_patch = self._patch_route()
                with (
                    route_patch,
                    context_patch,
                    patch.object(
                        tools_module,
                        "request_json",
                        side_effect=uncertain,
                    ),
                    self.assertRaises(type(error)),
                ):
                    tools_module._manage_conversations(
                        self._config(root),
                        self._args(),
                    )

                persisted = ProjectionStore(root).load_request(
                    bodies[0]["clientRequestId"]
                )
                self.assertEqual(persisted.state, "pending")
                self.assertIsNone(persisted.failure_code)

    def test_definitive_failure_is_terminal_and_not_dispatched_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claworld"
            bodies = []

            def rejected(_cfg, _method, _path, **kwargs):
                bodies.append(kwargs["body"])
                raise tools_module.ClaworldHttpError(
                    409,
                    {"error": "projection_idempotency_conflict"},
                )

            route_patch, context_patch = self._patch_route()
            with (
                route_patch,
                context_patch,
                patch.object(
                    tools_module,
                    "request_json",
                    side_effect=rejected,
                ) as request,
            ):
                with self.assertRaises(tools_module.ClaworldHttpError):
                    tools_module._manage_conversations(
                        self._config(root),
                        self._args(),
                    )
                with self.assertRaisesRegex(ProjectionStateError, "terminal"):
                    tools_module._manage_conversations(
                        self._config(root),
                        self._args(),
                    )

            request.assert_called_once()
            persisted = ProjectionStore(root).load_request(
                bodies[0]["clientRequestId"]
            )
            self.assertEqual(persisted.state, "failed")
            self.assertEqual(persisted.failure_code, "http_409")

    def test_mismatched_explicit_keys_fail_before_network_dispatch(self):
        for key, value in (
            ("clientRequestId", "forged-client-request"),
            ("idempotencyKey", "forged-idempotency-key"),
            ("dedupeKey", "forged-dedupe-key"),
        ):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                route_patch, context_patch = self._patch_route()
                with (
                    route_patch,
                    context_patch,
                    patch.object(tools_module, "request_json") as request,
                    self.assertRaisesRegex(ValueError, "route-derived stable key"),
                ):
                    tools_module._manage_conversations(
                        self._config(Path(tmp) / ".claworld"),
                        self._args(**{key: value}),
                    )
                request.assert_not_called()


class ProjectionPromptIsolationTests(unittest.TestCase):
    def test_mixed_independent_no_reply_line_suppresses_entire_projection(self):
        result = sanitize_public_projection_text(
            "这句看似可公开。\n  NO_REPLY  \n这句也不能泄露。"
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.suppressed_reason, "no_reply")
        self.assertEqual(result.text, "")

        ordinary = sanitize_public_projection_text("NO_REPLY 不是独立控制行")
        self.assertTrue(ordinary.allowed)

    def test_request_persists_profile_locally_without_sending_it_to_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claworld"
            cfg = ClaworldConfig(
                server_url="https://example.invalid",
                app_token="token",
                agent_id=LOCAL_AGENT_ID,
                working_memory_root=str(root),
            )
            captured = CapturedProjectionRoute(
                platform="telegram",
                chat_id="-100123456",
                chat_type="supergroup",
                origin_message_id="42",
                thread_id="88",
                profile="worker",
                origin_public_text="请联系 Agent B",
            )
            with (
                patch.object(
                    tools_module,
                    "get_current_projection_route",
                    return_value=captured,
                ),
                patch.object(
                    tools_module,
                    "_current_hermes_session_context",
                    return_value={},
                ),
            ):
                context = tools_module._conversation_request_context(
                    cfg,
                    {"projectToOriginGroup": True},
                )

            projection = context["projection"]
            self.assertNotIn("profile", projection)
            self.assertNotIn("profile", projection["route"])
            route = ProjectionRoute(
                platform="telegram",
                chat_id="-100123456",
                chat_type="supergroup",
                thread_id="88",
            )
            digest = projection_route_digest(route, "42")
            persisted = ProjectionProfileStore(root).load_origin_profile(digest)
            self.assertEqual(persisted, "worker")

    def test_public_prompt_does_not_read_or_render_private_memory_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claworld"
            working_memory.ensure_working_memory(root)
            secrets = {
                "context/NOW.md": "PRIVATE-NOW-SECRET",
                "context/MEMORY.md": "PRIVATE-MEMORY-SECRET",
                "context/PROFILE.md": "PRIVATE-PROFILE-SECRET",
            }
            for relative, secret in secrets.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(secret, encoding="utf-8")

            with patch.object(
                working_memory,
                "_file_section",
                side_effect=AssertionError("public prompt attempted a private file read"),
            ):
                prompt = working_memory.build_prompt_context(
                    root,
                    platform="claworld",
                    chat_id="conversation:public",
                    public_group=True,
                )

            self.assertIn("Public Group Projection", prompt)
            self.assertIn("Do not reveal private memory", prompt)
            for secret in secrets.values():
                self.assertNotIn(secret, prompt)


if __name__ == "__main__":
    unittest.main()
