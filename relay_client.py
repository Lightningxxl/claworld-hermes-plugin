"""Async Claworld relay WebSocket client."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import urllib.parse
from typing import Awaitable, Callable

from .config import ClaworldConfig
from .http_client import ClaworldHttpError, request_json
from .projection import projection_attempt_id, projection_idempotency_key
from .protocol import (
    auth_message,
    accepted_message,
    build_inbound_envelope,
    kept_silent_message,
    normalize_ws_url,
    projection_capability_message,
    projection_receipt_message,
    reply_message,
)
from .version import PLUGIN_CLIENT, PLUGIN_VERSION

DeliveryHandler = Callable[[object], Awaitable[None]]
ControlEventHandler = Callable[[dict], Awaitable[None]]

DELIVERY_VISIBILITY_RETRY_ATTEMPTS = 20
DELIVERY_VISIBILITY_RETRY_DELAY_SECONDS = 0.01
RELAY_AUTH_TIMEOUT_SECONDS = 30.0
RELAY_RECONNECT_BASE_DELAY_SECONDS = 1.0
RELAY_RECONNECT_MAX_DELAY_SECONDS = 60.0


class RelayClient:
    def __init__(
        self,
        config: ClaworldConfig,
        *,
        on_delivery: DeliveryHandler,
        on_control_event: ControlEventHandler | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.on_delivery = on_delivery
        self.on_control_event = on_control_event
        self.logger = logger or logging.getLogger(__name__)
        self.ws = None
        self._receiver_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._closed = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._auth_future: asyncio.Future | None = None
        self._ack_waiters: dict[tuple[str, str], list[tuple[asyncio.Future, tuple[str, ...]]]] = {}
        self._delivery_tasks: set[asyncio.Task] = set()
        self._reconnect_attempts = 0
        self.agent_id = config.agent_id

    async def connect(self) -> bool:
        self._closed.clear()
        if not self.agent_id:
            self.agent_id = await asyncio.to_thread(self._resolve_agent_id)
        if not self.agent_id:
            raise RuntimeError("CLAWORLD_AGENT_ID is required or /v1/account must resolve agentId")
        await self._open_once()
        return True

    async def close(self) -> None:
        self._closed.set()
        heartbeat_task = self._heartbeat_task
        receiver_task = self._receiver_task
        self._heartbeat_task = None
        self._receiver_task = None
        await self._cancel_task_and_wait(heartbeat_task)
        await self._cancel_task_and_wait(receiver_task)
        delivery_tasks = list(self._delivery_tasks)
        for task in delivery_tasks:
            self._cancel_task(task)
        if delivery_tasks:
            await asyncio.gather(*delivery_tasks, return_exceptions=True)
            self._delivery_tasks.clear()
        if self.ws is not None:
            await self.ws.close()
        self.ws = None
        self._heartbeat_task = None
        self._receiver_task = None

    async def send_accepted(self, delivery_id: str, session_key: str | None) -> None:
        await self._send_with_ack(
            accepted_message(delivery_id, session_key),
            ack_events=("delivery.accepted", "command.accepted"),
            delivery_id=delivery_id,
            command_names=("delivery.accepted.requested",),
            fallback=lambda: self._accepted_http(delivery_id, session_key),
        )

    async def send_reply(self, delivery_id: str, session_key: str | None, reply_text: str) -> None:
        await self._send_with_ack(
            reply_message(delivery_id, session_key, reply_text),
            ack_events=("reply.accepted", "command.accepted"),
            delivery_id=delivery_id,
            command_names=("delivery.reply.requested",),
            fallback=lambda: self._reply_http(delivery_id, reply_text),
        )

    async def send_kept_silent(self, delivery_id: str, session_key: str | None, reason: str) -> None:
        await self._send_with_ack(
            kept_silent_message(delivery_id, session_key, reason),
            ack_events=("kept_silent.accepted", "command.accepted"),
            delivery_id=delivery_id,
            command_names=("delivery.kept_silent.requested",),
            fallback=lambda: self._kept_silent_http(delivery_id, reason),
        )

    async def send_projection_capability(
        self,
        projection_binding_id: str,
        *,
        can_send: bool,
        external_bot_id: str | None,
        bot_mention: str | None = None,
        reason: str | None = None,
        evidence: dict | None = None,
    ) -> None:
        payload = projection_capability_message(
            projection_binding_id,
            can_send=can_send,
            external_bot_id=external_bot_id,
            bot_mention=bot_mention,
            reason=reason,
            evidence=evidence,
        )
        await self._send_with_ack(
            payload,
            ack_events=("command.accepted",),
            delivery_id=projection_binding_id,
            command_names=("projection.capability.reported",),
            fallback=lambda: self._projection_capability_http(projection_binding_id, payload),
        )

    async def send_projection_receipt(
        self,
        projection_attempt_id: str,
        *,
        projection_binding_id: str,
        delivery_id: str,
        idempotency_key: str,
        status: str,
        platform_message_id: str | None = None,
        failure_reason: str | None = None,
    ) -> object:
        payload = projection_receipt_message(
            projection_attempt_id,
            projection_binding_id=projection_binding_id,
            delivery_id=delivery_id,
            idempotency_key=idempotency_key,
            status=status,
            platform_message_id=platform_message_id,
            failure_reason=failure_reason,
        )
        return await self._send_with_ack(
            payload,
            # Both sent and failed receipts require the backend-authored ACK.
            # For failed sends it means FAILED + paused is durable; it never
            # turns the local failed outbox item into a successful projection.
            ack_events=("projection.receipt.accepted",),
            delivery_id=projection_attempt_id,
            fallback=lambda: self._projection_receipt_http(projection_attempt_id, payload),
        )

    async def _open_once(self) -> None:
        import websockets

        await self._cancel_task_and_wait(self._heartbeat_task)
        self._heartbeat_task = None
        if self._receiver_task is not asyncio.current_task():
            await self._cancel_task_and_wait(self._receiver_task)
            self._receiver_task = None
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

        ws_url = normalize_ws_url(self.config.server_url)
        self.logger.info("claworld relay connecting to %s", ws_url)
        ws = None
        receiver_task = None
        try:
            ws = await _websockets_connect(websockets, ws_url, self.config)
            self.ws = ws
            loop = asyncio.get_running_loop()
            self._auth_future = loop.create_future()
            receiver_task = asyncio.create_task(self._receive_loop(), name="claworld-relay-receiver")
            self._receiver_task = receiver_task
            await self._send_json(
                auth_message(
                    agent_id=self.agent_id,
                    credential=self.config.app_token,
                    client=PLUGIN_CLIENT,
                    client_version=PLUGIN_VERSION,
                )
            )
            await asyncio.wait_for(self._auth_future, timeout=RELAY_AUTH_TIMEOUT_SECONDS)
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="claworld-relay-heartbeat")
            self._reconnect_attempts = 0
        except Exception:
            await self._cancel_task_and_wait(receiver_task)
            if self._receiver_task is receiver_task:
                self._receiver_task = None
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            if self.ws is ws:
                self.ws = None
            if self._auth_future and not self._auth_future.done():
                self._auth_future.cancel()
            self._auth_future = None
            raise

    async def _receive_loop(self) -> None:
        while not self._closed.is_set():
            try:
                if self.ws is None:
                    raise RuntimeError("claworld relay websocket is not connected")
                async for raw in self.ws:
                    await self._handle_raw_message(raw)
                if self._closed.is_set():
                    return
                raise RuntimeError("claworld relay websocket closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._auth_future and not self._auth_future.done():
                    self._auth_future.set_exception(exc)
                if not self.config.reconnect or self._closed.is_set():
                    self.logger.warning("claworld relay receiver stopped: %s", exc)
                    return
                while self.config.reconnect and not self._closed.is_set():
                    delay = self._next_reconnect_delay()
                    self.logger.warning("claworld relay disconnected; reconnecting in %.1fs: %s", delay, exc)
                    await asyncio.sleep(delay)
                    try:
                        await self._open_once()
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception as reconnect_exc:
                        exc = reconnect_exc
                        self.logger.warning("claworld relay reconnect failed: %s", reconnect_exc)

    async def _handle_raw_message(self, raw) -> None:
        try:
            message = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        except Exception:
            self.logger.warning("claworld relay delivered invalid JSON")
            return

        event = message.get("event") or message.get("type")
        if event == "auth.ok":
            if self._auth_future and not self._auth_future.done():
                self._auth_future.set_result(True)
            return
        if event == "error" and not (self._auth_future and self._auth_future.done()):
            reason = message.get("data", {}).get("reason") or message.get("data", {}).get("error") or "relay auth failed"
            if self._auth_future and not self._auth_future.done():
                self._auth_future.set_exception(RuntimeError(reason))
            return
        if event in {"delivery.accepted", "reply.accepted", "command.accepted", "kept_silent.accepted"}:
            self._resolve_ack_waiters(event, message)
            return
        if event == "projection.receipt.accepted":
            # This event is both the durable ACK for the sender waiting in
            # send_projection_receipt() and a control notification used to
            # complete a receipt_pending outbox after reconnect.  Resolve the
            # waiter before dispatching the handler so a per-binding handler
            # lock cannot deadlock the task that is currently reporting it.
            if not _valid_projection_receipt_ack(message):
                self.logger.warning("claworld relay delivered an invalid projection receipt ACK")
                return
            self._resolve_ack_waiters(event, message)
            if self.on_control_event is not None:
                self._dispatch_control_event(message, event)
            return
        if event in {
            "projection.binding.verify",
            "projection.binding.ready",
            "projection.turn.requested",
            "projection.binding.paused",
            "projection.binding.ended",
            "projection.binding.expired",
        }:
            if self.on_control_event is None:
                self.logger.warning("claworld relay projection event ignored without handler: %s", event)
                return
            self._dispatch_control_event(message, event)
            return

        try:
            envelope = build_inbound_envelope(message)
        except ValueError as exc:
            self.logger.warning("claworld relay rejected invalid delivery envelope: %s", exc)
            return
        if envelope is not None:
            self._dispatch_delivery(envelope)

    def _dispatch_delivery(self, envelope) -> None:
        task = asyncio.create_task(self.on_delivery(envelope), name=f"claworld-delivery-{envelope.delivery_id}")
        self._delivery_tasks.add(task)

        def _done(done_task: asyncio.Task) -> None:
            self._delivery_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.logger.warning("claworld delivery handler failed: %s", exc)

        task.add_done_callback(_done)

    def _dispatch_control_event(self, message: dict, event: str) -> None:
        task = asyncio.create_task(
            self.on_control_event(message),
            name=f"claworld-control-{event}",
        )
        self._delivery_tasks.add(task)

        def _done(done_task: asyncio.Task) -> None:
            self._delivery_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.logger.warning("claworld projection control handler failed: %s", exc)

        task.add_done_callback(_done)

    async def _send_json(self, payload: dict) -> None:
        if self.ws is None:
            raise RuntimeError("claworld relay websocket is not connected")
        encoded = json.dumps(payload, ensure_ascii=False)
        async with self._send_lock:
            await self.ws.send(encoded)

    async def _send_with_ack(
        self,
        payload: dict,
        *,
        ack_events: tuple[str, ...],
        delivery_id: str,
        fallback,
        command_names: tuple[str, ...] = (),
    ) -> object:
        timeout = max(float(self.config.reply_ack_timeout_seconds), 0.1)
        if self.ws is None:
            return await asyncio.to_thread(fallback)

        ack_ids = _ack_ids_for_payload(payload, delivery_id)
        ack_future = self._register_ack_waiter(ack_events, ack_ids, command_names=command_names)
        try:
            await self._send_json(payload)
            return await asyncio.wait_for(ack_future, timeout=timeout)
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            self.logger.warning("claworld relay ack failed; using HTTP fallback: %s", detail)
            return await asyncio.to_thread(fallback)
        finally:
            self._remove_ack_waiter(ack_events, ack_ids, ack_future)

    def _register_ack_waiter(
        self,
        ack_events: tuple[str, ...],
        delivery_id: str | tuple[str, ...],
        *,
        command_names: tuple[str, ...] = (),
    ) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        delivery_ids = _normalize_ack_ids(delivery_id)
        expected_command_names = tuple(str(name).strip() for name in command_names if str(name).strip())
        for event in ack_events:
            for ack_id in delivery_ids:
                self._ack_waiters.setdefault((event, ack_id), []).append((future, expected_command_names))
        return future

    def _remove_ack_waiter(self, ack_events: tuple[str, ...], delivery_id: str | tuple[str, ...], future: asyncio.Future) -> None:
        delivery_ids = _normalize_ack_ids(delivery_id)
        for event in ack_events:
            for ack_id in delivery_ids:
                key = (event, ack_id)
                waiters = self._ack_waiters.get(key)
                if not waiters:
                    continue
                self._ack_waiters[key] = [item for item in waiters if item[0] is not future]
                if not self._ack_waiters[key]:
                    self._ack_waiters.pop(key, None)

    def _resolve_ack_waiters(self, event: str, message: dict) -> None:
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        command = data.get("command") if isinstance(data.get("command"), dict) else {}
        if event == "projection.receipt.accepted":
            # A receipt ACK also carries the source deliveryId, but the waiter
            # is intentionally scoped to the deterministic attempt id.
            delivery_id = data.get("projectionAttemptId")
        else:
            delivery_id = (
                data.get("acceptedDeliveryId")
                or data.get("repliedDeliveryId")
                or data.get("keptSilentDeliveryId")
                or data.get("deliveryId")
                or data.get("inboxItemId")
                or command.get("deliveryId")
                or command.get("aggregateId")
                or command.get("partitionKey")
                or data.get("projectionBindingId")
                or message.get("deliveryId")
            )
        delivery_id = str(delivery_id or "").strip()
        if not delivery_id:
            self.logger.debug("claworld relay ack ignored without delivery/session key: event=%s", event)
            return
        waiters = self._ack_waiters.get((event, delivery_id), [])
        if event == "command.accepted":
            command_name = str(command.get("name") or "").strip()
            matching_waiters = [
                item for item in waiters
                if not item[1] or command_name in item[1]
            ]
            if matching_waiters:
                remaining_waiters = [item for item in waiters if item not in matching_waiters]
                if remaining_waiters:
                    self._ack_waiters[(event, delivery_id)] = remaining_waiters
                else:
                    self._ack_waiters.pop((event, delivery_id), None)
                waiters = matching_waiters
            else:
                waiters = []
        else:
            waiters = self._ack_waiters.pop((event, delivery_id), [])
        if not waiters:
            self.logger.debug(
                "claworld relay ack had no waiter: event=%s key=%s pending=%s",
                event,
                delivery_id,
                [f"{item[0]}:{item[1]}" for item in self._ack_waiters.keys()],
            )
            return
        self.logger.debug("claworld relay ack resolved: event=%s key=%s waiters=%d", event, delivery_id, len(waiters))
        for future, _expected_command_names in waiters:
            if not future.done():
                future.set_result(message)

    def _delivery_path(self, delivery_id: str, suffix: str) -> str:
        return f"/v1/runtime-deliveries/{urllib.parse.quote(str(delivery_id), safe='')}/{suffix}"

    def _accepted_http(self, delivery_id: str, session_key: str | None) -> dict:
        return _request_json_with_delivery_visibility_retry(
            self.config,
            "POST",
            self._delivery_path(delivery_id, "accepted"),
            body={
                "fromAgentId": self.agent_id,
                "sessionKey": session_key,
                "source": "hermes_gateway_dispatch",
            },
            timeout=15.0,
        )

    def _reply_http(self, delivery_id: str, reply_text: str) -> dict:
        try:
            return _request_json_with_delivery_visibility_retry(
                self.config,
                "POST",
                self._delivery_path(delivery_id, "reply"),
                body={
                    "fromAgentId": self.agent_id,
                    "payload": {"text": reply_text, "source": "hermes_agent"},
                },
                timeout=30.0,
            )
        except ClaworldHttpError as exc:
            if exc.status == 409 and _delivery_status(exc.body) == "replied":
                return exc.body
            raise

    def _kept_silent_http(self, delivery_id: str, reason: str) -> dict:
        try:
            return _request_json_with_delivery_visibility_retry(
                self.config,
                "POST",
                self._delivery_path(delivery_id, "kept-silent"),
                body={
                    "fromAgentId": self.agent_id,
                    "reason": reason or "no_renderable_reply",
                    "source": "hermes_gateway",
                },
                timeout=30.0,
            )
        except ClaworldHttpError as exc:
            if exc.status == 409 and _delivery_status(exc.body) == "kept_silent":
                return exc.body
            raise

    def _projection_capability_http(self, projection_binding_id: str, payload: dict) -> dict:
        binding_id = urllib.parse.quote(str(projection_binding_id), safe="")
        return request_json(
            self.config,
            "POST",
            f"/v1/projection-bindings/{binding_id}/capabilities",
            body={
                "fromAgentId": self.agent_id,
                "canSend": payload.get("canSend"),
                "externalBotId": payload.get("externalBotId"),
                "botMention": payload.get("botMention"),
                "reason": payload.get("reason"),
                "evidence": payload.get("evidence"),
            },
            timeout=30.0,
        )

    def _projection_receipt_http(self, projection_attempt_id: str, payload: dict) -> dict:
        attempt_id = urllib.parse.quote(str(projection_attempt_id), safe="")
        return request_json(
            self.config,
            "POST",
            f"/v1/projection-attempts/{attempt_id}/receipt",
            body={
                "fromAgentId": self.agent_id,
                "projectionBindingId": payload.get("projectionBindingId"),
                "deliveryId": payload.get("deliveryId"),
                "idempotencyKey": payload.get("idempotencyKey"),
                "status": payload.get("status"),
                "platformMessageId": payload.get("platformMessageId"),
                "failureReason": payload.get("failureReason"),
            },
            timeout=30.0,
        )

    async def _heartbeat_loop(self) -> None:
        while not self._closed.is_set():
            await asyncio.sleep(self.config.heartbeat_seconds)
            try:
                await self._send_json({"type": "heartbeat"})
            except Exception as exc:
                self.logger.debug("claworld relay heartbeat failed: %s", exc)

    def _resolve_agent_id(self) -> str:
        try:
            payload = request_json(
                self.config,
                "GET",
                "/v1/account",
                query={"accountId": self.config.account_id},
                timeout=15.0,
            )
        except Exception:
            return ""
        return str(
            payload.get("agentId")
            or payload.get("relay", {}).get("agentId")
            or payload.get("profile", {}).get("agentId")
            or ""
        ).strip()

    def _cancel_task(self, task: asyncio.Task | None) -> None:
        if task and not task.done():
            task.cancel()

    async def _cancel_task_and_wait(self, task: asyncio.Task | None) -> None:
        if task is None or task is asyncio.current_task():
            return
        self._cancel_task(task)
        await asyncio.gather(task, return_exceptions=True)

    def _next_reconnect_delay(self) -> float:
        exponent = min(self._reconnect_attempts, 6)
        delay = min(RELAY_RECONNECT_BASE_DELAY_SECONDS * (2 ** exponent), RELAY_RECONNECT_MAX_DELAY_SECONDS)
        self._reconnect_attempts += 1
        return delay


async def _websockets_connect(websockets, ws_url: str, config: ClaworldConfig):
    kwargs = {"ping_interval": None}
    if _connect_supports_proxy(websockets.connect):
        kwargs["proxy"] = _websocket_proxy(config)
    try:
        return await websockets.connect(ws_url, **kwargs)
    except TypeError as exc:
        if "proxy" not in kwargs or "proxy" not in str(exc):
            raise
        kwargs.pop("proxy", None)
        return await websockets.connect(ws_url, **kwargs)


def _connect_supports_proxy(connect) -> bool:
    try:
        signature = inspect.signature(connect)
    except (TypeError, ValueError):
        return True
    return "proxy" in signature.parameters


def _websocket_proxy(config: ClaworldConfig):
    if config.http_proxy:
        return config.http_proxy
    if config.use_env_proxy:
        return True
    return None


def _delivery_status(body) -> str | None:
    if not isinstance(body, dict):
        return None
    delivery = body.get("delivery") if isinstance(body.get("delivery"), dict) else {}
    return str(delivery.get("status") or body.get("status") or "").strip() or None


def _ack_ids_for_payload(payload: dict, delivery_id: str) -> tuple[str, ...]:
    return _normalize_ack_ids((delivery_id, payload.get("deliveryId"), payload.get("sessionKey")))


def _valid_projection_receipt_ack(message: dict) -> bool:
    data = message.get("data") if isinstance(message.get("data"), dict) else {}
    attempt_id = str(data.get("projectionAttemptId") or "").strip()
    binding_id = str(data.get("projectionBindingId") or "").strip()
    delivery_id = str(data.get("deliveryId") or "").strip()
    idempotency_key = str(data.get("idempotencyKey") or "").strip()
    if not all((attempt_id, binding_id, delivery_id, idempotency_key)):
        return False
    try:
        return (
            idempotency_key == projection_idempotency_key(binding_id, delivery_id)
            and attempt_id == projection_attempt_id(binding_id, delivery_id)
        )
    except (TypeError, ValueError):
        return False


def _normalize_ack_ids(value: str | tuple[str, ...]) -> tuple[str, ...]:
    values = value if isinstance(value, tuple) else (value,)
    result = []
    seen = set()
    for item in values:
        normalized = str(item or "").strip()
        if not normalized or normalized in seen:
            continue
        result.append(normalized)
        seen.add(normalized)
    return tuple(result)


def _request_json_with_delivery_visibility_retry(config: ClaworldConfig, method: str, path: str, **kwargs) -> dict:
    for attempt in range(DELIVERY_VISIBILITY_RETRY_ATTEMPTS):
        try:
            return request_json(config, method, path, **kwargs)
        except ClaworldHttpError as exc:
            if not _is_delivery_visibility_miss(exc) or attempt >= DELIVERY_VISIBILITY_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(DELIVERY_VISIBILITY_RETRY_DELAY_SECONDS)
    return request_json(config, method, path, **kwargs)


def _is_delivery_visibility_miss(exc: ClaworldHttpError) -> bool:
    if exc.status != 404 or not isinstance(exc.body, dict):
        return False
    reason = str(exc.body.get("error") or exc.body.get("code") or exc.body.get("reason") or "").strip()
    return reason == "delivery_not_found"
