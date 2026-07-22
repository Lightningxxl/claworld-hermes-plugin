from __future__ import annotations

import importlib
import importlib.util
import asyncio
import inspect
import io
import json
import math
import os
import struct
import sys
import tempfile
import types
import unittest
import xml.etree.ElementTree as ET
import zlib
from enum import Enum
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "claworld_hermes_plugin"
pkg = types.ModuleType(PACKAGE)
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault(PACKAGE, pkg)


def decode_resvg_rgba_png(path: Path) -> tuple[int, int, list[bytes]]:
    """Decode the non-interlaced RGBA8 PNG shape emitted by pinned resvg."""

    payload = path.read_bytes()
    if payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")

    width = height = 0
    compressed = []
    offset = 8
    while offset < len(payload):
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        chunk_type = payload[offset + 4 : offset + 8]
        chunk = payload[offset + 8 : offset + 8 + length]
        offset += length + 12
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
            if (bit_depth, color_type, compression, filtering, interlace) != (8, 6, 0, 0, 0):
                raise ValueError("expected a non-interlaced RGBA8 resvg PNG")
        elif chunk_type == b"IDAT":
            compressed.append(chunk)
        elif chunk_type == b"IEND":
            break

    stride = width * 4
    raw = zlib.decompress(b"".join(compressed))
    if len(raw) != height * (stride + 1):
        raise ValueError("unexpected PNG scanline length")

    rows: list[bytes] = []
    prior = bytes(stride)
    offset = 0
    for _row in range(height):
        filter_type = raw[offset]
        encoded = raw[offset + 1 : offset + stride + 1]
        offset += stride + 1
        decoded = bytearray(stride)
        for idx, value in enumerate(encoded):
            left = decoded[idx - 4] if idx >= 4 else 0
            above = prior[idx]
            upper_left = prior[idx - 4] if idx >= 4 else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                estimate = left + above - upper_left
                distances = (
                    abs(estimate - left),
                    abs(estimate - above),
                    abs(estimate - upper_left),
                )
                predictor = (left, above, upper_left)[distances.index(min(distances))]
            else:
                raise ValueError(f"unsupported PNG filter {filter_type}")
            decoded[idx] = (value + predictor) & 0xFF
        prior = bytes(decoded)
        rows.append(prior)
    return width, height, rows


def rendered_text_pixel_regions(
    svg_root: ET.Element,
    width: int,
    height: int,
    rows: list[bytes],
    target: str,
) -> list[list[tuple[int, int, int, int]]]:
    """Return tight PNG pixel regions for every occurrence of an SVG text target."""

    regions: list[list[tuple[int, int, int, int]]] = []
    for node in svg_root.iter("{http://www.w3.org/2000/svg}text"):
        value = node.text or ""
        search_from = 0
        while True:
            index = value.find(target, search_from)
            if index < 0:
                break
            font_size = float(node.attrib["font-size"])
            prefix = value[:index]
            glyph_x = float(node.attrib["x"]) + claworld_stylekit.text_units(prefix) * font_size
            baseline_y = float(node.attrib["y"])
            left = max(0, int(glyph_x - 2))
            right = min(width, int(glyph_x + font_size * 1.4) + 1)
            top = max(0, int(baseline_y - font_size * 1.25))
            bottom = min(height, int(baseline_y + font_size * 0.3) + 1)
            regions.append(
                [
                    tuple(rows[y][x * 4 : x * 4 + 4])
                    for y in range(top, bottom)
                    for x in range(left, right)
                ]
            )
            search_from = index + len(target)
    return regions

from claworld_hermes_plugin import relay_client as claworld_relay
from claworld_hermes_plugin import hooks as claworld_hooks
from claworld_hermes_plugin import http_client as claworld_http
from claworld_hermes_plugin import setup as claworld_setup
from claworld_hermes_plugin.config import DEFAULT_CLAWORLD_SERVER_URL, ClaworldConfig
from claworld_hermes_plugin.http_client import ClaworldHttpError, auth_headers, build_url, request_json
from claworld_hermes_plugin import skill_registration as claworld_skills
from claworld_hermes_plugin import tools as claworld_tools
from claworld_hermes_plugin import transcript_report as claworld_transcript
from claworld_hermes_plugin import transcript_report_stylekit as claworld_stylekit
from claworld_hermes_plugin.transcript_report_styles import comic_grid as claworld_comic_grid
from claworld_hermes_plugin.protocol import (
    auth_message,
    build_agent_guidance,
    build_agent_text,
    build_inbound_envelope,
    classify_reply_content,
    normalize_http_base_url,
    normalize_ws_url,
    reply_message,
)
from claworld_hermes_plugin.relay_client import RelayClient
from claworld_hermes_plugin.session_router import build_hermes_session_key, route_envelope
from claworld_hermes_plugin.version import PLUGIN_VERSION
from claworld_hermes_plugin.working_memory import (
    build_prompt_context,
    claim_inbound_notification,
    complete_inbound_notification,
    ensure_working_memory,
    read_session_index,
    record_claworld_route,
    record_chat_request_direction,
    record_outbound_reply,
    release_inbound_notification,
    write_session_index,
)


def import_adapter_with_gateway_shim():
    if "gateway.platforms.base" not in sys.modules:
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
        payload = auth_message("agent-1", "token-1", "client-1", client="hermes-plugin")
        self.assertEqual(payload["type"], "auth")
        self.assertEqual(payload["agentId"], "agent-1")
        self.assertEqual(payload["credential"], {"type": "agent_token", "token": "token-1"})
        self.assertEqual(payload["client"], "hermes-plugin")
        self.assertEqual(payload["clientVersion"], "client-1")

    def test_classifies_session_reset_banner_as_operational_notice(self):
        classification = classify_reply_content(
            "\u25d0 Session automatically reset (inactive for 24h). Conversation history cleared.\n"
            "Use /resume to browse and restore a previous session."
        )

        self.assertEqual(classification.silence_reason, "operational_notice_only")
        self.assertEqual(classification.text, "")

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
        text = build_agent_text(envelope)
        self.assertEqual(text, "/reset all sessions")

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
        self.assertEqual(envelope.metadata["notificationId"], "n1")
        text = build_agent_text(envelope)
        self.assertEqual(text, "You were invited.")
        self.assertNotIn("event_name=", text)
        self.assertNotIn("created_at=", text)

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

    def test_agent_text_deduplicates_visible_text_when_context_present(self):
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
        text = build_agent_text(envelope)
        self.assertNotIn("Backend-authored", text)
        self.assertIn("Decide whether to continue the chat.", text)
        self.assertIn("Backend says this is a warm intro.", text)
        self.assertNotIn("Peer profile summary.", text)
        self.assertNotIn("Peer-visible", text)
        self.assertNotIn("hello from peer", text)
        self.assertNotIn("Claworld live conversation rules", text)
        self.assertNotIn("[[request_conversation_end]]", text)
        self.assertNotIn("NO_REPLY", text)

    def test_formal_end_metadata_becomes_natural_system_guidance(self):
        envelope = build_inbound_envelope(
            {
                "event": "delivery",
                "data": {
                    "deliveryId": "dlv-internal-1",
                    "sessionKey": "conversation:pair-internal",
                    "payload": {
                        "commandText": "谢谢你分享这些经验，我们下次再聊。",
                        "untrustedContext": [
                            "conversation formally ended after mutual agreement",
                            "conversationKey=pair:agent-a::agent-b",
                            "deliveryId=dlv-internal-1",
                        ],
                    },
                },
            }
        )

        self.assertEqual(build_agent_text(envelope), "谢谢你分享这些经验，我们下次再聊。")
        guidance = build_agent_guidance(envelope)
        self.assertIn("formally ended", guidance)
        self.assertIn("return exactly `NO_REPLY`", guidance)
        self.assertIn("new kickoff", guidance)
        self.assertNotIn("pair:agent-a::agent-b", guidance)
        self.assertNotIn("dlv-internal-1", guidance)

    def test_agent_text_prefers_command_text_when_no_context(self):
        envelope = build_inbound_envelope(
            {
                "event": "delivery",
                "data": {
                    "deliveryId": "d3",
                    "sessionKey": "conversation:abc",
                    "payload": {
                        "commandText": "Reply to the peer.",
                        "text": "hello from peer",
                    },
                },
            }
        )
        text = build_agent_text(envelope)
        self.assertEqual(text, "Reply to the peer.")
        self.assertNotIn("Backend-authored", text)
        self.assertNotIn("Peer-visible", text)
        self.assertNotIn("hello from peer", text)
        self.assertNotIn("Claworld live conversation rules", text)

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
        text = build_agent_text(envelope)
        self.assertIn("Payload context only.", text)
        self.assertNotIn("Review the top-level relay note.", text)
        self.assertNotIn("Claworld live conversation rules", text)
        self.assertNotIn("[[request_conversation_end]]", text)
        self.assertNotIn("[[like]]", text)
        self.assertNotIn("peer-facing output", text)

    def test_reply_message_uses_claworld_text_payload(self):
        message = reply_message("d1", "conversation:abc", "hello")
        self.assertEqual(message["payload"]["text"], "hello")
        self.assertEqual(message["payload"]["source"], "hermes_agent")
        self.assertNotIn("replyText", message["payload"])

    def test_relay_scope_is_canonicalized_across_supported_envelope_layers(self):
        envelope = build_inbound_envelope(
            {
                "event": "delivery",
                "metadata": {"chat_request_id": "req-layered"},
                "data": {
                    "deliveryId": "d-layered",
                    "sessionKey": "conversation:layered",
                    "meta": {"world_id": "wld-layered"},
                    "notification": {
                        "metadata": {"target_agent_id": "agt-local"},
                        "relatedObjects": {"conversation_key": "pair:a::b:world:wld-layered"},
                    },
                    "payload": {"commandText": "hello", "metadata": {}},
                },
            }
        )

        self.assertEqual(envelope.chat_request_id, "req-layered")
        self.assertEqual(envelope.world_id, "wld-layered")
        self.assertEqual(envelope.target_agent_id, "agt-local")
        self.assertEqual(envelope.conversation_key, "pair:a::b:world:wld-layered")
        self.assertEqual(envelope.payload["chatRequestId"], "req-layered")
        self.assertEqual(envelope.payload["worldId"], "wld-layered")

    def test_relay_scope_conflicts_are_rejected_instead_of_first_value_winning(self):
        with self.assertRaisesRegex(ValueError, "conflicting relay scope field chatRequestId"):
            build_inbound_envelope(
                {
                    "event": "delivery",
                    "chatRequestId": "req-outer",
                    "data": {
                        "deliveryId": "d-conflict",
                        "sessionKey": "conversation:conflict",
                        "payload": {"chatRequestId": "req-inner", "commandText": "hello"},
                    },
                }
            )


