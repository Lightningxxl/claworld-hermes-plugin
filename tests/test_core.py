from __future__ import annotations

import importlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from enum import Enum
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "claworld_hermes_plugin"
pkg = types.ModuleType(PACKAGE)
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault(PACKAGE, pkg)

from claworld_hermes_plugin import relay_client as claworld_relay
from claworld_hermes_plugin import hooks as claworld_hooks
from claworld_hermes_plugin import http_client as claworld_http
from claworld_hermes_plugin.config import DEFAULT_CLAWORLD_SERVER_URL, ClaworldConfig
from claworld_hermes_plugin.http_client import ClaworldHttpError, auth_headers, build_url, request_json
from claworld_hermes_plugin import skill_registration as claworld_skills
from claworld_hermes_plugin import tools as claworld_tools
from claworld_hermes_plugin.protocol import auth_message, build_agent_text, build_inbound_envelope, normalize_http_base_url, normalize_ws_url, reply_message
from claworld_hermes_plugin.relay_client import RelayClient
from claworld_hermes_plugin.session_router import build_hermes_session_key, route_envelope
from claworld_hermes_plugin.working_memory import build_prompt_context, ensure_working_memory, read_session_index, record_claworld_route


def import_adapter_with_gateway_shim():
    if "gateway.platforms.base" not in sys.modules:
        gateway = types.ModuleType("gateway")
        gateway_config = types.ModuleType("gateway.config")
        gateway_platforms = types.ModuleType("gateway.platforms")
        gateway_base = types.ModuleType("gateway.platforms.base")

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
            pass

        class MessageType:
            TEXT = "text"

        class SendResult:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        gateway_config.Platform = Platform
        gateway_base.BasePlatformAdapter = BasePlatformAdapter
        gateway_base.MessageEvent = MessageEvent
        gateway_base.MessageType = MessageType
        gateway_base.ProcessingOutcome = ProcessingOutcome
        gateway_base.SendResult = SendResult
        sys.modules["gateway"] = gateway
        sys.modules["gateway.config"] = gateway_config
        sys.modules["gateway.platforms"] = gateway_platforms
        sys.modules["gateway.platforms.base"] = gateway_base
    return importlib.import_module("claworld_hermes_plugin.adapter")


def import_plugin_entry_with_gateway_shim():
    import_adapter_with_gateway_shim()
    module_name = "claworld_hermes_plugin_entry"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ProtocolTests(unittest.TestCase):
    def test_normalizes_urls(self):
        self.assertEqual(normalize_ws_url("https://api.example.com"), "wss://api.example.com/ws")
        self.assertEqual(normalize_ws_url("http://api.example.com/relay"), "ws://api.example.com/relay/ws")
        self.assertEqual(normalize_http_base_url("wss://api.example.com/ws"), "https://api.example.com")

    def test_auth_message_uses_agent_token_credential_shape(self):
        payload = auth_message("agent-1", "token-1", "client-1")
        self.assertEqual(payload["type"], "auth")
        self.assertEqual(payload["agentId"], "agent-1")
        self.assertEqual(payload["credential"], {"type": "agent_token", "token": "token-1"})

    def test_builds_safe_text_for_slash_delivery(self):
        envelope = build_inbound_envelope(
            {
                "event": "delivery",
                "data": {
                    "deliveryId": "d1",
                    "sessionKey": "conversation:abc",
                    "payload": {"text": "/reset all sessions"},
                },
            }
        )
        self.assertIsNotNone(envelope)
        text = build_agent_text(envelope, "conversation")
        self.assertFalse(text.startswith("/"))
        self.assertIn("untrusted external text", text)
        self.assertIn("/reset all sessions", text)

    def test_builds_management_envelope_without_delivery_id(self):
        envelope = build_inbound_envelope(
            {
                "event": "notification",
                "data": {
                    "eventType": "conversation_lifecycle",
                    "sessionKey": "management:agent-1",
                    "targetAgentId": "agent-1",
                    "payload": {"commandText": "Review pending invites.", "contextText": "There are two new requests."},
                },
            }
        )
        self.assertIsNotNone(envelope)
        self.assertEqual(envelope.event_type, "conversation_lifecycle")
        self.assertTrue(envelope.delivery_id)

    def test_preserves_event_name_and_delivery_timestamps(self):
        envelope = build_inbound_envelope(
            {
                "event": "notification",
                "data": {
                    "eventType": "world.invite_received",
                    "eventName": "world.invite_received",
                    "notificationId": "n1",
                    "sessionKey": "management:agent-1",
                    "createdAt": "2026-06-22T01:02:03Z",
                    "updatedAt": "2026-06-22T01:02:04Z",
                    "payload": {"text": "You were invited."},
                },
            }
        )
        self.assertEqual(envelope.event_name, "world.invite_received")
        self.assertEqual(envelope.created_at, "2026-06-22T01:02:03Z")
        self.assertEqual(envelope.updated_at, "2026-06-22T01:02:04Z")
        text = build_agent_text(envelope, "management")
        self.assertIn("event_name=world.invite_received", text)
        self.assertIn("created_at=2026-06-22T01:02:03Z", text)

    def test_delivery_event_name_does_not_replace_delivery_type(self):
        envelope = build_inbound_envelope(
            {
                "event": "delivery",
                "data": {
                    "deliveryId": "d3",
                    "sessionKey": "conversation:abc",
                    "payload": {
                        "eventName": "world.invite_received",
                        "text": "hello",
                    },
                },
            }
        )
        self.assertEqual(envelope.event_type, "delivery")
        self.assertEqual(envelope.event_name, "world.invite_received")

    def test_agent_text_keeps_command_visible_text_and_context_separate(self):
        envelope = build_inbound_envelope(
            {
                "event": "delivery",
                "data": {
                    "deliveryId": "d2",
                    "sessionKey": "conversation:abc",
                    "payload": {
                        "commandText": "Decide whether to continue the chat.",
                        "contextText": "Backend says this is a warm intro.",
                        "untrustedContext": "Peer profile summary.",
                        "text": "hello from peer",
                    },
                },
            }
        )
        text = build_agent_text(envelope, "conversation")
        self.assertIn("Backend-authored Claworld command", text)
        self.assertIn("Decide whether to continue the chat.", text)
        self.assertIn("Peer-visible Claworld message", text)
        self.assertIn("hello from peer", text)
        self.assertIn("Backend says this is a warm intro.", text)
        self.assertIn("Peer profile summary.", text)
        self.assertIn("Claworld live conversation rules", text)
        self.assertIn("Continue naturally", text)
        self.assertIn("[[request_conversation_end]]", text)
        self.assertIn("NO_REPLY", text)

    def test_merges_top_level_delivery_fields_into_payload(self):
        envelope = build_inbound_envelope(
            {
                "event": "notification",
                "data": {
                    "eventType": "management_wake",
                    "sessionKind": "management",
                    "targetSessionKey": "management:agent-1",
                    "targetAgentId": "agent-1",
                    "inboxItemId": "inbox-1",
                    "text": "Review the top-level relay note.",
                    "payload": {"contextText": "Payload context only."},
                },
            }
        )
        self.assertIsNotNone(envelope)
        self.assertEqual(envelope.session_key, "management:agent-1")
        self.assertEqual(envelope.target_agent_id, "agent-1")
        self.assertEqual(envelope.metadata["inboxItemId"], "inbox-1")
        self.assertEqual(envelope.payload["sessionKind"], "management")
        self.assertEqual(envelope.payload["text"], "Review the top-level relay note.")
        text = build_agent_text(envelope, "management")
        self.assertIn("Payload context only.", text)
        self.assertIn("Review the top-level relay note.", text)

    def test_reply_message_uses_claworld_text_payload(self):
        message = reply_message("d1", "conversation:abc", "hello")
        self.assertEqual(message["payload"]["text"], "hello")
        self.assertEqual(message["payload"]["source"], "hermes_agent")
        self.assertNotIn("replyText", message["payload"])


