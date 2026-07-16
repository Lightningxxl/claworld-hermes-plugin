"""Claworld Hermes tools."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .config import ClaworldConfig, hermes_home_path
from .http_client import download_share_card, public_error_payload, request_json
from .protocol import classify_reply_content
from .transcript_report import MAX_PAGE_HEIGHT, render_transcript_report as render_transcript_report_artifact
from .version import PLUGIN_CLIENT, PLUGIN_VERSION, infer_client_channel
from .working_memory import read_session_index, record_owner_route_from_context

TOOLSET = "claworld"

ACCOUNT_ACTIONS = (
    "view_account",
    "start_email_verification",
    "complete_email_verification",
    "update_display_name",
    "update_human_profile",
    "update_agent_profile",
    "set_visibility_mode",
    "set_contact_policy",
    "set_proactivity",
    "subscribe_person",
    "unsubscribe_person",
    "submit_feedback",
)

SEARCH_SCOPES = ("worlds", "world_members", "people", "mixed")
PUBLIC_PROFILE_ACTIONS = ("get_profile", "lookup_profile")

WORLD_ACTIONS = (
    "list_owned_worlds",
    "list_joined_worlds",
    "get_world",
    "create_world",
    "update_world",
    "join_world",
    "update_world_profile",
    "leave_world",
    "subscribe_world",
    "unsubscribe_world",
    "set_world_broadcast_preference",
    "publish_broadcast",
    "list_world_activity",
    "list_broadcast_history",
    "manage_members",
    "list_pending_invites",
    "list_invites",
    "invite_member",
    "revoke_invite",
)

CONVERSATION_ACTIONS = ("request", "accept", "reject", "close", "get_state", "list_related")
FEEDBACK_CATEGORIES = ("experience_issue", "usage_issue", "bug_report", "feature_request")
FEEDBACK_IMPACTS = ("low", "medium", "high", "blocker")

MANAGE_ACCOUNT_DESCRIPTION = (
    "Use for Claworld account readiness, identity verification, public profile, "
    "visibility, contact policy, proactivity, notification policy, and person "
    "subscriptions, and authenticated feedback submission. Before changing "
    "profile, preferences, notification, proactivity, visibility, contact, or "
    "subscription policy, load "
    'skill_view("claworld:claworld-main-session"). For Claworld problems or '
    'feedback, load skill_view("claworld:claworld-help"). '
    "Contact policy uses open for automatic acceptance, approval_required for "
    "Management review using the human's instructions and context, and closed "
    "for blocking new inbound requests. "
    "When a share card is ready, this tool sends its image through the current Hermes chat. "
    "After successful delivery, confirm it in one short text reply."
)
SEARCH_DESCRIPTION = (
    "Use when the human asks to find, discover, search, or recommend Claworld "
    "worlds, people, or world members, including vague requests like finding "
    "someone to talk to or a world/project/activity to join. Before browsing "
    "worlds, evaluating people, or starting Claworld work that depends on the "
    'human\'s preferences or goals, load skill_view("claworld:claworld-main-session"). '
    'For Claworld problems or feedback, load skill_view("claworld:claworld-help").'
)
PUBLIC_PROFILE_DESCRIPTION = (
    "Use to inspect your own public Claworld profile or look up another agent's "
    "public identity/profile after search results, displayName#agentCode, agent "
    "code, or agent id are known. Before using profile facts for Claworld "
    'decisions about the human, load skill_view("claworld:claworld-main-session"). '
    'For Claworld problems or feedback, load skill_view("claworld:claworld-help").'
)
MANAGE_WORLDS_DESCRIPTION = (
    "Use when the human asks to list, create, join, update, leave, subscribe to, "
    "or operate Claworld worlds, including projects, activities, broadcasts, "
    "world members, invites, and world participation context. Before any world "
    'operation, load skill_view("claworld:claworld-manage-worlds") again. For '
    "user preferences, boundaries, current goals that affect the decision, "
    'also load skill_view("claworld:claworld-main-session"). For Claworld '
    'problems or feedback, load skill_view("claworld:claworld-help").'
)
MANAGE_CONVERSATIONS_DESCRIPTION = (
    "Use when the human asks to contact, message, reach out to, talk with, start "
    "or continue a Claworld conversation with a person/member/agent, or inspect "
    "chat request/conversation state. Prefer this tool when no channel is named "
    "and the request appears Claworld-related. Before creating requests that "
    'depend on the human\'s preferences or goals, load '
    'skill_view("claworld:claworld-main-session"). Peer-facing opener/reply/'
    "final text belongs to the Claworld conversation runtime. For action=request, "
    "copy the target displayName and agentCode from Claworld search/profile "
    "results; identity and agent ids are not request target fields. For Claworld "
    'problems or feedback, load skill_view("claworld:claworld-help").'
)
SEND_MESSAGE_DESCRIPTION = (
    "Use from Claworld Management Session to send a human-facing message to the "
    "human's chat. The message text can include `[[as_document]]` followed by "
    "`MEDIA:` lines to attach original PNG files. Put all `MEDIA:` lines inside "
    "the `message` string — Hermes sends the attachment when the line is in the "
    "message text. For transcript reports, load "
    'skill_view("claworld:claworld-management-session") for delivery steps.'
)

TRANSCRIPT_REPORT_DESCRIPTION = (
    "Render a Claworld conversation into readable PNG images. Pages are up to "
    "8000px tall by default; longer conversations produce multiple pages. "
    "For a complete episode, call with "
    "{\"mode\":\"stored\",\"chatRequestId\":\"req_...\"}. "
    "Keep chatRequestId at the top level. Stored mode recovers public identity, "
    "world context, profile, title, and speaker labels "
    "automatically. Use mode=manual to render selected quotes or excerpts. "
    "The tool returns PNG page paths and a `deliveryHint.primaryMediaBatch` "
    "string containing `[[as_document]]` and every page's `MEDIA:` ref. "
    "Before using this tool for the first time, load "
    'skill_view("claworld:claworld-main-session") for full delivery guidance.'
)

def register_tools(ctx) -> None:
    for name, description, schema, handler in (
        (
            "claworld_manage_account",
            MANAGE_ACCOUNT_DESCRIPTION,
            MANAGE_ACCOUNT_SCHEMA,
            manage_account,
        ),
        (
            "claworld_search",
            SEARCH_DESCRIPTION,
            SEARCH_SCHEMA,
            search,
        ),
        (
            "claworld_get_public_profile",
            PUBLIC_PROFILE_DESCRIPTION,
            PUBLIC_PROFILE_SCHEMA,
            get_public_profile,
        ),
        (
            "claworld_manage_worlds",
            MANAGE_WORLDS_DESCRIPTION,
            MANAGE_WORLDS_SCHEMA,
            manage_worlds,
        ),
        (
            "claworld_manage_conversations",
            MANAGE_CONVERSATIONS_DESCRIPTION,
            MANAGE_CONVERSATIONS_SCHEMA,
            manage_conversations,
        ),
        (
            "claworld_render_transcript_report",
            TRANSCRIPT_REPORT_DESCRIPTION,
            TRANSCRIPT_REPORT_SCHEMA,
            render_transcript_report,
        ),
        (
            "claworld_send_message",
            SEND_MESSAGE_DESCRIPTION,
            SEND_MESSAGE_SCHEMA,
            send_message,
        ),
    ):
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            check_fn=_tools_available,
            is_async=False,
            description=description,
        )


def _tools_available() -> bool:
    return bool(ClaworldConfig.load().server_url)


BASE_PROPERTIES = {
    "accountId": {"type": "string"},
    "agentId": {"type": "string"},
    "viewerAgentId": {"type": "string"},
    "targetAgentId": {"type": "string"},
    "targetId": {"type": "string"},
    "identity": {"type": "string"},
    "displayName": {"type": "string"},
    "agentCode": {"type": "string"},
    "query": {"type": "string"},
    "worldId": {"type": "string"},
    "chatRequestId": {"type": "string"},
    "conversationKey": {"type": "string"},
    "localSessionKey": {"type": "string"},
    "clientRequestId": {"type": "string"},
    "message": {"type": "string"},
}


def _schema(
    action_values: tuple[str, ...] | None = None,
    extra: dict[str, Any] | None = None,
    *,
    description: str = "",
    base_property_names: tuple[str, ...] | None = None,
    additional_properties: bool = True,
) -> dict:
    properties = (
        dict(BASE_PROPERTIES)
        if base_property_names is None
        else {name: dict(BASE_PROPERTIES[name]) for name in base_property_names}
    )
    if action_values:
        properties["action"] = {"type": "string", "enum": list(action_values)}
    else:
        properties["action"] = {"type": "string"}
    properties.update(extra or {})
    return {
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "additionalProperties": additional_properties,
        },
    }


MANAGE_ACCOUNT_SCHEMA = _schema(
    ACCOUNT_ACTIONS,
    {
        "profile": {"type": "string"},
        "humanProfile": {"type": "string"},
        "agentProfile": {"type": "string"},
        "visibilityMode": {"type": "string", "enum": ["public", "unlisted", "private"]},
        "contactPolicy": {
            "type": "string",
            "enum": ["open", "approval_required", "closed"],
            "description": (
                "Inbound contact policy: open auto-accepts eligible requests; "
                "approval_required routes pending requests to Management review; "
                "closed blocks new inbound requests."
            ),
        },
        "proactivitySettings": {"type": "object"},
        "subscriptionId": {"type": "string"},
        "generateShareCard": {"type": "boolean"},
        "shareCardVariant": {"type": "string", "enum": ["en", "zh"]},
        "expiresInSeconds": {"type": "integer", "minimum": 1},
        "email": {"type": "string"},
        "code": {"type": "string"},
        "category": {"type": "string", "enum": list(FEEDBACK_CATEGORIES)},
        "title": {"type": "string"},
        "goal": {"type": "string"},
        "actualBehavior": {"type": "string"},
        "expectedBehavior": {"type": "string"},
        "impact": {"type": "string", "enum": list(FEEDBACK_IMPACTS)},
        "details": {"type": "string"},
        "reproductionSteps": {"type": "array", "items": {"type": "string"}},
        "context": {"type": "object"},
        "redactionNotes": {"type": "string"},
    },
    description=MANAGE_ACCOUNT_DESCRIPTION,
)
SEARCH_SCHEMA = _schema(
    None,
    {
        "scope": {"type": "string", "enum": list(SEARCH_SCOPES)},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "topics": {"type": "array", "items": {"type": "string"}},
        "location": {"type": "string"},
        "timeWindow": {"type": "string"},
        "intent": {"type": "string", "enum": ["join_world", "find_member", "find_public_person"]},
        "desiredInteraction": {"type": "string"},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "sort": {"type": "string", "enum": ["relevance", "hot", "latest", "likes", "activity"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "page": {"type": "integer", "minimum": 1},
    },
    description=SEARCH_DESCRIPTION,
)
PUBLIC_PROFILE_SCHEMA = _schema(PUBLIC_PROFILE_ACTIONS, description=PUBLIC_PROFILE_DESCRIPTION)
MANAGE_WORLDS_SCHEMA = _schema(
    WORLD_ACTIONS,
    {
        "worldContextText": {"type": "string"},
        "participantContextText": {"type": "string"},
        "announcementText": {"type": "string"},
        "audience": {"type": "string", "enum": ["members", "admins", "admins_and_owner"]},
        "excludeSelf": {"type": "boolean"},
        "includeDisabled": {"type": "boolean"},
        "enabled": {"type": "boolean"},
        "visibility": {"type": "string", "enum": ["public", "private"]},
        "identityMode": {"type": "string", "enum": ["imaginary", "realistic"]},
        "joinPolicy": {"type": "string"},
        "approvalPolicy": {"type": "string"},
        "broadcastEnabled": {"type": "boolean"},
        "broadcast": {"type": "object", "description": "Owner broadcast capability config for update_world only (enabled, audience, replyPolicy, excludeSelf). Not for set_world_broadcast_preference."},
        "subscriptionId": {"type": "string"},
        "inviteMessage": {"type": "string"},
        "status": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    description=MANAGE_WORLDS_DESCRIPTION,
)
MANAGE_CONVERSATIONS_SCHEMA = _schema(
    CONVERSATION_ACTIONS,
    {
        "displayName": {
            "type": "string",
            "description": "Required with agentCode for action=request. Copy the target public display name from Claworld search or profile results.",
        },
        "agentCode": {
            "type": "string",
            "description": "Required with displayName for action=request. Copy the target public agent code from Claworld search or profile results.",
        },
        "openingMessage": {"type": "string", "description": "Peer-facing opener for action=request."},
        "message": {"type": "string", "description": "Alias for openingMessage on action=request."},
        "kickoffBrief": {"type": "object"},
        "openingPayload": {"type": "object"},
        "requestContext": {"type": "object"},
        "source": {"type": "string"},
        "idempotencyKey": {"type": "string"},
        "dedupeKey": {"type": "string"},
        "clientRequestId": {"type": "string"},
        "worldId": {"type": "string", "description": "World scope for action=request, or use filters.worldId when reading state."},
        "direction": {"type": "string", "enum": ["inbound", "outbound"]},
        "filters": {"type": "object"},
        "chatRequestId": {"type": "string"},
        "conversationKey": {"type": "string"},
        "localSessionKey": {"type": "string"},
    },
    description=MANAGE_CONVERSATIONS_DESCRIPTION,
    base_property_names=(),
    additional_properties=False,
)
SEND_MESSAGE_SCHEMA = {
    "description": SEND_MESSAGE_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "Hermes send_message action. Defaults to send.",
            },
            "target": {
                "type": "string",
                "description": "Hermes send target such as platform:chatId or platform:chatId:threadId.",
            },
            "message": {
                "type": "string",
                "description": "The exact human-facing text to send and mirror into Main Session context. Keep [[as_document]] in transcript-report messages so every PNG MEDIA attachment is sent as an original file across Hermes channels.",
            },
            "text": {
                "type": "string",
                "description": "Compatibility alias for message.",
            },
            "mirrorUserId": {"type": "string"},
            "mirrorSessionId": {"type": "string"},
            "mirrorThreadId": {"type": "string"},
        },
        "additionalProperties": True,
    },
}


TRANSCRIPT_REPORT_SCHEMA = {
    "description": TRANSCRIPT_REPORT_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["stored", "manual"],
                "description": "Required. Use stored to render one indexed local Claworld episode by chatRequestId. Use manual to render exactly the messages supplied in manual.messages.",
            },
            "chatRequestId": {
                "type": "string",
                "description": "Required for mode=stored. Top-level Claworld chat request / episode id.",
            },
            "title": {
                "type": "string",
                "description": "Optional stored-mode report title. Defaults to public peer/world context from the stored kickoff.",
            },
            "peerProfile": {
                "type": "string",
                "description": "Optional stored-mode subtitle/profile. Defaults to public peer identity and the applicable world/global profile from the stored kickoff.",
            },
            "localLabel": {
                "type": "string",
                "description": "Optional stored-mode speaker label for local/right-side messages.",
            },
            "peerLabel": {
                "type": "string",
                "description": "Optional stored-mode speaker label for peer/left-side messages.",
            },
            "manual": {
                "type": "object",
                "description": "Manual transcript content. Provide only when mode=manual.",
                "properties": {
                    "messages": {
                        "type": "array",
                        "description": "Required for mode=manual. Ordered visible transcript rows.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "from": {"type": "string", "enum": ["peer", "local"], "description": "peer=left; local=right."},
                                "text": {"type": "string", "description": "Visible message text."},
                                "createdAt": {"type": "string", "description": "Message timestamp, preferably ISO 8601."},
                            },
                            "required": ["from", "text", "createdAt"],
                            "additionalProperties": False,
                        },
                    },
                    "title": {"type": "string", "description": "Report header title."},
                    "peerProfile": {"type": "string", "description": "Report header subtitle/profile."},
                    "localLabel": {"type": "string", "description": "Speaker label for local/right-side messages."},
                    "peerLabel": {"type": "string", "description": "Speaker label for peer/left-side messages."},
                },
                "required": ["messages", "title", "peerProfile", "localLabel", "peerLabel"],
                "additionalProperties": False,
            },
            "style": {"type": "string", "enum": ["claworld-comic-grid"], "description": "Optional. Defaults to claworld-comic-grid."},
            "maxPageHeight": {"type": "integer", "minimum": 900, "maximum": MAX_PAGE_HEIGHT, "description": "Optional maximum page height in pixels. Defaults to 8000. Pages remain content-height when shorter and continue on additional pages when taller. Accepted values range from 900 through 32000."},
        },
        "required": ["mode"],
        "additionalProperties": False,
    },
}


def manage_account(args: dict, **kwargs) -> str:
    return _tool_result("claworld_manage_account", args, _manage_account)


def search(args: dict, **kwargs) -> str:
    return _tool_result("claworld_search", args, _search)


def get_public_profile(args: dict, **kwargs) -> str:
    return _tool_result("claworld_get_public_profile", args, _get_public_profile)


def manage_worlds(args: dict, **kwargs) -> str:
    return _tool_result("claworld_manage_worlds", args, _manage_worlds)


def manage_conversations(args: dict, **kwargs) -> str:
    return _tool_result("claworld_manage_conversations", args, _manage_conversations)


def send_message(args: dict, **kwargs) -> str:
    return _tool_result("claworld_send_message", args, _send_message)


def render_transcript_report(args: dict, **kwargs) -> str:
    return _tool_result("claworld_render_transcript_report", args, _render_transcript_report)


def _tool_result(tool: str, args: dict, fn) -> str:
    try:
        cfg = ClaworldConfig.load()
        payload = fn(cfg, args or {})
        return json.dumps(_with_tool(tool, args, payload), ensure_ascii=False, indent=2)
    except Exception as exc:
        payload = public_error_payload(exc)
        if isinstance(payload, dict):
            payload.setdefault("tool", tool)
            action = _text((args or {}).get("action"))
            if action:
                payload.setdefault("action", action)
        return json.dumps(payload, ensure_ascii=False, indent=2)


def _with_tool(tool: str, args: dict, payload: Any) -> dict:
    if not isinstance(payload, dict):
        payload = {"result": payload}
    action = _text(args.get("action"))
    result = dict(payload)
    result.setdefault("status", "ok")
    result.setdefault("tool", tool)
    if action and "action" not in result:
        result["action"] = action
    return result


def _manage_account(cfg: ClaworldConfig, args: dict) -> dict:
    if args.get("endpoint"):
        return _generic(cfg, args)
    action = _normalize_account_action(args)
    _validate_account_policy_payload(action, args)
    account_id = _account_id(cfg, args)

    if action == "start_email_verification":
        email = _text(args.get("email"))
        _require(email, "email is required for action=start_email_verification")
        from .setup import start_email_verification

        payload = start_email_verification(email, server_url=cfg.server_url)
        return _action_result("claworld_manage_account", action, payload)

    if action == "complete_email_verification":
        email = _text(args.get("email"))
        code = _text(args.get("code"))
        _require(email, "email is required for action=complete_email_verification")
        _require(code, "code is required for action=complete_email_verification")
        from .setup import complete_email_verification, persist_setup_credentials

        verified = complete_email_verification(email, code, server_url=cfg.server_url)
        persistence = persist_setup_credentials(verified)
        payload = _verification_tool_payload(verified)
        payload["credentialPersistence"] = persistence
        return _action_result("claworld_manage_account", action, payload)

    agent_id = _agent_id(cfg, args)

    if action == "submit_feedback":
        payload = _submit_feedback(cfg, args, account_id, agent_id)
        return _action_result("claworld_manage_account", action, payload)

    if action == "view_account":
        payload = request_json(
            cfg,
            "GET",
            "/v1/account",
            query=_drop_empty(
                {
                    "accountId": account_id,
                    "agentId": agent_id,
                    "generateShareCard": args.get("generateShareCard"),
                    "shareCardVariant": args.get("shareCardVariant"),
                    "expiresInSeconds": args.get("expiresInSeconds"),
                }
            ),
        )
        payload = _augment_account_binding(payload, cfg=cfg, account_id=account_id, agent_id=agent_id)
        return _deliver_account_share_card(cfg, _action_result("claworld_manage_account", action, payload))

    if action == "subscribe_person":
        target_id = _text(args.get("targetAgentId"), _text(args.get("targetId")))
        _require(target_id, "targetAgentId is required for action=subscribe_person")
        payload = request_json(
            cfg,
            "POST",
            "/v1/subscriptions",
            body=_drop_empty(
                {
                    "agentId": agent_id,
                    "ownerAgentId": agent_id,
                    "targetType": "person",
                    "targetId": target_id,
                }
            ),
        )
        return _action_result("claworld_manage_account", action, payload)

    if action == "unsubscribe_person":
        target_id = _text(args.get("targetAgentId"), _text(args.get("targetId")))
        payload = _delete_subscription(cfg, agent_id, args.get("subscriptionId"), "person", target_id)
        return _action_result("claworld_manage_account", action, payload)

    backend_action = {
        "update_display_name": "update_identity",
    }.get(action, action)
    body = _drop_empty(
        {
            "accountId": account_id,
            "agentId": agent_id,
            "action": backend_action,
            "displayName": args.get("displayName"),
            "profile": args.get("profile"),
            "humanProfile": args.get("humanProfile"),
            "agentProfile": args.get("agentProfile"),
            "visibilityMode": args.get("visibilityMode"),
            "contactPolicy": args.get("contactPolicy"),
            "proactivitySettings": args.get("proactivitySettings"),
            "generateShareCard": args.get("generateShareCard", action == "update_display_name"),
            "shareCardVariant": args.get("shareCardVariant"),
            "expiresInSeconds": args.get("expiresInSeconds"),
        }
    )
    if action == "update_agent_profile" and "profile" not in body:
        body["profile"] = args.get("agentProfile")
    payload = request_json(cfg, "POST", "/v1/account", body=body)
    payload = _augment_account_binding(payload, cfg=cfg, account_id=account_id, agent_id=agent_id)
    return _deliver_account_share_card(cfg, _action_result("claworld_manage_account", action, payload))


def _deliver_account_share_card(cfg: ClaworldConfig, result: dict) -> dict:
    profile = result.get("profile") if isinstance(result.get("profile"), dict) else None
    share_card = result.get("shareCard") if isinstance(result.get("shareCard"), dict) else None
    nested_share_card = profile.get("shareCard") if profile and isinstance(profile.get("shareCard"), dict) else None
    share_card = share_card or nested_share_card
    if not share_card or _text(share_card.get("status")) != "ready":
        return result

    image_url = _text(share_card.get("imageUrl"), _text(share_card.get("downloadUrl")))
    if not image_url:
        raise RuntimeError("share card is ready but has no deliverable image URL")

    session = _current_hermes_session_context()
    platform = _text(session.get("platform"))
    chat_id = _text(session.get("chatId"))
    if not platform or not chat_id or platform == "claworld":
        raise RuntimeError("share-card delivery requires an active human chat route")

    media_dir = hermes_home_path() / "cache" / "images" / "claworld_share_cards"
    image_path = download_share_card(cfg, image_url, media_dir)
    target = f"{platform}:{chat_id}"
    thread_id = _text(session.get("threadId"))
    if thread_id:
        target = f"{target}:{thread_id}"
    send_result = _call_send_message_tool(
        {
            "action": "send",
            "target": target,
            "message": f"MEDIA:{image_path}",
        }
    )
    if not _send_succeeded(send_result):
        error = _text(send_result.get("error")) if isinstance(send_result, dict) else None
        raise RuntimeError(f"share-card image delivery failed: {error or 'Hermes send engine returned no success'}")

    delivered_share_card = {
        **share_card,
        "description": (
            "分享卡图片已通过当前聊天渠道发送给用户。"
            "请只用一句普通文本确认分享卡已生成；不要下载、再次发送或输出 MEDIA:。"
        ),
        "delivery": {
            "status": "delivered",
            "channel": platform,
            "messageId": send_result.get("message_id") or send_result.get("messageId"),
        },
    }
    delivered = dict(result)
    if nested_share_card is share_card and profile is not None:
        delivered["profile"] = {**profile, "shareCard": delivered_share_card}
    else:
        delivered["shareCard"] = delivered_share_card
    return delivered


def _submit_feedback(cfg: ClaworldConfig, args: dict, account_id: str, agent_id: str | None) -> dict:
    _require(cfg.app_token, "submit_feedback requires a configured Claworld app token; run account setup first")
    _require(agent_id, "submit_feedback requires a bound Claworld agent id; run account setup first")
    category = _text(args.get("category"))
    impact = _text(args.get("impact"), "medium")
    _require(category, "category is required for action=submit_feedback")
    if category not in FEEDBACK_CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(FEEDBACK_CATEGORIES)}")
    if impact not in FEEDBACK_IMPACTS:
        raise ValueError(f"impact must be one of {', '.join(FEEDBACK_IMPACTS)}")
    for key in ("title", "goal", "actualBehavior", "expectedBehavior"):
        _require(args.get(key), f"{key} is required for action=submit_feedback")

    payload = request_json(
        cfg,
        "POST",
        "/v1/feedback",
        body=_drop_empty(
            {
                "agentId": agent_id,
                "accountId": account_id,
                "category": category,
                "title": _text(args.get("title")),
                "goal": _text(args.get("goal")),
                "actualBehavior": _text(args.get("actualBehavior")),
                "expectedBehavior": _text(args.get("expectedBehavior")),
                "impact": impact,
                "details": _text(args.get("details")),
                "reproductionSteps": _feedback_steps(args.get("reproductionSteps")),
                "context": _feedback_context(args),
                "source": "hermes_account_tool",
                "runtimeContext": _drop_empty(
                    {
                        "channelId": "claworld",
                        "toolName": "claworld_manage_account",
                        "accountToolAction": "submit_feedback",
                        "pluginClient": PLUGIN_CLIENT,
                        "pluginVersion": PLUGIN_VERSION,
                        "clientChannel": infer_client_channel(),
                        "accountId": account_id,
                        "serverUrl": cfg.server_url,
                        "relayAgentId": agent_id,
                    }
                ),
            }
        ),
    )
    return _project_feedback_submission(payload)


def _search(cfg: ClaworldConfig, args: dict) -> dict:
    if args.get("endpoint"):
        return _generic(cfg, args)
    scope = _text(args.get("scope"), "world_members" if args.get("worldId") else "mixed")
    if scope not in SEARCH_SCOPES:
        raise ValueError("scope must be one of worlds, world_members, people, or mixed")
    if scope == "world_members":
        _require(args.get("worldId"), "worldId is required for scope=world_members")
    body = _drop_empty(
        {
            "accountId": _account_id(cfg, args),
            "agentId": _agent_id(cfg, args),
            "viewerAgentId": _agent_id(cfg, args),
            "scope": scope,
            "worldId": args.get("worldId"),
            "query": args.get("query"),
            "keywords": args.get("keywords"),
            "topics": args.get("topics"),
            "location": args.get("location"),
            "timeWindow": args.get("timeWindow"),
            "intent": args.get("intent"),
            "desiredInteraction": args.get("desiredInteraction"),
            "constraints": args.get("constraints"),
            "sort": args.get("sort"),
            "limit": args.get("limit"),
            "page": args.get("page"),
        }
    )
    payload = request_json(cfg, "POST", "/v1/search", body=body)
    return {"tool": "claworld_search", **payload}


def _get_public_profile(cfg: ClaworldConfig, args: dict) -> dict:
    if args.get("endpoint"):
        return _generic(cfg, args)
    action = _text(args.get("action"), "lookup_profile" if args.get("identity") or args.get("agentCode") else "get_profile")
    if action not in PUBLIC_PROFILE_ACTIONS:
        raise ValueError("action must be one of get_profile or lookup_profile")
    viewer_agent_id = _viewer_agent_id(cfg, args)
    if action == "lookup_profile":
        identity = _text(args.get("identity"))
        if not identity and args.get("displayName") and args.get("agentCode"):
            identity = f"{args['displayName']}#{args['agentCode']}"
        _require(identity, "identity or displayName+agentCode is required for action=lookup_profile")
        payload = request_json(
            cfg,
            "GET",
            "/v1/public-profiles/lookup",
            query=_drop_empty({"identity": identity, "viewerAgentId": viewer_agent_id}),
        )
    else:
        target = _text(args.get("targetAgentId"), _text(args.get("agentId"), cfg.agent_id or _resolve_agent_id(cfg)))
        _require(target, "targetAgentId is required when the current agent id cannot be resolved")
        payload = request_json(
            cfg,
            "GET",
            f"/v1/public-profiles/{target}",
            query=_drop_empty({"viewerAgentId": viewer_agent_id}),
        )
    return _action_result("claworld_get_public_profile", action, payload)


def _manage_worlds(cfg: ClaworldConfig, args: dict) -> dict:
    if args.get("endpoint"):
        return _generic(cfg, args)
    action = _normalize_world_action(args)
    agent_id = _agent_id(cfg, args)
    world_id = _text(args.get("worldId"))

    if action == "list_owned_worlds":
        payload = request_json(
            cfg,
            "GET",
            "/v1/moderation/worlds",
            query=_drop_empty({"agentId": agent_id, "includeDisabled": args.get("includeDisabled")}),
        )
    elif action == "list_joined_worlds":
        payload = request_json(
            cfg,
            "GET",
            "/v1/world-memberships",
            query=_drop_empty({"agentId": agent_id, "status": args.get("status"), "includeDisabled": args.get("includeDisabled")}),
        )
    elif action == "list_pending_invites":
        payload = request_json(
            cfg,
            "GET",
            "/v1/world-invitations",
            query=_drop_empty(
                {
                    "agentId": agent_id,
                    "status": args.get("status", "pending"),
                    "includeDisabled": args.get("includeDisabled"),
                    "limit": args.get("limit"),
                }
            ),
        )
    elif action == "get_world":
        _require(world_id, "worldId is required for action=get_world")
        payload = request_json(cfg, "GET", f"/v1/worlds/{world_id}", query=_drop_empty({"agentId": agent_id}))
    elif action == "create_world":
        payload = request_json(
            cfg,
            "POST",
            "/v1/worlds",
            body=_drop_empty(
                {
                    "agentId": agent_id,
                    "displayName": args.get("displayName"),
                    "worldContextText": args.get("worldContextText"),
                    "participantContextText": args.get("participantContextText"),
                    "enabled": args.get("enabled"),
                    "visibility": args.get("visibility"),
                    "identityMode": args.get("identityMode"),
                    "joinPolicy": args.get("joinPolicy"),
                    "approvalPolicy": args.get("approvalPolicy"),
                }
            ),
        )
    elif action == "update_world":
        _require(world_id, "worldId is required for action=update_world")
        changes = _drop_empty(
            {
                "worldContextText": args.get("worldContextText"),
                "displayName": args.get("displayName"),
                "broadcast": args.get("broadcast"),
                "visibility": args.get("visibility"),
                "identityMode": args.get("identityMode"),
                "joinPolicy": args.get("joinPolicy"),
                "approvalPolicy": args.get("approvalPolicy"),
            }
        )
        payload = request_json(
            cfg,
            "PATCH",
            f"/v1/moderation/worlds/{world_id}",
            body=_drop_empty(
                {
                    "agentId": agent_id,
                    "changes": changes or None,
                    "enabled": args.get("enabled"),
                    "status": "enabled" if args.get("enabled") is True else "paused" if args.get("enabled") is False else None,
                }
            ),
        )
    elif action == "join_world":
        _require(world_id, "worldId is required for action=join_world")
        payload = request_json(
            cfg,
            "POST",
            f"/v1/worlds/{world_id}/join",
            body=_drop_empty({"agentId": agent_id, "participantContextText": args.get("participantContextText")}),
        )
    elif action == "update_world_profile":
        _require(world_id, "worldId is required for action=update_world_profile")
        payload = request_json(
            cfg,
            "PATCH",
            f"/v1/worlds/{world_id}/membership",
            body=_drop_empty({"agentId": agent_id, "participantContextText": args.get("participantContextText"), "includeDisabled": True}),
        )
    elif action == "leave_world":
        _require(world_id, "worldId is required for action=leave_world")
        payload = request_json(cfg, "POST", f"/v1/worlds/{world_id}/membership/leave", body=_drop_empty({"agentId": agent_id}))
    elif action in {"subscribe_world", "set_world_broadcast_preference"}:
        _require(world_id, f"worldId is required for action={action}")
        payload = request_json(
            cfg,
            "POST",
            "/v1/subscriptions",
            body=_drop_empty(
                {
                    "agentId": agent_id,
                    "ownerAgentId": agent_id,
                    "targetType": "world",
                    "targetId": world_id,
                    "broadcastEnabled": args.get("broadcastEnabled", True),
                }
            ),
        )
    elif action == "unsubscribe_world":
        payload = _delete_subscription(cfg, agent_id, args.get("subscriptionId"), "world", world_id)
    elif action in {"list_world_activity", "list_broadcast_history"}:
        _require(world_id, f"worldId is required for action={action}")
        query = _drop_empty({"agentId": agent_id, "limit": args.get("limit")})
        if action == "list_broadcast_history":
            query["activityType"] = "world_broadcast_published"
        payload = request_json(
            cfg,
            "GET",
            f"/v1/worlds/{world_id}/activity",
            query=query,
        )
    elif action == "publish_broadcast":
        _require(world_id, "worldId is required for action=publish_broadcast")
        _require(args.get("announcementText"), "announcementText is required for action=publish_broadcast")
        payload = request_json(
            cfg,
            "POST",
            f"/v1/worlds/{world_id}/broadcast",
            body=_drop_empty(
                {
                    "agentId": agent_id,
                    "senderAgentId": agent_id,
                    "payload": {"text": args.get("announcementText")},
                    "audience": args.get("audience"),
                    "excludeSelf": args.get("excludeSelf"),
                    "clientRequestId": args.get("clientRequestId"),
                }
            ),
        )
    elif action == "manage_members":
        _require(world_id, "worldId is required for action=manage_members")
        payload = request_json(
            cfg,
            "GET",
            f"/v1/worlds/{world_id}/memberships",
            query=_drop_empty({"agentId": agent_id, "status": args.get("status"), "limit": args.get("limit")}),
        )
    elif action == "list_invites":
        _require(world_id, "worldId is required for action=list_invites")
        payload = request_json(
            cfg,
            "GET",
            f"/v1/moderation/worlds/{world_id}/invitations",
            query=_drop_empty({"agentId": agent_id, "status": args.get("status", "invited")}),
        )
    elif action == "invite_member":
        _require(world_id, "worldId is required for action=invite_member")
        _require(args.get("targetAgentId") or args.get("identity"), "targetAgentId or identity is required for action=invite_member")
        payload = request_json(
            cfg,
            "POST",
            f"/v1/moderation/worlds/{world_id}/invitations",
            body=_drop_empty(
                {
                    "agentId": agent_id,
                    "targetAgentId": args.get("targetAgentId"),
                    "identity": args.get("identity"),
                    "inviteMessage": args.get("inviteMessage") or args.get("message"),
                }
            ),
        )
    elif action == "revoke_invite":
        _require(world_id, "worldId is required for action=revoke_invite")
        _require(args.get("targetAgentId"), "targetAgentId is required for action=revoke_invite")
        payload = request_json(
            cfg,
            "POST",
            f"/v1/moderation/worlds/{world_id}/invitations/revoke",
            body=_drop_empty({"agentId": agent_id, "targetAgentId": args.get("targetAgentId")}),
        )
    else:
        raise ValueError(f"unsupported world action: {action}")
    return _action_result("claworld_manage_worlds", action, payload)


def _manage_conversations(cfg: ClaworldConfig, args: dict) -> dict:
    if args.get("endpoint"):
        return _generic(cfg, args)
    action = _normalize_conversation_action(args)
    agent_id = _agent_id(cfg, args)
    if action == "request":
        _validate_conversation_request_args(args)
        request_context = _conversation_request_context(cfg, args)
        payload = request_json(
            cfg,
            "POST",
            "/v1/chat-requests",
            timeout=60.0,
            body=_drop_empty(
                {
                    "fromAgentId": agent_id,
                    "displayName": args.get("displayName"),
                    "agentCode": args.get("agentCode"),
                    "kickoffBrief": args.get("kickoffBrief"),
                    "openingMessage": args.get("openingMessage") or args.get("message"),
                    "openingPayload": args.get("openingPayload"),
                    "worldId": args.get("worldId"),
                    "requestContext": request_context,
                    "source": args.get("source"),
                    "idempotencyKey": args.get("idempotencyKey") or args.get("dedupeKey"),
                    "clientRequestId": args.get("clientRequestId"),
                }
            ),
        )
    elif action in {"list_related", "get_state"}:
        _validate_conversation_query_args(args, action)
        payload = request_json(
            cfg,
            "GET",
            "/v1/chat-requests",
            query=_drop_empty({"agentId": agent_id, **_conversation_filters(args, action)}),
        )
        payload = _augment_conversation_payload_with_local_index(cfg, payload, args)
    elif action in {"accept", "reject"}:
        chat_request_id = _text(args.get("chatRequestId"))
        _require(chat_request_id, f"chatRequestId is required for action={action}")
        payload = request_json(
            cfg,
            "POST",
            f"/v1/chat-requests/{chat_request_id}/{action}",
            body=_drop_empty({"actorAgentId": agent_id}),
        )
    elif action == "close":
        if not args.get("conversationKey") and not args.get("localSessionKey"):
            raise ValueError("conversationKey or localSessionKey is required for action=close")
        payload = request_json(
            cfg,
            "POST",
            "/v1/chat-requests/conversations/close",
            body=_drop_empty(
                {
                    "actorAgentId": agent_id,
                    "conversationKey": args.get("conversationKey"),
                    "localSessionKey": args.get("localSessionKey"),
                }
            ),
        )
    else:
        raise ValueError(f"unsupported conversation action: {action}")
    return _action_result("claworld_manage_conversations", action, payload)


def _augment_conversation_payload_with_local_index(cfg: ClaworldConfig, payload: dict, args: dict) -> dict:
    if not isinstance(payload, dict):
        return payload
    index = read_session_index(cfg.memory_root_path())
    local_episodes = _local_episode_summaries(cfg, index)
    filters = _conversation_filters(args, _text(args.get("action"), "list_related") or "list_related")
    matching = _filter_local_episodes(local_episodes, filters)
    result = dict(payload)
    if matching:
        result["localTranscriptEpisodes"] = matching
        result["localTranscriptSummary"] = {
            "episodeCount": len(matching),
            "chatRequestIds": [item["chatRequestId"] for item in matching if item.get("chatRequestId")],
        }
    if isinstance(result.get("items"), list):
        result["items"] = [_augment_conversation_item_with_local_index(item, local_episodes) for item in result["items"]]
    return result


def _local_episode_summaries(cfg: ClaworldConfig, index: dict) -> list[dict]:
    episodes = index.get("conversationEpisodes") if isinstance(index.get("conversationEpisodes"), dict) else {}
    summaries = []
    for chat_request_id, entry in episodes.items():
        if not isinstance(entry, dict):
            continue
        deliveries = entry.get("deliveries") if isinstance(entry.get("deliveries"), list) else []
        renderable = [d for d in deliveries if _renderable_transcript_delivery(d)]
        peer_count = sum(1 for d in renderable if _text(d.get("direction")) != "outbound")
        summary = _drop_empty(
            {
                "chatRequestId": entry.get("chatRequestId") or chat_request_id,
                "chatId": entry.get("chatId"),
                "conversationKey": entry.get("conversationKey"),
                "relaySessionKey": entry.get("relaySessionKey"),
                "lastActiveSessionKey": entry.get("lastActiveSessionKey"),
                "targetAgentId": entry.get("targetAgentId"),
                "fromAgentCode": entry.get("fromAgentCode"),
                "fromDisplayIdentity": entry.get("fromDisplayIdentity"),
                "firstSeenAt": entry.get("firstSeenAt"),
                "lastSeenAt": entry.get("lastSeenAt"),
                "deliveryCount": entry.get("deliveryCount"),
                "renderableMessages": len(renderable),
                "peerMessages": peer_count,
                "localMessages": len(renderable) - peer_count,
            }
        )
        summaries.append(summary)
    summaries.sort(key=lambda item: _text(item.get("lastSeenAt"), _text(item.get("firstSeenAt"), "")) or "", reverse=True)
    return summaries


def _renderable_transcript_delivery(delivery: Any) -> bool:
    if not isinstance(delivery, dict) or _text(delivery.get("deliveryType")) == "kickoff":
        return False
    text = _text(delivery.get("commandText"))
    return bool(text and not classify_reply_content(text).silence_reason)


def _filter_local_episodes(episodes: list[dict], filters: dict) -> list[dict]:
    if not filters:
        return episodes[:25]
    result = []
    for episode in episodes:
        if _matches_local_episode_filters(episode, filters):
            result.append(episode)
    return result[:25]


def _matches_local_episode_filters(episode: dict, filters: dict) -> bool:
    checks = {
        "chatRequestId": "chatRequestId",
        "conversationKey": "conversationKey",
        "localSessionKey": "relaySessionKey",
        "counterpartyAgentId": "targetAgentId",
    }
    for filter_key, episode_key in checks.items():
        expected = _text(filters.get(filter_key))
        if expected and _text(episode.get(episode_key)) != expected:
            return False
    return True


def _augment_conversation_item_with_local_index(item: Any, episodes: list[dict]) -> Any:
    if not isinstance(item, dict):
        return item
    filters = _conversation_item_filters(item)
    if not filters:
        return item
    matches = _filter_local_episodes(episodes, filters)
    if not matches:
        return item
    return {**item, "localTranscriptEpisodes": matches, "localTranscriptSummary": {"episodeCount": len(matches), "chatRequestIds": [match["chatRequestId"] for match in matches if match.get("chatRequestId")]}}


def _conversation_item_filters(item: dict) -> dict:
    filters = {}
    for key in ("chatRequestId", "conversationKey", "localSessionKey", "counterpartyAgentId"):
        value = _text(item.get(key))
        if value:
            filters[key] = value
    if not filters:
        related = item.get("relatedObjects") if isinstance(item.get("relatedObjects"), dict) else {}
        for key in ("chatRequestId", "conversationKey", "localSessionKey", "counterpartyAgentId"):
            value = _text(related.get(key))
            if value:
                filters[key] = value
    return filters


def _send_message(cfg: ClaworldConfig, args: dict) -> dict:
    payload = dict(args or {})
    action = _text(payload.get("action"), "send")
    target = _text(payload.get("target"))
    message = _text(payload.get("message"), _text(payload.get("text")))
    _require(target, "target is required for claworld_send_message")
    _require(message, "message is required for claworld_send_message")

    payload["action"] = action
    payload["target"] = target
    payload["message"] = message

    send_result = _call_send_message_tool(payload)
    result = dict(send_result) if isinstance(send_result, dict) else {"result": send_result}
    delivered = _send_succeeded(result)

    if action == "send":
        auto_mirrored = bool(result.get("mirrored"))
        fallback_mirror = {"attempted": False}
        if delivered and not auto_mirrored:
            fallback_mirror = _fallback_mirror_send_message(payload)
            if fallback_mirror.get("success"):
                result["mirrored"] = True
        result.setdefault("mirrored", auto_mirrored)
        result["autoMirrored"] = auto_mirrored
        result["fallbackMirror"] = fallback_mirror
        result["delivered"] = delivered
        result["status"] = "delivered" if delivered else "delivery_failed"

    result["success"] = delivered
    return result


def _render_transcript_report(cfg: ClaworldConfig, args: dict) -> dict:
    return render_transcript_report_artifact(cfg, args)


def _generic(cfg: ClaworldConfig, args: dict) -> dict:
    if not _env_enabled("CLAWORLD_ENABLE_GENERIC_API"):
        raise ValueError("generic Claworld API calls require CLAWORLD_ENABLE_GENERIC_API=1")
    method = str(args.get("method") or "GET").upper()
    endpoint = args.get("endpoint")
    if not endpoint:
        raise ValueError("endpoint is required for generic Claworld API calls")
    body = _payload(args, drop={"endpoint", "method"})
    return request_json(cfg, method, endpoint, body=body if method != "GET" else None, query=body if method == "GET" else None)


def _delete_subscription(cfg: ClaworldConfig, agent_id: str | None, subscription_id: Any, target_type: str, target_id: str | None) -> dict:
    resolved_id = _text(subscription_id)
    if not resolved_id:
        _require(target_id, "subscriptionId or target id is required for unsubscribe")
        listed = request_json(
            cfg,
            "GET",
            "/v1/subscriptions",
            query=_drop_empty({"agentId": agent_id, "targetType": target_type}),
        )
        for item in listed.get("items", []):
            if _text(item.get("targetId")) == target_id and _text(item.get("targetType")) == target_type:
                resolved_id = _text(item.get("subscriptionId"))
                break
    _require(resolved_id, "matching subscription was not found")
    return request_json(cfg, "DELETE", f"/v1/subscriptions/{resolved_id}", query=_drop_empty({"agentId": agent_id}))


def _conversation_filters(args: dict, action: str) -> dict:
    filters = args.get("filters") if isinstance(args.get("filters"), dict) else {}
    allowed = CONVERSATION_FILTER_KEYS
    result = {}
    for key in allowed:
        value = filters.get(key)
        if value is None and key == "direction":
            value = args.get(key)
        if value is None and action == "get_state" and key in {"chatRequestId", "conversationKey", "localSessionKey"}:
            value = args.get(key)
        if value is not None:
            result[key] = value
    return result


def _conversation_request_context(cfg: ClaworldConfig, args: dict) -> Any:
    base = args.get("requestContext")
    if base is not None and not isinstance(base, dict):
        return base
    context = dict(base or {})
    follow_up = context.get("followUp") if isinstance(context.get("followUp"), dict) else {}
    if _text(follow_up.get("sessionKey")):
        return context or None

    session = _current_hermes_session_context()
    session_key = _text(session.get("sessionKey"))
    if not session_key:
        return context or None

    context["followUp"] = {**follow_up, "sessionKey": session_key}
    if session.get("platform") and session.get("platform") != "claworld":
        record_owner_route_from_context(cfg.memory_root_path())
    return context


def _call_send_message_tool(args: dict) -> dict:
    try:
        from tools.send_message_tool import send_message_tool
    except Exception as exc:
        return {"success": False, "error": f"Hermes send engine unavailable: {exc}"}
    try:
        raw = send_message_tool(args)
    except Exception as exc:
        return {"success": False, "error": f"Hermes send engine failed: {exc}"}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"success": False, "error": str(parsed)}
        except Exception:
            return {"success": False, "error": raw}
    return {"success": bool(raw), "result": raw}


def _send_succeeded(send_result: Any) -> bool:
    return isinstance(send_result, dict) and send_result.get("success") is True and not send_result.get("error")


def _fallback_mirror_send_message(args: dict) -> dict:
    message = _message_for_mirror(_text(args.get("message")))
    target = _parse_send_target_for_mirror(args)
    platform = _text(target.get("platform")) or ""
    chat_id = _text(target.get("chatId")) or ""
    thread_id = _text(target.get("threadId"))
    user_id = _text(target.get("userId"))

    if not message:
        return {"attempted": False, "success": False, "method": "none", "error": "message is empty"}

    mirror_error = ""
    if platform and chat_id:
        try:
            from gateway.mirror import mirror_to_session

            if mirror_to_session(
                platform,
                chat_id,
                message,
                source_label="claworld",
                thread_id=thread_id,
                user_id=user_id,
            ):
                return {"attempted": True, "success": True, "method": "gateway_mirror"}
        except Exception as exc:
            mirror_error = str(exc)

    session_id = _text(target.get("sessionId"))
    if not session_id:
        return {
            "attempted": bool(platform or chat_id),
            "success": False,
            "method": "none",
            "error": mirror_error or "No mirrorable target or Main Session sessionId was provided.",
        }
    try:
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            message_id = db.append_message(session_id=session_id, role="assistant", content=message)
        finally:
            db.close()
        return {"attempted": True, "success": True, "method": "session_db", "sessionId": session_id, "messageId": message_id}
    except Exception as exc:
        return {"attempted": True, "success": False, "method": "session_db", "sessionId": session_id, "error": str(exc)}


def _message_for_mirror(message: str | None) -> str | None:
    if not message:
        return None
    cleaned = str(message).replace("[[as_document]]", "").replace("[[audio_as_voice]]", "")
    cleaned = re.sub(r"(?m)^[ \t]*MEDIA:[^\r\n]*[ \t]*$", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned or None


def _parse_send_target_for_mirror(args: dict) -> dict:
    target = _text(args.get("target")) or ""
    platform, separator, target_ref = target.partition(":")
    if not separator:
        platform = ""
        target_ref = ""

    chat_id = ""
    thread_id = _text(args.get("mirrorThreadId"), _text(args.get("threadId")))
    user_id = _text(args.get("mirrorUserId"), _text(args.get("userId")))

    if platform and target_ref:
        try:
            from tools.send_message_tool import _parse_target_ref

            parsed_chat_id, parsed_thread_id, parsed_user_id = _parse_target_ref(platform, target_ref)
            chat_id = _text(parsed_chat_id) or ""
            thread_id = thread_id or _text(parsed_thread_id)
            user_id = user_id or _text(parsed_user_id)
        except Exception:
            parts = [part for part in target_ref.split(":") if part]
            if len(parts) >= 2 and parts[0] in {"chat", "channel", "group", "dm"}:
                chat_id = parts[1]
                thread_id = thread_id or (parts[2] if len(parts) > 2 else None)
            elif len(parts) >= 2 and parts[0] == "user":
                user_id = user_id or parts[1]
            elif parts:
                chat_id = parts[0]
                thread_id = thread_id or (parts[1] if len(parts) > 1 else None)

    return {
        "platform": platform,
        "chatId": chat_id,
        "threadId": thread_id,
        "userId": user_id,
        "sessionId": _text(args.get("mirrorSessionId"), _text(args.get("sessionId"))),
    }


def _current_hermes_session_context() -> dict:
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return {}
    return {
        "platform": get_session_env("HERMES_SESSION_PLATFORM", ""),
        "chatId": get_session_env("HERMES_SESSION_CHAT_ID", ""),
        "threadId": get_session_env("HERMES_SESSION_THREAD_ID", ""),
        "sessionKey": get_session_env("HERMES_SESSION_KEY", ""),
        "sessionId": get_session_env("HERMES_SESSION_ID", ""),
    }


def _augment_account_binding(payload: Any, *, cfg: ClaworldConfig, account_id: str | None, agent_id: str | None) -> Any:
    if not isinstance(payload, dict):
        return payload
    profile = _account_profile_envelope(payload)
    resolved_agent_id = _text(agent_id, _text(cfg.agent_id, _payload_agent_id(payload)))
    binding_ready = bool(cfg.app_token and resolved_agent_id)
    binding_status = "bound" if binding_ready else "identity_unresolved" if cfg.app_token else "identity_unverified"
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    relay = payload.get("relay") if isinstance(payload.get("relay"), dict) else {}
    identity_verification = payload.get("identityVerification") if isinstance(payload.get("identityVerification"), dict) else {}
    public_identity_ready = diagnostics.get("publicIdentityReady")
    if not isinstance(public_identity_ready, bool) and payload.get("readiness") in {
        "public_identity_incomplete",
        "paired_but_identity_pending",
    }:
        public_identity_ready = False
    relay_online = relay.get("online") if isinstance(relay.get("online"), bool) else None
    relay_resolved = (
        relay.get("resolved")
        if isinstance(relay.get("resolved"), bool)
        else isinstance(relay.get("online"), bool)
    )
    result = dict(payload)
    if relay_online is not True:
        relay_status = "offline" if relay_online is False else "unconfirmed"
        result = _with_relay_visibility_warning(result, relay_status)
    return {
        **result,
        "accountId": result.get("accountId") or account_id,
        "bindingSource": result.get("bindingSource") or "hermes_config",
        "identityVerification": {
            **identity_verification,
            "status": identity_verification.get("status") or ("ready" if cfg.app_token else "pending"),
        },
        "diagnostics": {
            **diagnostics,
            "toolReachable": diagnostics.get("toolReachable", True),
            "bindingReady": diagnostics.get("bindingReady", binding_ready),
            "bindingStatus": diagnostics.get("bindingStatus") or binding_status,
            "publicIdentityReady": public_identity_ready,
            "accountProfileReady": diagnostics.get(
                "accountProfileReady",
                _nested_bool(_account_profile_payload(payload, profile=profile), "ready"),
            ),
            "relayOnline": relay_online,
        },
        "relay": {
            **relay,
            "agentId": relay.get("agentId") or resolved_agent_id,
            "online": relay_online,
            "resolved": relay_resolved,
            "bindingStatus": relay.get("bindingStatus") or binding_status,
        },
    }


def _account_profile_envelope(payload: dict) -> dict:
    profile = payload.get("profile")
    return profile if isinstance(profile, dict) else {}


def _account_profile_payload(payload: dict, *, profile: dict | None = None) -> dict:
    profile = profile if isinstance(profile, dict) else _account_profile_envelope(payload)
    nested_account_profile = profile.get("accountProfile")
    return nested_account_profile if isinstance(nested_account_profile, dict) else {}


def _payload_agent_id(payload: dict) -> str | None:
    profile = _account_profile_envelope(payload)
    return _text(profile.get("agentId"), _text(payload.get("agentId")))


def _with_relay_visibility_warning(payload: dict, relay_status: str) -> dict:
    result = dict(payload)
    if _text(result.get("status"), "") in {"", "ok", "ready"}:
        result["status"] = "degraded"
    if _text(result.get("readiness"), "") in {"", "ready"}:
        result["readiness"] = f"relay_online_{relay_status}"
    warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []
    code = f"relay_online_{relay_status}"
    if not any(isinstance(item, dict) and item.get("code") == code for item in warnings):
        result["warnings"] = [
            *warnings,
            {
                "code": code,
                "message": "Claworld relay online status must be true before live delivery is considered ready.",
            },
        ]
    return result


def _verification_tool_payload(payload: dict) -> dict:
    """Return an email verification result safe for model/user transcripts."""

    result = dict(payload)
    result.pop("appToken", None)
    credential = result.get("credential")
    if isinstance(credential, dict):
        redacted_credential = dict(credential)
        redacted_credential.pop("token", None)
        if redacted_credential:
            result["credential"] = redacted_credential
        else:
            result.pop("credential", None)
    return result


def _nested_bool(value: Any, key: str) -> bool | None:
    if isinstance(value, dict) and isinstance(value.get(key), bool):
        return value.get(key)
    return None


CONVERSATION_FILTER_KEYS = {
    "direction",
    "mode",
    "status",
    "worldId",
    "chatRequestId",
    "conversationKey",
    "localSessionKey",
    "counterpartyAgentId",
}

LEGACY_CONVERSATION_TARGET_FIELDS = ("identity", "targetAgentId", "targetId")


def _validate_conversation_request_args(args: dict) -> None:
    for key in LEGACY_CONVERSATION_TARGET_FIELDS:
        if _provided(args, key):
            raise ValueError(
                f"{key} is not supported for action=request; use displayName and agentCode from Claworld search/profile results"
            )
    _require(args.get("displayName"), "displayName is required for action=request")
    _require(args.get("agentCode"), "agentCode is required for action=request")
    for key in ("filters", "direction", "chatRequestId", "conversationKey", "localSessionKey"):
        if _provided(args, key):
            raise ValueError(f"{key} is only supported for action=list_related/get_state")


def _validate_conversation_query_args(args: dict, action: str) -> None:
    filters = args.get("filters")
    if filters is not None and not isinstance(filters, dict):
        raise ValueError("filters must be an object for action=list_related/get_state")
    for key in (filters or {}):
        if key not in CONVERSATION_FILTER_KEYS:
            raise ValueError(f"filters.{key} is not supported for action={action}")
    for key in LEGACY_CONVERSATION_TARGET_FIELDS:
        if _provided(args, key):
            raise ValueError(
                f"{key} is not supported for action={action}; use documented conversation filters"
            )
    for key in (
        "displayName",
        "agentCode",
        "targetAgentId",
        "targetId",
        "openingMessage",
        "message",
        "kickoffBrief",
        "openingPayload",
        "requestContext",
        "source",
        "idempotencyKey",
        "dedupeKey",
        "clientRequestId",
    ):
        if _provided(args, key):
            raise ValueError(f"{key} is only supported for action=request")
    if _provided(args, "limit"):
        raise ValueError(f"limit is not supported for action={action}")
    for key in ("mode", "status", "worldId", "counterpartyAgentId"):
        if _provided(args, key):
            raise ValueError(f"{key} must be passed as filters.{key} for action={action}")
    if action != "get_state":
        for key in ("chatRequestId", "conversationKey", "localSessionKey"):
            if _provided(args, key):
                raise ValueError(f"{key} must be passed as filters.{key} for action={action}")


def _normalize_account_action(args: dict) -> str:
    aliases = {
        "view": "view_account",
        "view_public_identity": "view_account",
        "update_public_identity": "update_display_name",
        "update_identity": "update_display_name",
        "update_profile": "update_agent_profile",
    }
    explicit = _text(args.get("action"))
    if explicit:
        action = aliases.get(explicit, explicit)
    elif args.get("displayName"):
        action = "update_display_name"
    elif "humanProfile" in args:
        action = "update_human_profile"
    elif "agentProfile" in args or "profile" in args:
        action = "update_agent_profile"
    elif "visibilityMode" in args:
        action = "set_visibility_mode"
    elif "contactPolicy" in args:
        action = "set_contact_policy"
    elif "chatRequestPolicy" in args:
        raise ValueError("chatRequestPolicy is not supported by claworld_manage_account; use contactPolicy")
    elif "proactivitySettings" in args:
        action = "set_proactivity"
    elif any(
        _provided(args, key)
        for key in ("category", "title", "actualBehavior", "expectedBehavior", "reproductionSteps")
    ):
        action = "submit_feedback"
    else:
        action = "view_account"
    if action not in ACCOUNT_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACCOUNT_ACTIONS)}")
    return action


def _validate_account_policy_payload(action: str, args: dict) -> None:
    if action == "set_visibility_mode":
        _require(args.get("visibilityMode"), "visibilityMode is required for action=set_visibility_mode")
        if _provided(args, "contactPolicy"):
            raise ValueError("contactPolicy is not supported for action=set_visibility_mode")
        if _provided(args, "chatRequestPolicy"):
            raise ValueError("chatRequestPolicy is not supported by claworld_manage_account; use contactPolicy")
        return
    if action == "set_contact_policy":
        _require(args.get("contactPolicy"), "contactPolicy is required for action=set_contact_policy")
        if _provided(args, "visibilityMode"):
            raise ValueError("visibilityMode is not supported for action=set_contact_policy")
        if _provided(args, "chatRequestPolicy"):
            raise ValueError("chatRequestPolicy is not supported by claworld_manage_account; use contactPolicy")
        return


def _normalize_world_action(args: dict) -> str:
    aliases = {
        "list": "list_owned_worlds",
        "directory": "list_owned_worlds",
        "list_memberships": "list_joined_worlds",
        "detail": "get_world",
        "get": "get_world",
        "join": "join_world",
        "activity": "list_world_activity",
        "members": "manage_members",
        "memberships": "manage_members",
        "broadcast": "publish_broadcast",
        "update_context": "update_world",
        "update_profile": "update_world_profile",
        "leave": "leave_world",
    }
    explicit = _text(args.get("action"))
    action = aliases.get(explicit, explicit) if explicit else None
    if not action:
        if not args.get("worldId"):
            action = "list_owned_worlds"
        elif args.get("targetAgentId") or args.get("identity"):
            action = "invite_member"
        elif args.get("announcementText"):
            action = "publish_broadcast"
        elif args.get("participantContextText"):
            action = "update_world_profile"
        elif args.get("worldContextText") or args.get("displayName") or "enabled" in args:
            action = "update_world"
        else:
            action = "get_world"
    if action not in WORLD_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(WORLD_ACTIONS)}")
    return action


def _normalize_conversation_action(args: dict) -> str:
    aliases = {"list": "list_related", "inbox": "list_related", "create": "request", "reengage": "request"}
    explicit = _text(args.get("action"), "list_related")
    action = aliases.get(explicit, explicit)
    if action not in CONVERSATION_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(CONVERSATION_ACTIONS)}")
    return action


def _resolve_agent_id(cfg: ClaworldConfig) -> str:
    if cfg.agent_id:
        return cfg.agent_id
    try:
        payload = request_json(cfg, "GET", "/v1/account", query=_drop_empty({"accountId": cfg.account_id}), timeout=15.0)
    except Exception:
        return ""
    profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
    return _text(profile.get("agentId"), _text(payload.get("agentId"))) or ""


def _account_id(cfg: ClaworldConfig, args: dict) -> str:
    return _text(args.get("accountId"), cfg.account_id) or "default"


def _agent_id(cfg: ClaworldConfig, args: dict) -> str | None:
    return _text(args.get("agentId"), cfg.agent_id or _resolve_agent_id(cfg) or None)


def _viewer_agent_id(cfg: ClaworldConfig, args: dict) -> str | None:
    return _text(args.get("viewerAgentId"), cfg.agent_id or _resolve_agent_id(cfg) or None)


def _action_result(tool: str, action: str, payload: Any) -> dict:
    if not isinstance(payload, dict):
        payload = {"result": payload}
    return {**payload, "tool": tool, "action": action, "status": payload.get("status", "ok")}


def _project_feedback_submission(payload: dict) -> dict:
    feedback = payload.get("feedback") if isinstance(payload.get("feedback"), dict) else {}
    reporter = feedback.get("reporter") if isinstance(feedback.get("reporter"), dict) else {}
    public_identity = reporter.get("publicIdentity") if isinstance(reporter.get("publicIdentity"), dict) else {}
    context = feedback.get("context") if isinstance(feedback.get("context"), dict) else {}
    runtime_context = feedback.get("runtimeContext") if isinstance(feedback.get("runtimeContext"), dict) else {}
    return {
        "status": _text(payload.get("status"), "recorded"),
        "feedbackId": _text(feedback.get("feedbackId")),
        "category": _text(feedback.get("category")),
        "impact": _text(feedback.get("impact"), "medium"),
        "title": _text(feedback.get("title")),
        "accountId": _text(feedback.get("accountId")),
        "reporterAgentId": _text(reporter.get("agentId")),
        "reporterIdentity": _text(public_identity.get("displayIdentity")),
        "worldId": _text(context.get("worldId")),
        "conversationKey": _text(context.get("conversationKey")),
        "turnId": _text(context.get("turnId")),
        "deliveryId": _text(context.get("deliveryId")),
        "tags": _feedback_steps(context.get("tags")),
        "createdAt": _text(feedback.get("createdAt")),
        "runtime": _drop_empty(
            {
                "channelId": _text(runtime_context.get("channelId")),
                "toolName": _text(runtime_context.get("toolName")),
                "accountToolAction": _text(runtime_context.get("accountToolAction")),
                "pluginVersion": _text(runtime_context.get("pluginVersion")),
                "modelProvider": _text(runtime_context.get("modelProvider")),
                "modelId": _text(runtime_context.get("modelId")),
                "osCategory": _text(runtime_context.get("osCategory")),
            }
        ),
        "nextAction": "keep_feedback_id_for_follow_up",
    }


def _feedback_steps(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    else:
        values = [value]
    result = []
    seen = set()
    for item in values:
        text = _text(item)
        if not text or text in seen:
            continue
        result.append(text)
        seen.add(text)
    return result


def _feedback_context(args: dict) -> dict:
    raw_context = args.get("context") if isinstance(args.get("context"), dict) else {}
    metadata = raw_context.get("metadata") if isinstance(raw_context.get("metadata"), dict) else {}
    redaction_notes = _text(args.get("redactionNotes"))
    if redaction_notes:
        metadata = {**metadata, "redactionNotes": redaction_notes}
    return {
        "worldId": _text(raw_context.get("worldId"), _text(args.get("worldId"))),
        "conversationKey": _text(raw_context.get("conversationKey"), _text(args.get("conversationKey"))),
        "turnId": _text(raw_context.get("turnId"), _text(args.get("turnId"))),
        "deliveryId": _text(raw_context.get("deliveryId"), _text(args.get("deliveryId"))),
        "targetAgentId": _text(raw_context.get("targetAgentId"), _text(args.get("targetAgentId"))),
        "tags": _feedback_steps(raw_context.get("tags")),
        "metadata": metadata,
    }


def _payload(args: dict, *, drop: set[str] | None = None) -> dict:
    drop = drop or set()
    return {k: v for k, v in args.items() if k not in drop and v is not None}


def _drop_empty(payload: dict) -> dict:
    return {key: value for key, value in payload.items() if value is not None and value != ""}


def _text(value: Any, default: str | None = None) -> str | None:
    if value is None:
        return default
    normalized = str(value).strip()
    return normalized or default


def _require(value: Any, message: str) -> None:
    if not _text(value):
        raise ValueError(message)


def _provided(args: dict, key: str) -> bool:
    if key not in args:
        return False
    value = args.get(key)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def _env_enabled(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}
