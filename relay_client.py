"""Async Claworld relay WebSocket client."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
from typing import Awaitable, Callable

from .config import ClaworldConfig
from .http_client import ClaworldHttpError, request_json
from .protocol import (
    auth_message,
    accepted_message,
    build_inbound_envelope,
    kept_silent_message,
    normalize_ws_url,
    reply_message,
)

DeliveryHandler = Callable[[object], Awaitable[None]]

DELIVERY_VISIBILITY_RETRY_ATTEMPTS = 20
DELIVERY_VISIBILITY_RETRY_DELAY_SECONDS = 0.01


class RelayClient:
    def __init__(
        self,
        config: ClaworldConfig,
        *,
        on_delivery: DeliveryHandler,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.on_delivery = on_delivery
        self.logger = logger or logging.getLogger(__name__)
        self.ws = None
        self._receiver_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._closed = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._auth_future: asyncio.Future | None = None
        self._ack_waiters: dict[tuple[str, str], list[asyncio.Future]] = {}
        self._delivery_tasks: set[asyncio.Task] = set()
        self._reconnect_failures = 0
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
        current_task = asyncio.current_task()
        control_tasks = [task for task in (self._heartbeat_task, self._receiver_task) if task and task is not current_task]
        for task in control_tasks:
            self._cancel_task(task)
        delivery_tasks = list(self._delivery_tasks)
        for task in delivery_tasks:
            self._cancel_task(task)
        if control_tasks:
            await asyncio.gather(*control_tasks, return_exceptions=True)
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
            fallback=lambda: self._accepted_http(delivery_id, session_key),
        )

    async def send_reply(self, delivery_id: str, session_key: str | None, reply_text: str) -> None:
        await self._send_with_ack(
            reply_message(delivery_id, session_key, reply_text),
            ack_events=("reply.accepted", "command.accepted"),
            delivery_id=delivery_id,
            fallback=lambda: self._reply_http(delivery_id, reply_text),
        )

    async def send_kept_silent(self, delivery_id: str, session_key: str | None, reason: str) -> None:
        await self._send_with_ack(
            kept_silent_message(delivery_id, session_key, reason),
            ack_events=("kept_silent.accepted", "command.accepted"),
            delivery_id=delivery_id,
            fallback=lambda: self._kept_silent_http(delivery_id, reason),
        )

    async def _open_once(self) -> None:
        import websockets

        current_task = asyncio.current_task()
        previous_receiver_task = self._receiver_task
        previous_ws = self.ws

        await self._cancel_and_wait_task(self._heartbeat_task)
        self._heartbeat_task = None
        if previous_receiver_task and previous_receiver_task is not current_task:
            await self._cancel_and_wait_task(previous_receiver_task)
            if self._receiver_task is previous_receiver_task:
                self._receiver_task = None
        if previous_ws is not None:
            try:
                await previous_ws.close()
            except Exception:
                pass
            if self.ws is previous_ws:
                self.ws = None

        ws_url = normalize_ws_url(self.config.server_url)
        self.logger.info("claworld relay connecting to %s", ws_url)
        ws = await websockets.connect(ws_url, ping_interval=None)
        self.ws = ws
        loop = asyncio.get_running_loop()
        self._auth_future = loop.create_future()
        receiver_task = asyncio.create_task(self._receive_loop(ws), name="claworld-relay-receiver")
        self._receiver_task = receiver_task
        try:
            await self._send_json(
                auth_message(
                    agent_id=self.agent_id,
                    credential=self.config.app_token,
                    client_version="claworld-hermes-plugin/0.1.0",
                ),
                ws=ws,
            )
            await asyncio.wait_for(self._auth_future, timeout=30.0)
        except Exception:
            await self._cancel_and_wait_task(receiver_task)
            try:
                await ws.close()
            except Exception:
                pass
            if self.ws is ws:
                self.ws = None
            if self._receiver_task is receiver_task:
                self._receiver_task = previous_receiver_task if previous_receiver_task is current_task else None
            raise

        self._reconnect_failures = 0
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws), name="claworld-relay-heartbeat")

    async def _receive_loop(self, ws) -> None:
        try:
            async for raw in ws:
                if self.ws is not ws:
                    return
                await self._handle_raw_message(raw)
            if self._closed.is_set() or self.ws is not ws:
                return
            raise RuntimeError("claworld relay websocket closed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.ws is not ws and self.ws is not None:
                return
            if self._auth_future and not self._auth_future.done():
                self._auth_future.set_exception(exc)
            if not self.config.reconnect or self._closed.is_set():
                self.logger.warning("claworld relay receiver stopped: %s", exc)
                return
            await self._reconnect_from_receiver(exc)

    async def _reconnect_from_receiver(self, disconnect_exc: Exception) -> None:
        owner_task = asyncio.current_task()
        self.logger.warning("claworld relay disconnected; reconnecting: %s", disconnect_exc)
        while self.config.reconnect and not self._closed.is_set():
            if self._receiver_task is not owner_task:
                return
            await asyncio.sleep(self._reconnect_delay_seconds())
            if self._receiver_task is not owner_task:
                return
            try:
                await self._open_once()
                return
            except asyncio.CancelledError:
                raise
            except Exception as reconnect_exc:
                self._reconnect_failures += 1
                if self._should_log_reconnect_failure(self._reconnect_failures):
                    self.logger.warning(
                        "claworld relay reconnect failed: %s%s",
                        reconnect_exc,
                        "" if self._reconnect_failures <= 3 else f" (attempt {self._reconnect_failures})",
                    )

    def _reconnect_delay_seconds(self) -> float:
        return min(max(self.config.heartbeat_seconds / 2, 0.5), 5.0)

    def _should_log_reconnect_failure(self, attempt: int) -> bool:
        return attempt <= 3 or attempt & (attempt - 1) == 0

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

        envelope = build_inbound_envelope(message)
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

    async def _send_json(self, payload: dict, *, ws=None) -> None:
        target_ws = ws or self.ws
        if target_ws is None:
            raise RuntimeError("claworld relay websocket is not connected")
        encoded = json.dumps(payload, ensure_ascii=False)
        async with self._send_lock:
            await target_ws.send(encoded)

    async def _send_with_ack(self, payload: dict, *, ack_events: tuple[str, ...], delivery_id: str, fallback) -> None:
        timeout = max(float(self.config.reply_ack_timeout_seconds), 0.1)
        if self.ws is None:
            await asyncio.to_thread(fallback)
            return

        ack_ids = _ack_ids_for_payload(payload, delivery_id)
        ack_future = self._register_ack_waiter(ack_events, ack_ids)
        try:
            await self._send_json(payload)
            await asyncio.wait_for(ack_future, timeout=timeout)
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            self.logger.warning("claworld relay ack failed; using HTTP fallback: %s", detail)
            await asyncio.to_thread(fallback)
        finally:
            self._remove_ack_waiter(ack_events, ack_ids, ack_future)

    def _register_ack_waiter(self, ack_events: tuple[str, ...], delivery_id: str | tuple[str, ...]) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        delivery_ids = _normalize_ack_ids(delivery_id)
        for event in ack_events:
            for ack_id in delivery_ids:
                self._ack_waiters.setdefault((event, ack_id), []).append(future)
        return future

    def _remove_ack_waiter(self, ack_events: tuple[str, ...], delivery_id: str | tuple[str, ...], future: asyncio.Future) -> None:
        delivery_ids = _normalize_ack_ids(delivery_id)
        for event in ack_events:
            for ack_id in delivery_ids:
                key = (event, ack_id)
                waiters = self._ack_waiters.get(key)
                if not waiters:
                    continue
                self._ack_waiters[key] = [item for item in waiters if item is not future]
                if not self._ack_waiters[key]:
                    self._ack_waiters.pop(key, None)

    def _resolve_ack_waiters(self, event: str, message: dict) -> None:
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        command = data.get("command") if isinstance(data.get("command"), dict) else {}
        delivery_id = (
            data.get("acceptedDeliveryId")
            or data.get("repliedDeliveryId")
            or data.get("keptSilentDeliveryId")
            or data.get("deliveryId")
            or data.get("inboxItemId")
            or command.get("deliveryId")
            or command.get("aggregateId")
            or command.get("partitionKey")
            or message.get("deliveryId")
        )
        delivery_id = str(delivery_id or "").strip()
        if not delivery_id:
            self.logger.debug("claworld relay ack ignored without delivery/session key: event=%s", event)
            return
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
        for future in waiters:
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

    async def _heartbeat_loop(self, ws) -> None:
        while not self._closed.is_set():
            await asyncio.sleep(self.config.heartbeat_seconds)
            if self.ws is not ws:
                return
            try:
                await self._send_json({"type": "heartbeat"}, ws=ws)
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

    async def _cancel_and_wait_task(self, task: asyncio.Task | None) -> None:
        if not task:
            return
        if task is asyncio.current_task():
            return
        self._cancel_task(task)
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)


def _delivery_status(body) -> str | None:
    if not isinstance(body, dict):
        return None
    delivery = body.get("delivery") if isinstance(body.get("delivery"), dict) else {}
    return str(delivery.get("status") or body.get("status") or "").strip() or None


def _ack_ids_for_payload(payload: dict, delivery_id: str) -> tuple[str, ...]:
    return _normalize_ack_ids((delivery_id, payload.get("deliveryId"), payload.get("sessionKey")))


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