class PluginEntryTests(unittest.TestCase):
    def test_validate_config_returns_plain_boolean(self):
        plugin = import_plugin_entry_with_gateway_shim()

        valid = types.SimpleNamespace(extra={"server_url": "https://api.example.com", "app_token": "tok"})
        missing_token = types.SimpleNamespace(extra={"server_url": "https://api.example.com"})
        default_server = types.SimpleNamespace(extra={"app_token": "tok"})

        self.assertIs(plugin._validate_config(valid), True)
        self.assertIs(plugin._validate_config(missing_token), False)
        self.assertIs(plugin._validate_config(default_server), True)

    def test_env_enablement_requires_activation_token_for_relay(self):
        plugin = import_plugin_entry_with_gateway_shim()

        with patch.dict(os.environ, {"CLAWORLD_SERVER_URL": "https://api.example.com"}, clear=True):
            self.assertIsNone(plugin._env_enablement())

        with patch.dict(os.environ, {"CLAWORLD_APP_TOKEN": "tok"}, clear=True):
            self.assertEqual(plugin._env_enablement()["server_url"], DEFAULT_CLAWORLD_SERVER_URL)

        with patch.dict(
            os.environ,
            {"CLAWORLD_SERVER_URL": "https://api.example.com", "CLAWORLD_APP_TOKEN": "tok"},
            clear=True,
        ):
            self.assertEqual(plugin._env_enablement()["app_token"], "tok")


