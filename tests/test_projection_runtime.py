from __future__ import annotations

import asyncio
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "claworld_hermes_plugin"
pkg = sys.modules.get(PACKAGE)
if pkg is None:
    pkg = types.ModuleType(PACKAGE)
    pkg.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = pkg

from claworld_hermes_plugin import projection_runtime as runtime


class _Platform:
    def __init__(self, value: str):
        self.value = value


def _source(
    *,
    platform: str = "feishu",
    chat_id: str = "oc_group",
    chat_type: str = "group",
    message_id: str = "om_origin",
    thread_id: str | None = None,
    profile: str | None = None,
):
    return types.SimpleNamespace(
        platform=_Platform(platform),
        chat_id=chat_id,
        chat_type=chat_type,
        message_id=message_id,
        thread_id=thread_id,
        profile=profile,
    )


def _event(source=None, *, internal: bool = False, message_id: str | None = None):
    source = source or _source()
    return types.SimpleNamespace(
        source=source,
        message_id=source.message_id if message_id is None else message_id,
        internal=internal,
    )


def _gateway(platform: str, adapter):
    adapters = {_Platform(platform): adapter}
    router = types.SimpleNamespace(adapters=adapters)
    gateway = types.SimpleNamespace(
        adapters=adapters,
        delivery_router=router,
        _profile_adapters={},
        _active_profile_name=lambda: "default",
    )
    return gateway, router