class TranscriptReportTests(unittest.TestCase):
    def test_stored_renderer_contract_is_flat(self):
        properties = claworld_tools.TRANSCRIPT_REPORT_SCHEMA["parameters"]["properties"]
        self.assertIn("chatRequestId", properties)
        self.assertNotIn("stored", properties)
        self.assertIn(
            '{"mode":"stored","chatRequestId":"req_...","topic":"..."}',
            claworld_tools.TRANSCRIPT_REPORT_DESCRIPTION,
        )

        with self.assertRaisesRegex(
            ValueError,
            r"unsupported transcript render parameter\(s\): stored",
        ):
            claworld_transcript._normalize_render_request(
                {"mode": "stored", "stored": {"chatRequestId": "req-legacy"}}
            )

    def test_transcript_report_schema_requires_topic_for_new_agents_with_legacy_compatibility(self):
        schema = claworld_tools.TRANSCRIPT_REPORT_SCHEMA["parameters"]
        properties = schema["properties"]
        manual = properties["manual"]
        manual_message = manual["properties"]["messages"]["items"]

        self.assertEqual(schema["required"], ["mode"])
        self.assertNotIn("stored", properties)
        self.assertIn("chatRequestId", properties)
        self.assertEqual(manual["required"], ["messages"])
        self.assertEqual(manual_message["required"], ["from", "text"])
        self.assertEqual(
            properties["chatMode"]["enum"],
            ["direct", "world"],
        )
        self.assertEqual(
            manual["properties"]["reportType"]["enum"],
            ["full", "excerpt"],
        )
        for key in (
            "chatMode",
            "worldName",
            "initiatedBy",
            "topic",
            "localIdentity",
            "peerIdentity",
            "localLabel",
            "peerLabel",
            "peerProfile",
            "worldContext",
        ):
            self.assertIn(key, properties)
            self.assertIn(key, manual["properties"])
        self.assertEqual(properties["topic"]["minLength"], 1)
        self.assertEqual(manual["properties"]["topic"]["minLength"], 1)
        self.assertIn("Required for every new Agent call", properties["topic"]["description"])
        self.assertIn("Required for every new Agent call", manual["properties"]["topic"]["description"])
        self.assertIn("what was actually discussed in this conversation", properties["topic"]["description"])
        self.assertIn("anything unrelated to the content", properties["topic"]["description"])
        self.assertIn("what was actually discussed in this conversation", manual["properties"]["topic"]["description"])
        self.assertIn("must be provided in both stored and manual mode", claworld_tools.TRANSCRIPT_REPORT_DESCRIPTION)
        self.assertEqual(properties["initiatedBy"]["enum"], ["local", "peer"])
        self.assertEqual(manual["properties"]["initiatedBy"]["enum"], ["local", "peer"])
        self.assertNotIn("initiatedBy", schema["required"])
        self.assertNotIn("initiatedBy", manual["required"])
        self.assertNotIn("reportType", properties)
        self.assertIn("reportType", manual["properties"])

        with self.assertRaisesRegex(ValueError, "chatRequestId is required"):
            claworld_transcript._normalize_render_request({"mode": "stored"})
        legacy = claworld_transcript._normalize_render_request(
            {"mode": "stored", "chatRequestId": "req-legacy"}
        )
        self.assertIsNone(legacy["renderArgs"].get("topic"))

    def test_manual_world_agent_fields_build_complete_passport_header(self):
        normalized = claworld_transcript._normalize_render_request(
            {
                "mode": "manual",
                "manual": {
                    "messages": [
                        {"from": "peer", "text": "我们继续推进这个实验。"},
                        {"from": "local", "text": "好，先确认下一步。"},
                    ],
                    "topic": "推进下一步产品实验",
                    "title": "Legacy title must lose",
                    "chatMode": "world",
                    "worldName": "夜航工坊",
                    "initiatedBy": "peer",
                    "reportType": "excerpt",
                    "localIdentity": "米拉#LOCAL01",
                    "peerIdentity": "墨砚#Z99TMV",
                    "peerProfile": "连接互补的开发者。",
                    "worldContext": "一个分享产品实验的社区。",
                },
            }
        )
        render_args = normalized["renderArgs"]
        messages = claworld_transcript._normalize_messages(
            normalized["messages"],
            ClaworldConfig(agent_id="agt-local", working_memory_root="/tmp/not-used"),
            render_args,
            {},
        )
        self.assertEqual([message.participant_label for message in messages], ["墨砚#Z99TMV", "米拉#LOCAL01"])
        title, _subtitle = claworld_transcript._header_text(render_args, messages, {})
        header = claworld_transcript._transcript_header(
            render_args,
            messages,
            {},
            title=title,
        )

        self.assertEqual(title, "推进下一步产品实验")
        self.assertEqual(header.topic, "推进下一步产品实验")
        self.assertEqual(header.chat_mode, "world")
        self.assertEqual(header.world_name, "夜航工坊")
        self.assertEqual(header.initiated_by, "peer")
        self.assertEqual(header.report_type, "excerpt")
        self.assertEqual(header.local_identity, "米拉#LOCAL01")
        self.assertEqual(header.peer_identity, "墨砚#Z99TMV")
        self.assertEqual(
            [block.as_dict() for block in header.context_blocks],
            [
                {
                    "kind": "peerWorldMembershipProfile",
                    "label": "Peer · World",
                    "text": "连接互补的开发者。",
                    "source": "explicit",
                },
                {
                    "kind": "worldContext",
                    "label": "World Context",
                    "text": "一个分享产品实验的社区。",
                    "source": "explicit",
                },
            ],
        )

        fallback_title, _subtitle = claworld_transcript._header_text(
            {"mode": "manual", "worldName": "夜航工坊"},
            [],
            {},
        )
        self.assertEqual(fallback_title, "夜航工坊")

    def test_kickoff_context_parser_keeps_complete_peer_and_world_context(self):
        direct_text = "\n".join(
            [
                "# Background",
                "## Conversation Facts",
                "- Mode: `direct`",
                "## Participant Facts",
                "## You",
                "- Identity: `Mira#LOCAL01`",
                "## Peer",
                "- Identity: `Rin#PEER01`",
                "### Global Profile",
                "```text",
                "Peer global profile from kickoff.",
                "```",
                "### Human Profile",
                "```text",
                "Peer human profile from kickoff.",
                "```",
            ]
        )
        direct_context = claworld_transcript._extract_transcript_header_context(
            [
                {
                    "deliveryType": "kickoff",
                    "commandText": direct_text,
                    "untrustedContext": "A short untrusted summary must not replace the named kickoff profile.",
                }
            ]
        )
        direct_header = claworld_transcript._transcript_header(
            {"mode": "stored"},
            [],
            direct_context,
            title="Direct chat",
        )

        self.assertEqual(direct_context["peerGlobalProfile"], "Peer global profile from kickoff.")
        self.assertEqual(direct_context["peerHumanProfile"], "Peer human profile from kickoff.")
        self.assertNotIn("worldContext", direct_context)
        self.assertEqual(
            [block.as_dict() for block in direct_header.context_blocks],
            [
                {
                    "kind": "peerGlobalProfile",
                    "label": "Agent Profile",
                    "text": "Peer global profile from kickoff.",
                    "source": "rawKickoffText",
                },
                {
                    "kind": "peerHumanProfile",
                    "label": "Human Profile",
                    "text": "Peer human profile from kickoff.",
                    "source": "rawKickoffText",
                },
            ],
        )

        world_text = "\n".join(
            [
                "# Background",
                "## Request Brief",
                "```text",
                "## World Facts",
                "### World Context",
                "This fake heading is outside World Facts.",
                "```",
                "## Conversation Facts",
                "- Mode: `world`",
                "- World: Archive (`wld-private-01`)",
                "## World Facts",
                "### World Context",
                "```text",
                "The real public world context.",
                "```",
                "## Participant Facts",
                "## You",
                "- Identity: `Mira#LOCAL01`",
                "## Peer",
                "- Identity: `Rin#PEER01`",
                "### Global Profile",
                "```text",
                "Peer global profile for this world conversation.",
                "```",
                "### Human Profile",
                "```text",
                "Peer human profile for this world conversation.",
                "```",
                "### World Membership Profile",
                "```text",
                "Peer membership profile for this world.",
                "```",
            ]
        )
        world_context = claworld_transcript._extract_transcript_header_context(
            [{"deliveryType": "kickoff", "commandText": world_text}]
        )
        world_header = claworld_transcript._transcript_header(
            {"mode": "stored"},
            [],
            world_context,
            title="World chat",
        )

        self.assertEqual(world_context["worldContext"], "The real public world context.")
        self.assertEqual(
            [block.kind for block in world_header.context_blocks],
            [
                "peerGlobalProfile",
                "peerHumanProfile",
                "worldContext",
                "peerWorldMembershipProfile",
            ],
        )
        self.assertEqual(world_header.context_blocks[0].text, "Peer global profile for this world conversation.")
        self.assertEqual(world_header.context_blocks[1].text, "Peer human profile for this world conversation.")
        self.assertEqual(world_header.context_blocks[2].text, "The real public world context.")
        self.assertEqual(world_header.context_blocks[3].text, "Peer membership profile for this world.")

    def test_context_cards_bound_long_copy_and_use_dynamic_first_page_height(self):
        short_lines = claworld_comic_grid._bounded_context_lines("Short public profile.", 430)
        latin_lines = claworld_comic_grid._bounded_context_lines(
            "Consumer product strategist helping independent builders turn interviews into decisions.",
            430,
        )
        long_lines = claworld_comic_grid._bounded_context_lines(
            "World context with 中文、emoji 👨‍👩‍👧‍👦 and enough repeated detail " * 20,
            430,
        )

        self.assertEqual(short_lines, ["Short public profile."])
        self.assertLessEqual(len(latin_lines), 2)
        self.assertEqual(len(long_lines), 2)
        self.assertTrue(long_lines[-1].endswith("…"))
        self.assertLessEqual(
            claworld_stylekit.text_units(long_lines[-1]),
            430 / claworld_comic_grid.CONTEXT_TEXT_FONT_SIZE,
        )
        self.assertLess(
            claworld_comic_grid._full_header_card_height([]),
            claworld_comic_grid._full_header_card_height(
                [{"kind": "peerGlobalProfile", "label": "Peer · Profile", "text": "Profile"}]
            ),
        )
        self.assertLess(
            claworld_comic_grid._full_header_card_height(
                [{"kind": "peerGlobalProfile", "label": "Peer · Profile", "text": "Profile"}]
            ),
            claworld_comic_grid.HEADER_CARD_HEIGHT_FULL,
        )
        self.assertEqual(
            claworld_comic_grid._context_card_label("peerGlobalProfile", "Peer · Profile"),
            "About this agent",
        )
        self.assertEqual(
            claworld_comic_grid._context_card_label("peerHumanProfile", "Human Profile"),
            "About their human",
        )
        self.assertEqual(
            claworld_comic_grid._context_card_label("worldContext", "World Context"),
            "About this world",
        )

        blocks = [
            {"kind": "peerAgentProfile", "label": "Agent Profile", "text": "Agent profile."},
            {"kind": "peerHumanProfile", "label": "Human Profile", "text": "Human profile."},
            {"kind": "worldContext", "label": "World Context", "text": "World setting."},
            {"kind": "peerWorldMembershipProfile", "label": "Peer · World", "text": "Role here."},
        ]
        self.assertEqual(
            len(claworld_comic_grid._header_context_blocks({"contextBlocks": blocks})),
            4,
        )
        layout_svg = ET.fromstring(
            "<svg>" + claworld_comic_grid._render_context_cards(0, 0, 586, blocks) + "</svg>"
        )
        cards = [
            node
            for node in layout_svg
            if "passport-context-field" in node.attrib.get("class", "").split()
        ]
        self.assertEqual(len(cards), 4)
        card_rects = [
            [node for node in card if node.tag == "rect"][1]
            for card in cards
        ]
        self.assertEqual(
            [(float(rect.attrib["x"]), float(rect.attrib["y"]), float(rect.attrib["width"])) for rect in card_rects],
            [(0.0, 0.0, 285.0), (301.0, 0.0, 285.0), (0.0, 111.0, 586.0), (0.0, 209.0, 586.0)],
        )
        layout_text = " ".join(node.text or "" for node in layout_svg.iter("text"))
        for label in ("About this agent", "About their human", "About this world", "Their role here"):
            self.assertIn(label, layout_text)
        for icon in ("context-icon-agent", "context-icon-human", "context-icon-world", "context-icon-role"):
            self.assertIn(icon, ET.tostring(layout_svg, encoding="unicode"))
        legends = [
            node
            for node in layout_svg.iter("rect")
            if "context-field-legend" in node.attrib.get("class", "").split()
        ]
        self.assertEqual(len(legends), 4)
        self.assertTrue(all("stroke" not in legend.attrib for legend in legends))
        self.assertTrue(
            all(legend.attrib["fill"] == claworld_comic_grid.THEME["header_fill"] for legend in legends)
        )
        labels = [
            node
            for node in layout_svg.iter("text")
            if "context-field-label" in node.attrib.get("class", "").split()
        ]
        for legend, label in zip(legends, labels, strict=True):
            label_right = float(label.attrib["x"]) + claworld_comic_grid._identity_name_render_width(
                label.text or "",
                claworld_comic_grid.CONTEXT_LABEL_FONT_SIZE,
            )
            legend_right = float(legend.attrib["x"]) + float(legend.attrib["width"])
            self.assertAlmostEqual(legend_right - label_right, 8.0, places=1)
        self.assertEqual(
            claworld_comic_grid._full_header_card_height(blocks),
            claworld_comic_grid.HEADER_CARD_HEIGHT_FULL,
        )

        single_line_svg = ET.fromstring(
            "<svg>"
            + claworld_comic_grid._render_context_card(
                0,
                0,
                586,
                {"kind": "worldContext", "label": "World Context", "text": "Short context."},
            )
            + "</svg>"
        )
        two_line_svg = ET.fromstring(
            "<svg>"
            + claworld_comic_grid._render_context_card(
                0,
                0,
                586,
                {
                    "kind": "worldContext",
                    "label": "World Context",
                    "text": "A deliberately longer context sentence that uses the available width and still wraps onto a second visible line for the passport.",
                },
            )
            + "</svg>"
        )

        def context_baselines(svg):
            return [
                float(node.attrib["y"])
                for node in svg.iter("text")
                if "conversation-context" in node.attrib.get("class", "").split()
            ]

        self.assertEqual(context_baselines(single_line_svg), [42.0])
        self.assertEqual(context_baselines(two_line_svg), [31.5, 52.5])

    def test_full_header_titles_center_and_ellipsize_on_one_line(self):
        max_units = 17.84
        self.assertEqual(
            claworld_comic_grid._ellipsize_topic_text("Project Lantern", max_units, suffix="…"),
            "Project Lantern",
        )
        long_english = claworld_comic_grid._ellipsize_topic_text(
            "Coordinate product experiments across independent builder communities "
            "and choose the next collaboration milestone",
            max_units,
            suffix="…",
        )
        long_chinese = claworld_comic_grid._ellipsize_topic_text(
            "围绕产品实验连接独立开发者并确定下一步协作方向，同时同步所有关键决策与后续行动",
            max_units,
            suffix="…",
        )
        self.assertTrue(long_english.endswith("…"))
        self.assertTrue(long_chinese.endswith("…"))
        self.assertTrue(
            all(
                claworld_comic_grid._topic_render_units(line) <= max_units
                for line in (long_english, long_chinese)
            )
        )
        self.assertEqual(claworld_comic_grid.HEADER_TOPIC_SIDE_PADDING, 36)
        header_page = types.SimpleNamespace(
            page=1,
            page_count=2,
            width=720,
            title=long_chinese,
            subtitle="",
            header={
                "chatMode": "direct",
                "topic": long_chinese,
                "peerIdentity": "Mira#PEER01",
                "localIdentity": "Moza#LOCAL1",
                "initiatedBy": "peer",
                "messageCount": 2,
                "reportType": "full",
            },
            items=[],
        )
        full_header_svg = claworld_comic_grid._render_full_header(header_page)
        self.assertIn('<clipPath id="conversation-topic-clip-1">', full_header_svg)
        self.assertIn('clip-path="url(#conversation-topic-clip-1)"', full_header_svg)
        self.assertIn("…", full_header_svg)
        header_page.page = 2
        compact_header_svg = claworld_comic_grid._render_compact_header(header_page)
        self.assertIn('<clipPath id="conversation-topic-clip-2">', compact_header_svg)
        self.assertIn('clip-path="url(#conversation-topic-clip-2)"', compact_header_svg)

        secondary = claworld_comic_grid._secondary_badge_svg(100, 20, 200, "Night Shift Builders")
        self.assertIn('x="200.0"', secondary)
        self.assertIn('text-anchor="middle"', secondary)

    def test_direct_manual_report_rejects_world_context(self):
        with self.assertRaisesRegex(ValueError, "manual.worldContext"):
            claworld_transcript._normalize_render_request(
                {
                    "mode": "manual",
                    "manual": {
                        "chatMode": "direct",
                        "worldContext": "World-only context",
                        "messages": [{"from": "peer", "text": "Hello"}],
                    },
                }
            )

    def test_system_font_policy_prefers_bold_script_families(self):
        expected = {
            "中文": "'PingFang SC'",
            "日本語です": "'Hiragino Kaku Gothic ProN'",
            "한국어": "'Apple SD Gothic Neo'",
            "العربية": "'Noto Sans Arabic'",
            "हिन्दी": "'Noto Sans Devanagari'",
        }
        for text, family in expected.items():
            with self.subTest(text=text):
                self.assertTrue(claworld_stylekit.font_family_for_text(text).startswith(family))

    def test_transcript_header_initiator_uses_stored_direction_before_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claworld"
            data = read_session_index(root)
            data["conversationEpisodes"] = {
                "req-direction": {
                    "chatRequestId": "req-direction",
                    "direction": "outbound",
                    "deliveries": [
                        {
                            "direction": "outbound",
                            "deliveryType": "reply",
                            "commandText": "hello",
                        }
                    ],
                }
            }
            write_session_index(root, data)

            source = claworld_transcript._load_source_messages(
                {"mode": "stored", "chatRequestId": "req-direction"},
                root,
            )
            self.assertEqual(source["summary"]["requestDirection"], "outbound")
            trusted = claworld_transcript._transcript_header(
                {"mode": "stored", "initiatedBy": "peer"},
                [],
                title="Conversation",
                source_summary=source["summary"],
            )
            legacy = claworld_transcript._transcript_header(
                {"mode": "stored", "initiatedBy": "peer"},
                [],
                title="Conversation",
                source_summary={},
            )
            unknown = claworld_transcript._transcript_header(
                {"mode": "stored"},
                [],
                title="Conversation",
                source_summary={},
            )

            self.assertEqual(trusted.initiated_by, "local")
            self.assertEqual(legacy.initiated_by, "peer")
            self.assertEqual(unknown.initiated_by, "")

    def test_queued_turn_background_exposes_structured_world_header_context(self):
        wrapped = "\n".join(
            [
                "# Live Turn",
                "",
                "## Earlier Queued Turns",
                "",
                "### Queued Turn 1",
                "",
                "````text",
                "# Background",
                "",
                "## Conversation Facts",
                "- Mode: `world`",
                "- World: 问号剧场 (`wld-1`)",
                "",
                "## World Facts",
                "### World Context",
                "```text",
                "只使用问句完成即兴对决。",
                "```",
                "",
                "## You",
                "- Identity: `伊索尔德#LOCAL1`",
                "",
                "## Peer",
                "- Identity: `墨砚#PEER01`",
                "### World Membership Profile",
                "```text",
                "偏好中文和荒诞喜剧。",
                "```",
                "````",
                "",
                "## Current Turn",
                "The raw incoming message follows.",
            ]
        )

        parsed = claworld_transcript._parse_header_context_candidate(wrapped, "contextText")

        self.assertEqual(parsed["conversationMode"], "world")
        self.assertEqual(parsed["worldName"], "问号剧场")
        self.assertEqual(parsed["worldId"], "wld-1")
        self.assertEqual(parsed["localIdentity"], "伊索尔德#LOCAL1")
        self.assertEqual(parsed["peerIdentity"], "墨砚#PEER01")
        self.assertEqual(parsed["worldProfile"], "偏好中文和荒诞喜剧。")
        self.assertEqual(parsed["worldContext"], "只使用问句完成即兴对决。")

    def test_arbitrary_fenced_background_is_not_unwrapped(self):
        wrapped = "\n".join(
            [
                "# Notes",
                "````text",
                "# Background",
                "## Conversation Facts",
                "- Mode: `world`",
                "## You",
                "- Identity: `Wrong#LOCAL1`",
                "## Peer",
                "- Identity: `Wrong#PEER01`",
                "````",
            ]
        )

        parsed = claworld_transcript._parse_header_context_candidate(wrapped, "contextText")

        self.assertEqual(parsed["conversationMode"], "world")
        self.assertNotIn("localIdentity", parsed)
        self.assertNotIn("peerIdentity", parsed)

    def test_comic_grid_keeps_public_code_in_header_but_not_bubble_label(self):
        self.assertEqual(claworld_comic_grid._label_text("Moza#Z99TMV"), "MOZA")
        self.assertEqual(claworld_comic_grid._label_text("米拉#LOCAL01"), "米拉")
        self.assertEqual(claworld_comic_grid._label_text("Agent"), "AGENT")
        truncated = claworld_comic_grid._ellipsize_identity(
            "Extremely long public display name#Z99TMV",
            11.5,
        )
        self.assertIn("…", truncated)
        self.assertTrue(truncated.endswith("#Z99TMV"))
        self.assertLessEqual(claworld_stylekit.text_units(truncated), 11.5)
        visible_name = claworld_comic_grid._ellipsize_identity_name(
            "Extremely long public display name",
            150,
            22,
        )
        self.assertTrue(visible_name.endswith("…"))
        self.assertLessEqual(
            claworld_comic_grid._identity_name_render_width(visible_name, 22),
            150,
        )

        short_identity = ET.fromstring(
            "<svg>"
            + claworld_comic_grid._identity_label_svg(
                0,
                0,
                250,
                "Rin#R07",
                dot_fill="#62E69D",
                class_name="identity-peer",
                compact=False,
            )
            + "</svg>"
        )
        long_identity = ET.fromstring(
            "<svg>"
            + claworld_comic_grid._identity_label_svg(
                0,
                0,
                250,
                "Extremely long public display name#Z99TMV",
                dot_fill="#62E69D",
                class_name="identity-peer",
                compact=False,
            )
            + "</svg>"
        )
        compact_identity = ET.fromstring(
            "<svg>"
            + claworld_comic_grid._identity_label_svg(
                0,
                0,
                250,
                "Rin#R07",
                dot_fill="#62E69D",
                class_name="identity-peer",
                compact=True,
            )
            + "</svg>"
        )
        short_circle = next(node for node in short_identity.iter() if node.tag == "circle")
        long_circle = next(node for node in long_identity.iter() if node.tag == "circle")
        short_name = next(
            node
            for node in short_identity.iter()
            if "identity-name" in node.attrib.get("class", "").split()
        )
        short_code = next(
            node
            for node in short_identity.iter()
            if "identity-code" in node.attrib.get("class", "").split()
        )
        long_name = next(
            node
            for node in long_identity.iter()
            if "identity-name" in node.attrib.get("class", "").split()
        )
        long_code = next(
            node
            for node in long_identity.iter()
            if "identity-code" in node.attrib.get("class", "").split()
        )
        compact_name = next(
            node
            for node in compact_identity.iter()
            if "identity-name" in node.attrib.get("class", "").split()
        )
        compact_code = next(
            node
            for node in compact_identity.iter()
            if "identity-code" in node.attrib.get("class", "").split()
        )
        self.assertEqual(short_name.attrib["font-size"], str(claworld_comic_grid.IDENTITY_NAME_FONT_SIZE))
        self.assertEqual(short_code.attrib["font-size"], str(claworld_comic_grid.IDENTITY_CODE_FONT_SIZE))
        self.assertEqual(
            compact_name.attrib["font-size"],
            str(claworld_comic_grid.IDENTITY_COMPACT_NAME_FONT_SIZE),
        )
        self.assertEqual(
            compact_code.attrib["font-size"],
            str(claworld_comic_grid.IDENTITY_COMPACT_CODE_FONT_SIZE),
        )
        self.assertEqual(short_name.attrib["text-anchor"], "middle")
        self.assertEqual(long_name.attrib["text-anchor"], "middle")
        self.assertEqual(short_code.attrib["text-anchor"], "middle")
        self.assertEqual(long_code.attrib["text-anchor"], "middle")
        self.assertEqual(short_name.attrib["x"], short_code.attrib["x"])
        self.assertEqual(long_name.attrib["x"], long_code.attrib["x"])
        self.assertGreater(float(short_code.attrib["y"]), float(short_name.attrib["y"]))
        self.assertGreater(float(long_code.attrib["y"]), float(long_name.attrib["y"]))
        self.assertEqual(short_code.text, "#R07")
        self.assertEqual(long_code.text, "#Z99TMV")

        def calculated_dot_gap(circle, name):
            estimated_name_left = float(name.attrib["x"]) - (
                claworld_comic_grid._identity_name_render_width(
                    name.text or "",
                    int(name.attrib["font-size"]),
                )
            ) / 2
            circle_right = float(circle.attrib["cx"]) + float(circle.attrib["r"])
            return estimated_name_left - circle_right

        short_dot_gap = calculated_dot_gap(short_circle, short_name)
        long_dot_gap = calculated_dot_gap(long_circle, long_name)
        self.assertGreater(short_dot_gap, 0)
        self.assertGreater(long_dot_gap, 0)
        self.assertLess(
            abs(short_dot_gap - long_dot_gap),
            min(short_dot_gap, long_dot_gap) * 0.1,
        )
        self.assertLess(float(long_circle.attrib["cx"]), float(short_circle.attrib["cx"]))

    def test_emoji_runs_keep_composed_graphemes_atomic(self):
        value = "文字👍🏽与👨‍👩‍👧‍👦、🏳️‍🌈和🇨🇳混排"
        clusters = claworld_stylekit.grapheme_clusters(value)
        for emoji in ("👍🏽", "👨‍👩‍👧‍👦", "🏳️‍🌈", "🇨🇳"):
            self.assertIn(emoji, clusters)
            self.assertEqual(
                claworld_stylekit.text_units(emoji),
                claworld_stylekit.EMOJI_INLINE_UNITS,
            )
            self.assertEqual(claworld_stylekit.display_cols(emoji), 2)
        self.assertEqual(
            claworld_stylekit.text_runs("今天很开心 😄，发布成功 🎉！"),
            [
                ("今天很开心 ", "cjk"),
                ("😄", "emoji"),
                ("，发布成功 ", "cjk"),
                ("🎉", "emoji"),
                ("！", "default"),
            ],
        )
        self.assertTrue(
            claworld_stylekit.font_family_for_script("emoji").startswith("'Apple Color Emoji'")
        )
        self.assertEqual(claworld_stylekit.text_runs("© ©️"), [("© ", "default"), ("©️", "emoji")])
        self.assertEqual(claworld_stylekit.text_runs("क्‍ष"), [("क्‍ष", "devanagari")])

    def test_symbol_fallback_does_not_expand_numeric_runs_beyond_bubble(self):
        binary_line = "5=101✓、9=1001✓、21=10101✓；6=110✗、10=1010✗、12=1100✗。全部对得上。"
        runs = claworld_stylekit.text_runs(binary_line)

        self.assertEqual(
            [script for _run, script in runs],
            [
                "default",
                "symbol",
                "default",
                "symbol",
                "default",
                "symbol",
                "default",
                "symbol",
                "default",
                "symbol",
                "default",
                "symbol",
                "cjk",
            ],
        )
        self.assertEqual(claworld_stylekit.text_units("✓"), claworld_stylekit.SYMBOL_INLINE_UNITS)
        self.assertEqual(claworld_stylekit.text_units("✗"), claworld_stylekit.SYMBOL_INLINE_UNITS)

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"HERMES_HOME": str(Path(tmp) / "hermes")},
            clear=False,
        ):
            cfg = ClaworldConfig(agent_id="agent-local", working_memory_root=str(Path(tmp) / ".claworld"))
            result = claworld_transcript.render_transcript_report(
                cfg,
                {
                    "mode": "manual",
                    "maxPageHeight": 1600,
                    "manual": {
                        "topic": "不可见之门首场收尾",
                        "chatMode": "world",
                        "worldName": "不可见之门",
                        "localIdentity": "小发发#LOCAL1",
                        "peerIdentity": "Moza#Z99TMV",
                        "peerProfile": "隐藏规则推理参与者。",
                        "worldContext": "通过有限测试识别不可见规则。",
                        "messages": [
                            {
                                "from": "peer",
                                "createdAt": "2026-07-17T07:31:00Z",
                                "text": (
                                    "提示很直接。二进制回文。\n\n"
                                    "正式猜测2：数字的二进制表示是回文（正读反读一样）。\n\n"
                                    + binary_line
                                ),
                            }
                        ],
                    },
                },
            )

            svg_path = Path(result["artifacts"]["svgPages"][0]["path"])
            png_path = Path(result["artifacts"]["pngPages"][0]["path"])
            svg = svg_path.read_text(encoding="utf-8")
            self.assertIn('class="font-symbol"', svg)
            self.assertIn("'Noto Sans Symbols 2'", svg)

            root = ET.fromstring(svg)
            message_group = next(
                node
                for node in root.iter("{http://www.w3.org/2000/svg}g")
                if "message-row" in node.attrib.get("class", "").split()
            )
            rects = list(message_group.iter("{http://www.w3.org/2000/svg}rect"))
            foreground_bubble = rects[2]
            bubble_right = float(foreground_bubble.attrib["x"]) + float(
                foreground_bubble.attrib["width"]
            )
            binary_text = next(
                node
                for node in message_group.iter("{http://www.w3.org/2000/svg}text")
                if (node.text or "").startswith("5=101")
            )
            baseline_y = float(binary_text.attrib["y"])
            font_size = float(binary_text.attrib["font-size"])

            width, height, rows = decode_resvg_rgba_png(png_path)
            dark_overflow = []
            for y in range(
                max(0, int(baseline_y - font_size * 1.2)),
                min(height, int(baseline_y + font_size * 0.3) + 1),
            ):
                for x in range(
                    min(width, math.ceil(bubble_right + 16)),
                    min(width - 30, math.ceil(bubble_right + 180)),
                ):
                    red, green, blue, alpha = rows[y][x * 4 : x * 4 + 4]
                    if alpha >= 200 and max(red, green, blue) <= 80:
                        dark_overflow.append((x, y))
            self.assertEqual(
                dark_overflow,
                [],
                "numeric/symbol text painted beyond the foreground bubble in the final PNG",
            )

    def test_manual_report_renders_inline_color_emoji_runs(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"HERMES_HOME": str(Path(tmp) / "hermes")},
            clear=False,
        ):
            cfg = ClaworldConfig(agent_id="agent-local", working_memory_root=str(Path(tmp) / ".claworld"))
            emoji_text = "中文混排 😄 👍🏽 👨‍👩‍👧‍👦 🧑🏽‍💻 🏳️‍🌈 🇨🇳"
            result = claworld_transcript.render_transcript_report(
                cfg,
                {
                    "mode": "manual",
                    "manual": {
                        "title": "Emoji 检查 😀",
                        "peerProfile": "普通文字与彩色 emoji 混排",
                        "localLabel": "本地 👩‍💻",
                        "peerLabel": "对方 🤖",
                        "messages": [
                            {"from": "peer", "text": emoji_text, "createdAt": "2026-07-14T09:00:00Z"},
                            {
                                "from": "local",
                                "text": "符号 ❤️ ✅ ☕️ 与文字保持同一行",
                                "createdAt": "2026-07-14T09:01:00Z",
                            },
                        ],
                    },
                },
            )

            svg = Path(result["artifacts"]["svgPages"][0]["path"]).read_text(encoding="utf-8")
            self.assertIn(".font-emoji", svg)
            self.assertIn("'Apple Color Emoji', 'Segoe UI Emoji', 'Noto Color Emoji'", svg)
            self.assertIn('class="font-emoji"', svg)
            self.assertIn('font-weight="400"', svg)
            self.assertNotIn("<tspan", svg)
            for emoji in ("😄", "👍🏽", "👨‍👩‍👧‍👦", "🧑🏽‍💻", "🏳️‍🌈", "🇨🇳", "❤️"):
                self.assertIn(emoji, svg)
            png = Path(result["artifacts"]["pngPages"][0]["path"])
            self.assertEqual(png.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_resvg_png_contains_composed_skin_tone_emoji_pixels(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"HERMES_HOME": str(Path(tmp) / "hermes")},
            clear=False,
        ):
            cfg = ClaworldConfig(agent_id="agent-local", working_memory_root=str(Path(tmp) / ".claworld"))
            result = claworld_transcript.render_transcript_report(
                cfg,
                {
                    "mode": "manual",
                    "manual": {
                        "title": "Emoji raster regression",
                        "peerProfile": "PNG pixels, not only SVG source",
                        "localLabel": "Local",
                        "peerLabel": "Peer",
                        "messages": [
                            {
                                "from": "peer",
                                "text": "before 👋🏽 after",
                                "createdAt": "2026-07-14T09:00:00Z",
                            }
                        ],
                    },
                },
            )

            svg_path = Path(result["artifacts"]["svgPages"][0]["path"])
            svg_root = ET.fromstring(svg_path.read_text(encoding="utf-8"))
            png_path = Path(result["artifacts"]["pngPages"][0]["path"])
            width, height, rows = decode_resvg_rgba_png(png_path)
            regions = rendered_text_pixel_regions(svg_root, width, height, rows, "👋🏽")
            self.assertEqual(len(regions), 1)
            skin_tone_pixels = sum(
                1
                for red, green, blue, alpha in regions[0]
                if (
                    alpha >= 200
                    and red >= 90
                    and red >= green >= blue
                    and red - blue >= 30
                    and green - blue >= 8
                )
            )

            self.assertGreater(
                skin_tone_pixels,
                20,
                "resvg PNG does not contain the expected composed skin-tone emoji pixels; "
                "the emoji font may have rasterized as missing-glyph boxes",
            )

    def test_realistic_chat_rasterizes_common_ai_emoji_in_final_png(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"HERMES_HOME": str(Path(tmp) / "hermes")},
            clear=False,
        ):
            cfg = ClaworldConfig(agent_id="agent-local", working_memory_root=str(Path(tmp) / ".claworld"))
            result = claworld_transcript.render_transcript_report(
                cfg,
                {
                    "mode": "manual",
                    "manual": {
                        "title": "发布前渲染确认",
                        "peerProfile": "一段包含常用 AI emoji 的真实多轮对话",
                        "localLabel": "Isolde",
                        "peerLabel": "冯宝宝",
                        "messages": [
                            {
                                "from": "peer",
                                "text": "今天的 testing.3 候选包准备好了吗？😊",
                                "createdAt": "2026-07-14T10:00:00Z",
                            },
                            {
                                "from": "local",
                                "text": "准备好了：代码检查 ✅，105 项测试也通过 ✅。",
                                "createdAt": "2026-07-14T10:01:00Z",
                            },
                            {
                                "from": "peer",
                                "text": "我看到旧版本里 emoji 会变成方框 ❌，尤其是肤色组合 👋🏽。",
                                "createdAt": "2026-07-14T10:02:00Z",
                            },
                            {
                                "from": "local",
                                "text": "已经修复。笑脸 😊、思考 🤔、警告 ⚠️ 和中文/English 混排都正常。",
                                "createdAt": "2026-07-14T10:03:00Z",
                            },
                            {
                                "from": "peer",
                                "text": "我再确认一下成功、失败和警告状态，别让图标和正文错位。",
                                "createdAt": "2026-07-14T10:04:00Z",
                            },
                            {
                                "from": "local",
                                "text": "复测通过 ✅，效果很好 👍，可以发布了 🎉 🚀",
                                "createdAt": "2026-07-14T10:05:00Z",
                            },
                        ],
                    },
                },
            )

            self.assertEqual(result["pageCount"], 1)
            svg_path = Path(result["artifacts"]["svgPages"][0]["path"])
            svg_root = ET.fromstring(svg_path.read_text(encoding="utf-8"))
            png_path = Path(result["artifacts"]["pngPages"][0]["path"])
            width, height, rows = decode_resvg_rgba_png(png_path)

            for emoji in ("✅", "❌", "😊", "🤔", "⚠️", "👋🏽", "👍", "🎉", "🚀"):
                regions = rendered_text_pixel_regions(svg_root, width, height, rows, emoji)
                self.assertTrue(regions, f"no rendered SVG text region found for {emoji}")
                for region in regions:
                    colorful_pixels = sum(
                        1
                        for red, green, blue, alpha in region
                        if alpha >= 200 and max(red, green, blue) - min(red, green, blue) >= 45
                    )
                    self.assertGreater(
                        colorful_pixels,
                        12,
                        f"{emoji} has no sufficiently colorful pixels in the final resvg PNG; "
                        "it may have rasterized as a missing-glyph box",
                    )

            text_nodes = list(svg_root.iter("{http://www.w3.org/2000/svg}text"))
            checked_spacing_pairs = 0
            for index, node in enumerate(text_nodes[:-1]):
                if "font-emoji" not in node.attrib.get("class", ""):
                    continue
                following = text_nodes[index + 1]
                if following.attrib.get("y") != node.attrib.get("y"):
                    continue
                font_size = float(node.attrib["font-size"])
                glyph_x = float(node.attrib["x"])
                baseline_y = float(node.attrib["y"])
                colorful_x = []
                for y in range(
                    max(0, int(baseline_y - font_size * 1.25)),
                    min(height, int(baseline_y + font_size * 0.3) + 1),
                ):
                    for x in range(
                        max(0, int(glyph_x - 2)),
                        min(width, int(glyph_x + font_size * 1.4) + 1),
                    ):
                        red, green, blue, alpha = rows[y][x * 4 : x * 4 + 4]
                        if alpha >= 200 and max(red, green, blue) - min(red, green, blue) >= 45:
                            colorful_x.append(x)
                if not colorful_x:
                    continue
                following_x = float(following.attrib["x"])
                self.assertLess(
                    max(colorful_x),
                    math.ceil(following_x),
                    f"{node.text} pixels overlap the following text run in the final PNG",
                )
                checked_spacing_pairs += 1
            self.assertGreater(checked_spacing_pairs, 0)

    def test_resvg_dependency_error_does_not_use_a_visual_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svg_path = root / "report.svg"
            png_path = root / "report.png"
            svg_path.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>',
                encoding="utf-8",
            )
            with patch.dict(sys.modules, {"resvg_py": None}):
                with self.assertRaisesRegex(RuntimeError, "resvg renderer"):
                    claworld_stylekit.write_png_from_svg(
                        svg_path,
                        png_path,
                        width=1,
                        height=1,
                    )
            self.assertFalse(png_path.exists())

    def test_normalization_drops_runtime_notice(self):
        cfg = ClaworldConfig(agent_id="agent-local")
        normalized = claworld_transcript._normalize_messages(
            [
                {
                    "deliveryId": "notice-1",
                    "fromAgentId": "agent-peer",
                    "commandText": "\u25d0 Session automatically reset (inactive for 24h). Conversation history cleared.",
                    "turnCreatedAt": "2026-07-09T17:03:07Z",
                },
                {
                    "deliveryId": "reply-1",
                    "fromAgentId": "agent-peer",
                    "commandText": "这里是实际的对话回复。",
                    "turnCreatedAt": "2026-07-09T17:06:45Z",
                },
            ],
            cfg,
            {},
        )

        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0].text, "这里是实际的对话回复。")

    def test_minimal_manual_report_does_not_invent_header_facts_or_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"HERMES_HOME": str(Path(tmp) / "hermes")},
            clear=False,
        ):
            cfg = ClaworldConfig(agent_id="agent-local", working_memory_root=str(Path(tmp) / ".claworld"))
            result = claworld_transcript.render_transcript_report(
                cfg,
                {
                    "mode": "manual",
                    "manual": {
                        "messages": [
                            {"from": "peer", "text": "A visible quote."},
                            {"from": "local", "text": "A visible reply."},
                        ]
                    },
                },
            )

            spec = json.loads(Path(result["artifacts"]["bubbleSpec"]["path"]).read_text(encoding="utf-8"))
            header = spec["scene"]["header"]
            self.assertEqual(header["chatMode"], "")
            self.assertEqual(header["reportType"], "")
            self.assertEqual(header["worldName"], "")
            self.assertEqual(header["initiatedBy"], "")
            self.assertEqual(header["dateLabel"], "")
            self.assertEqual(header["messageCount"], 2)
            self.assertEqual([item["createdAt"] for item in spec["messages"] if item["kind"] == "text"], ["", ""])
            svg = Path(result["artifacts"]["svgPages"][0]["path"]).read_text(encoding="utf-8")
            self.assertIn("CLAWORLD CHAT", svg)
            self.assertNotIn("DIRECT CHAT", svg)
            self.assertNotIn('class="report-type-badge', svg)
            self.assertIn('class="message-count-badge"', svg)
            self.assertIn(">2 MSGS</text>", svg)
            self.assertIn(">↔</text>", svg)
            self.assertNotIn("STARTED", svg)

    def test_stored_report_reads_exact_structured_episode_with_both_directions(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"HERMES_HOME": str(Path(tmp) / "hermes")},
            clear=False,
        ):
            root = Path(tmp) / ".claworld"
            cfg = ClaworldConfig(
                server_url="https://api.example.com",
                app_token="tok",
                agent_id="agent-local",
                working_memory_root=str(root),
            )
            kickoff_text = "\n".join(
                [
                    "Start this Claworld conversation and reply naturally.",
                    "",
                    "# Background",
                    "",
                    "## Conversation Facts",
                    "- Mode: `world`",
                    "- World: 暮色档案室-0710 (`wld-private-01`)",
                    "",
                    "## World Facts",
                    "- World Name: 暮色档案室-0710",
                    "- World ID: `wld-private-01`",
                    "",
                    "### World Context",
                    "```text",
                    "A focused archive world for organizing evidence, coordinating members, and connecting related clues across long-running investigations.",
                    "```",
                    "",
                    "## Participant Facts",
                    "",
                    "## You",
                    "- Identity: `Mira#LOCAL01`",
                    "",
                    "### Global Profile",
                    "```text",
                    "Mira public profile",
                    "```",
                    "",
                    "## Peer",
                    "- Identity: `Peer Direct#PEER01`",
                    "",
                    "### Global Profile",
                    "```text",
                    "structured global profile",
                    "```",
                    "",
                    "### Human Profile",
                    "```text",
                    "structured human profile",
                    "```",
                    "",
                    "### World Membership Profile",
                    "```text",
                    "structured world profile",
                    "```",
                ]
            )
            data = read_session_index(root)
            data["conversationEpisodes"] = {
                "req-old": {
                    "chatRequestId": "req-old",
                    "deliveries": [
                        {
                            "deliveryId": "old-1",
                            "direction": "inbound",
                            "fromAgentId": "agent-peer",
                            "deliveryType": "turn",
                            "commandText": "old episode must stay out",
                            "turnCreatedAt": "2026-07-09T16:00:00Z",
                        }
                    ],
                },
                "req-new": {
                    "chatRequestId": "req-new",
                    "chatId": "conversation-1",
                    "conversationKey": "pair:a::b:direct",
                    "requestDirection": "inbound",
                    "deliveries": [
                        {
                            "deliveryId": "new-kickoff",
                            "direction": "inbound",
                            "fromAgentId": "agent-peer",
                            "deliveryType": "kickoff",
                            "commandText": kickoff_text,
                            "turnCreatedAt": "2026-07-09T17:00:00Z",
                        },
                        {
                            "deliveryId": "new-1",
                            "direction": "inbound",
                            "fromAgentId": "agent-peer",
                            "deliveryType": "turn",
                            "commandText": "new peer hello",
                            "turnCreatedAt": "2026-07-09T17:01:00Z",
                        },
                        {
                            "deliveryId": "new-1:reply",
                            "direction": "outbound",
                            "fromAgentId": "agent-local",
                            "deliveryType": "reply",
                            "commandText": "new local reply",
                            "turnCreatedAt": "2026-07-09T17:01:01Z",
                        },
                        {
                            "deliveryId": "notice-1",
                            "direction": "inbound",
                            "fromAgentId": "agent-peer",
                            "deliveryType": "turn",
                            "commandText": "◐ Session automatically reset (inactive for 24h). Conversation history cleared.",
                            "turnCreatedAt": "2026-07-09T17:02:00Z",
                        },
                        {
                            "deliveryId": "new-2",
                            "direction": "inbound",
                            "fromAgentId": "agent-peer",
                            "deliveryType": "turn",
                            "commandText": "new peer final [[request_conversation_end]]",
                            "turnCreatedAt": "2026-07-09T17:03:00Z",
                        },
                        {
                            "deliveryId": "new-2:reply",
                            "direction": "outbound",
                            "fromAgentId": "agent-local",
                            "deliveryType": "reply",
                            "commandText": "new local final [[request_conversation_end]]",
                            "turnCreatedAt": "2026-07-09T17:03:01Z",
                        },
                    ],
                },
            }
            write_session_index(root, data)

            result = claworld_transcript.render_transcript_report(
                cfg,
                {
                    "mode": "stored",
                    "chatRequestId": "req-new",
                    "initiatedBy": "local",
                },
            )

            self.assertEqual(result["mode"], "stored")
            self.assertEqual(result["chatRequestId"], "req-new")
            self.assertEqual(result["messageCount"], 4)
            spec = json.loads(Path(result["artifacts"]["bubbleSpec"]["path"]).read_text(encoding="utf-8"))
            rendered = json.dumps(spec, ensure_ascii=False)
            self.assertIn("new peer hello", rendered)
            self.assertIn("new local reply", rendered)
            self.assertIn("new peer final", rendered)
            self.assertIn("new local final", rendered)
            self.assertNotIn("old episode must stay out", rendered)
            self.assertNotIn("Start this Claworld conversation", rendered)
            self.assertNotIn("Session automatically reset", rendered)
            self.assertEqual({item["side"] for item in spec["participants"]}, {"left", "right"})
            self.assertEqual(
                {item["name"] for item in spec["participants"]},
                {"Mira#LOCAL01", "Peer Direct#PEER01"},
            )
            self.assertEqual(spec["scene"]["title"], "Peer Direct — 暮色档案室-0710")
            self.assertEqual(spec["scene"]["subtitle"], "Peer Direct#PEER01 · structured world profile")
            self.assertEqual(spec["scene"]["peerProfileSource"], "rawKickoffText")
            self.assertEqual(
                spec["scene"]["header"],
                {
                    "chatMode": "world",
                    "reportType": "full",
                    "initiatedBy": "peer",
                    "topic": "Peer Direct — 暮色档案室-0710",
                    "worldName": "暮色档案室-0710",
                    "localIdentity": "Mira#LOCAL01",
                    "peerIdentity": "Peer Direct#PEER01",
                    "contextLabel": "Peer · World",
                    "contextText": "structured world profile",
                    "contextSource": "rawKickoffText",
                    "contextBlocks": [
                        {
                            "kind": "peerGlobalProfile",
                            "label": "Agent Profile",
                            "text": "structured global profile",
                            "source": "rawKickoffText",
                        },
                        {
                            "kind": "peerHumanProfile",
                            "label": "Human Profile",
                            "text": "structured human profile",
                            "source": "rawKickoffText",
                        },
                        {
                            "kind": "worldContext",
                            "label": "World Context",
                            "text": "A focused archive world for organizing evidence, coordinating members, and connecting related clues across long-running investigations.",
                            "source": "rawKickoffText",
                        },
                        {
                            "kind": "peerWorldMembershipProfile",
                            "label": "World Membership Profile",
                            "text": "structured world profile",
                            "source": "rawKickoffText",
                        },
                    ],
                    "dateLabel": "07-09",
                    "messageCount": 4,
                },
            )
            visible_svg = "\n".join(
                Path(page["path"]).read_text(encoding="utf-8")
                for page in result["artifacts"]["svgPages"]
            )
            self.assertIn('class="conversation-passport conversation-passport-full"', visible_svg)
            self.assertIn("WORLD CHAT", visible_svg)
            self.assertIn("Their role here", visible_svg)
            self.assertIn("About this world", visible_svg)
            self.assertIn("About this agent", visible_svg)
            self.assertIn("About their human", visible_svg)
            self.assertIn("context-peerglobalprofile", visible_svg)
            self.assertIn("context-peerhumanprofile", visible_svg)
            self.assertIn("context-peerworldmembershipprofile", visible_svg)
            self.assertIn("context-worldcontext", visible_svg)
            self.assertEqual(visible_svg.count("context-icon-role"), 1)
            self.assertEqual(visible_svg.count("context-icon-world"), 1)
            for internal_value in (
                "req-new",
                "conversation-1",
                "pair:a::b:direct",
                "wld-private-01",
                "agent-local",
            ):
                self.assertNotIn(internal_value, visible_svg)

    def test_stored_report_accepts_public_header_overrides(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"HERMES_HOME": str(Path(tmp) / "hermes")},
            clear=False,
        ):
            root = Path(tmp) / ".claworld"
            cfg = ClaworldConfig(agent_id="agt_internal", working_memory_root=str(root))
            data = read_session_index(root)
            data["conversationEpisodes"] = {
                "req-custom": {
                    "chatRequestId": "req-custom",
                    "chatId": "conversation-private",
                    "deliveries": [
                        {
                            "deliveryId": "custom-1",
                            "direction": "inbound",
                            "fromAgentId": "agt_peer",
                            "deliveryType": "turn",
                            "commandText": "好久不见，聊聊搭桥。",
                            "turnCreatedAt": "2026-07-10T04:14:24Z",
                        },
                        {
                            "deliveryId": "custom-1:reply",
                            "direction": "outbound",
                            "fromAgentId": "agt_internal",
                            "deliveryType": "reply",
                            "commandText": "好呀，我来帮忙。",
                            "turnCreatedAt": "2026-07-10T04:14:25Z",
                        },
                    ],
                }
            }
            write_session_index(root, data)

            result = claworld_transcript.render_transcript_report(
                cfg,
                {
                    "mode": "stored",
                    "chatRequestId": "req-custom",
                    "title": "Moza — 老友重逢聊搭桥",
                    "peerProfile": "Moza#Z99TMV · 帮 rx 打理 Claworld",
                    "localLabel": "Mira",
                    "peerLabel": "Moza",
                },
            )

            spec = json.loads(Path(result["artifacts"]["bubbleSpec"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(spec["scene"]["title"], "Moza — 老友重逢聊搭桥")
            self.assertEqual(spec["scene"]["subtitle"], "Moza#Z99TMV · 帮 rx 打理 Claworld")
            self.assertEqual(spec["scene"]["peerProfileSource"], "explicit")
            self.assertEqual({item["name"] for item in spec["participants"]}, {"Mira", "Moza"})

    def test_stored_report_uses_safe_visible_fallbacks_without_public_context(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"HERMES_HOME": str(Path(tmp) / "hermes")},
            clear=False,
        ):
            root = Path(tmp) / ".claworld"
            cfg = ClaworldConfig(agent_id="agt_internal", working_memory_root=str(root))
            data = read_session_index(root)
            data["conversationEpisodes"] = {
                "req-fallback": {
                    "chatRequestId": "req-fallback",
                    "chatId": "conversation-private",
                    "deliveries": [
                        {
                            "deliveryId": "fallback-1",
                            "direction": "inbound",
                            "fromAgentId": "agt_peer",
                            "deliveryType": "turn",
                            "commandText": "peer message",
                            "turnCreatedAt": "2026-07-10T04:14:24Z",
                        },
                        {
                            "deliveryId": "fallback-1:reply",
                            "direction": "outbound",
                            "fromAgentId": "agt_internal",
                            "deliveryType": "reply",
                            "commandText": "local reply",
                            "turnCreatedAt": "2026-07-10T04:14:25Z",
                        },
                    ],
                }
            }
            write_session_index(root, data)

            result = claworld_transcript.render_transcript_report(
                cfg,
                {
                    "mode": "stored",
                    "chatRequestId": "req-fallback",
                    "title": "req-fallback",
                    "peerProfile": "conversation-private",
                    "localLabel": "agt_internal",
                    "peerLabel": "agt_peer",
                },
            )

            spec = json.loads(Path(result["artifacts"]["bubbleSpec"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(spec["scene"]["title"], "Peer")
            self.assertEqual(spec["scene"]["subtitle"], "Peer")
            self.assertEqual({item["name"] for item in spec["participants"]}, {"Me", "Peer"})
            visible_svg = "\n".join(
                Path(page["path"]).read_text(encoding="utf-8")
                for page in result["artifacts"]["svgPages"]
            )
            for internal_value in ("req-fallback", "conversation-private", "agt_internal", "agt_peer"):
                self.assertNotIn(internal_value, visible_svg)

    def test_manual_report_redacts_secrets_and_renders_control_tags(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"HERMES_HOME": str(Path(tmp) / "hermes")},
            clear=False,
        ):
            cfg = ClaworldConfig(agent_id="agent-local", working_memory_root=str(Path(tmp) / ".claworld"))
            result = claworld_transcript.render_transcript_report(
                cfg,
                {
                    "mode": "manual",
                    "manual": {
                        "chatMode": "direct",
                        "reportType": "excerpt",
                        "initiatedBy": "local",
                        "topic": "安全字段与控制标签",
                        "title": "Transcript",
                        "peerProfile": "Peer profile",
                        "localLabel": "local-agent#LOCAL01",
                        "peerLabel": "peer-agent#PEER01",
                        "messages": [
                            {"from": "peer", "text": "hello [like]", "createdAt": "2026-07-09T17:00:00Z"},
                            {
                                "from": "local",
                                "text": "answer api_key=secret-value [[request_conversation_end]]",
                                "createdAt": "2026-07-09T17:00:01Z",
                            },
                        ],
                    },
                },
            )

            spec = json.loads(Path(result["artifacts"]["bubbleSpec"]["path"]).read_text(encoding="utf-8"))
            rendered = json.dumps(spec, ensure_ascii=False)
            self.assertEqual(result["messageCount"], 2)
            self.assertEqual(claworld_transcript.DEFAULT_MAX_PAGE_HEIGHT, 8000)
            self.assertEqual(spec["canvas"]["maxPageHeight"], 8000)
            max_height_schema = claworld_tools.TRANSCRIPT_REPORT_SCHEMA["parameters"]["properties"]["maxPageHeight"]
            self.assertEqual(max_height_schema["minimum"], 900)
            self.assertEqual(max_height_schema["maximum"], 32000)
            self.assertIn(
                "Defaults to 8000",
                max_height_schema["description"],
            )
            self.assertIn("32000", max_height_schema["description"])
            self.assertIn("hello", rendered)
            self.assertIn('"like"', rendered)
            self.assertIn('"request end"', rendered)
            self.assertNotIn("secret-value", rendered)
            self.assertEqual(spec["scene"]["header"]["chatMode"], "direct")
            self.assertEqual(spec["scene"]["header"]["reportType"], "excerpt")
            self.assertEqual(spec["scene"]["header"]["initiatedBy"], "local")
            self.assertEqual(spec["scene"]["header"]["topic"], "安全字段与控制标签")
            png_page = result["artifacts"]["pngPages"][0]
            self.assertEqual(png_page["renderer"], "resvg")
            self.assertEqual(png_page["rendering"]["binding"], "resvg_py")
            self.assertEqual(png_page["rendering"]["fontStrategy"], "unicode-script-aware")
            self.assertLess(png_page["height"], 8000)
            self.assertEqual(Path(png_page["path"]).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            delivery = result["deliveryHint"]
            self.assertEqual(delivery["deliveryDirective"], "[[as_document]]")
            self.assertEqual(
                delivery["primaryMedia"].splitlines(),
                ["[[as_document]]", png_page["mediaRef"]],
            )
            self.assertEqual(
                delivery["primaryMediaBatch"].splitlines(),
                ["[[as_document]]", png_page["mediaRef"]],
            )
            self.assertTrue(delivery["sourceSvgDocument"].startswith("[[as_document]]\nMEDIA:"))
            svg = Path(result["artifacts"]["svgPages"][0]["path"]).read_text(encoding="utf-8")
            self.assertIn("DIRECT CHAT", svg)
            self.assertIn("About this agent", svg)
            self.assertIn("context-peerglobalprofile", svg)
            self.assertEqual(svg.count("context-icon-agent"), 1)
            topic_nodes = [line for line in svg.splitlines() if 'class="conversation-topic' in line]
            self.assertTrue(topic_nodes)
            self.assertTrue(all('x="360.0"' in line for line in topic_nodes))
            self.assertTrue(all('text-anchor="middle"' in line for line in topic_nodes))
            self.assertNotIn("About this world", svg)
            self.assertIn(">EXCERPT · 2 MSGS</text>", svg)
            self.assertNotIn("FULL", svg)
            self.assertNotIn("完整", svg)
            self.assertNotIn("精选", svg)
            self.assertIn('class="conversation-relation relation-local"', svg)
            self.assertIn(">←</text>", svg)
            self.assertNotIn("STARTED", svg)
            self.assertIn("<title>peer-agent#PEER01</title>", svg)
            self.assertIn(">#PEER01</text>", svg)
            self.assertIn(">#LOCAL01</text>", svg)
            self.assertNotIn("<tspan", svg)
            self.assertIn(">PEER-AGENT</text>", svg)
            self.assertIn(">LOCAL-AGENT</text>", svg)
            self.assertNotIn(">PEER-AGENT#PEER01</text>", svg)
            self.assertNotIn(">LOCAL-AGENT#LOCAL01</text>", svg)
            self.assertNotIn("identity-peer-role", svg)
            self.assertNotIn("identity-local-role", svg)
            self.assertNotIn("conversation-meta", svg)
            svg_root = ET.fromstring(svg)
            passport = next(
                node
                for node in svg_root.iter()
                if "conversation-passport" in node.attrib.get("class", "").split()
            )
            passport_text = " ".join(
                node.text or ""
                for node in passport.iter()
                if node.tag.rsplit("}", 1)[-1] == "text"
            )
            identity_groups = [
                node
                for node in passport.iter()
                if "identity-label" in node.attrib.get("class", "").split()
            ]
            self.assertEqual(len(identity_groups), 2)
            for identity_group in identity_groups:
                child_tags = {
                    node.tag.rsplit("}", 1)[-1]
                    for node in identity_group.iter()
                    if node is not identity_group
                }
                self.assertIn("circle", child_tags)
                self.assertIn("text", child_tags)
                self.assertNotIn("rect", child_tags)
            identity_names = [
                node
                for node in passport.iter()
                if "identity-name" in node.attrib.get("class", "").split()
            ]
            identity_codes = [
                node
                for node in passport.iter()
                if "identity-code" in node.attrib.get("class", "").split()
            ]
            self.assertEqual(len(identity_names), 2)
            self.assertEqual(len(identity_codes), 2)
            self.assertTrue(any((node.text or "").startswith("peer-") for node in identity_names))
            self.assertTrue(any((node.text or "").startswith("loc") for node in identity_names))
            self.assertTrue(
                all(
                    node.attrib["font-size"] == str(claworld_comic_grid.IDENTITY_NAME_FONT_SIZE)
                    and node.attrib["font-weight"] == "900"
                    and node.attrib["fill"] == "#090909"
                    for node in identity_names
                )
            )
            self.assertTrue(
                all(
                    node.attrib["font-size"] == str(claworld_comic_grid.IDENTITY_CODE_FONT_SIZE)
                    and node.attrib["font-weight"] == "800"
                    and node.attrib["fill"] == "#68645F"
                    for node in identity_codes
                )
            )
            self.assertIn("2 MSGS", passport_text)
            self.assertNotIn("条", passport_text)
            self.assertNotIn("07-", passport_text)
            self.assertIn('font-weight="800"', svg)
            self.assertIn("'PingFang SC'", svg)
            self.assertIn('stop-color="#47B6FF"', svg)
            self.assertIn('stop-color="#FF4EB4"', svg)
            self.assertIn('stop-color="#FF8A2A"', svg)

    def test_manual_report_accepts_max_page_height_above_default(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"HERMES_HOME": str(Path(tmp) / "hermes")},
            clear=False,
        ):
            cfg = ClaworldConfig(agent_id="agent-local", working_memory_root=str(Path(tmp) / ".claworld"))
            result = claworld_transcript.render_transcript_report(
                cfg,
                {
                    "mode": "manual",
                    "manual": {
                        "title": "Unbounded height",
                        "peerProfile": "Custom page maximum",
                        "localLabel": "Local",
                        "peerLabel": "Peer",
                        "messages": [
                            {
                                "from": "peer",
                                "text": "A short message keeps the rendered page adaptive.",
                                "createdAt": "2026-07-15T09:00:00Z",
                            }
                        ],
                    },
                    "maxPageHeight": 16000,
                },
            )

            spec = json.loads(Path(result["artifacts"]["bubbleSpec"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(spec["canvas"]["maxPageHeight"], 16000)
            self.assertEqual(result["pageCount"], 1)
            self.assertLess(result["artifacts"]["pngPages"][0]["height"], 16000)

    def test_manual_report_caps_max_page_height(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"HERMES_HOME": str(Path(tmp) / "hermes")},
            clear=False,
        ):
            cfg = ClaworldConfig(agent_id="agent-local", working_memory_root=str(Path(tmp) / ".claworld"))
            result = claworld_transcript.render_transcript_report(
                cfg,
                {
                    "mode": "manual",
                    "manual": {
                        "title": "Capped height",
                        "peerProfile": "Hard page maximum",
                        "localLabel": "Local",
                        "peerLabel": "Peer",
                        "messages": [
                            {
                                "from": "peer",
                                "text": "A short message keeps the rendered page adaptive.",
                                "createdAt": "2026-07-15T09:00:00Z",
                            }
                        ],
                    },
                    "maxPageHeight": 300000,
                },
            )

            spec = json.loads(Path(result["artifacts"]["bubbleSpec"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(claworld_transcript.MAX_PAGE_HEIGHT, 32000)
            self.assertEqual(spec["canvas"]["maxPageHeight"], 32000)
            self.assertLess(result["artifacts"]["pngPages"][0]["height"], 32000)

    def test_manual_report_paginates_long_conversation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"HERMES_HOME": str(Path(tmp) / "hermes")},
            clear=False,
        ):
            cfg = ClaworldConfig(agent_id="agent-local", working_memory_root=str(Path(tmp) / ".claworld"))
            messages = []
            for idx in range(12):
                messages.extend(
                    [
                        {
                            "from": "peer",
                            "text": f"Round {idx + 1}: peer message with 中文 and enough English text to wrap cleanly.",
                            "createdAt": str(1700000000 + idx * 360),
                        },
                        {
                            "from": "local",
                            "text": "Local response with enough detail to exercise visual pagination.",
                            "createdAt": str(1700000001 + idx * 360),
                        },
                    ]
                )

            result = claworld_transcript.render_transcript_report(
                cfg,
                {
                    "mode": "manual",
                    "manual": {
                        "chatMode": "world",
                        "worldName": "分页测试世界",
                        "reportType": "full",
                        "initiatedBy": "peer",
                        "topic": "长对话分页",
                        "title": "Paging test",
                        "peerProfile": "Peer profile",
                        "localLabel": "local-agent#LOCAL01",
                        "peerLabel": "peer-agent#PEER01",
                        "messages": messages,
                    },
                    "maxPageHeight": 980,
                },
            )

            self.assertGreater(result["pageCount"], 3)
            self.assertEqual(len(result["artifacts"]["pngPages"]), result["pageCount"])
            self.assertEqual(len(result["artifacts"]["svgPages"]), result["pageCount"])
            self.assertTrue(
                all(page["height"] <= 980 for page in result["artifacts"]["pngPages"])
            )
            self.assertEqual(
                result["deliveryHint"]["primaryMediaBatch"].splitlines(),
                [
                    "[[as_document]]",
                    *(page["mediaRef"] for page in result["artifacts"]["pngPages"]),
                ],
            )
            svg_pages = [
                Path(page["path"]).read_text(encoding="utf-8")
                for page in result["artifacts"]["svgPages"]
            ]
            self.assertIn('class="conversation-passport conversation-passport-full"', svg_pages[0])
            self.assertNotIn("conversation-passport-compact", svg_pages[0])
            self.assertEqual(svg_pages[0].count('class="passport-context-field'), 1)
            self.assertIn("Their role here", svg_pages[0])
            self.assertNotIn("About this world", svg_pages[0])
            self.assertIn(">FULL · 24 MSGS</text>", svg_pages[0])
            self.assertIn('class="conversation-relation relation-peer"', svg_pages[0])
            self.assertIn(">→</text>", svg_pages[0])
            self.assertNotIn("STARTED", svg_pages[0])
            self.assertIn(">PEER-AGENT</text>", "\n".join(svg_pages))
            self.assertIn(">LOCAL-AGENT</text>", "\n".join(svg_pages))
            self.assertNotIn(">PEER-AGENT#PEER01</text>", "\n".join(svg_pages))
            self.assertNotIn(">LOCAL-AGENT#LOCAL01</text>", "\n".join(svg_pages))
            for page_no, svg in enumerate(svg_pages[1:], start=2):
                self.assertIn('class="conversation-passport conversation-passport-compact"', svg)
                self.assertIn("WORLD CHAT", svg)
                self.assertIn(f"{page_no} / {result['pageCount']}", svg)
                self.assertIn('class="conversation-relation relation-peer"', svg)
                self.assertIn(">→</text>", svg)
                self.assertIn("<title>peer-agent#PEER01</title>", svg)
                self.assertIn(">#PEER01</text>", svg)
                self.assertIn("<title>local-agent#LOCAL01</title>", svg)
                self.assertIn(">#LOCAL01</text>", svg)
                compact_root = ET.fromstring(svg)
                compact_codes = [
                    node
                    for node in compact_root.iter()
                    if "identity-code" in node.attrib.get("class", "").split()
                ]
                compact_names = [
                    node
                    for node in compact_root.iter()
                    if "identity-name" in node.attrib.get("class", "").split()
                ]
                self.assertEqual(len(compact_names), 2)
                self.assertEqual(len(compact_codes), 2)
                self.assertTrue(any((node.text or "").startswith("peer-") for node in compact_names))
                self.assertTrue(any((node.text or "").startswith("loc") for node in compact_names))
                self.assertTrue(
                    all(
                        node.attrib["font-size"]
                        == str(claworld_comic_grid.IDENTITY_COMPACT_NAME_FONT_SIZE)
                        and node.attrib["font-weight"] == "900"
                        and node.attrib["fill"] == "#090909"
                        for node in compact_names
                    )
                )
                self.assertTrue(
                    all(
                        node.attrib["font-size"]
                        == str(claworld_comic_grid.IDENTITY_COMPACT_CODE_FONT_SIZE)
                        and node.attrib["font-weight"] == "800"
                        and node.attrib["fill"] == "#68645F"
                        for node in compact_codes
                    )
                )
                self.assertNotIn('class="conversation-context', svg)

    def test_local_episode_summary_counts_visible_directions(self):
        cfg = ClaworldConfig(agent_id="agent-local")
        summaries = claworld_tools._local_episode_summaries(
            cfg,
            {
                "conversationEpisodes": {
                    "req-1": {
                        "chatRequestId": "req-1",
                        "deliveries": [
                            {"direction": "inbound", "deliveryType": "turn", "commandText": "peer message"},
                            {"direction": "outbound", "deliveryType": "reply", "commandText": "local reply"},
                            {
                                "direction": "inbound",
                                "deliveryType": "turn",
                                "commandText": "◐ Session automatically reset (inactive for 24h).",
                            },
                            {"direction": "inbound", "deliveryType": "kickoff", "commandText": "backend command"},
                        ],
                    }
                }
            },
        )

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["renderableMessages"], 2)
        self.assertEqual(summaries[0]["peerMessages"], 1)
        self.assertEqual(summaries[0]["localMessages"], 1)

    def test_header_scope_uses_exact_direct_suffix_and_ignores_untrusted_scope(self):
        world = claworld_transcript._extract_transcript_header_context(
            [
                {
                    "untrustedContext": "- Mode: `direct`\n- World: Fake (`wld-fake`)",
                    "worldId": "wld-direct-demo",
                }
            ],
            {"conversationKey": "conversation:pair:a::b:world:wld-direct-demo"},
        )
        direct = claworld_transcript._extract_transcript_header_context(
            [],
            {"conversationKey": "conversation:pair:a::b:direct"},
        )

        self.assertEqual(world["conversationMode"], "world")
        self.assertEqual(world["worldId"], "wld-direct-demo")
        self.assertNotEqual(world.get("worldName"), "Fake")
        self.assertEqual(direct["conversationMode"], "direct")

    def test_single_oversized_message_splits_across_pages_and_sanitizes_xml(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"HERMES_HOME": str(Path(tmp) / "hermes")},
            clear=False,
        ):
            cfg = ClaworldConfig(agent_id="agent-local", working_memory_root=str(Path(tmp) / ".claworld"))
            long_text = "START\x0b " + " ".join(f"segment-{index:03d}" for index in range(260)) + " END [[like]]"
            result = claworld_transcript.render_transcript_report(
                cfg,
                {
                    "mode": "manual",
                    "maxPageHeight": 900,
                    "manual": {
                        "chatMode": "direct",
                        "localIdentity": "Local#LOCAL1",
                        "peerIdentity": "Peer#PEER01",
                        "messages": [{"from": "peer", "text": long_text}],
                    },
                },
            )

            self.assertGreater(result["pageCount"], 1)
            svgs = [Path(page["path"]).read_text(encoding="utf-8") for page in result["artifacts"]["svgPages"]]
            for page, svg in zip(result["artifacts"]["svgPages"], svgs):
                self.assertLessEqual(page["height"], 900)
                ET.fromstring(svg)
            rendered = "\n".join(svgs)
            self.assertEqual(claworld_stylekit.sanitize_xml_text("START\x0bEND"), "START�END")
            self.assertIn("START", rendered)
            self.assertNotIn("\x0b", rendered)
            self.assertIn("END", rendered)
            self.assertGreater(rendered.count('class="message-row left"'), 1)
            self.assertEqual(rendered.count("tag-like"), 1)
            self.assertIn("tag-like", svgs[-1])

    def test_complex_script_layout_budgets_match_final_raster_bounds(self):
        self.assertGreaterEqual(claworld_stylekit.text_units("அ"), 2.5)
        self.assertGreaterEqual(claworld_stylekit.text_units("అ"), 2.1)
        self.assertGreaterEqual(claworld_stylekit.text_units("ಅ"), 2.0)
        self.assertGreaterEqual(claworld_stylekit.text_units("﷽"), 10.0)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"HERMES_HOME": str(Path(tmp) / "hermes")},
            clear=False,
        ):
            cfg = ClaworldConfig(agent_id="agent-local", working_memory_root=str(Path(tmp) / ".claworld"))
            result = claworld_transcript.render_transcript_report(
                cfg,
                {
                    "mode": "manual",
                    "manual": {
                        "messages": [
                            {"from": "peer", "text": glyph * 28}
                            for glyph in ("அ", "అ", "ಅ", "﷽")
                        ]
                    },
                },
            )

            png_by_page = {item["page"]: item for item in result["artifacts"]["pngPages"]}
            for svg_page in result["artifacts"]["svgPages"]:
                root = ET.fromstring(Path(svg_page["path"]).read_text(encoding="utf-8"))
                _width, _height, rows = decode_resvg_rgba_png(Path(png_by_page[svg_page["page"]]["path"]))
                for group in root.iter("{http://www.w3.org/2000/svg}g"):
                    if "message-row" not in group.attrib.get("class", "").split():
                        continue
                    rects = [node for node in list(group) if node.tag == "{http://www.w3.org/2000/svg}rect"]
                    foreground = rects[2]
                    x = int(float(foreground.attrib["x"]))
                    y = int(float(foreground.attrib["y"]))
                    width = int(float(foreground.attrib["width"]))
                    height = int(float(foreground.attrib["height"]))
                    sample_y = range(y + 28, min(len(rows), y + height - 5))
                    outside_x = [*range(28, max(28, x - 3)), *range(min(700, x + width + 16), 700)]
                    dark_pixels = sum(
                        1
                        for py in sample_y
                        for px in outside_x
                        if rows[py][px * 4] < 55
                        and rows[py][px * 4 + 1] < 55
                        and rows[py][px * 4 + 2] < 55
                        and rows[py][px * 4 + 3] > 180
                    )
                    self.assertEqual(dark_pixels, 0)

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

    def test_is_connected_requires_activation_token_for_setup_status(self):
        plugin = import_plugin_entry_with_gateway_shim()
        cfg = types.SimpleNamespace(extra={})

        with patch.dict(os.environ, {"CLAWORLD_SERVER_URL": "https://api.example.com"}, clear=True):
            self.assertIs(plugin._validate_config(cfg), False)

        with patch.dict(os.environ, {"CLAWORLD_APP_TOKEN": "tok"}, clear=True):
            self.assertIs(plugin._validate_config(cfg), True)


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
        help_description = next(
            description for name, _path, description in registered if name == "claworld-help"
        )
        self.assertIn("upgrade", help_description)

    def test_claworld_skills_are_hermes_native(self):
        for skill_name in claworld_skills.SKILL_DESCRIPTIONS:
            path = ROOT / "skills" / skill_name / "SKILL.md"
            text = path.read_text(encoding="utf-8")
            self.assertIn(f"name: {skill_name}", text)
            self.assertNotIn("OpenClaw", text)
            self.assertNotIn("openclaw", text)
            self.assertNotIn("sessions_send", text)
            self.assertNotIn("claworld_report_owner", text)
        management = (ROOT / "skills" / "claworld-management-session" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("You are currently acting as the private Claworld Manager for your human.", management)
        self.assertIn("You may initiate multiple chats at once.", management)
        self.assertIn("Always report the outcome to the human", management)
        self.assertIn("value affects length, not whether to report", management)
        self.assertIn("use the notification's exact `chatRequestId`", management)
        self.assertIn("Process every delivered conversation-ended notification", management)
        self.assertNotIn("has already been reported successfully", management)
        self.assertIn("Use `claworld_send_message` once when a report should go to the human.", management)
        self.assertIn("claworld_send_message(", management)
        self.assertIn("`mirrored: true` means the Main Session transcript received the report", management)
        self.assertIn("Include every rendered page", management)
        self.assertIn("write `[[as_document]]` once", management)
        self.assertIn("append each `artifacts.pngPages[].mediaRef`", management)
        self.assertIn("together in one `claworld_send_message` call", management)
        self.assertNotIn("first 3", management)
        self.assertIn("`approval_required` is review mode", management)
        self.assertIn("Accept, reject, or ask the human", management)
        self.assertIn("No request, review, or accept/reject action reaches you", management)
        self.assertIn("#### What To Report", management)
        self.assertIn("Every conversation-ended report includes a text summary and a transcript image", management)
        self.assertIn("The same person can matter differently in different", management)
        self.assertNotIn("ANNOUNCE_READY", management)
        self.assertNotIn("report artifact exists when owner reporting was needed", management)
        main = (ROOT / "skills" / "claworld-main-session" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Main Session owns the review instructions", main)
        self.assertIn("participantContextField", main)
        self.assertIn("inspect the resolved person and exact direct/world scope", main)
        self.assertIn("If the backend returns `conversation_already_active`, do not retry", main)
        self.assertIn("backend returns `conversation_already_active`", management)
        self.assertIn("do not retry or create a", management)
        self.assertIn("Select only visible original messages", main)
        self.assertIn("writes local SVG and PNG files", main)
        for skill in (management, main):
            self.assertIn(
                '`{"mode":"stored","chatRequestId":"req_...","topic":"<short exact-episode topic>"}`',
                skill,
            )
            self.assertIn("every stored-mode fallback at the top level", skill)
            self.assertIn('top-level `initiatedBy="local"|"peer"`', skill)
            self.assertIn("what was actually discussed", skill)
            self.assertIn("anything unrelated to the content", skill)
            self.assertIn(
                "Always provide top-level `topic` after reading the exact episode",
                skill,
            )
            self.assertIn(
                "only to supply known public context missing from the indexed kickoff",
                skill,
            )
            self.assertIn(
                "Every new Agent call supplies `manual.messages` and one short `manual.topic`",
                skill,
            )
            self.assertIn("`manual.peerProfile` means the Peer World Membership Profile", skill)
            self.assertNotIn("`stored.chatRequestId`", skill)
            self.assertNotIn("`stored.topic`", skill)
            self.assertNotIn("`stored.initiatedBy", skill)
        self.assertNotIn("send no header overrides", main)
        self.assertNotIn("Leave those header fields unchanged", management)
        self.assertIn("`.claworld/context/PROFILE.md`", main)
        self.assertIn("`.claworld/context/NOW.md`", main)
        self.assertIn("host-wide or generic user memory", main)
        self.assertIn("attach every rendered PNG", main)
        self.assertIn("writing `[[as_document]]` once", main)
        self.assertIn("Do not omit later pages", main)
        self.assertNotIn("first 3", main)
        self.assertNotIn("send_message", main)
        self.assertIn("Before installing, upgrading", main)
        help_skill = (ROOT / "skills" / "claworld-help" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn('claworld_manage_account(action="view_account")', help_skill)
        self.assertIn("`upgradeCommand`", help_skill)
        self.assertIn("send `/restart`", help_skill)
        self.assertIn("Hermes Agent runtime update", help_skill)

    def test_manage_worlds_skill_requires_broadcast_confirmation_preview(self):
        text = (ROOT / "skills" / "claworld-manage-worlds" / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("World Operation Confirmation", text)
        self.assertIn("material for the draft", text)
        self.assertIn("`publish_broadcast`", text)
        self.assertIn("Keep field names like", text)
        self.assertIn("list_broadcast_history", text)

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
        self.assertIs(registered["platforms"][0]["setup_fn"], plugin.interactive_setup)
        self.assertIs(registered["platforms"][0]["is_connected"], plugin._validate_config)
        self.assertEqual(len(registered["tools"]), 7)
        self.assertIn("claworld_send_message", {entry["name"] for entry in registered["tools"]})
        self.assertEqual(len(registered["skills"]), 4)
        self.assertEqual({name for name, _path, _description in registered["skills"]}, set(claworld_skills.SKILL_DESCRIPTIONS))
        self.assertEqual([name for name, _handler in registered["hooks"]], ["post_tool_call"])


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_management_notification_idempotency_uses_notification_identity(self):
        adapter_module = import_adapter_with_gateway_shim()

        with tempfile.TemporaryDirectory() as tmp:
            memory_root = Path(tmp) / ".claworld"
            config = types.SimpleNamespace(
                extra={
                    "server_url": "https://api.example.com",
                    "app_token": "tok",
                    "working_memory_root": str(memory_root),
                }
            )
            adapter = adapter_module.ClaworldPlatformAdapter(config)
            handled = []

            async def fake_handle_message(event):
                handled.append(event)
                await asyncio.sleep(0.02)

            adapter.handle_message = fake_handle_message

            def management_notification(notification_id: str, chat_request_id: str):
                return build_inbound_envelope(
                    {
                        "event": "conversation_ended",
                        "data": {
                            "eventType": "notification",
                            "eventName": "conversation_ended",
                            "inboxItemId": f"notification:{notification_id}",
                            "sessionKind": "management",
                            "sessionKey": "management:agent-1",
                            "targetAgentId": "agent-1",
                            "text": f"Conversation ended: {chat_request_id}",
                            "notification": {
                                "notificationId": notification_id,
                                "relatedObjects": {
                                    "chatRequestId": chat_request_id,
                                    "conversationKey": "pair:agent-1::agent-2:direct",
                                },
                            },
                        },
                    }
                )

            first = management_notification("ntf-first", "req-first")
            await asyncio.gather(adapter._on_delivery(first), adapter._on_delivery(first))
            self.assertEqual(len(handled), 1)
            self.assertEqual(first.chat_request_id, "req-first")

            restarted_adapter = adapter_module.ClaworldPlatformAdapter(config)
            restarted_adapter.handle_message = fake_handle_message
            await restarted_adapter._on_delivery(first)
            self.assertEqual(len(handled), 1)

            second = management_notification("ntf-second", "req-second")
            await restarted_adapter._on_delivery(second)
            self.assertEqual(len(handled), 2)
            self.assertEqual(second.conversation_key, first.conversation_key)
            self.assertNotEqual(second.chat_request_id, first.chat_request_id)

    async def test_failed_management_notification_is_retryable(self):
        adapter_module = import_adapter_with_gateway_shim()

        with tempfile.TemporaryDirectory() as tmp:
            adapter = adapter_module.ClaworldPlatformAdapter(
                types.SimpleNamespace(
                    extra={
                        "server_url": "https://api.example.com",
                        "app_token": "tok",
                        "working_memory_root": str(Path(tmp) / ".claworld"),
                    }
                )
            )
            attempts = 0

            async def fake_handle_message(_event):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("temporary model failure")

            adapter.handle_message = fake_handle_message
            envelope = build_inbound_envelope(
                {
                    "event": "conversation_ended",
                    "data": {
                        "eventType": "notification",
                        "notificationId": "ntf-retry",
                        "sessionKind": "management",
                        "sessionKey": "management:agent-1",
                        "targetAgentId": "agent-1",
                        "text": "Conversation ended: req-retry",
                    },
                }
            )

            with self.assertRaisesRegex(RuntimeError, "temporary model failure"):
                await adapter._on_delivery(envelope)
            await adapter._on_delivery(envelope)
            self.assertEqual(attempts, 2)

    def setUp(self):
        self.hermes_home = tempfile.TemporaryDirectory()
        self.addCleanup(self.hermes_home.cleanup)
        self.hermes_home_env = patch.dict(os.environ, {"HERMES_HOME": self.hermes_home.name}, clear=False)
        self.hermes_home_env.start()
        self.addCleanup(self.hermes_home_env.stop)

    def test_connect_accepts_gateway_reconnect_kwarg(self):
        adapter_module = import_adapter_with_gateway_shim()

        signature = inspect.signature(adapter_module.ClaworldPlatformAdapter.connect)
        parameter = signature.parameters.get("is_reconnect")

        self.assertIsNotNone(parameter)
        self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(parameter.default, False)

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

    async def test_acknowledged_reply_is_added_to_structured_transcript(self):
        adapter_module = import_adapter_with_gateway_shim()

        class FakeRelayClient:
            async def send_reply(self, delivery_id, session_key, reply_text):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            memory_root = Path(tmp) / ".claworld"
            adapter = adapter_module.ClaworldPlatformAdapter(
                types.SimpleNamespace(
                    extra={
                        "server_url": "https://api.example.com",
                        "app_token": "tok",
                        "agent_id": "agent-local",
                        "working_memory_root": str(memory_root),
                    }
                )
            )
            adapter.client = FakeRelayClient()
            envelope = build_inbound_envelope(
                {
                    "event": "delivery",
                    "data": {
                        "deliveryId": "d-transcript",
                        "sessionKey": "conversation:abc",
                        "chatRequestId": "req-transcript",
                        "payload": {
                            "commandText": "peer message",
                            "fromAgentId": "agent-peer",
                        },
                    },
                }
            )
            route = route_envelope(envelope, adapter.claworld_config)
            record_claworld_route(memory_root, route, build_hermes_session_key(route), envelope)
            record = adapter_module.DeliveryRecord(
                delivery_id="d-transcript",
                relay_session_key="conversation:abc",
                chat_id=route.chat_id,
                chat_request_id="req-transcript",
            )
            adapter._deliveries_by_id[record.delivery_id] = record
            adapter._latest_by_chat[record.chat_id] = record.delivery_id

            result = await adapter.send(record.chat_id, "local reply")

            self.assertTrue(result.success)
            deliveries = read_session_index(memory_root)["conversationEpisodes"]["req-transcript"]["deliveries"]
            self.assertEqual([item["commandText"] for item in deliveries], ["peer message", "local reply"])
            self.assertEqual(deliveries[-1]["fromAgentId"], "agent-local")
            self.assertEqual(deliveries[-1]["deliveryType"], "reply")

    async def test_reply_delivery_stays_successful_when_local_index_write_fails(self):
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
            chat_request_id="req-1",
        )
        adapter._deliveries_by_id[record.delivery_id] = record
        adapter._latest_by_chat[record.chat_id] = record.delivery_id

        with patch.object(adapter_module, "record_outbound_reply", side_effect=OSError("disk full")):
            result = await adapter.send(record.chat_id, "real reply")

        self.assertTrue(result.success)
        self.assertTrue(record.replied)
        self.assertEqual(adapter.client.replies, [("d1", "conversation:abc", "real reply")])

    async def test_hermes_transient_status_does_not_consume_replyable_delivery(self):
        adapter_module = import_adapter_with_gateway_shim()

        class FakeRelayClient:
            def __init__(self):
                self.replies = []
                self.silences = []

            async def send_reply(self, delivery_id, session_key, reply_text):
                self.replies.append((delivery_id, session_key, reply_text))

            async def send_kept_silent(self, delivery_id, session_key, reason):
                self.silences.append((delivery_id, session_key, reason))

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

        for notice in (
            "⏳ Working — 3 min — iteration 1/90, waiting for non-streaming API response",
            "🔄 Primary model failed — switching to fallback: gpt-5.5 via openai-codex",
        ):
            notice_result = await adapter.send(record.chat_id, notice)
            self.assertTrue(notice_result.success)
            self.assertFalse(record.replied)

        self.assertEqual(adapter.client.replies, [])
        self.assertEqual(adapter.client.silences, [])

        reply_result = await adapter.send(record.chat_id, "real peer-visible reply")

        self.assertTrue(reply_result.success)
        self.assertTrue(record.replied)
        self.assertEqual(adapter.client.replies, [("d1", "conversation:abc", "real peer-visible reply")])
        self.assertEqual(adapter.client.silences, [])

    async def test_runtime_error_reply_defers_kept_silent(self):
        adapter_module = import_adapter_with_gateway_shim()

        class FakeRelayClient:
            def __init__(self):
                self.replies = []
                self.silences = []

            async def send_reply(self, delivery_id, session_key, reply_text):
                self.replies.append((delivery_id, session_key, reply_text))

            async def send_kept_silent(self, delivery_id, session_key, reason):
                self.silences.append((delivery_id, session_key, reason))

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

        result = await adapter.send(record.chat_id, "LLM request failed: provider unavailable")

        self.assertTrue(result.success)
        self.assertFalse(record.replied)
        self.assertTrue(record.saw_operational_notice)
        self.assertEqual(adapter.client.replies, [])
        self.assertEqual(adapter.client.silences, [])

    async def test_operational_notice_reply_defers_kept_silent(self):
        adapter_module = import_adapter_with_gateway_shim()

        class FakeRelayClient:
            def __init__(self):
                self.replies = []
                self.silences = []

            async def send_reply(self, delivery_id, session_key, reply_text):
                self.replies.append((delivery_id, session_key, reply_text))

            async def send_kept_silent(self, delivery_id, session_key, reason):
                self.silences.append((delivery_id, session_key, reason))

        adapter = adapter_module.ClaworldPlatformAdapter(
            types.SimpleNamespace(extra={"server_url": "https://api.example.com", "app_token": "tok"})
        )
        adapter.client = FakeRelayClient()
        record = adapter_module.DeliveryRecord(
            delivery_id="d2",
            relay_session_key="conversation:def",
            chat_id="conversation-def",
        )
        adapter._deliveries_by_id[record.delivery_id] = record
        adapter._latest_by_chat[record.chat_id] = record.delivery_id

        result = await adapter.send(record.chat_id, "Sent the Claworld reply.\nUsage: 1 in / 2 out")

        self.assertTrue(result.success)
        self.assertFalse(record.replied)
        self.assertTrue(record.saw_operational_notice)
        self.assertEqual(adapter.client.replies, [])
        self.assertEqual(adapter.client.silences, [])

    async def test_operational_notice_then_real_reply_sends_real_reply(self):
        adapter_module = import_adapter_with_gateway_shim()

        class FakeRelayClient:
            def __init__(self):
                self.replies = []
                self.silences = []

            async def send_reply(self, delivery_id, session_key, reply_text):
                self.replies.append((delivery_id, session_key, reply_text))

            async def send_kept_silent(self, delivery_id, session_key, reason):
                self.silences.append((delivery_id, session_key, reason))

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

        notice_result = await adapter.send(record.chat_id, "\U0001f9f9 Auto-compaction complete (count 1).")
        self.assertTrue(notice_result.success)
        self.assertFalse(record.replied)
        self.assertTrue(record.saw_operational_notice)

        reply_result = await adapter.send(record.chat_id, "real peer-visible reply")
        self.assertTrue(reply_result.success)
        self.assertTrue(record.replied)
        self.assertEqual(adapter.client.replies, [("d1", "conversation:abc", "real peer-visible reply")])
        self.assertEqual(adapter.client.silences, [])

    async def test_kickoff_delivery_retries_on_operational_notice_only(self):
        adapter_module = import_adapter_with_gateway_shim()

        class FakeRelayClient:
            def __init__(self):
                self.replies = []
                self.silences = []

            async def send_accepted(self, delivery_id, session_key):
                pass

            async def send_reply(self, delivery_id, session_key, reply_text):
                self.replies.append((delivery_id, session_key, reply_text))

            async def send_kept_silent(self, delivery_id, session_key, reason):
                self.silences.append((delivery_id, session_key, reason))

        adapter = adapter_module.ClaworldPlatformAdapter(
            types.SimpleNamespace(extra={"server_url": "https://api.example.com", "app_token": "tok"})
        )
        adapter.client = FakeRelayClient()
        handled_count = 0

        async def fake_handle_message(event):
            nonlocal handled_count
            handled_count += 1
            if handled_count == 1:
                await adapter.send(event.source.chat_id, "\U0001f9f9 Auto-compaction complete (count 1).")

        adapter.handle_message = fake_handle_message

        envelope = build_inbound_envelope(
            {
                "event": "delivery",
                "data": {
                    "deliveryId": "d1",
                    "sessionKey": "conversation:abc",
                    "payload": {"text": "kickoff body"},
                    "metadata": {"deliveryType": "kickoff"},
                },
            }
        )

        await adapter._on_delivery(envelope)
        self.assertEqual(handled_count, 1)
        self.assertTrue(adapter._deliveries_by_id["d1"].saw_operational_notice)
        self.assertFalse(adapter._deliveries_by_id["d1"].replied)

        await adapter.on_processing_complete(
            types.SimpleNamespace(message_id="d1"),
            adapter_module.ProcessingOutcome.SUCCESS,
        )
        self.assertEqual(handled_count, 2)
        self.assertTrue(adapter._deliveries_by_id["d1"].retried)

    async def test_reply_strips_operational_usage_suffix(self):
        adapter_module = import_adapter_with_gateway_shim()

        class FakeRelayClient:
            def __init__(self):
                self.replies = []
                self.silences = []

            async def send_reply(self, delivery_id, session_key, reply_text):
                self.replies.append((delivery_id, session_key, reply_text))

            async def send_kept_silent(self, delivery_id, session_key, reason):
                self.silences.append((delivery_id, session_key, reason))

        adapter = adapter_module.ClaworldPlatformAdapter(
            types.SimpleNamespace(extra={"server_url": "https://api.example.com", "app_token": "tok"})
        )
        adapter.client = FakeRelayClient()
        record = adapter_module.DeliveryRecord(
            delivery_id="d3",
            relay_session_key="conversation:ghi",
            chat_id="conversation-ghi",
        )
        adapter._deliveries_by_id[record.delivery_id] = record
        adapter._latest_by_chat[record.chat_id] = record.delivery_id

        result = await adapter.send(record.chat_id, "real reply\nUsage: 1 in / 2 out")

        self.assertTrue(result.success)
        self.assertTrue(record.replied)
        self.assertEqual(adapter.client.replies, [("d3", "conversation:ghi", "real reply")])
        self.assertEqual(adapter.client.silences, [])

    async def test_acceptance_failure_does_not_block_delivery_handling(self):
        adapter_module = import_adapter_with_gateway_shim()

        class FakeRelayClient:
            async def send_accepted(self, delivery_id, session_key):
                raise RuntimeError("ack unavailable")

            async def send_kept_silent(self, delivery_id, session_key, reason):
                raise AssertionError("kept_silent should not be sent")

        adapter = adapter_module.ClaworldPlatformAdapter(
            types.SimpleNamespace(extra={"server_url": "https://api.example.com", "app_token": "tok"})
        )
        adapter.client = FakeRelayClient()
        handled = []

        async def fake_handle_message(event):
            handled.append(event)

        adapter.handle_message = fake_handle_message
        envelope = build_inbound_envelope(
            {
                "event": "delivery",
                "data": {
                    "deliveryId": "d4",
                    "sessionKey": "conversation:abc",
                    "payload": {"text": "hello"},
                    "metadata": {},
                },
            }
        )

        with self.assertLogs(adapter_module.logger, level="WARNING") as logs:
            await adapter._on_delivery(envelope)

        self.assertEqual(len(handled), 1)
        self.assertIn("hello", handled[0].text)
        self.assertTrue(any("failed to acknowledge Claworld delivery acceptance" in item for item in logs.output))

    async def test_delivery_events_include_claworld_channel_prompt(self):
        adapter_module = import_adapter_with_gateway_shim()

        class FakeRelayClient:
            async def send_accepted(self, delivery_id, session_key):
                return None

            async def send_kept_silent(self, delivery_id, session_key, reason):
                raise AssertionError("kept_silent should not be sent")

        with tempfile.TemporaryDirectory() as tmp:
            adapter = adapter_module.ClaworldPlatformAdapter(
                types.SimpleNamespace(
                    extra={
                        "server_url": "https://api.example.com",
                        "app_token": "tok",
                        "working_memory_root": str(Path(tmp) / ".claworld"),
                    }
                )
            )
            adapter.client = FakeRelayClient()
            handled = []

            async def fake_handle_message(event):
                handled.append(event)

            adapter.handle_message = fake_handle_message

            conversation = build_inbound_envelope(
                {
                    "event": "delivery",
                    "data": {
                        "deliveryId": "d-channel-conversation",
                        "sessionKey": "conversation:abc",
                        "payload": {
                            "commandText": "Thanks for the thoughtful exchange.",
                            "untrustedContext": [
                                "conversation formally ended after mutual agreement",
                                "deliveryId=d-channel-conversation",
                            ],
                        },
                    },
                }
            )
            management = build_inbound_envelope(
                {
                    "event": "delivery",
                    "data": {
                        "deliveryId": "d-channel-management",
                        "sessionKey": "management:agent-1",
                        "payload": {"sessionKind": "management", "text": "wake"},
                    },
                }
            )

            await adapter._on_delivery(conversation)
            await adapter._on_delivery(management)

        self.assertEqual(len(handled), 2)
        self.assertTrue(handled[0].source.role_authorized)
        self.assertTrue(handled[1].source.role_authorized)
        conversation_prompt = handled[0].channel_prompt
        management_prompt = handled[1].channel_prompt
        self.assertEqual(handled[0].text, "Thanks for the thoughtful exchange.")
        self.assertNotIn("deliveryId", handled[0].text)
        self.assertIn("# Claworld Conversation Startup Context", conversation_prompt)
        self.assertIn("## `.claworld/context/NOW.md`", conversation_prompt)
        self.assertIn("## `.claworld/context/MEMORY.md`", conversation_prompt)
        self.assertIn("## `.claworld/context/PROFILE.md`", conversation_prompt)
        self.assertIn(
            "You should never report your activity to the human or modify the claworld working memory.",
            conversation_prompt,
        )
        self.assertNotIn("claworld:claworld-main-session", conversation_prompt)
        self.assertNotIn("sessions/index.json summary", conversation_prompt)
        self.assertIn("This episode has formally ended", conversation_prompt)
        self.assertIn("return exactly `NO_REPLY`", conversation_prompt)
        self.assertNotIn("d-channel-conversation", conversation_prompt)
        self.assertTrue(management_prompt.startswith("## Your Role"))
        self.assertIn("You are currently acting as the private Claworld Manager", management_prompt)
        self.assertFalse(management_prompt.startswith("---"))
        self.assertNotIn("description: |", management_prompt)
        self.assertNotIn("metadata:", management_prompt)
        self.assertNotIn("# Claworld Management Startup Memory", management_prompt)
        self.assertIn("# Claworld Working Memory Root", management_prompt)
        self.assertIn("Configured root:", management_prompt)
        self.assertIn("# Claworld Working Memory Startup Preview", management_prompt)
        self.assertIn("short, truncated startup index", management_prompt)
        self.assertIn("### `.claworld/context/PROFILE.md`", management_prompt)
        self.assertIn("### `.claworld/context/MEMORY.md`", management_prompt)
        self.assertIn("### `.claworld/context/NOW.md`", management_prompt)
        self.assertNotIn("sessions/index.json summary", management_prompt)

    async def test_handler_failure_marks_replyable_delivery_kept_silent(self):
        adapter_module = import_adapter_with_gateway_shim()

        class FakeRelayClient:
            def __init__(self):
                self.silences = []

            async def send_accepted(self, delivery_id, session_key):
                return None

            async def send_kept_silent(self, delivery_id, session_key, reason):
                self.silences.append((delivery_id, session_key, reason))

        adapter = adapter_module.ClaworldPlatformAdapter(
            types.SimpleNamespace(extra={"server_url": "https://api.example.com", "app_token": "tok"})
        )
        adapter.client = FakeRelayClient()

        async def fake_handle_message(event):
            raise RuntimeError("model unavailable")

        adapter.handle_message = fake_handle_message
        envelope = build_inbound_envelope(
            {
                "event": "delivery",
                "data": {
                    "deliveryId": "d5",
                    "sessionKey": "conversation:def",
                    "payload": {"text": "hello"},
                    "metadata": {},
                },
            }
        )

        with self.assertRaisesRegex(RuntimeError, "model unavailable"):
            await adapter._on_delivery(envelope)

        self.assertEqual(adapter.client.silences, [("d5", "conversation:def", "runtime_failed_before_reply")])


class ToolSchemaTests(unittest.TestCase):
    def test_manage_account_delivers_ready_share_card_to_current_hermes_chat(self):
        cfg = ClaworldConfig(server_url="https://staging.claworld.love", agent_id="agt_moza")
        share_card = {
            "status": "ready",
            "imageUrl": "https://staging.claworld.love/v1/share-card/moza.jpg?token=card-token",
            "downloadUrl": "https://staging.claworld.love/v1/share-card/moza.jpg?token=card-token",
            "variant": "zh",
        }
        downloaded = Path("/tmp/claworld-share-card-moza.jpg")

        with patch(
            "claworld_hermes_plugin.tools.request_json",
            return_value={"status": "ready", "profile": {"status": "ready", "shareCard": share_card}},
        ), patch(
            "claworld_hermes_plugin.tools._augment_account_binding",
            side_effect=lambda payload, **_: payload,
        ), patch(
            "claworld_hermes_plugin.tools._current_hermes_session_context",
            return_value={"platform": "feishu", "chatId": "oc_test", "threadId": "thread-1"},
        ), patch(
            "claworld_hermes_plugin.tools.download_share_card",
            return_value=downloaded,
        ) as download, patch(
            "claworld_hermes_plugin.tools._call_send_message_tool",
            return_value={"success": True, "message_id": "om_image_1", "mirrored": True},
        ) as send:
            result = claworld_tools._manage_account(
                cfg,
                {"action": "view_account", "generateShareCard": True, "shareCardVariant": "zh"},
            )

        download.assert_called_once_with(
            cfg,
            share_card["imageUrl"],
            claworld_tools.hermes_home_path() / "cache" / "images" / "claworld_share_cards",
        )
        send.assert_called_once_with(
            {
                "action": "send",
                "target": "feishu:oc_test:thread-1",
                "message": f"MEDIA:{downloaded}",
            }
        )
        delivered_card = result["profile"]["shareCard"]
        self.assertEqual(delivered_card["delivery"]["status"], "delivered")
        self.assertEqual(delivered_card["delivery"]["messageId"], "om_image_1")
        self.assertIn("只用一句普通文本确认", delivered_card["description"])

    def test_manage_account_fails_clearly_when_share_card_has_no_human_chat_route(self):
        cfg = ClaworldConfig(server_url="https://staging.claworld.love", agent_id="agt_moza")
        with patch(
            "claworld_hermes_plugin.tools.request_json",
            return_value={
                "status": "ready",
                "shareCard": {"status": "ready", "imageUrl": "https://staging.claworld.love/card.jpg"},
            },
        ), patch(
            "claworld_hermes_plugin.tools._augment_account_binding",
            side_effect=lambda payload, **_: payload,
        ), patch(
            "claworld_hermes_plugin.tools._current_hermes_session_context",
            return_value={"platform": "local", "chatId": ""},
        ):
            with self.assertRaisesRegex(RuntimeError, "active human chat route"):
                claworld_tools._manage_account(cfg, {"action": "view_account", "generateShareCard": True})

    def test_tool_schemas_are_hermes_function_schemas(self):
        schemas = [
            claworld_tools.MANAGE_ACCOUNT_SCHEMA,
            claworld_tools.SEARCH_SCHEMA,
            claworld_tools.PUBLIC_PROFILE_SCHEMA,
            claworld_tools.MANAGE_WORLDS_SCHEMA,
            claworld_tools.MANAGE_CONVERSATIONS_SCHEMA,
            claworld_tools.TRANSCRIPT_REPORT_SCHEMA,
            claworld_tools.SEND_MESSAGE_SCHEMA,
        ]
        for schema in schemas:
            self.assertIn("description", schema)
            self.assertIn("parameters", schema)
            self.assertEqual(schema["parameters"]["type"], "object")
            self.assertIn("properties", schema["parameters"])

    def test_conversation_schema_exposes_only_canonical_target_fields(self):
        parameters = claworld_tools.MANAGE_CONVERSATIONS_SCHEMA["parameters"]
        properties = parameters["properties"]

        self.assertFalse(parameters["additionalProperties"])
        self.assertIn("displayName", properties)
        self.assertIn("agentCode", properties)
        self.assertIn("Owner intent for the upcoming chat", properties["openingMessage"]["description"])
        self.assertIn("preferred speaking order", properties["openingMessage"]["description"])
        self.assertIn("Structured owner intent", properties["kickoffBrief"]["description"])
        self.assertNotIn("identity", properties)
        self.assertNotIn("targetAgentId", properties)
        self.assertNotIn("targetId", properties)
        self.assertNotIn("agentId", properties)

    def test_register_tools_passes_function_schema_shape(self):
        registered = []

        class FakeCtx:
            def register_tool(self, **kwargs):
                registered.append(kwargs)

        claworld_tools.register_tools(FakeCtx())

        self.assertEqual(len(registered), 7)
        for entry in registered:
            self.assertIn("parameters", entry["schema"])
            self.assertIn("description", entry["schema"])
            self.assertEqual(entry["schema"]["parameters"]["type"], "object")
            self.assertNotIn("endpoint", entry["schema"]["parameters"]["properties"])
            self.assertTrue(callable(entry["check_fn"]))

    def test_tool_descriptions_route_main_session_to_claworld_skills(self):
        descriptions = [
            claworld_tools.MANAGE_ACCOUNT_DESCRIPTION,
            claworld_tools.SEARCH_DESCRIPTION,
            claworld_tools.PUBLIC_PROFILE_DESCRIPTION,
            claworld_tools.MANAGE_WORLDS_DESCRIPTION,
            claworld_tools.MANAGE_CONVERSATIONS_DESCRIPTION,
        ]

        for description in descriptions:
            self.assertIn('skill_view("claworld:claworld-help")', description)
            self.assertNotIn('skill_view("claworld-main-session")', description)
            self.assertNotIn('skill_view("claworld-help")', description)

        self.assertIn('skill_view("claworld:claworld-main-session")', claworld_tools.SEARCH_DESCRIPTION)
        self.assertIn("preferences or goals", claworld_tools.SEARCH_DESCRIPTION)
        self.assertIn("notification policy", claworld_tools.MANAGE_ACCOUNT_DESCRIPTION)
        self.assertIn("approval_required for Management review", claworld_tools.MANAGE_ACCOUNT_DESCRIPTION)
        self.assertIn('skill_view("claworld:claworld-main-session")', claworld_tools.MANAGE_ACCOUNT_DESCRIPTION)
        self.assertIn('skill_view("claworld:claworld-manage-worlds")', claworld_tools.MANAGE_WORLDS_DESCRIPTION)
        self.assertIn("Before any world operation", claworld_tools.MANAGE_WORLDS_DESCRIPTION)
        self.assertIn("user preferences, boundaries, current goals", claworld_tools.MANAGE_WORLDS_DESCRIPTION)
        self.assertNotIn("broader owner context", claworld_tools.MANAGE_WORLDS_DESCRIPTION)
        self.assertIn("broadcast", claworld_tools.MANAGE_WORLDS_DESCRIPTION)
        self.assertNotIn("draft/preview", claworld_tools.MANAGE_WORLDS_DESCRIPTION)

    def test_manage_account_schema_uses_terminal_policy_fields(self):
        properties = claworld_tools.MANAGE_ACCOUNT_SCHEMA["parameters"]["properties"]

        self.assertEqual(properties["visibilityMode"]["enum"], ["public", "unlisted", "private"])
        self.assertEqual(properties["contactPolicy"]["enum"], ["open", "approval_required", "closed"])
        self.assertIn("Management review", properties["contactPolicy"]["description"])
        self.assertNotIn("discoverable", properties)
        self.assertNotIn("contactable", properties)
        self.assertNotIn("contactMode", properties)
        self.assertNotIn("chatRequestApprovalPolicy", properties)
        self.assertNotIn("chatRequestPolicy", properties)

        action_values = properties["action"]["enum"]
        self.assertIn("set_visibility_mode", action_values)
        self.assertIn("set_contact_policy", action_values)
        self.assertIn("submit_feedback", action_values)
        self.assertEqual(properties["category"]["enum"], ["experience_issue", "usage_issue", "bug_report", "feature_request"])
        self.assertEqual(properties["impact"]["enum"], ["low", "medium", "high", "blocker"])
        self.assertNotIn("set_chat_request_policy", action_values)
        self.assertNotIn("set_discoverability", action_values)
        self.assertNotIn("set_contactability", action_values)
        self.assertNotIn("set_chat_policy", action_values)

    def test_manage_worlds_schema_exposes_pending_invite_inbox(self):
        action_values = claworld_tools.MANAGE_WORLDS_SCHEMA["parameters"]["properties"]["action"]["enum"]
        self.assertIn("list_pending_invites", action_values)

    def test_generic_api_is_opt_in(self):
        with patch.dict(os.environ, {"CLAWORLD_ENABLE_GENERIC_API": ""}, clear=False):
            with self.assertRaisesRegex(ValueError, "CLAWORLD_ENABLE_GENERIC_API"):
                claworld_tools._search(ClaworldConfig(server_url="https://api.example.com", app_token="tok"), {"endpoint": "/v1/search"})


class ClaworldSendMessageToolTests(unittest.TestCase):
    def _cfg(self) -> ClaworldConfig:
        return ClaworldConfig(server_url="https://api.example.com", app_token="tok")

    def test_fallback_mirror_strips_media_delivery_markup(self):
        self.assertEqual(
            claworld_tools._message_for_mirror(
                "Full conversation below:\n\n"
                "[[as_document]]\n"
                "MEDIA:/tmp/transcript-p01.png\n"
                "MEDIA:/tmp/transcript-p02.png"
            ),
            "Full conversation below:",
        )
        self.assertIsNone(
            claworld_tools._message_for_mirror(
                "[[as_document]]\nMEDIA:/tmp/transcript-p01.png"
            )
        )

    def test_send_message_requires_claworld_management_session(self):
        args = {
            "target": "feishu:oc_owner",
            "message": "Owner-visible report",
        }
        rejected_contexts = [
            {},
            {"platform": "feishu", "chatId": "oc_owner"},
            {"platform": "claworld", "chatId": "conversation-abc"},
        ]
        for context in rejected_contexts:
            with self.subTest(context=context), patch(
                "claworld_hermes_plugin.tools.ClaworldConfig.load",
                return_value=self._cfg(),
            ), patch(
                "claworld_hermes_plugin.tools._current_hermes_session_context",
                return_value=context,
            ), patch("claworld_hermes_plugin.tools._send_message") as send:
                result = json.loads(claworld_tools.send_message(args))

            send.assert_not_called()
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error"]["type"], "PermissionError")
            self.assertIn("Management Session", result["error"]["message"])

        with patch(
            "claworld_hermes_plugin.tools.ClaworldConfig.load",
            return_value=self._cfg(),
        ), patch(
            "claworld_hermes_plugin.tools._current_hermes_session_context",
            return_value={"platform": "claworld", "chatId": "management-abc"},
        ), patch(
            "claworld_hermes_plugin.tools._send_message",
            return_value={"status": "delivered", "success": True},
        ) as send:
            result = json.loads(claworld_tools.send_message(args))

        send.assert_called_once_with(self._cfg(), args)
        self.assertEqual(result["status"], "delivered")
        self.assertTrue(result["success"])

    def test_send_message_forwards_to_hermes_and_trusts_auto_mirror(self):
        args = {"action": "send", "target": "feishu:oc_owner:thread-1", "message": "Owner-visible report"}
        with patch("claworld_hermes_plugin.tools._call_send_message_tool", return_value={"success": True, "mirrored": True}) as send, patch(
            "claworld_hermes_plugin.tools._fallback_mirror_send_message"
        ) as mirror:
            result = claworld_tools._send_message(self._cfg(), args)

        send.assert_called_once_with(args)
        mirror.assert_not_called()
        self.assertEqual(result["status"], "delivered")
        self.assertTrue(result["delivered"])
        self.assertTrue(result["mirrored"])
        self.assertTrue(result["autoMirrored"])
        self.assertEqual(result["fallbackMirror"], {"attempted": False})

    def test_send_message_fallback_mirrors_once_when_auto_mirror_is_missing(self):
        args = {"target": "feishu:oc_owner", "message": "Owner-visible report"}
        with patch("claworld_hermes_plugin.tools._call_send_message_tool", return_value={"success": True}) as send, patch(
            "claworld_hermes_plugin.tools._fallback_mirror_send_message",
            return_value={"attempted": True, "success": True, "method": "gateway_mirror"},
        ) as mirror:
            result = claworld_tools._send_message(self._cfg(), args)

        send.assert_called_once_with({"target": "feishu:oc_owner", "message": "Owner-visible report", "action": "send"})
        mirror.assert_called_once_with({"target": "feishu:oc_owner", "message": "Owner-visible report", "action": "send"})
        self.assertEqual(result["status"], "delivered")
        self.assertTrue(result["delivered"])
        self.assertFalse(result["autoMirrored"])
        self.assertTrue(result["mirrored"])
        self.assertEqual(result["fallbackMirror"]["method"], "gateway_mirror")

    def test_send_message_does_not_mirror_when_delivery_fails(self):
        args = {"target": "feishu:oc_owner", "message": "Owner-visible report"}
        with patch("claworld_hermes_plugin.tools._call_send_message_tool", return_value={"success": False, "error": "offline"}) as send, patch(
            "claworld_hermes_plugin.tools._fallback_mirror_send_message"
        ) as mirror:
            result = claworld_tools._send_message(self._cfg(), args)

        send.assert_called_once()
        mirror.assert_not_called()
        self.assertEqual(result["status"], "delivery_failed")
        self.assertFalse(result["delivered"])
        self.assertFalse(result["mirrored"])
        self.assertEqual(result["fallbackMirror"], {"attempted": False})

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

    def test_existing_episode_keeps_chat_bucket_when_delivery_omits_conversation_key(self):
        cfg = ClaworldConfig(account_id="acct", agent_id="agent-1")
        envelope = build_inbound_envelope(
            {
                "event": "delivery",
                "data": {
                    "deliveryId": "c-followup",
                    "sessionKey": "conversation:new-relay-key",
                    "payload": {"chatRequestId": "req-stable", "commandText": "follow up"},
                },
            }
        )
        route = route_envelope(
            envelope,
            cfg,
            existing_episode={
                "chatId": "conversation-existing",
                "conversationKey": "conversation:pair:a::b:world:wld-stable",
            },
        )

        self.assertEqual(route.chat_id, "conversation-existing")
        self.assertEqual(route.conversation_key, "conversation:pair:a::b:world:wld-stable")


class WorkingMemoryTests(unittest.TestCase):
    def test_inbound_notification_claim_supports_completion_release_and_stale_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claworld"
            ensure_working_memory(root)

            first = claim_inbound_notification(root, "ntf-claim", now=100.0)
            self.assertTrue(first["claimed"])
            self.assertEqual(
                claim_inbound_notification(root, "ntf-claim", now=101.0)["reason"],
                "processing",
            )
            complete_inbound_notification(first, now=102.0)
            self.assertEqual(
                claim_inbound_notification(root, "ntf-claim", now=103.0)["reason"],
                "completed",
            )

            retryable = claim_inbound_notification(root, "ntf-release", now=200.0)
            release_inbound_notification(retryable)
            self.assertTrue(claim_inbound_notification(root, "ntf-release", now=201.0)["claimed"])

            stale = claim_inbound_notification(root, "ntf-stale", now=300.0)
            self.assertTrue(stale["claimed"])
            reclaimed = claim_inbound_notification(
                root,
                "ntf-stale",
                now=301.1,
                lease_seconds=1.0,
            )
            self.assertTrue(reclaimed["claimed"])

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
                        "worldId": "world-route-1",
                        "payload": {"chatRequestId": "cr-route-1", "commandText": "hello"},
                    },
                }
            )
            route = route_envelope(envelope, cfg)
            record_claworld_route(root, route, build_hermes_session_key(route), envelope)
            index = read_session_index(root)
            self.assertIn(route.chat_id, index["conversationSessions"])
            episode = index["conversationEpisodes"]["cr-route-1"]
            self.assertEqual(episode["deliveryIds"], ["c1"])
            self.assertEqual(episode["worldId"], "world-route-1")
            self.assertEqual(episode["deliveries"][0]["direction"], "inbound")
            self.assertEqual(episode["deliveries"][0]["worldId"], "world-route-1")
            context = build_prompt_context(root, platform="claworld", chat_id=route.chat_id)
            self.assertIn("# Claworld Conversation Startup Context", context)
            self.assertIn("## `.claworld/context/NOW.md`", context)
            self.assertIn("## `.claworld/context/MEMORY.md`", context)
            self.assertIn("## `.claworld/context/PROFILE.md`", context)
            self.assertIn(
                "You should never report your activity to the human or modify the claworld working memory.",
                context,
            )
            self.assertNotIn('skill_view("claworld:claworld-main-session")', context)
            self.assertNotIn("sessions/index.json summary", context)

    def test_episode_scope_and_direction_are_first_write_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claworld"
            cfg = ClaworldConfig(agent_id="agent-local")
            first = build_inbound_envelope(
                {
                    "event": "delivery",
                    "data": {
                        "deliveryId": "scope-1",
                        "sessionKey": "conversation:one",
                        "conversationKey": "conversation:pair:a::b:world:wld-one",
                        "worldId": "wld-one",
                        "targetAgentId": "agent-local",
                        "payload": {
                            "chatRequestId": "req-scope",
                            "commandText": "first",
                            "metadata": {"fromAgentId": "agent-peer"},
                        },
                    },
                }
            )
            first_route = route_envelope(first, cfg)
            record_claworld_route(root, first_route, build_hermes_session_key(first_route), first)
            existing = read_session_index(root)["conversationEpisodes"]["req-scope"]
            second = build_inbound_envelope(
                {
                    "event": "delivery",
                    "data": {
                        "deliveryId": "scope-2",
                        "sessionKey": "conversation:two",
                        "conversationKey": "conversation:pair:a::b:direct",
                        "worldId": "wld-two",
                        "targetAgentId": "agent-other",
                        "payload": {"chatRequestId": "req-scope", "commandText": "second"},
                    },
                }
            )
            second_route = route_envelope(second, cfg, existing_episode=existing)
            record_claworld_route(root, second_route, build_hermes_session_key(second_route), second)
            record_chat_request_direction(
                root,
                "req-scope",
                "inbound",
                viewer_agent_id="agent-local",
                viewer_account_id="acct-local",
            )
            record_chat_request_direction(
                root,
                "req-scope",
                "outbound",
                viewer_agent_id="agent-local",
                viewer_account_id="acct-local",
            )

            episode = read_session_index(root)["conversationEpisodes"]["req-scope"]
            self.assertEqual(episode["conversationMode"], "world")
            self.assertEqual(episode["worldId"], "wld-one")
            self.assertEqual(episode["targetAgentId"], "agent-local")
            self.assertEqual(episode["peerAgentId"], "agent-peer")
            self.assertEqual(episode["requestDirection"], "inbound")
            self.assertEqual(episode["directionViewerAccountId"], "acct-local")
            self.assertTrue(any(item["field"] == "worldId" for item in episode["scopeConflicts"]))
            self.assertTrue(any(item["field"] == "requestDirection" for item in episode["directionConflicts"]))

    def test_outbound_reply_recording_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claworld"
            cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok", agent_id="agent-local")
            envelope = build_inbound_envelope(
                {
                    "event": "delivery",
                    "data": {
                        "deliveryId": "c1",
                        "sessionKey": "conversation:remote-a",
                        "chatRequestId": "req-1",
                        "payload": {"commandText": "hello", "fromAgentId": "agent-peer"},
                    },
                }
            )
            route = route_envelope(envelope, cfg)
            record_claworld_route(root, route, build_hermes_session_key(route), envelope)

            for _ in range(2):
                record_outbound_reply(
                    root,
                    chat_request_id="req-1",
                    delivery_id="c1",
                    from_agent_id="agent-local",
                    command_text="reply",
                )

            episode = read_session_index(root)["conversationEpisodes"]["req-1"]
            self.assertEqual([item["commandText"] for item in episode["deliveries"]], ["hello", "reply"])
            self.assertEqual(episode["deliveryCount"], 2)
            self.assertEqual([item["direction"] for item in episode["deliveries"]], ["inbound", "outbound"])

    def test_prompt_context_prefers_plugin_qualified_claworld_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claworld"

            management = build_prompt_context(root, platform="claworld", chat_id="management-abc")
            conversation = build_prompt_context(root, platform="claworld", chat_id="conversation-abc")

        self.assertTrue(management.startswith("## Your Role"))
        self.assertIn("You are currently acting as the private Claworld Manager", management)
        self.assertNotIn("# Claworld Management Startup Memory", management)
        self.assertIn("# Claworld Working Memory Root", management)
        self.assertIn(f"Configured root: `{root}`", management)
        self.assertIn(f"`{root / 'context' / 'NOW.md'}`", management)
        self.assertIn(f"`{root / 'sessions' / 'index.json'}`", management)
        self.assertIn("# Claworld Working Memory Startup Preview", management)
        self.assertIn("### `.claworld/context/PROFILE.md`", management)
        self.assertIn("### `.claworld/context/MEMORY.md`", management)
        self.assertIn("### `.claworld/context/NOW.md`", management)
        self.assertNotIn("sessions/index.json summary", management)
        self.assertIn("# Claworld Conversation Startup Context", conversation)
        self.assertIn(
            "You should never report your activity to the human or modify the claworld working memory.",
            conversation,
        )
        self.assertNotIn('skill_view("claworld:claworld-main-session")', conversation)

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
        self.memory_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.memory_root.cleanup)
        self.cfg = ClaworldConfig(
            server_url="https://api.example.com",
            app_token="tok",
            account_id="acct",
            agent_id="agent-1",
            working_memory_root=str(Path(self.memory_root.name) / ".claworld"),
        )

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
        self.assertNotIn("targetAgentId", calls[0]["body"])
        self.assertEqual(calls[0]["body"]["kickoffBrief"]["text"], "context for sender")
        self.assertEqual(calls[0]["body"]["openingPayload"]["source"], "test")
        self.assertEqual(calls[0]["body"]["requestContext"]["followUp"]["sessionKey"], "main:owner")
        self.assertEqual(calls[0]["body"]["source"], "direct_lookup")
        self.assertEqual(calls[0]["body"]["worldId"], "world-1")
        self.assertEqual(calls[0]["body"]["idempotencyKey"], "request-1")
        self.assertEqual(calls[0]["body"]["clientRequestId"], "client-1")
        self.assertEqual(calls[0]["timeout"], 60.0)
        self.assertEqual(result["action"], "request")

    def test_stored_render_hydrates_and_persists_direction_internally(self):
        root = self.cfg.memory_root_path()
        data = read_session_index(root)
        data["conversationEpisodes"] = {
            "req-render": {
                "chatRequestId": "req-render",
                "deliveries": [
                    {
                        "deliveryId": "delivery-1",
                        "direction": "inbound",
                        "deliveryType": "turn",
                        "commandText": "hello",
                    }
                ],
            }
        }
        write_session_index(root, data)
        calls = []

        def fake_request(cfg, method, endpoint, body=None, query=None, timeout=None):
            calls.append({"method": method, "endpoint": endpoint, "query": query})
            return {
                "chats": [
                    {
                        "chatRequestId": "req-render",
                        "direction": "outbound",
                    }
                ]
            }

        def fake_render(cfg, args):
            episode = read_session_index(root)["conversationEpisodes"]["req-render"]
            self.assertEqual(episode["requestDirection"], "outbound")
            self.assertEqual(args["initiatedBy"], "local")
            return {"rendered": True}

        with patch("claworld_hermes_plugin.tools.request_json", side_effect=fake_request), patch(
            "claworld_hermes_plugin.tools.render_transcript_report_artifact",
            side_effect=fake_render,
        ):
            result = claworld_tools._render_transcript_report(
                self.cfg,
                {"mode": "stored", "chatRequestId": "req-render", "topic": "Test"},
            )

        self.assertEqual(result, {"rendered": True})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["method"], "GET")
        self.assertEqual(calls[0]["endpoint"], "/v1/chat-requests")
        self.assertEqual(calls[0]["query"]["chatRequestId"], "req-render")
        self.assertEqual(calls[0]["query"]["agentId"], "agent-1")

    def test_stored_render_uses_local_direction_without_backend_call(self):
        root = self.cfg.memory_root_path()
        record_chat_request_direction(root, "req-local", "inbound")

        with patch("claworld_hermes_plugin.tools.request_json") as request, patch(
            "claworld_hermes_plugin.tools.render_transcript_report_artifact",
            return_value={"rendered": True},
        ):
            result = claworld_tools._render_transcript_report(
                self.cfg,
                {"mode": "stored", "chatRequestId": "req-local", "topic": "Test"},
            )

        self.assertEqual(result, {"rendered": True})
        request.assert_not_called()

    def test_conflicting_backend_directions_are_not_cached(self):
        payload = {
            "items": [
                {"chatRequestId": "req-conflict", "direction": "inbound"},
                {"chatRequestId": "req-conflict", "direction": "outbound"},
                {"chatRequestId": "req-good", "direction": "outbound"},
            ]
        }

        directions = claworld_tools._persist_conversation_directions(self.cfg, payload)

        self.assertEqual(directions, {"req-good": "outbound"})
        episodes = read_session_index(self.cfg.memory_root_path())["conversationEpisodes"]
        self.assertNotIn("req-conflict", episodes)
        self.assertEqual(episodes["req-good"]["directionViewerAgentId"], "agent-1")
        self.assertEqual(episodes["req-good"]["directionViewerAccountId"], "acct")

    def test_stored_render_backend_failure_does_not_block_rendering(self):
        with patch(
            "claworld_hermes_plugin.tools.request_json",
            side_effect=RuntimeError("offline"),
        ), patch(
            "claworld_hermes_plugin.tools.render_transcript_report_artifact",
            return_value={"rendered": True},
        ):
            result = claworld_tools._render_transcript_report(
                self.cfg,
                {
                    "mode": "stored",
                    "chatRequestId": "req-offline",
                    "topic": "Test",
                    "initiatedBy": "peer",
                },
            )

        self.assertEqual(result, {"rendered": True})

    def test_conversation_request_rejects_noncanonical_target_fields(self):
        for field, value in (
            ("identity", "Peer#ABC"),
            ("targetAgentId", "agent-peer"),
            ("targetId", "agent-peer"),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "use displayName and agentCode"):
                    claworld_tools._manage_conversations(
                        self.cfg,
                        {
                            "action": "request",
                            field: value,
                            "displayName": "Peer",
                            "agentCode": "ABC",
                            "openingMessage": "hi",
                        },
                    )

    def test_conversation_request_requires_public_name_and_code(self):
        with self.assertRaisesRegex(ValueError, "displayName is required"):
            claworld_tools._manage_conversations(
                self.cfg,
                {"action": "request", "agentCode": "ABC", "openingMessage": "hi"},
            )
        with self.assertRaisesRegex(ValueError, "agentCode is required"):
            claworld_tools._manage_conversations(
                self.cfg,
                {"action": "request", "displayName": "Peer", "openingMessage": "hi"},
            )

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

    def test_list_pending_invites_uses_invitee_inbox_route(self):
        calls = []

        def fake_request(cfg, method, endpoint, body=None, query=None, timeout=None):
            calls.append({"method": method, "endpoint": endpoint, "body": body, "query": query, "timeout": timeout})
            return {"items": [{"worldId": "w1", "membershipStatus": "invited"}], "totalItems": 1}

        with patch("claworld_hermes_plugin.tools.request_json", side_effect=fake_request):
            result = claworld_tools._manage_worlds(
                self.cfg,
                {"action": "list_pending_invites", "limit": 10},
            )

        self.assertEqual(calls[0]["method"], "GET")
        self.assertEqual(calls[0]["endpoint"], "/v1/world-invitations")
        self.assertEqual(calls[0]["query"]["agentId"], "agent-1")
        self.assertEqual(calls[0]["query"]["status"], "pending")
        self.assertEqual(calls[0]["query"]["limit"], 10)
        self.assertIsNone(calls[0]["body"])
        self.assertEqual(result["action"], "list_pending_invites")
        self.assertEqual(result["items"][0]["worldId"], "w1")

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
                "profile": {"accountProfile": {"ready": False}},
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
        self.assertEqual(result["relay"]["resolved"], False)
        self.assertEqual(result["relay"]["bindingStatus"], "bound")
        self.assertEqual(result["identityVerification"]["status"], "ready")

    def test_account_view_reads_canonical_nested_profile(self):
        cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok", account_id="acct")
        calls = []

        def fake_request(cfg_arg, method, endpoint, body=None, query=None, timeout=None):
            calls.append({"method": method, "endpoint": endpoint, "body": body, "query": query, "timeout": timeout})
            return {
                "status": "ready",
                "readiness": "ready",
                "diagnostics": {"publicIdentityReady": True},
                "relay": {"online": True},
                "profile": {
                    "agentId": "agent-from-profile",
                    "accountProfile": {"ready": True},
                    "shareCard": {
                        "status": "ready",
                        "imageUrl": "https://api.example.com/v1/share-card/card.jpg?token=abc",
                        "downloadUrl": "https://api.example.com/v1/share-card/card.jpg?token=abc",
                    },
                },
            }

        with patch("claworld_hermes_plugin.tools.request_json", side_effect=fake_request), patch(
            "claworld_hermes_plugin.tools._deliver_account_share_card",
            side_effect=lambda _cfg, result: result,
        ):
            result = claworld_tools._manage_account(cfg, {"action": "view_account", "generateShareCard": True})

        self.assertEqual(calls[0]["endpoint"], "/v1/account")
        self.assertNotIn("agentId", calls[0]["query"])
        self.assertEqual(result["relay"]["agentId"], "agent-from-profile")
        self.assertEqual(result["diagnostics"]["accountProfileReady"], True)
        self.assertEqual(
            result["profile"]["shareCard"]["imageUrl"],
            "https://api.example.com/v1/share-card/card.jpg?token=abc",
        )
        self.assertEqual(
            result["profile"]["shareCard"]["downloadUrl"],
            "https://api.example.com/v1/share-card/card.jpg?token=abc",
        )

    def test_update_display_name_requests_card_and_returns_canonical_nested_share_card(self):
        calls = []

        def fake_request(cfg, method, endpoint, body=None, query=None, timeout=None):
            calls.append({"method": method, "endpoint": endpoint, "body": body, "query": query, "timeout": timeout})
            return {
                "status": "ready",
                "readiness": "ready",
                "diagnostics": {"publicIdentityReady": True, "accountProfileReady": True},
                "relay": {"online": True},
                "profile": {
                    "agentId": "agent-1",
                    "publicIdentity": {"displayName": "Mira", "displayIdentity": "Mira#ABC"},
                    "shareCard": {
                        "status": "ready",
                        "imageUrl": "https://api.example.com/v1/share-card/card.jpg?token=abc",
                        "downloadUrl": "https://api.example.com/v1/share-card/card.jpg?token=abc",
                    },
                },
            }

        with patch("claworld_hermes_plugin.tools.request_json", side_effect=fake_request), patch(
            "claworld_hermes_plugin.tools._deliver_account_share_card",
            side_effect=lambda _cfg, result: result,
        ):
            result = claworld_tools._manage_account(
                self.cfg,
                {"action": "update_display_name", "displayName": "Mira"},
            )

        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["endpoint"], "/v1/account")
        self.assertEqual(calls[0]["body"]["action"], "update_identity")
        self.assertEqual(calls[0]["body"]["generateShareCard"], True)
        self.assertEqual(result["action"], "update_display_name")
        self.assertEqual(
            result["profile"]["shareCard"]["imageUrl"],
            "https://api.example.com/v1/share-card/card.jpg?token=abc",
        )

    def test_account_policy_updates_send_terminal_fields(self):
        calls = []

        def fake_request(cfg, method, endpoint, body=None, query=None, timeout=None):
            calls.append({"method": method, "endpoint": endpoint, "body": body, "query": query, "timeout": timeout})
            return {"status": "ready"}

        with patch("claworld_hermes_plugin.tools.request_json", side_effect=fake_request):
            visibility_result = claworld_tools._manage_account(
                self.cfg,
                {
                    "action": "set_visibility_mode",
                    "visibilityMode": "unlisted",
                },
            )
            contact_result = claworld_tools._manage_account(
                self.cfg,
                {
                    "action": "set_contact_policy",
                    "contactPolicy": "approval_required",
                },
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["endpoint"], "/v1/account")
        self.assertEqual(calls[0]["body"]["action"], "set_visibility_mode")
        self.assertEqual(calls[0]["body"]["visibilityMode"], "unlisted")
        self.assertNotIn("contactPolicy", calls[0]["body"])
        self.assertNotIn("chatRequestPolicy", calls[0]["body"])
        self.assertEqual(calls[1]["body"]["action"], "set_contact_policy")
        self.assertEqual(calls[1]["body"]["contactPolicy"], "approval_required")
        self.assertNotIn("visibilityMode", calls[1]["body"])
        self.assertNotIn("chatRequestPolicy", calls[1]["body"])
        for call in calls:
            self.assertNotIn("discoverable", call["body"])
            self.assertNotIn("contactable", call["body"])
            self.assertNotIn("chatRequestApprovalPolicy", call["body"])
        self.assertEqual(visibility_result["action"], "set_visibility_mode")
        self.assertEqual(contact_result["action"], "set_contact_policy")

    def test_account_policy_updates_reject_mixed_or_missing_fields(self):
        with self.assertRaisesRegex(ValueError, "visibilityMode is required"):
            claworld_tools._manage_account(self.cfg, {"action": "set_visibility_mode"})
        with self.assertRaisesRegex(ValueError, "visibilityMode is not supported"):
            claworld_tools._manage_account(
                self.cfg,
                {
                    "action": "set_contact_policy",
                    "visibilityMode": "public",
                    "contactPolicy": "approval_required",
                },
            )
        with self.assertRaisesRegex(ValueError, "action must be one of"):
            claworld_tools._manage_account(
                self.cfg,
                {
                    "action": "set_chat_request_policy",
                    "chatRequestPolicy": {"mode": "manual_review"},
                },
            )

    def test_account_policy_action_inference_uses_terminal_fields(self):
        self.assertEqual(
            claworld_tools._normalize_account_action({"visibilityMode": "private"}),
            "set_visibility_mode",
        )
        self.assertEqual(
            claworld_tools._normalize_account_action({"contactPolicy": "approval_required"}),
            "set_contact_policy",
        )
        with self.assertRaisesRegex(ValueError, "chatRequestPolicy is not supported"):
            claworld_tools._normalize_account_action({"chatRequestPolicy": {"mode": "reject_all"}})
        with self.assertRaisesRegex(ValueError, "action must be one of"):
            claworld_tools._normalize_account_action({"action": "update_chat_request_policy"})

    def test_account_submit_feedback_posts_authenticated_runtime_context(self):
        calls = []

        def fake_request(cfg, method, endpoint, body=None, query=None, timeout=None):
            calls.append({"method": method, "endpoint": endpoint, "body": body, "query": query, "timeout": timeout})
            return {
                "status": "recorded",
                "feedback": {
                    "feedbackId": "fb_123",
                    "category": body["category"],
                    "impact": body["impact"],
                    "title": body["title"],
                    "accountId": body["accountId"],
                    "reporter": {"agentId": body["agentId"], "publicIdentity": {"displayIdentity": "Mira#TEST"}},
                    "context": body["context"],
                    "runtimeContext": body["runtimeContext"],
                    "createdAt": "2026-07-08T00:00:00.000Z",
                },
            }

        with patch("claworld_hermes_plugin.tools.request_json", side_effect=fake_request):
            result = claworld_tools._manage_account(
                self.cfg,
                {
                    "action": "submit_feedback",
                    "category": "bug_report",
                    "title": "Feedback tool should use account auth",
                    "goal": "report a Claworld runtime issue",
                    "actualBehavior": "agent tried to run curl",
                    "expectedBehavior": "account tool submits it",
                    "impact": "medium",
                    "details": "Manual HTTP should not be needed.",
                    "reproductionSteps": ["Ask to report feedback"],
                    "context": {"worldId": "w1", "tags": ["feedback"]},
                },
            )

        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["endpoint"], "/v1/feedback")
        self.assertEqual(calls[0]["body"]["agentId"], "agent-1")
        self.assertEqual(calls[0]["body"]["accountId"], "acct")
        self.assertEqual(calls[0]["body"]["runtimeContext"]["toolName"], "claworld_manage_account")
        self.assertEqual(calls[0]["body"]["runtimeContext"]["accountToolAction"], "submit_feedback")
        self.assertEqual(calls[0]["body"]["source"], "hermes_account_tool")
        self.assertEqual(result["action"], "submit_feedback")
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(result["feedbackId"], "fb_123")
        self.assertEqual(result["reporterAgentId"], "agent-1")
        self.assertEqual(result["runtime"]["toolName"], "claworld_manage_account")

    def test_account_submit_feedback_requires_configured_app_token(self):
        cfg = ClaworldConfig(server_url="https://api.example.com", account_id="acct", agent_id="agent-1")
        with self.assertRaisesRegex(ValueError, "configured Claworld app token"):
            claworld_tools._manage_account(
                cfg,
                {
                    "action": "submit_feedback",
                    "category": "bug_report",
                    "title": "Feedback should be authenticated",
                    "goal": "report a Claworld runtime issue",
                    "actualBehavior": "missing token",
                    "expectedBehavior": "clear setup error",
                },
            )

    def test_account_view_degrades_ready_status_without_live_relay(self):
        for relay, readiness in (
            ({}, "relay_online_unconfirmed"),
            ({"online": False}, "relay_online_offline"),
        ):
            with self.subTest(relay=relay):
                payload = {
                    "status": "ready",
                    "readiness": "ready",
                    "relay": relay,
                    "diagnostics": {"publicIdentityReady": True},
                }

                result = claworld_tools._augment_account_binding(
                    payload,
                    cfg=self.cfg,
                    account_id="acct",
                    agent_id="agent-1",
                )

                self.assertEqual(result["status"], "degraded")
                self.assertEqual(result["readiness"], readiness)
                self.assertEqual(result["diagnostics"]["relayOnline"], relay.get("online"))
                self.assertEqual(result["relay"]["resolved"], isinstance(relay.get("online"), bool))
                self.assertEqual(result["warnings"][-1]["code"], readiness)

    def test_account_view_keeps_ready_status_with_live_relay(self):
        payload = {
            "status": "ready",
            "readiness": "ready",
            "relay": {"online": True},
            "diagnostics": {"publicIdentityReady": True},
        }

        result = claworld_tools._augment_account_binding(
            payload,
            cfg=self.cfg,
            account_id="acct",
            agent_id="agent-1",
        )

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["readiness"], "ready")
        self.assertTrue(result["relay"]["online"])
        self.assertNotIn("warnings", result)

    def test_setup_verification_persists_claworld_env(self):
        calls = []
        saved = {}

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
            if endpoint == "/v1/identity/email/start":
                return {"status": "verification_started", "email": body["email"], "expiresAt": "2026-06-24T00:10:00.000Z"}
            if endpoint == "/v1/identity/email/verify":
                return {
                    "status": "verified",
                    "agentId": "agent-email",
                    "appToken": "token-email",
                    "created": True,
                    "recovered": False,
                }
            raise AssertionError(endpoint)

        def fake_save(values):
            saved.update(values)
            return {"status": "saved_to_hermes_env", "path": "/tmp/.hermes/.env", "restartRequired": True}

        with patch("claworld_hermes_plugin.setup.request_json", side_effect=fake_request), patch(
            "claworld_hermes_plugin.setup.save_env_values",
            side_effect=fake_save,
        ):
            started = claworld_setup.start_email_verification(
                "agent@example.com",
                server_url="https://api.example.com",
            )
            verified = claworld_setup.complete_email_verification(
                "agent@example.com",
                "123456",
                server_url="https://api.example.com",
            )
            persistence = claworld_setup.persist_setup_credentials(verified)

        self.assertEqual(started["status"], "verification_started")
        self.assertEqual(calls[0]["endpoint"], "/v1/identity/email/start")
        self.assertEqual(calls[0]["body"], {"email": "agent@example.com"})
        self.assertFalse(calls[0]["cfg"].app_token)
        self.assertEqual(calls[1]["endpoint"], "/v1/identity/email/verify")
        self.assertEqual(calls[1]["body"], {"email": "agent@example.com", "code": "123456"})
        self.assertFalse(calls[1]["cfg"].app_token)
        self.assertEqual(
            saved,
            {
                "CLAWORLD_APP_TOKEN": "token-email",
                "CLAWORLD_AGENT_ID": "agent-email",
            },
        )
        self.assertEqual(persistence["status"], "saved_to_hermes_env")

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

    def test_get_state_persists_structured_request_direction_for_later_rendering(self):
        with patch(
            "claworld_hermes_plugin.tools.request_json",
            return_value={
                "chats": [
                    {
                        "chatRequestId": "req-cached",
                        "direction": "inbound",
                    }
                ]
            },
        ):
            claworld_tools._manage_conversations(
                self.cfg,
                {"action": "get_state", "chatRequestId": "req-cached"},
            )

        episode = read_session_index(self.cfg.memory_root_path())["conversationEpisodes"]["req-cached"]
        self.assertEqual(episode["requestDirection"], "inbound")

    def test_list_related_rejects_request_and_top_level_filter_fields(self):
        with self.assertRaisesRegex(ValueError, "identity is not supported"):
            claworld_tools._manage_conversations(self.cfg, {"action": "list_related", "identity": "Peer#ABC"})
        with self.assertRaisesRegex(ValueError, "displayName is only supported"):
            claworld_tools._manage_conversations(self.cfg, {"action": "list_related", "displayName": "Peer"})
        with self.assertRaisesRegex(ValueError, "worldId must be passed as filters.worldId"):
            claworld_tools._manage_conversations(self.cfg, {"action": "list_related", "worldId": "w1"})
        with self.assertRaisesRegex(ValueError, "filters.extra is not supported"):
            claworld_tools._manage_conversations(self.cfg, {"action": "list_related", "filters": {"extra": "x"}})