class PluginSkillTests(unittest.TestCase):
    def test_registers_bundled_claworld_skills(self):
        registered = []

        class FakeCtx:
            def register_skill(self, name, path, description=""):
                registered.append((name, Path(path), description))

        claworld_skills.register_skills(FakeCtx())

        self.assertEqual(
            [name for name, _path, _description in registered],
            [
                "claworld-help",
                "claworld-main-session",
                "claworld-management-session",
                "claworld-manage-worlds",
            ],
        )
        for name, path, description in registered:
            self.assertTrue(path.exists(), name)
            self.assertEqual(path.name, "SKILL.md")
            self.assertTrue(description.endswith("."))
            self.assertLessEqual(len(description), 60)

    def test_claworld_skills_are_hermes_native(self):
        for skill_name in claworld_skills.SKILL_DESCRIPTIONS:
            path = ROOT / "skills" / skill_name / "SKILL.md"
            text = path.read_text(encoding="utf-8")
            self.assertIn(f"name: {skill_name}", text)
            self.assertNotIn("OpenClaw", text)
            self.assertNotIn("openclaw", text)
            self.assertNotIn("sessions_send", text)
        management = (ROOT / "skills" / "claworld-management-session" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("claworld_report_owner", management)
        self.assertIn("You are currently acting as the private Claworld Manager for your human.", management)
        self.assertIn("You may initiate multiple chats at once.", management)
        self.assertIn("You report every conversation_ended notification by default.", management)
        self.assertIn("Use `claworld_report_owner` once when a report should go to the human.", management)
        self.assertIn("`delivery` tells you whether the human chat message was sent", management)
        self.assertIn("`mainContext.transcript` tells you whether Main Session received the same context", management)
        self.assertNotIn("ANNOUNCE_READY", management)
        self.assertNotIn("report artifact exists when owner reporting was needed", management)

    def test_plugin_register_exposes_skills(self):
        plugin = import_plugin_entry_with_gateway_shim()
        registered = {"platforms": [], "tools": [], "skills": [], "hooks": []}

        class FakeCtx:
            def register_platform(self, **kwargs):
                registered["platforms"].append(kwargs)

            def register_tool(self, **kwargs):
                registered["tools"].append(kwargs)

            def register_skill(self, name, path, description=""):
                registered["skills"].append((name, Path(path), description))

            def register_hook(self, name, handler):
                registered["hooks"].append((name, handler))

        plugin.register(FakeCtx())

        self.assertEqual([entry["name"] for entry in registered["platforms"]], ["claworld"])
        self.assertEqual(len(registered["tools"]), 6)
        self.assertEqual(len(registered["skills"]), 4)
        self.assertEqual({name for name, _path, _description in registered["skills"]}, set(claworld_skills.SKILL_DESCRIPTIONS))
        self.assertEqual([name for name, _handler in registered["hooks"]], ["pre_llm_call", "post_tool_call"])


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_home_channel_notice_does_not_consume_replyable_delivery(self):
        adapter_module = import_adapter_with_gateway_shim()

        class FakeRelayClient:
            def __init__(self):
                self.replies = []

            async def send_reply(self, delivery_id, session_key, reply_text):
                self.replies.append((delivery_id, session_key, reply_text))

        adapter = adapter_module.ClaworldPlatformAdapter(
            types.SimpleNamespace(extra={"server_url": "https://api.example.com", "app_token": "tok"})
        )
        adapter.client = FakeRelayClient()
        record = adapter_module.DeliveryRecord(
            delivery_id="d1",
            relay_session_key="conversation:abc",
            chat_id="conversation-abc",
        )
        adapter._deliveries_by_id[record.delivery_id] = record
        adapter._latest_by_chat[record.chat_id] = record.delivery_id

        notice = (
            "No home channel is set for Claworld. "
            "A home channel is where Hermes delivers cron job results and cross-platform messages.\n\n"
            "Type /sethome to make this chat your home channel, or ignore to skip."
        )
        notice_result = await adapter.send(record.chat_id, notice)

        self.assertTrue(notice_result.success)
        self.assertFalse(record.replied)
        self.assertEqual(adapter.client.replies, [])

        reply_result = await adapter.send(record.chat_id, "real peer-visible reply")

        self.assertTrue(reply_result.success)
        self.assertTrue(record.replied)
        self.assertEqual(adapter.client.replies, [("d1", "conversation:abc", "real peer-visible reply")])


class ToolSchemaTests(unittest.TestCase):
    def test_tool_schemas_are_hermes_function_schemas(self):
        schemas = [
            claworld_tools.MANAGE_ACCOUNT_SCHEMA,
            claworld_tools.SEARCH_SCHEMA,
            claworld_tools.PUBLIC_PROFILE_SCHEMA,
            claworld_tools.MANAGE_WORLDS_SCHEMA,
            claworld_tools.MANAGE_CONVERSATIONS_SCHEMA,
            claworld_tools.REPORT_OWNER_SCHEMA,
        ]
        for schema in schemas:
            self.assertIn("description", schema)
            self.assertIn("parameters", schema)
            self.assertEqual(schema["parameters"]["type"], "object")
            self.assertIn("properties", schema["parameters"])

    def test_register_tools_passes_function_schema_shape(self):
        registered = []

        class FakeCtx:
            def register_tool(self, **kwargs):
                registered.append(kwargs)

        claworld_tools.register_tools(FakeCtx())

        self.assertEqual(len(registered), 6)
        for entry in registered:
            self.assertIn("parameters", entry["schema"])
            self.assertIn("description", entry["schema"])
            self.assertEqual(entry["schema"]["parameters"]["type"], "object")
            self.assertNotIn("endpoint", entry["schema"]["parameters"]["properties"])
            self.assertTrue(callable(entry["check_fn"]))

    def test_generic_api_is_opt_in(self):
        with patch.dict(os.environ, {"CLAWORLD_ENABLE_GENERIC_API": ""}, clear=False):
            with self.assertRaisesRegex(ValueError, "CLAWORLD_ENABLE_GENERIC_API"):
                claworld_tools._search(ClaworldConfig(server_url="https://api.example.com", app_token="tok"), {"endpoint": "/v1/search"})


class SessionRouterTests(unittest.TestCase):
    def test_maps_management_and_conversation_to_stable_buckets(self):
        cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok", account_id="acct", agent_id="agent-1")
        management = build_inbound_envelope(
            {
                "event": "delivery",
                "data": {
                    "deliveryId": "m1",
                    "sessionKey": "management:agent-1",
                    "targetAgentId": "agent-1",
                    "payload": {"text": "wake"},
                },
            }
        )
        conversation = build_inbound_envelope(
            {
                "event": "delivery",
                "data": {
                    "deliveryId": "c1",
                    "sessionKey": "conversation:remote-a",
                    "conversationKey": "remote-a",
                    "payload": {"text": "hello"},
                },
            }
        )
        m_route = route_envelope(management, cfg)
        c_route = route_envelope(conversation, cfg)
        self.assertEqual(m_route.session_kind, "management")
        self.assertEqual(c_route.session_kind, "conversation")
        self.assertTrue(build_hermes_session_key(m_route).startswith("agent:main:claworld:dm:management-"))
        self.assertTrue(build_hermes_session_key(c_route).startswith("agent:main:claworld:dm:conversation-"))

    def test_routes_payload_marked_management(self):
        cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok", account_id="acct", agent_id="agent-1")
        envelope = build_inbound_envelope(
            {
                "event": "notification",
                "data": {
                    "eventType": "platform_recommendation",
                    "sessionKey": "events:agent-1",
                    "payload": {"sessionKind": "management", "commandText": "review"},
                },
            }
        )
        route = route_envelope(envelope, cfg)
        self.assertEqual(route.session_kind, "management")


class WorkingMemoryTests(unittest.TestCase):
    def test_ensure_and_session_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claworld"
            result = ensure_working_memory(root)
            self.assertTrue((root / "INDEX.md").exists())
            self.assertTrue((root / "context" / "NOW.md").exists())
            self.assertIn("INDEX.md", result["created"])
            index = read_session_index(root)
            self.assertEqual(index["schema"], "claworld.sessions.v1")

    def test_records_conversation_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claworld"
            cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok")
            envelope = build_inbound_envelope(
                {
                    "event": "delivery",
                    "data": {
                        "deliveryId": "c1",
                        "sessionKey": "conversation:remote-a",
                        "conversationKey": "remote-a",
                        "payload": {"text": "hello"},
                    },
                }
            )
            route = route_envelope(envelope, cfg)
            record_claworld_route(root, route, build_hermes_session_key(route), envelope)
            index = read_session_index(root)
            self.assertIn(route.chat_id, index["conversationSessions"])
            context = build_prompt_context(root, platform="claworld", chat_id=route.chat_id)
            self.assertIn("Claworld Conversation Session", context)
            self.assertIn('skill_view("claworld:claworld-main-session")', context)
            self.assertIn("sessions/index.json summary", context)
            self.assertIn(route.chat_id, context)

    def test_prompt_context_prefers_plugin_qualified_claworld_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claworld"

            main = build_prompt_context(root, platform="feishu", chat_id="chat-main")
            management = build_prompt_context(root, platform="claworld", chat_id="management-abc")
            conversation = build_prompt_context(root, platform="claworld", chat_id="conversation-abc")

        for context in (main, management, conversation):
            self.assertIn("Canonical Claworld guidance lives in plugin-qualified skills", context)
            self.assertIn("local/user-authored Claworld notes", context)
        self.assertIn('skill_view("claworld:claworld-main-session")', main)
        self.assertIn('skill_view("claworld:claworld-help")', main)
        self.assertIn('skill_view("claworld:claworld-management-session")', management)
        self.assertIn('skill_view("claworld:claworld-main-session")', conversation)

    def test_post_tool_call_journals_successful_claworld_tools_with_redaction(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
            "claworld_hermes_plugin.hooks.ClaworldConfig.load",
            return_value=ClaworldConfig(server_url="https://api.example.com", working_memory_root=str(Path(tmp) / ".claworld")),
        ):
            claworld_hooks.post_tool_call(
                tool_name="claworld_search",
                args={"query": "builder", "appToken": "secret-token"},
                result=json.dumps({"status": "ok", "items": []}),
                task_id="task-1",
                duration_ms=12,
            )
            claworld_hooks.post_tool_call(
                tool_name="claworld_search",
                args={"query": "bad"},
                result=json.dumps({"status": "error", "message": "failed"}),
            )
            journal_files = sorted((Path(tmp) / ".claworld" / "journal").glob("*.md"))
            journal_text = journal_files[0].read_text(encoding="utf-8")

        self.assertIn('"kind": "tool_call"', journal_text)
        self.assertIn('"toolName": "claworld_search"', journal_text)
        self.assertIn('"appToken": "[redacted]"', journal_text)
        self.assertNotIn("secret-token", journal_text)
        self.assertNotIn('"query": "bad"', journal_text)


class ToolRoutingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok", account_id="acct", agent_id="agent-1")

    def test_conversation_request_uses_public_chat_requests_route(self):
        calls = []

        def fake_request(cfg, method, endpoint, body=None, query=None, timeout=None):
            calls.append({"method": method, "endpoint": endpoint, "body": body, "query": query, "timeout": timeout})
            return {"chatRequestId": "cr1"}

        with patch("claworld_hermes_plugin.tools.request_json", side_effect=fake_request):
            result = claworld_tools._manage_conversations(
                self.cfg,
                {
                    "action": "request",
                    "targetAgentId": "agent-peer",
                    "displayName": "Peer",
                    "agentCode": "ABC",
                    "kickoffBrief": {"text": "context for sender", "source": "direct_lookup"},
                    "openingMessage": "hi",
                    "openingPayload": {"text": "hi", "source": "test"},
                    "requestContext": {"followUp": {"sessionKey": "main:owner"}},
                    "source": "direct_lookup",
                    "worldId": "world-1",
                    "dedupeKey": "request-1",
                    "clientRequestId": "client-1",
                },
            )

        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["endpoint"], "/v1/chat-requests")
        self.assertEqual(calls[0]["body"]["fromAgentId"], "agent-1")
        self.assertEqual(calls[0]["body"]["targetAgentId"], "agent-peer")
        self.assertEqual(calls[0]["body"]["kickoffBrief"]["text"], "context for sender")
        self.assertEqual(calls[0]["body"]["openingPayload"]["source"], "test")
        self.assertEqual(calls[0]["body"]["requestContext"]["followUp"]["sessionKey"], "main:owner")
        self.assertEqual(calls[0]["body"]["source"], "direct_lookup")
        self.assertEqual(calls[0]["body"]["worldId"], "world-1")
        self.assertEqual(calls[0]["body"]["idempotencyKey"], "request-1")
        self.assertEqual(calls[0]["body"]["clientRequestId"], "client-1")
        self.assertEqual(result["action"], "request")

    def test_conversation_request_adds_hermes_followup_session_key(self):
        calls = []

        def fake_request(cfg, method, endpoint, body=None, query=None, timeout=None):
            calls.append({"method": method, "endpoint": endpoint, "body": body, "query": query, "timeout": timeout})
            return {"chatRequestId": "cr1"}

        with patch("claworld_hermes_plugin.tools.request_json", side_effect=fake_request), patch(
            "claworld_hermes_plugin.tools._current_hermes_session_context",
            return_value={"platform": "telegram", "sessionKey": "agent:main:telegram:dm:owner"},
        ), patch("claworld_hermes_plugin.tools.record_owner_route_from_context", return_value=None):
            result = claworld_tools._manage_conversations(
                self.cfg,
                {
                    "action": "request",
                    "displayName": "Peer",
                    "agentCode": "ABC",
                    "openingMessage": "hi",
                    "requestContext": {"origin": {"type": "manual"}},
                },
            )

        self.assertEqual(calls[0]["endpoint"], "/v1/chat-requests")
        self.assertEqual(calls[0]["body"]["requestContext"]["origin"]["type"], "manual")
        self.assertEqual(calls[0]["body"]["requestContext"]["followUp"]["sessionKey"], "agent:main:telegram:dm:owner")
        self.assertEqual(result["action"], "request")

    def test_report_owner_delivers_and_records_main_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claworld"
            cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok", working_memory_root=str(root))
            route = {"platform": "feishu", "chatId": "chat-1", "sessionId": "sid-main"}
            append_calls = []

            def fake_append(route_arg, report_text):
                append_calls.append((route_arg, report_text))
                return {"status": "appended", "sessionId": "sid-main", "role": "assistant"}

            with patch("claworld_hermes_plugin.tools.record_owner_route_from_context", return_value=route), patch(
                "claworld_hermes_plugin.tools._send_owner_route",
                return_value={"ok": True, "result": {"message_id": "m1"}},
            ), patch("claworld_hermes_plugin.tools._append_main_session_context", side_effect=fake_append):
                result = claworld_tools._report_owner(cfg, {"report_text": "Owner-visible Claworld report.", "deliver": True})

            self.assertEqual(append_calls, [(route, "Owner-visible Claworld report.")])
            self.assertEqual(result["delivery"]["ok"], True)
            self.assertEqual(result["mainContext"]["transcript"]["status"], "appended")
            self.assertEqual(set(result["mainContext"]), {"transcript"})
            self.assertNotIn("reportPath", result)
            self.assertEqual(list((root / "reports").glob("*.md")), [])
            now_text = (root / "context" / "NOW.md").read_text(encoding="utf-8")
            self.assertNotIn("Recent Owner Reports", now_text)
            self.assertNotIn("Owner-visible Claworld report.", now_text)
            journal_text = "\n".join(path.read_text(encoding="utf-8") for path in (root / "journal").glob("*.md"))
            self.assertIn('"kind": "owner_report"', journal_text)
            self.assertIn('"mainContext"', journal_text)
            self.assertIn('"status": "appended"', journal_text)

    def test_append_main_session_context_writes_to_session_db_and_dedupes(self):
        class FakeSessionDB:
            initial_messages = []
            instances = []

            def __init__(self):
                self.appended = []
                self.closed = False
                FakeSessionDB.instances.append(self)

            def get_session(self, session_id):
                return {"id": session_id} if session_id == "sid-main" else None

            def get_messages_as_conversation(self, session_id):
                self.seen_session_id = session_id
                return list(FakeSessionDB.initial_messages)

            def append_message(self, **kwargs):
                self.appended.append(kwargs)
                return 41

            def close(self):
                self.closed = True

        fake_module = types.SimpleNamespace(SessionDB=FakeSessionDB)
        with patch.dict(sys.modules, {"hermes_state": fake_module}):
            FakeSessionDB.initial_messages = []
            appended = claworld_tools._append_main_session_context({"sessionId": "sid-main"}, "report text")
            first = FakeSessionDB.instances[-1]

            FakeSessionDB.initial_messages = [{"role": "assistant", "content": "report text"}]
            deduped = claworld_tools._append_main_session_context({"sessionId": "sid-main"}, "report text")
            second = FakeSessionDB.instances[-1]

        self.assertEqual(appended["status"], "appended")
        self.assertEqual(first.appended[0]["session_id"], "sid-main")
        self.assertEqual(first.appended[0]["role"], "assistant")
        self.assertEqual(first.appended[0]["content"], "report text")
        self.assertTrue(first.closed)
        self.assertEqual(deduped["status"], "already_present")
        self.assertEqual(second.appended, [])
        self.assertTrue(second.closed)

    def test_world_broadcast_uses_world_broadcast_route(self):
        calls = []

        def fake_request(cfg, method, endpoint, body=None, query=None, timeout=None):
            calls.append({"method": method, "endpoint": endpoint, "body": body, "query": query, "timeout": timeout})
            return {"deliveryId": "d1"}

        with patch("claworld_hermes_plugin.tools.request_json", side_effect=fake_request):
            result = claworld_tools._manage_worlds(
                self.cfg,
                {"action": "publish_broadcast", "worldId": "w1", "announcementText": "hello members"},
            )

        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["endpoint"], "/v1/worlds/w1/broadcast")
        self.assertEqual(calls[0]["body"]["payload"]["text"], "hello members")
        self.assertEqual(result["action"], "publish_broadcast")

    def test_search_defaults_world_members_when_world_id_is_present(self):
        calls = []

        def fake_request(cfg, method, endpoint, body=None, query=None, timeout=None):
            calls.append({"method": method, "endpoint": endpoint, "body": body, "query": query, "timeout": timeout})
            return {"items": []}

        with patch("claworld_hermes_plugin.tools.request_json", side_effect=fake_request):
            result = claworld_tools._search(self.cfg, {"worldId": "w1", "query": "builder"})

        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["endpoint"], "/v1/search")
        self.assertEqual(calls[0]["body"]["scope"], "world_members")
        self.assertEqual(calls[0]["body"]["worldId"], "w1")
        self.assertEqual(result["tool"], "claworld_search")

    def test_get_public_profile_agent_id_is_target_alias_not_viewer(self):
        calls = []

        def fake_request(cfg, method, endpoint, body=None, query=None, timeout=None):
            calls.append({"method": method, "endpoint": endpoint, "body": body, "query": query, "timeout": timeout})
            return {"agentId": "agent-peer"}

        with patch("claworld_hermes_plugin.tools.request_json", side_effect=fake_request):
            result = claworld_tools._get_public_profile(self.cfg, {"action": "get_profile", "agentId": "agent-peer"})

        self.assertEqual(calls[0]["method"], "GET")
        self.assertEqual(calls[0]["endpoint"], "/v1/public-profiles/agent-peer")
        self.assertEqual(calls[0]["query"]["viewerAgentId"], "agent-1")
        self.assertEqual(result["action"], "get_profile")

    def test_account_view_adds_hermes_binding_diagnostics(self):
        calls = []

        def fake_request(cfg, method, endpoint, body=None, query=None, timeout=None):
            calls.append({"method": method, "endpoint": endpoint, "body": body, "query": query, "timeout": timeout})
            return {
                "status": "pending",
                "readiness": "account_profile_incomplete",
                "accountProfile": {"ready": False},
                "diagnostics": {"publicIdentityReady": True},
            }

        with patch("claworld_hermes_plugin.tools.request_json", side_effect=fake_request):
            result = claworld_tools._manage_account(self.cfg, {"action": "view_account"})

        self.assertEqual(calls[0]["method"], "GET")
        self.assertEqual(calls[0]["endpoint"], "/v1/account")
        self.assertEqual(calls[0]["query"]["agentId"], "agent-1")
        self.assertEqual(result["diagnostics"]["bindingReady"], True)
        self.assertEqual(result["diagnostics"]["bindingStatus"], "bound")
        self.assertEqual(result["diagnostics"]["accountProfileReady"], False)
        self.assertEqual(result["relay"]["agentId"], "agent-1")
        self.assertEqual(result["relay"]["bindingStatus"], "bound")
        self.assertEqual(result["activation"]["status"], "ready")

    def test_activate_account_bootstraps_token_updates_identity_and_persists_env(self):
        calls = []

        def fake_request(cfg, method, endpoint, body=None, query=None, timeout=None):
            calls.append(
                {
                    "cfg": cfg,
                    "method": method,
                    "endpoint": endpoint,
                    "body": body,
                    "query": query,
                    "timeout": timeout,
                }
            )
            if endpoint == "/v1/onboarding/activate":
                self.assertFalse(cfg.app_token)
                return {
                    "status": "activated",
                    "created": True,
                    "bindingSource": "created_agent_app_token",
                    "agentId": "agent-new",
                    "appToken": "token-new",
                }
            if endpoint == "/v1/account":
                self.assertEqual(cfg.app_token, "token-new")
                self.assertEqual(cfg.agent_id, "agent-new")
                return {
                    "ready": True,
                    "readiness": "paired_and_ready",
                    "accountProfile": {"ready": True, "profile": "ready profile"},
                }
            raise AssertionError(f"unexpected endpoint {endpoint}")

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True), patch(
            "claworld_hermes_plugin.tools.hermes_home_path",
            return_value=Path(tmp),
        ), patch("claworld_hermes_plugin.tools.request_json", side_effect=fake_request):
            result = claworld_tools._manage_account(
                ClaworldConfig(server_url="https://api.example.com", account_id="acct"),
                {"action": "activate_account", "displayName": "Hermes Agent"},
            )
            env_text = (Path(tmp) / ".env").read_text(encoding="utf-8")

        self.assertEqual([call["endpoint"] for call in calls], ["/v1/onboarding/activate", "/v1/account"])
        self.assertEqual(calls[1]["body"]["action"], "update_identity")
        self.assertEqual(calls[1]["body"]["agentId"], "agent-new")
        self.assertEqual(result["action"], "activate_account")
        self.assertEqual(result["runtimeActivation"]["status"], "activated")
        self.assertEqual(result["runtimeActivation"]["agentId"], "agent-new")
        self.assertEqual(result["credentialPersistence"]["status"], "saved_to_hermes_env")
        self.assertIn("CLAWORLD_APP_TOKEN=token-new", env_text)
        self.assertIn("CLAWORLD_AGENT_ID=agent-new", env_text)
        self.assertNotIn("token-new", json.dumps(result, sort_keys=True))

    def test_tool_result_exposes_backend_remediation_fields(self):
        def failing_tool(cfg, args):
            raise ClaworldHttpError(
                409,
                {
                    "error": "account_profile_incomplete",
                    "message": "profile required",
                    "requiredAction": "update_agent_profile",
                    "nextAction": "update_agent_profile",
                    "nextTool": "claworld_manage_account",
                    "missingFields": [{"fieldId": "profile"}],
                },
            )

        payload = json.loads(claworld_tools._tool_result("claworld_manage_worlds", {"action": "join_world"}, failing_tool))
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["tool"], "claworld_manage_worlds")
        self.assertEqual(payload["action"], "join_world")
        self.assertEqual(payload["backendCode"], "account_profile_incomplete")
        self.assertEqual(payload["nextTool"], "claworld_manage_account")
        self.assertEqual(payload["nextAction"], "update_agent_profile")

    def test_get_state_accepts_top_level_conversation_target(self):
        calls = []

        def fake_request(cfg, method, endpoint, body=None, query=None, timeout=None):
            calls.append({"method": method, "endpoint": endpoint, "body": body, "query": query, "timeout": timeout})
            return {"items": []}

        with patch("claworld_hermes_plugin.tools.request_json", side_effect=fake_request):
            result = claworld_tools._manage_conversations(
                self.cfg,
                {"action": "get_state", "conversationKey": "pair:a::b"},
            )

        self.assertEqual(calls[0]["query"]["conversationKey"], "pair:a::b")
        self.assertEqual(result["action"], "get_state")

    def test_list_related_rejects_request_and_top_level_filter_fields(self):
        with self.assertRaisesRegex(ValueError, "displayName is only supported"):
            claworld_tools._manage_conversations(self.cfg, {"action": "list_related", "displayName": "Peer"})
        with self.assertRaisesRegex(ValueError, "worldId must be passed as filters.worldId"):
            claworld_tools._manage_conversations(self.cfg, {"action": "list_related", "worldId": "w1"})
        with self.assertRaisesRegex(ValueError, "filters.extra is not supported"):
            claworld_tools._manage_conversations(self.cfg, {"action": "list_related", "filters": {"extra": "x"}})


class RelayClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_dispatch_does_not_block_ack_processing(self):
        import asyncio

        started = asyncio.Event()
        finish = asyncio.Event()

        async def on_delivery(envelope):
            started.set()
            await finish.wait()

        cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok", agent_id="agent-1")
        client = RelayClient(cfg, on_delivery=on_delivery)
        future = client._register_ack_waiter(("command.accepted",), "d1")
        await client._handle_raw_message(
            json.dumps(
                {
                    "event": "delivery",
                    "data": {
                        "deliveryId": "d1",
                        "sessionKey": "conversation:abc",
                        "payload": {"text": "hello"},
                    },
                }
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        await client._handle_raw_message(
            json.dumps(
                {
                    "event": "command.accepted",
                    "data": {
                        "command": {
                            "name": "delivery.reply.requested",
                            "partitionKey": "d1",
                        }
                    },
                }
            )
        )

        self.assertTrue(future.done())
        finish.set()
        await asyncio.gather(*list(client._delivery_tasks), return_exceptions=True)

    async def test_resolves_command_accepted_ack_by_command_delivery_id(self):
        cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok", agent_id="agent-1")
        client = RelayClient(cfg, on_delivery=lambda envelope: None)
        future = client._register_ack_waiter(("command.accepted",), "d1")
        client._resolve_ack_waiters(
            "command.accepted",
            {
                "event": "command.accepted",
                "data": {
                    "command": {
                        "name": "delivery.reply.requested",
                        "aggregateId": "d1",
                    }
                },
            },
        )
        self.assertTrue(future.done())

    async def test_resolves_command_accepted_ack_by_session_key_alias(self):
        cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok", agent_id="agent-1")
        client = RelayClient(cfg, on_delivery=lambda envelope: None)
        future = client._register_ack_waiter(("command.accepted",), ("d1", "conversation:abc"))
        client._resolve_ack_waiters(
            "command.accepted",
            {
                "event": "command.accepted",
                "data": {
                    "command": {
                        "name": "delivery.accepted.requested",
                        "partitionKey": "conversation:abc",
                    }
                },
            },
        )
        self.assertTrue(future.done())

    async def test_resolves_kept_silent_command_accepted_ack_by_partition_key(self):
        cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok", agent_id="agent-1")
        client = RelayClient(cfg, on_delivery=lambda envelope: None)
        future = client._register_ack_waiter(("command.accepted",), "d1")
        client._resolve_ack_waiters(
            "command.accepted",
            {
                "event": "command.accepted",
                "data": {
                    "command": {
                        "name": "delivery.kept_silent.requested",
                        "partitionKey": "d1",
                    }
                },
            },
        )
        self.assertTrue(future.done())

    async def test_accepted_and_kept_silent_wait_for_command_accepted_ack(self):
        cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok", agent_id="agent-1")
        client = RelayClient(cfg, on_delivery=lambda envelope: None)
        calls = []

        async def fake_send_with_ack(payload, *, ack_events, delivery_id, fallback):
            calls.append((payload["type"], ack_events, delivery_id))

        client._send_with_ack = fake_send_with_ack
        await client.send_accepted("d1", "conversation:abc")
        await client.send_kept_silent("d2", "conversation:def", "no_renderable_reply")

        self.assertIn("command.accepted", calls[0][1])
        self.assertIn("command.accepted", calls[1][1])

    def test_delivery_visibility_retry_retries_404_delivery_not_found(self):
        calls = []
        cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok")

        def fake_request(cfg_arg, method, path, **kwargs):
            calls.append((method, path, kwargs))
            if len(calls) == 1:
                raise ClaworldHttpError(404, {"error": "delivery_not_found"})
            return {"ok": True}

        with patch("claworld_hermes_plugin.relay_client.request_json", side_effect=fake_request), patch("claworld_hermes_plugin.relay_client.time.sleep"):
            result = claworld_relay._request_json_with_delivery_visibility_retry(cfg, "POST", "/v1/runtime-deliveries/d1/reply")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 2)


class AdapterCompletionTests(unittest.TestCase):
    def test_adapter_exposes_basic_chat_info(self):
        adapter = import_adapter_with_gateway_shim()
        instance = adapter.ClaworldPlatformAdapter(types.SimpleNamespace(extra={}))

        async def run():
            return await instance.get_chat_info("conversation:test")

        self.assertEqual(
            self._run_async(run()),
            {"name": "conversation:test", "type": "dm", "chat_id": "conversation:test"},
        )

    def _run_async(self, coro):
        import asyncio

        return asyncio.run(coro)

    def test_completion_silence_reason_matches_claworld_delivery_semantics(self):
        adapter = import_adapter_with_gateway_shim()
        record = adapter.DeliveryRecord(
            delivery_id="d1",
            relay_session_key="conversation:abc",
            chat_id="conversation-1",
            replyable=True,
        )
        non_replyable = adapter.DeliveryRecord(
            delivery_id="d2",
            relay_session_key="conversation:def",
            chat_id="conversation-2",
            replyable=False,
        )

        self.assertEqual(adapter._completion_silence_reason(adapter.ProcessingOutcome.SUCCESS, record), "no_renderable_reply")
        self.assertEqual(adapter._completion_silence_reason(adapter.ProcessingOutcome.SUCCESS, non_replyable), "non_replyable_delivery")
        self.assertEqual(adapter._completion_silence_reason(adapter.ProcessingOutcome.FAILURE, record), "runtime_failed_before_reply")

    def test_delivery_metadata_controls_reply_and_acceptance(self):
        adapter = import_adapter_with_gateway_shim()
        non_replyable = types.SimpleNamespace(event_type="delivery", metadata={"allowReply": False}, payload={})
        no_acceptance = types.SimpleNamespace(event_type="delivery", metadata={"acceptanceRequired": False}, payload={})
        normal = types.SimpleNamespace(event_type="delivery", metadata={}, payload={})

        self.assertFalse(adapter._is_replyable_delivery(non_replyable))
        self.assertFalse(adapter._requires_acceptance_delivery(no_acceptance))
        self.assertTrue(adapter._is_replyable_delivery(normal))
        self.assertTrue(adapter._requires_acceptance_delivery(normal))

    def test_no_reply_token_is_exact(self):
        adapter = import_adapter_with_gateway_shim()

        self.assertTrue(adapter._is_no_reply("NO_REPLY"))
        self.assertFalse(adapter._is_no_reply("kept_silent"))
        self.assertFalse(adapter._is_no_reply("NO_REPLY please"))


class HttpClientTests(unittest.TestCase):
    def test_auth_headers_and_url(self):
        cfg = ClaworldConfig(server_url="https://api.example.com", api_key="api", app_token="tok")
        headers = auth_headers(cfg)
        self.assertIn("claworld-hermes-plugin/0.1.0", headers["User-Agent"])
        self.assertEqual(headers["authorization"], "Bearer tok")
        self.assertEqual(headers["x-claworld-app-token"], "tok")
        self.assertEqual(headers["x-api-key"], "api")
        self.assertEqual(build_url(cfg, "/v1/search", query={"q": "x", "empty": ""}), "https://api.example.com/v1/search?q=x")

    def test_default_http_transport_ignores_process_proxy_env(self):
        with patch.dict(
            os.environ,
            {"HTTPS_PROXY": "http://127.0.0.1:7897", "ALL_PROXY": "socks5://127.0.0.1:7897"},
            clear=False,
        ):
            handler = claworld_http._proxy_handler(ClaworldConfig(server_url="https://api.example.com"))

        self.assertEqual(handler.proxies, {})

    def test_http_transport_can_opt_into_env_proxy(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7897"}, clear=True):
            handler = claworld_http._proxy_handler(ClaworldConfig(server_url="https://api.example.com", use_env_proxy=True))

        self.assertEqual(handler.proxies.get("https"), "http://127.0.0.1:7897")

    def test_explicit_http_proxy_overrides_env_proxy(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://env-proxy.example"}, clear=True):
            handler = claworld_http._proxy_handler(
                ClaworldConfig(server_url="https://api.example.com", http_proxy="http://configured-proxy.example", use_env_proxy=True)
            )

        self.assertEqual(handler.proxies, {"http": "http://configured-proxy.example", "https": "http://configured-proxy.example"})

    def test_request_json_retries_transient_transport_error_with_fresh_request(self):
        calls = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"ok": true}'

        class FakeOpener:
            def open(self, request, timeout=30.0):
                calls.append((request, timeout))
                if len(calls) == 1:
                    raise claworld_http.urllib.error.URLError("tls eof")
                return FakeResponse()

        cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok", http_retries=1)
        with patch("claworld_hermes_plugin.http_client._build_opener", return_value=FakeOpener()), patch(
            "claworld_hermes_plugin.http_client.time.sleep"
        ) as sleep:
            result = request_json(cfg, "GET", "/v1/chat-requests", query={"agentId": "agent-1"})

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0].full_url, "https://api.example.com/v1/chat-requests?agentId=agent-1")
        self.assertEqual(calls[1][0].full_url, "https://api.example.com/v1/chat-requests?agentId=agent-1")
        sleep.assert_called_once()

    def test_request_json_does_not_retry_http_status_errors(self):
        calls = []

        class FakeOpener:
            def open(self, request, timeout=30.0):
                calls.append(request)
                raise claworld_http.urllib.error.HTTPError(
                    request.full_url,
                    401,
                    "Unauthorized",
                    {},
                    io.BytesIO(b'{"error":"unauthorized"}'),
                )

        cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok", http_retries=2)
        with patch("claworld_hermes_plugin.http_client._build_opener", return_value=FakeOpener()):
            with self.assertRaises(ClaworldHttpError) as ctx:
                request_json(cfg, "GET", "/v1/chat-requests")

        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(len(calls), 1)