class ProjectionRouteCaptureTests(unittest.TestCase):
    def setUp(self):
        runtime._reset_projection_runtime_for_tests()

    def tearDown(self):
        runtime._reset_projection_runtime_for_tests()

    def test_captures_exact_native_group_route(self):
        gateway, _ = _gateway("feishu", object())
        event = _event(
            _source(
                platform="feishu",
                chat_id="oc_exact",
                message_id="om_exact",
                thread_id="omt_thread",
            )
        )

        result = runtime.pre_gateway_dispatch(event=event, gateway=gateway)

        self.assertIsNone(result)
        route = runtime.get_captured_projection_route(
            platform="feishu",
            chat_id="oc_exact",
            origin_message_id="om_exact",
        )
        self.assertIsNotNone(route)
        self.assertEqual(route.chat_id, "oc_exact")
        self.assertEqual(route.thread_id, "omt_thread")
        self.assertEqual(route.chat_type, "group")
        self.assertEqual(route.origin_message_id, "om_exact")

        # There is deliberately no global/latest fallback.
        self.assertIsNone(
            runtime.get_captured_projection_route(
                platform="feishu",
                chat_id="oc_exact",
                origin_message_id="om_other",
            )
        )

    def test_binds_event_message_id_to_source_for_exact_session_lookup(self):
        gateway, _ = _gateway("feishu", object())
        source = _source(
            platform="feishu",
            chat_id="oc_exact",
            message_id="",
        )
        event = _event(source, message_id="om_event_exact")

        runtime.pre_gateway_dispatch(event=event, gateway=gateway)

        # Feishu supplies the inbound id on MessageEvent while Hermes builds
        # HERMES_SESSION_MESSAGE_ID from SessionSource.  The hook must bridge
        # those two adapter-owned representations without using a latest-route
        # fallback or model-authored routing data.
        self.assertEqual(source.message_id, "om_event_exact")
        session_values = {
            "HERMES_SESSION_PLATFORM": "feishu",
            "HERMES_SESSION_CHAT_ID": "oc_exact",
            "HERMES_SESSION_MESSAGE_ID": source.message_id,
            "HERMES_SESSION_PROFILE": "default",
        }
        gateway_module = types.ModuleType("gateway")
        session_context_module = types.ModuleType("gateway.session_context")
        session_context_module.get_session_env = (
            lambda name, default="": session_values.get(name, default)
        )
        gateway_module.session_context = session_context_module
        with patch.dict(
            sys.modules,
            {
                "gateway": gateway_module,
                "gateway.session_context": session_context_module,
            },
        ):
            route = runtime.get_current_projection_route()
        self.assertIsNotNone(route)
        self.assertEqual(route.origin_message_id, "om_event_exact")

    def test_rejects_mismatched_source_and_event_message_ids(self):
        gateway, _ = _gateway("feishu", object())
        source = _source(
            platform="feishu",
            chat_id="oc_exact",
            message_id="om_source",
        )

        runtime.pre_gateway_dispatch(
            event=_event(source, message_id="om_event"),
            gateway=gateway,
        )

        self.assertEqual(source.message_id, "om_source")
        self.assertIsNone(
            runtime.get_captured_projection_route(
                platform="feishu",
                chat_id="oc_exact",
                origin_message_id="om_source",
            )
        )
        self.assertIsNone(
            runtime.get_captured_projection_route(
                platform="feishu",
                chat_id="oc_exact",
                origin_message_id="om_event",
            )
        )

    def test_real_hermes_session_context_resolves_exact_event_message_id(self):
        hermes_root = Path(sys.prefix).resolve().parent
        if not (hermes_root / "gateway" / "run.py").is_file():
            self.skipTest("Hermes source tree is unavailable")
        script = r'''
import importlib.util
import sys
import types

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_context
from gateway.session_context import get_session_env

spec = importlib.util.spec_from_file_location("projection_runtime_under_test", sys.argv[1])
runtime = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runtime
spec.loader.exec_module(runtime)

source = SessionSource(
    platform=Platform.FEISHU,
    chat_id="oc_contract",
    chat_type="group",
    user_id="ou_user",
    message_id=None,
    profile="default",
)
event = MessageEvent(
    text="approved projection request",
    source=source,
    message_id="om_contract_exact",
)
runner = object.__new__(GatewayRunner)
runner.adapters = {}
runner.delivery_router = types.SimpleNamespace(adapters={})
runner._profile_adapters = {}
runner._active_profile_name = lambda: "default"

runtime.pre_gateway_dispatch(event=event, gateway=runner)
context = build_session_context(source, GatewayConfig(platforms={}))
context.session_key = "agent:main:feishu:group:oc_contract:ou_user"
tokens = runner._set_session_env(context)
try:
    assert get_session_env("HERMES_SESSION_MESSAGE_ID", "") == "om_contract_exact"
    route = runtime.get_current_projection_route()
finally:
    runner._clear_session_env(tokens)

assert route is not None
assert route.origin_message_id == "om_contract_exact"
'''
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(ROOT / "projection_runtime.py"),
            ],
            cwd=hermes_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"Hermes contract subprocess failed: {completed.stderr}",
        )

    def test_ignores_dm_internal_and_unsupported_routes(self):
        gateway, _ = _gateway("feishu", object())
        runtime.pre_gateway_dispatch(
            event=_event(_source(chat_type="dm", message_id="dm1")),
            gateway=gateway,
        )
        runtime.pre_gateway_dispatch(
            event=_event(_source(message_id="internal1"), internal=True),
            gateway=gateway,
        )
        runtime.pre_gateway_dispatch(
            event=_event(_source(platform="discord", message_id="discord1")),
            gateway=gateway,
        )
        runtime.pre_gateway_dispatch(
            event=_event(_source(chat_type="forum", message_id="forum1")),
            gateway=gateway,
        )

        for platform, message_id in (
            ("feishu", "dm1"),
            ("feishu", "internal1"),
            ("feishu", "forum1"),
            ("discord", "discord1"),
        ):
            self.assertIsNone(
                runtime.get_captured_projection_route(
                    platform=platform,
                    chat_id="oc_group",
                    origin_message_id=message_id,
                )
            )

    def test_captures_telegram_forum_as_supergroup_with_thread(self):
        gateway, _ = _gateway("telegram", object())
        runtime.pre_gateway_dispatch(
            event=_event(
                _source(
                    platform="telegram",
                    chat_id="-100forum",
                    chat_type="forum",
                    message_id="44",
                    thread_id="901",
                )
            ),
            gateway=gateway,
        )

        route = runtime.get_captured_projection_route(
            platform="telegram",
            chat_id="-100forum",
            origin_message_id="44",
        )
        self.assertIsNotNone(route)
        self.assertEqual(route.chat_type, "supergroup")
        self.assertEqual(route.thread_id, "901")
        self.assertEqual(route.profile, "default")

    def test_captures_telegram_channel_route(self):
        gateway, _ = _gateway("telegram", object())
        runtime.pre_gateway_dispatch(
            event=_event(
                _source(
                    platform="telegram",
                    chat_id="-1002",
                    chat_type="channel",
                    message_id="43",
                )
            ),
            gateway=gateway,
        )

        route = runtime.get_captured_projection_route(
            platform="telegram",
            chat_id="-1002",
            origin_message_id="43",
        )
        self.assertIsNotNone(route)
        self.assertEqual(route.chat_type, "channel")

    def test_route_is_profile_scoped(self):
        gateway, _ = _gateway("telegram", object())
        runtime.pre_gateway_dispatch(
            event=_event(
                _source(
                    platform="telegram",
                    chat_id="-1001",
                    message_id="42",
                    profile="worker",
                )
            ),
            gateway=gateway,
        )

        self.assertIsNotNone(
            runtime.get_captured_projection_route(
                platform="telegram",
                chat_id="-1001",
                origin_message_id="42",
                profile="worker",
            )
        )
        self.assertIsNone(
            runtime.get_captured_projection_route(
                platform="telegram",
                chat_id="-1001",
                origin_message_id="42",
                profile="default",
            )
        )

    def test_telegram_message_id_collision_is_chat_scoped(self):
        gateway, _ = _gateway("telegram", object())
        for chat_id in ("-1001", "-1002"):
            runtime.pre_gateway_dispatch(
                event=_event(
                    _source(
                        platform="telegram",
                        chat_id=chat_id,
                        message_id="42",
                    )
                ),
                gateway=gateway,
            )

        first = runtime.get_captured_projection_route(
            platform="telegram",
            chat_id="-1001",
            origin_message_id="42",
        )
        second = runtime.get_captured_projection_route(
            platform="telegram",
            chat_id="-1002",
            origin_message_id="42",
        )
        self.assertEqual(first.chat_id, "-1001")
        self.assertEqual(second.chat_id, "-1002")


class ProjectionCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        runtime._reset_projection_runtime_for_tests()

    async def asyncTearDown(self):
        runtime._reset_projection_runtime_for_tests()

    async def test_feishu_uses_is_in_chat(self):
        response = types.SimpleNamespace(
            success=lambda: True,
            data=types.SimpleNamespace(is_in_chat=True),
        )
        is_in_chat = unittest.mock.Mock(return_value=response)
        client = types.SimpleNamespace(
            im=types.SimpleNamespace(
                v1=types.SimpleNamespace(
                    chat_members=types.SimpleNamespace(is_in_chat=is_in_chat)
                )
            )
        )

        class Adapter:
            _client = client
            _bot_open_id = "ou_bot"
            _app_id = "cli_app"
            _bot_name = "小发发"

            async def _run_blocking(self, fn, request):
                return fn(request)

            async def get_chat_info(self, chat_id):
                return {"chat_id": chat_id, "name": "xfx", "type": "group", "raw_type": "group"}

        adapter = Adapter()
        gateway, _ = _gateway("feishu", adapter)
        runtime.pre_gateway_dispatch(event=None, gateway=gateway)

        with patch.object(runtime, "_build_feishu_is_in_chat_request", return_value=object()):
            result = await runtime.can_send_to(
                platform="feishu",
                chat_id="oc_xfx",
                profile="default",
            )

        self.assertTrue(result["success"])
        self.assertTrue(result["canSend"])
        self.assertEqual(result["reason"], "member_verified")
        self.assertEqual(result["permissionBasis"], "feishu_is_in_chat")
        self.assertEqual(result["externalBotId"], "ou_bot")
        self.assertEqual(result["botMention"], "ou_bot")
        is_in_chat.assert_called_once()

    async def test_feishu_not_member_is_definitive(self):
        response = types.SimpleNamespace(
            success=lambda: True,
            data=types.SimpleNamespace(is_in_chat=False),
        )
        client = types.SimpleNamespace(
            im=types.SimpleNamespace(
                v1=types.SimpleNamespace(
                    chat_members=types.SimpleNamespace(is_in_chat=lambda _request: response)
                )
            )
        )

        class Adapter:
            _client = client
            _bot_open_id = ""
            _app_id = "cli_app"
            _bot_name = "Agent B"

            async def _run_blocking(self, fn, request):
                return fn(request)

            async def get_chat_info(self, _chat_id):
                return {"type": "group", "raw_type": "group"}

        gateway, _ = _gateway("feishu", Adapter())
        runtime.pre_gateway_dispatch(event=None, gateway=gateway)

        with patch.object(runtime, "_build_feishu_is_in_chat_request", return_value=object()):
            result = await runtime.can_send_to(
                platform="feishu",
                chat_id="oc_xfx",
                profile="default",
            )

        self.assertTrue(result["success"])
        self.assertFalse(result["canSend"])
        self.assertEqual(result["reason"], "bot_not_member")
        self.assertEqual(result["externalBotId"], "cli_app")
        self.assertEqual(result["botMention"], "Agent B")

    async def test_feishu_chat_lookup_fails_closed_for_dm_fallback(self):
        response = types.SimpleNamespace(
            success=lambda: True,
            data=types.SimpleNamespace(is_in_chat=True),
        )
        client = types.SimpleNamespace(
            im=types.SimpleNamespace(
                v1=types.SimpleNamespace(
                    chat_members=types.SimpleNamespace(is_in_chat=lambda _request: response)
                )
            )
        )

        class Adapter:
            _client = client
            _bot_open_id = "ou_bot"
            _app_id = "cli_app"
            _bot_name = "Agent A"

            async def _run_blocking(self, fn, request):
                return fn(request)

            async def get_chat_info(self, _chat_id):
                return {"type": "dm"}

        gateway, _ = _gateway("feishu", Adapter())
        runtime.pre_gateway_dispatch(event=None, gateway=gateway)

        with patch.object(runtime, "_build_feishu_is_in_chat_request", return_value=object()):
            result = await runtime.can_send_to(
                platform="feishu",
                chat_id="oc_xfx",
                profile="default",
            )

        self.assertTrue(result["success"])
        self.assertFalse(result["canSend"])
        self.assertEqual(result["reason"], "not_group_chat")

    async def test_telegram_checks_membership_and_permissions(self):
        bot = types.SimpleNamespace(id=123, username="agent_a_bot")
        bot.get_chat = AsyncMock(
            return_value=types.SimpleNamespace(
                type="supergroup",
                is_forum=False,
                permissions=types.SimpleNamespace(can_send_messages=True),
            )
        )
        bot.get_chat_member = AsyncMock(
            return_value=types.SimpleNamespace(status="member")
        )
        adapter = types.SimpleNamespace(_bot=bot)
        gateway, _ = _gateway("telegram", adapter)
        runtime.pre_gateway_dispatch(event=None, gateway=gateway)

        result = await runtime.can_send_to(
            platform="telegram",
            chat_id="-100123",
            profile="default",
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["canSend"])
        self.assertEqual(result["reason"], "member_verified")
        self.assertEqual(result["botUsername"], "agent_a_bot")
        self.assertEqual(result["externalBotId"], "123")
        self.assertEqual(result["botMention"], "@agent_a_bot")
        bot.get_chat_member.assert_awaited_once_with(-100123, 123)

    async def test_telegram_forum_keeps_supergroup_chat_type(self):
        bot = types.SimpleNamespace(id=123, username="agent_a_bot")
        bot.get_chat = AsyncMock(
            return_value=types.SimpleNamespace(
                type="supergroup",
                is_forum=True,
                permissions=types.SimpleNamespace(can_send_messages=True),
            )
        )
        bot.get_chat_member = AsyncMock(
            return_value=types.SimpleNamespace(status="member")
        )
        gateway, _ = _gateway("telegram", types.SimpleNamespace(_bot=bot))
        runtime.pre_gateway_dispatch(event=None, gateway=gateway)

        result = await runtime.can_send_to(
            platform="telegram",
            chat_id="-100123",
            thread_id="88",
            profile="default",
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["canSend"])
        self.assertEqual(result["chatType"], "supergroup")
        self.assertFalse(result["threadVerified"])

    async def test_telegram_restricted_bot_fails_closed(self):
        bot = types.SimpleNamespace(id=123, username="agent_b_bot")
        bot.get_chat = AsyncMock(
            return_value=types.SimpleNamespace(
                type="supergroup",
                is_forum=False,
                permissions=types.SimpleNamespace(can_send_messages=True),
            )
        )
        bot.get_chat_member = AsyncMock(
            return_value=types.SimpleNamespace(
                status="restricted",
                can_send_messages=False,
            )
        )
        gateway, _ = _gateway("telegram", types.SimpleNamespace(_bot=bot))
        runtime.pre_gateway_dispatch(event=None, gateway=gateway)

        result = await runtime.can_send_to(
            platform="telegram",
            chat_id="-100123",
            profile="default",
        )

        self.assertTrue(result["success"])
        self.assertFalse(result["canSend"])
        self.assertEqual(result["reason"], "permission_denied")

    async def test_telegram_channel_requires_post_permission(self):
        bot = types.SimpleNamespace(id=123, username="agent_b_bot")
        bot.get_chat = AsyncMock(
            return_value=types.SimpleNamespace(
                type="channel",
                is_forum=False,
                permissions=None,
            )
        )
        bot.get_chat_member = AsyncMock(
            return_value=types.SimpleNamespace(
                status="administrator",
                can_post_messages=False,
            )
        )
        gateway, _ = _gateway("telegram", types.SimpleNamespace(_bot=bot))
        runtime.pre_gateway_dispatch(event=None, gateway=gateway)

        result = await runtime.can_send_to(
            platform="telegram",
            chat_id="-100123",
            profile="default",
        )

        self.assertTrue(result["success"])
        self.assertFalse(result["canSend"])
        self.assertEqual(result["reason"], "permission_denied")


class ProjectionSendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        runtime._reset_projection_runtime_for_tests()

    async def asyncTearDown(self):
        runtime._reset_projection_runtime_for_tests()

    async def test_send_uses_adapter_directly_and_strips_mentions(self):
        adapter = types.SimpleNamespace()
        adapter.send = AsyncMock(
            return_value=types.SimpleNamespace(
                success=True,
                message_id="om_external",
                error_kind=None,
            )
        )
        gateway, _ = _gateway("feishu", adapter)
        runtime.pre_gateway_dispatch(event=None, gateway=gateway)

        mirror = unittest.mock.Mock(side_effect=AssertionError("must not mirror"))
        fake_mirror_module = types.SimpleNamespace(mirror_to_session=mirror)
        with patch.dict(sys.modules, {"gateway.mirror": fake_mirror_module}):
            result = await runtime.send_without_mirror(
                platform="feishu",
                chat_id="oc_xfx",
                thread_id="omt_topic",
                content="@AgentB 请看 <at user_id=\"ou_x\">Agent C</at>",
                profile="default",
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["messageId"], "om_external")
        self.assertTrue(result["receiptReady"])
        self.assertTrue(result["mentionsStripped"])
        mirror.assert_not_called()
        adapter.send.assert_awaited_once_with(
            chat_id="oc_xfx",
            content="AgentB 请看 Agent C",
            metadata={"thread_id": "omt_topic"},
        )

    async def test_send_does_not_return_raw_adapter_error(self):
        adapter = types.SimpleNamespace()
        adapter.send = AsyncMock(
            return_value=types.SimpleNamespace(
                success=False,
                message_id=None,
                error="authorization Bearer super-secret",
                error_kind="forbidden",
            )
        )
        gateway, _ = _gateway("telegram", adapter)
        runtime.pre_gateway_dispatch(event=None, gateway=gateway)

        result = await runtime.send_without_mirror(
            platform="telegram",
            chat_id="-100123",
            content="public-safe text",
            profile="default",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "adapter_rejected")
        self.assertEqual(result["errorKind"], "forbidden")
        self.assertNotIn("super-secret", repr(result))

    async def test_empty_after_mention_filter_is_not_sent(self):
        adapter = types.SimpleNamespace(send=AsyncMock())
        gateway, _ = _gateway("telegram", adapter)
        runtime.pre_gateway_dispatch(event=None, gateway=gateway)

        result = await runtime.send_without_mirror(
            platform="telegram",
            chat_id="-100123",
            content="<at user_id=\"ou_x\"></at>",
            profile="default",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "empty_content")
        adapter.send.assert_not_awaited()

    async def test_capability_and_send_require_explicit_profile(self):
        adapter = types.SimpleNamespace(send=AsyncMock())
        gateway, _ = _gateway("telegram", adapter)
        runtime.pre_gateway_dispatch(event=None, gateway=gateway)

        capability = await runtime.can_send_to(
            platform="telegram",
            chat_id="-100123",
        )
        sent = await runtime.send_without_mirror(
            platform="telegram",
            chat_id="-100123",
            content="must not fallback",
        )

        self.assertEqual(capability["reason"], "profile_required")
        self.assertEqual(sent["reason"], "profile_required")
        adapter.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