class RelayClientTests(unittest.IsolatedAsyncioTestCase):
    def test_websocket_proxy_uses_claworld_proxy_settings(self):
        direct = ClaworldConfig(server_url="https://api.example.com", app_token="tok")
        env_proxy = ClaworldConfig(server_url="https://api.example.com", app_token="tok", use_env_proxy=True)
        explicit_proxy = ClaworldConfig(
            server_url="https://api.example.com",
            app_token="tok",
            http_proxy="http://127.0.0.1:7890",
            use_env_proxy=True,
        )

        self.assertIsNone(claworld_relay._websocket_proxy(direct))
        self.assertIs(claworld_relay._websocket_proxy(env_proxy), True)
        self.assertEqual(claworld_relay._websocket_proxy(explicit_proxy), "http://127.0.0.1:7890")

    def test_reconnect_delay_exponentially_backs_off(self):
        cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok", agent_id="agent-1")
        client = RelayClient(cfg, on_delivery=lambda envelope: None)

        self.assertEqual(
            [client._next_reconnect_delay() for _ in range(8)],
            [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0],
        )

    async def test_open_once_cleans_up_websocket_when_auth_send_fails(self):
        class FakeWebSocket:
            def __init__(self):
                self.closed = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.sleep(60)
                raise StopAsyncIteration

            async def send(self, payload):
                raise RuntimeError("auth send failed")

            async def close(self):
                self.closed = True

        ws = FakeWebSocket()
        calls = []

        async def fake_connect(url, *, ping_interval=None, proxy=True):
            calls.append({"url": url, "ping_interval": ping_interval, "proxy": proxy})
            return ws

        fake_websockets = types.SimpleNamespace(connect=fake_connect)
        cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok", agent_id="agent-1")
        client = RelayClient(cfg, on_delivery=lambda envelope: None)

        with patch.dict(sys.modules, {"websockets": fake_websockets}):
            with self.assertRaisesRegex(RuntimeError, "auth send failed"):
                await client._open_once()

        self.assertEqual(calls, [{"url": "wss://api.example.com/ws", "ping_interval": None, "proxy": None}])
        self.assertTrue(ws.closed)
        self.assertIsNone(client.ws)
        self.assertIsNone(client._receiver_task)
        self.assertIsNone(client._auth_future)

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

    async def test_command_accepted_waiter_ignores_wrong_command_name(self):
        cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok", agent_id="agent-1")
        client = RelayClient(cfg, on_delivery=lambda envelope: None)
        future = client._register_ack_waiter(
            ("command.accepted",),
            "d1",
            command_names=("delivery.reply.requested",),
        )
        client._resolve_ack_waiters(
            "command.accepted",
            {
                "event": "command.accepted",
                "data": {
                    "command": {
                        "name": "delivery.kept_silent.requested",
                        "aggregateId": "d1",
                    }
                },
            },
        )
        self.assertFalse(future.done())

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

        async def fake_send_with_ack(payload, *, ack_events, delivery_id, fallback, command_names=()):
            calls.append((payload["type"], ack_events, delivery_id, command_names))

        client._send_with_ack = fake_send_with_ack
        await client.send_accepted("d1", "conversation:abc")
        await client.send_kept_silent("d2", "conversation:def", "no_renderable_reply")

        self.assertIn("command.accepted", calls[0][1])
        self.assertIn("command.accepted", calls[1][1])
        self.assertEqual(calls[0][3], ("delivery.accepted.requested",))
        self.assertEqual(calls[1][3], ("delivery.kept_silent.requested",))

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

    def test_delivery_visibility_retry_does_not_retry_transport_errors(self):
        calls = []
        cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok", http_retries=2)

        def fake_request(cfg_arg, method, path, **kwargs):
            calls.append((method, path, kwargs))
            raise TimeoutError("response timed out after the request was sent")

        with patch("claworld_hermes_plugin.relay_client.request_json", side_effect=fake_request), patch(
            "claworld_hermes_plugin.relay_client.time.sleep"
        ) as sleep:
            with self.assertRaises(TimeoutError):
                claworld_relay._request_json_with_delivery_visibility_retry(
                    cfg,
                    "POST",
                    "/v1/runtime-deliveries/d1/reply",
                )

        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()


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
        notice_record = adapter.DeliveryRecord(
            delivery_id="d3",
            relay_session_key="conversation:ghi",
            chat_id="conversation-3",
            replyable=True,
            saw_operational_notice=True,
        )

        self.assertEqual(adapter._completion_silence_reason(adapter.ProcessingOutcome.SUCCESS, record), "no_renderable_reply")
        self.assertEqual(adapter._completion_silence_reason(adapter.ProcessingOutcome.SUCCESS, non_replyable), "non_replyable_delivery")
        self.assertEqual(adapter._completion_silence_reason(adapter.ProcessingOutcome.SUCCESS, notice_record), "operational_notice_only")
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
        self.assertEqual(classify_reply_content("NO_REPLY").silence_reason, "no_reply")
        self.assertIsNone(classify_reply_content("kept_silent").silence_reason)
        self.assertIsNone(classify_reply_content("NO_REPLY please").silence_reason)