class ConfigTests(unittest.TestCase):
    def test_load_uses_default_server_url_when_not_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HERMES_HOME": tmp}, clear=True):
                cfg = ClaworldConfig.load()

        self.assertEqual(cfg.server_url, DEFAULT_CLAWORLD_SERVER_URL)

    def test_expands_env_placeholders_in_extra_config(self):
        with patch.dict(os.environ, {"CLAWORLD_TEST_TOKEN": "expanded-token"}, clear=False):
            cfg = ClaworldConfig.from_extra({"server_url": "https://api.example.com", "app_token": "${CLAWORLD_TEST_TOKEN}"})
        self.assertEqual(cfg.app_token, "expanded-token")

    def test_loads_http_transport_options_from_extra_and_env(self):
        file_cfg = ClaworldConfig.from_extra({"proxy_url": "http://file-proxy.example", "use_env_proxy": True, "http_retries": 5})
        self.assertEqual(file_cfg.http_proxy, "http://file-proxy.example")
        self.assertTrue(file_cfg.use_env_proxy)
        self.assertEqual(file_cfg.http_retries, 5)

        with patch.dict(
            os.environ,
            {
                "CLAWORLD_HTTP_PROXY": "http://env-proxy.example",
                "CLAWORLD_USE_ENV_PROXY": "1",
                "CLAWORLD_HTTP_RETRIES": "0",
            },
            clear=True,
        ):
            env_cfg = ClaworldConfig.from_env()

        self.assertEqual(env_cfg.http_proxy, "http://env-proxy.example")
        self.assertTrue(env_cfg.use_env_proxy)
        self.assertEqual(env_cfg.http_retries, 0)

    def test_loads_claworld_extra_from_hermes_config_when_yaml_is_available(self):
        try:
            import yaml  # noqa: F401
        except Exception:
            self.skipTest("PyYAML is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.yaml").write_text(
                """
gateway:
  platforms:
    claworld:
      extra:
        server_url: "https://api.example.com"
        app_token: "file-token"
        api_key: "file-api"
        account_id: "acct-file"
        agent_id: "agent-file"
        working_memory_root: "/tmp/claworld-memory"
""".lstrip(),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "HERMES_HOME": str(home),
                    "CLAWORLD_APP_TOKEN": "env-token",
                },
                clear=True,
            ):
                cfg = ClaworldConfig.load()

        self.assertEqual(cfg.server_url, "https://api.example.com")
        self.assertEqual(cfg.app_token, "env-token")
        self.assertEqual(cfg.api_key, "file-api")
        self.assertEqual(cfg.account_id, "acct-file")
        self.assertEqual(cfg.agent_id, "agent-file")
        self.assertEqual(cfg.working_memory_root, "/tmp/claworld-memory")


if __name__ == "__main__":
    unittest.main()