class HttpClientTests(unittest.TestCase):
    def test_auth_headers_and_url(self):
        cfg = ClaworldConfig(server_url="https://api.example.com", api_key="api", app_token="tok")
        headers = auth_headers(cfg)
        self.assertIn(f"claworld-hermes-plugin/{PLUGIN_VERSION}", headers["User-Agent"])
        self.assertEqual(headers["x-claworld-client"], "hermes-plugin")
        self.assertEqual(headers["x-claworld-client-version"], PLUGIN_VERSION)
        self.assertEqual(headers["x-claworld-client-channel"], "testing")
        self.assertNotIn("x-claworld-plugin-version", headers)
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

    def test_request_json_does_not_retry_mutating_transport_errors(self):
        class FakeOpener:
            def open(self, request, timeout=30.0):
                calls.append((request, timeout))
                raise TimeoutError("response timed out after the request was sent")

        cfg = ClaworldConfig(server_url="https://api.example.com", app_token="tok", http_retries=2)
        for method in ("POST", "PATCH", "DELETE"):
            with self.subTest(method=method):
                calls = []
                with patch("claworld_hermes_plugin.http_client._build_opener", return_value=FakeOpener()), patch(
                    "claworld_hermes_plugin.http_client.time.sleep"
                ) as sleep:
                    with self.assertRaises(TimeoutError):
                        request_json(
                            cfg,
                            method,
                            "/v1/chat-requests",
                            body={"fromAgentId": "agent-1"},
                            timeout=60.0,
                        )

                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0].method, method)
                self.assertEqual(calls[0][1], 60.0)
                sleep.assert_not_called()


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
