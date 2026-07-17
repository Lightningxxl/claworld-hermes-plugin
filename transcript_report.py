"""Local transcript report rendering for Claworld conversations."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ClaworldConfig, hermes_home_path
from .protocol import classify_reply_content
from .transcript_report_stylekit import display_cols
from .transcript_report_styles import resolve_report_style
from .transcript_report_types import TranscriptContextBlock, TranscriptHeader, TranscriptMessage
from .working_memory import append_journal, atomic_write_text, read_session_index


DEFAULT_WIDTH = 720
DEFAULT_MAX_PAGE_HEIGHT = 8000
MAX_PAGE_HEIGHT = 32000
DOCUMENT_DELIVERY_DIRECTIVE = "[[as_document]]"

TIME_SPLIT_SECONDS = 5 * 60

PUBLIC_HEADER_RENDER_FIELDS = {
    "chatMode",
    "worldName",
    "initiatedBy",
    "topic",
    "title",
    "peerProfile",
    "worldContext",
    "localIdentity",
    "peerIdentity",
    "localLabel",
    "peerLabel",
}
MANUAL_RENDER_FIELDS = {"messages", "reportType", *PUBLIC_HEADER_RENDER_FIELDS}
STORED_RENDER_FIELDS = {"chatRequestId", *PUBLIC_HEADER_RENDER_FIELDS}
TOP_LEVEL_RENDER_FIELDS = {"mode", "manual", "style", "maxPageHeight", *STORED_RENDER_FIELDS}
MANUAL_MESSAGE_FIELDS = {"from", "text", "createdAt"}


def render_transcript_report(cfg: ClaworldConfig, args: dict) -> dict:
    """Render a local Claworld transcript as BubbleSpec, SVG, and PNG files."""

    request = _normalize_render_request(args or {})
    render_args = request["renderArgs"]
    root = cfg.memory_root_path()
    source = _load_source_messages(request, root)
    header_context = _extract_transcript_header_context(source["messages"], source.get("summary"))
    normalized = _normalize_messages(source["messages"], cfg, render_args, header_context)
    if not normalized:
        raise ValueError("no visible transcript messages were found for rendering")

    selected = normalized
    selection = _selection_summary(request, len(selected))

    width = DEFAULT_WIDTH
    max_page_height = _int(
        render_args.get("maxPageHeight"),
        DEFAULT_MAX_PAGE_HEIGHT,
        minimum=900,
        maximum=MAX_PAGE_HEIGHT,
    )
    style = resolve_report_style(_report_style_name(render_args))
    participants = _participants(selected)
    title, subtitle = _header_text(render_args, selected, header_context)
    header = _transcript_header(
        render_args,
        selected,
        header_context,
        title=title,
        source_summary=source.get("summary"),
    )
    bubbles = _decorate_selection(selected, selection)
    bubble_spec = {
        "version": "1",
        "kind": "claworld.transcript_report",
        "scene": {
            "title": title,
            "subtitle": subtitle,
            "peerId": title,
            "peerProfile": subtitle,
            "peerProfileSource": header.context_source or "fallback",
            "header": header.as_dict(),
            "generatedAt": _iso_now(),
            "source": source["summary"],
            "selection": selection,
        },
        "canvas": {
            "width": width,
            "style": style.name,
            "maxPageHeight": max_page_height,
        },
        "participants": participants,
        "messages": [_bubble_message_payload(item) for item in bubbles],
    }

    measured = [style.measure_item(item, width) for item in bubbles]
    pages = style.paginate(measured, width, max_page_height, title, subtitle, header)
    artifact_id = _artifact_id(source["summary"], selection, style.name)
    output_dirs = _output_dirs()
    files = []
    for page in pages:
        svg_path = output_dirs["documents"] / f"{artifact_id}-p{page.page:02d}.svg"
        png_path = output_dirs["images"] / f"{artifact_id}-p{page.page:02d}.png"
        svg = style.render_svg(page)
        atomic_write_text(svg_path, svg)
        png_result = style.write_png(svg_path, png_path, page)
        files.append(
            {
                "page": page.page,
                "format": "svg",
                "path": str(svg_path),
                "width": page.width,
                "height": page.height,
                "sha256": _sha256(svg_path),
                "role": "source",
            }
        )
        files.append(
            {
                "page": page.page,
                "format": "png",
                "path": str(png_path),
                "width": page.width,
                "height": page.height,
                "sha256": _sha256(png_path),
                "role": "primary",
                "renderer": png_result["renderer"],
                "rendering": png_result,
            }
        )

    spec_path = output_dirs["documents"] / f"{artifact_id}.bubblespec.json"
    atomic_write_text(spec_path, json.dumps(bubble_spec, ensure_ascii=False, indent=2, sort_keys=True))
    stats = {
        "sourceMessages": len(source["messages"]),
        "normalizedMessages": len(normalized),
        "renderedMessages": len(selected),
        "pages": len(pages),
        "omittedBefore": selection.get("omittedBefore", 0),
        "omittedAfter": selection.get("omittedAfter", 0),
    }
    primary_pngs = [item["path"] for item in files if item["format"] == "png"]
    source_svgs = [item["path"] for item in files if item["format"] == "svg"]
    png_pages = [_artifact_page(item) for item in files if item["format"] == "png"]
    svg_pages = [_artifact_page(item) for item in files if item["format"] == "svg"]
    primary_media = _document_media_block(primary_pngs[:1])
    primary_media_batch = _document_media_block(primary_pngs)
    result = {
        "status": "ok",
        "mode": request["mode"],
        **({"chatRequestId": request["chatRequestId"]} if request["mode"] == "stored" else {}),
        "artifactId": artifact_id,
        "messageCount": len(selected),
        "pageCount": len(pages),
        "style": style.name,
        "artifacts": {
            "bubbleSpec": {
                "format": "bubblespec",
                "path": str(spec_path),
                "sha256": _sha256(spec_path),
            },
            "pngPages": png_pages,
            "svgPages": svg_pages,
        },
        "deliveryHint": {
            "deliveryDirective": DOCUMENT_DELIVERY_DIRECTIVE,
            "primaryMedia": primary_media,
            "primaryMediaBatch": primary_media_batch,
            "sourceSvgDocument": _document_media_block(source_svgs[:1]),
            "reportOwnerArgs": {
                "media_path": primary_pngs[0] if primary_pngs else None,
                "media_paths": primary_pngs,
                "send_source_svg": False,
            },
        },
        "diagnostics": {
            "source": source["summary"],
            "stats": stats,
        },
    }
    append_journal(
        root,
        {
            "kind": "transcript_report",
            "artifactId": artifact_id,
            "source": source["summary"],
            "selection": selection,
            "files": [{"format": item["format"], "page": item["page"], "path": item["path"]} for item in files],
            "stats": stats,
        },
    )
    return result


def _document_media_block(paths: list[str]) -> str | None:
    if not paths:
        return None
    return "\n".join([DOCUMENT_DELIVERY_DIRECTIVE, *(f"MEDIA:{path}" for path in paths)])


def _artifact_page(item: dict) -> dict:
    return {
        "page": item["page"],
        "format": item["format"],
        "path": item["path"],
        "width": item["width"],
        "height": item["height"],
        "sha256": item["sha256"],
        **(
            {"renderer": item["renderer"], "rendering": item["rendering"]}
            if item["format"] == "png"
            else {}
        ),
        **({"mediaRef": f"MEDIA:{item['path']}"} if item["format"] == "png" else {}),
    }


def _normalize_render_request(args: dict) -> dict:
    if not isinstance(args, dict):
        raise ValueError("render arguments must be an object")
    extra = sorted(set(args) - TOP_LEVEL_RENDER_FIELDS)
    if extra:
        raise ValueError(f"unsupported transcript render parameter(s): {', '.join(extra)}")

    mode = _text(args.get("mode"))
    if mode not in {"stored", "manual"}:
        raise ValueError("mode is required and must be one of stored or manual")

    render_args = {
        "mode": mode,
        "style": _report_style_name(args),
        "maxPageHeight": args.get("maxPageHeight"),
    }

    if mode == "stored":
        if args.get("manual") is not None:
            raise ValueError("manual must not be provided when mode=stored")
        chat_request_id = _text(args.get("chatRequestId"))
        if not chat_request_id:
            raise ValueError("chatRequestId is required when mode=stored")
        _validate_header_render_fields("stored", args)
        for key in PUBLIC_HEADER_RENDER_FIELDS:
            render_args[key] = args.get(key)
        return {
            "mode": mode,
            "chatRequestId": chat_request_id,
            "renderArgs": render_args,
        }

    stored_only = sorted(key for key in STORED_RENDER_FIELDS if args.get(key) is not None)
    if stored_only:
        raise ValueError(
            f"{', '.join(stored_only)} must not be provided at top level when mode=manual"
        )
    manual = args.get("manual")
    if not isinstance(manual, dict):
        raise ValueError("manual must be an object when mode=manual")
    _reject_unknown_nested("manual", manual, MANUAL_RENDER_FIELDS)
    _validate_header_render_fields("manual", manual)
    messages = manual.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("manual.messages must be a non-empty array when mode=manual")
    _validate_manual_messages(messages)
    for key in (*PUBLIC_HEADER_RENDER_FIELDS, "reportType"):
        render_args[key] = manual.get(key)
    return {
        "mode": mode,
        "messages": messages,
        "renderArgs": render_args,
    }


def _reject_unknown_nested(name: str, value: dict, allowed: set[str]) -> None:
    extra = sorted(set(value) - allowed)
    if extra:
        raise ValueError(f"unsupported {name} parameter(s): {', '.join(extra)}")


def _validate_header_render_fields(name: str, value: dict) -> None:
    chat_mode = _text(value.get("chatMode"))
    if chat_mode and chat_mode not in {"direct", "world"}:
        raise ValueError(f"{name}.chatMode must be direct or world")
    report_type = _text(value.get("reportType"))
    if report_type and report_type not in {"full", "excerpt"}:
        raise ValueError(f"{name}.reportType must be full or excerpt")
    initiated_by = _text(value.get("initiatedBy"))
    if initiated_by and initiated_by not in {"local", "peer"}:
        raise ValueError(f"{name}.initiatedBy must be local or peer")
    if chat_mode == "direct":
        if _text(value.get("worldName")):
            raise ValueError(f"{name}.worldName must not be provided when chatMode=direct")
        if _text(value.get("worldContext")):
            raise ValueError(f"{name}.worldContext must not be provided when chatMode=direct")


def _validate_manual_messages(messages: list) -> None:
    for idx, message in enumerate(messages, start=1):
        if not isinstance(message, dict):
            raise ValueError(f"manual.messages[{idx}] must be an object")
        _reject_unknown_nested(f"manual.messages[{idx}]", message, MANUAL_MESSAGE_FIELDS)
        side = _text(message.get("from"))
        if side not in {"peer", "local"}:
            raise ValueError(f"manual.messages[{idx}].from must be peer or local")
        if not _text(message.get("text")):
            raise ValueError(f"manual.messages[{idx}].text is required")


def _load_source_messages(request: dict, root: Path) -> dict:
    if request["mode"] == "manual":
        explicit_messages = request["messages"]
        return {
            "messages": explicit_messages,
            "summary": {"kind": "manual", "messageCount": len(explicit_messages)},
        }

    index = read_session_index(root)
    episodes = index.get("conversationEpisodes") if isinstance(index.get("conversationEpisodes"), dict) else {}
    episode = episodes.get(request["chatRequestId"]) if isinstance(episodes.get(request["chatRequestId"]), dict) else None
    if not episode:
        raise ValueError(f"chatRequestId was not found in local Claworld transcript index: {request['chatRequestId']}")
    deliveries = episode.get("deliveries") if isinstance(episode.get("deliveries"), list) else []
    if not deliveries:
        raise ValueError(f"chatRequestId was found but no deliveries were indexed: {request['chatRequestId']}")
    return {
        "messages": deliveries,
        "summary": {
            "kind": "chatRequestId",
            "chatRequestId": request["chatRequestId"],
            "chatId": episode.get("chatId"),
            "conversationKey": episode.get("conversationKey"),
            "relaySessionKey": episode.get("relaySessionKey"),
            "lastActiveSessionKey": episode.get("lastActiveSessionKey"),
            "worldId": episode.get("worldId"),
            "requestDirection": episode.get("requestDirection") or episode.get("direction"),
            "firstSeenAt": episode.get("firstSeenAt"),
            "lastSeenAt": episode.get("lastSeenAt"),
            "indexSource": "conversationEpisodes",
        },
    }


def _report_style_name(args: dict) -> str:
    return _text(args.get("style"), "claworld-comic-grid") or "claworld-comic-grid"


def _normalize_messages(
    raw_messages: list,
    cfg: ClaworldConfig,
    args: dict,
    header_context: dict | None = None,
) -> list[TranscriptMessage]:
    header_context = header_context or {}
    source_mode = _text(args.get("mode")) or ""
    trusted_local_identity = _public_header_value(header_context.get("localIdentity"))
    trusted_peer_identity = _public_header_value(header_context.get("peerIdentity")) or _public_header_value(
        header_context.get("peerId")
    )
    explicit_local_identity = _public_header_value(args.get("localIdentity")) or _public_header_value(
        args.get("localLabel")
    )
    explicit_peer_identity = _public_header_value(args.get("peerIdentity")) or _public_header_value(
        args.get("peerLabel")
    )
    if source_mode == "stored":
        local_identity = trusted_local_identity or explicit_local_identity
        peer_identity = trusted_peer_identity or explicit_peer_identity
    else:
        local_identity = explicit_local_identity or trusted_local_identity
        peer_identity = explicit_peer_identity or trusted_peer_identity
    local_id = _text(local_identity, cfg.agent_id) or "local-agent"
    peer_id = peer_identity or "peer-agent"
    local_label = _public_header_value(local_identity) or "Me"
    peer_label = _public_header_value(peer_identity) or "Peer"
    normalized: list[TranscriptMessage] = []
    for idx, raw in enumerate(raw_messages):
        if not isinstance(raw, dict):
            continue
        if "from" in raw and raw.get("from") in ("peer", "local"):
            side = "left" if raw["from"] == "peer" else "right"
            text = _text(raw.get("text"))
            created_at = _format_timestamp(raw.get("createdAt"))
            message_id = _text(raw.get("id"), f"msg-{idx + 1}") or f"msg-{idx + 1}"
            participant_id = peer_id if side == "left" else local_id
            participant_label = peer_label if side == "left" else local_label
        else:
            delivery_type = _text(raw.get("deliveryType"))
            if delivery_type == "kickoff":
                continue
            direction = _text(raw.get("direction"))
            from_agent_id = _text(raw.get("fromAgentId"))
            text = _text(raw.get("commandText"))
            if not text:
                continue
            classification = classify_reply_content(text)
            if classification.silence_reason:
                continue
            text = classification.text
            created_at = _format_timestamp(raw.get("turnCreatedAt") or raw.get("createdAt"))
            message_id = _text(raw.get("deliveryId"), f"msg-{idx + 1}") or f"msg-{idx + 1}"
            if direction == "outbound" or (not direction and from_agent_id and cfg.agent_id and from_agent_id == cfg.agent_id):
                side = "right"
                participant_id = local_id
                participant_label = local_label
            else:
                side = "left"
                participant_id = peer_id
                participant_label = peer_label
        cleaned_text, tags = _extract_control_tags(text)
        cleaned_text = _redact_text(cleaned_text)
        cleaned_text = _strip_internal_markup(cleaned_text)
        if not cleaned_text and not tags:
            continue
        normalized.append(
            TranscriptMessage(
                id=message_id,
                side=side,
                participant_id=participant_id,
                participant_label=participant_label,
                text=cleaned_text,
                created_at=created_at,
                tags=tags,
                source_index=idx,
            )
        )
    return normalized


def _selection_summary(request: dict, message_count: int) -> dict:
    if request["mode"] == "manual":
        return {
            "mode": "manual",
            "messageCount": message_count,
            "omittedBefore": 0,
            "omittedAfter": 0,
        }

    return {
        "mode": "stored",
        "chatRequestId": request["chatRequestId"],
        "messageCount": message_count,
        "omittedBefore": 0,
        "omittedAfter": 0,
    }


def _decorate_selection(messages: list[TranscriptMessage], selection: dict) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if selection.get("omittedBefore", 0) > 0:
        items.append({"kind": "ellipsis", "omitted": selection["omittedBefore"], "label": f"{selection['omittedBefore']} earlier messages omitted"})
    previous_ts: float | None = None
    for idx, message in enumerate(messages):
        ts = _parse_timeish(message.created_at)
        if message.created_at and (idx == 0 or (previous_ts is not None and ts is not None and ts - previous_ts > TIME_SPLIT_SECONDS)):
            label = _format_time_marker(message.created_at)
            if label:
                items.append({"kind": "time", "label": label, "timestamp": message.created_at})
        items.append({"kind": "message", "message": message})
        previous_ts = ts or previous_ts
    if selection.get("omittedAfter", 0) > 0:
        items.append({"kind": "ellipsis", "omitted": selection["omittedAfter"], "label": f"{selection['omittedAfter']} later messages omitted"})
    return items


def _participants(messages: list[TranscriptMessage]) -> list[dict[str, str]]:
    seen: dict[str, TranscriptMessage] = {}
    for message in messages:
        seen.setdefault(message.participant_id, message)
    if not seen:
        return []
    participants = []
    for participant_id, message in seen.items():
        participants.append(
            {
                "id": participant_id,
                "name": message.participant_label,
                "side": message.side,
                "avatar": _avatar_text(message.participant_label),
            }
        )
    return participants


def _bubble_message_payload(item: dict[str, Any]) -> dict:
    if item["kind"] == "ellipsis":
        return {"kind": "ellipsis", "omitted": item["omitted"], "label": item["label"]}
    if item["kind"] == "time":
        return {"kind": "time", "label": item["label"], "timestamp": item.get("timestamp", "")}
    message = item["message"]
    return {
        "id": message.id,
        "kind": "text",
        "from": message.participant_id,
        "side": message.side,
        "speaker": message.participant_label,
        "text": message.text,
        "createdAt": message.created_at,
        "tags": list(message.tags),
    }


def _header_text(
    args: dict,
    messages: list[TranscriptMessage],
    header_context: dict | None = None,
) -> tuple[str, str]:
    header_context = header_context or {}
    source_mode = _text(args.get("mode")) or ""
    explicit_title = _public_header_value(args.get("topic")) or _public_header_value(args.get("title"))
    explicit_peer_identity = _public_header_value(args.get("peerIdentity")) or _public_header_value(
        args.get("peerLabel")
    )
    trusted_peer_identity = _public_header_value(header_context.get("peerIdentity")) or _public_header_value(
        header_context.get("peerId")
    )
    peer_identity = (
        trusted_peer_identity or explicit_peer_identity
        if source_mode == "stored"
        else explicit_peer_identity or trusted_peer_identity
    )
    if not peer_identity:
        for message in messages:
            if message.side == "left":
                peer_identity = _public_header_value(message.participant_label)
                break
    peer_name = _display_name(peer_identity)
    explicit_world_name = _public_header_value(args.get("worldName"))
    trusted_world_name = _public_header_value(header_context.get("worldName"))
    world_name = (
        trusted_world_name or explicit_world_name
        if source_mode == "stored"
        else explicit_world_name or trusted_world_name
    )
    title = explicit_title or _semantic_header_title(peer_name, world_name)

    explicit_profile = _public_header_value(args.get("peerProfile"))
    trusted_profile = _public_header_value(header_context.get("peerProfile"))
    if explicit_profile and not (_text(args.get("mode")) == "stored" and trusted_profile):
        return title, explicit_profile
    profile = trusted_profile
    subtitle_parts = [part for part in (peer_identity, profile) if part]
    if not subtitle_parts and world_name:
        subtitle_parts.append(world_name)
    subtitle = " · ".join(subtitle_parts) or "Conversation transcript"
    return title, subtitle


def _transcript_header(
    args: dict,
    messages: list[TranscriptMessage],
    header_context: dict | None = None,
    *,
    title: str,
    source_summary: dict | None = None,
) -> TranscriptHeader:
    """Build public header facts without requiring Agent-authored metadata."""

    header_context = header_context or {}
    source_mode = _text(args.get("mode")) or ""
    explicit_chat_mode = _text(args.get("chatMode")) or ""
    trusted_chat_mode = _text(header_context.get("conversationMode")) or ""
    explicit_world_name = _public_header_value(args.get("worldName"))
    trusted_world_name = _public_header_value(header_context.get("worldName"))

    # Stored kickoff context is backend-authored and wins over optional fallback
    # hints.  Manual reports have no such guarantee, so their explicit fields win.
    if source_mode == "stored":
        chat_mode = trusted_chat_mode or explicit_chat_mode
        world_name = trusted_world_name or (explicit_world_name if chat_mode != "direct" else "")
    else:
        chat_mode = explicit_chat_mode or trusted_chat_mode
        world_name = explicit_world_name or trusted_world_name
    explicit_world_context = _public_header_value(args.get("worldContext"))
    trusted_world_context = _public_header_value(header_context.get("worldContext"))
    if not chat_mode and (world_name or explicit_world_context or trusted_world_context):
        chat_mode = "world"
    if chat_mode == "direct":
        world_name = ""

    explicit_local_identity = _public_header_value(args.get("localIdentity")) or _public_header_value(
        args.get("localLabel")
    )
    trusted_local_identity = _public_header_value(header_context.get("localIdentity"))
    explicit_peer_identity = _public_header_value(args.get("peerIdentity")) or _public_header_value(
        args.get("peerLabel")
    )
    trusted_peer_identity = _public_header_value(header_context.get("peerIdentity")) or _public_header_value(
        header_context.get("peerId")
    )
    if source_mode == "stored":
        local_identity = trusted_local_identity or explicit_local_identity
        peer_identity = trusted_peer_identity or explicit_peer_identity
    else:
        local_identity = explicit_local_identity or trusted_local_identity
        peer_identity = explicit_peer_identity or trusted_peer_identity
    local_identity = local_identity or _message_participant_identity(messages, "right") or "Me"
    peer_identity = peer_identity or _message_participant_identity(messages, "left") or "Peer"

    explicit_context = _public_header_value(args.get("peerProfile"))
    trusted_context = _public_header_value(header_context.get("peerProfile"))
    if source_mode == "stored":
        context_text = trusted_context or explicit_context
        context_source = (
            (_text(header_context.get("profileSource")) or "rawKickoffText")
            if trusted_context
            else ("explicit" if explicit_context else "")
        )
        world_context = trusted_world_context or explicit_world_context
        world_context_source = (
            (_text(header_context.get("worldContextSource")) or "rawKickoffText")
            if trusted_world_context
            else ("explicit" if explicit_world_context else "")
        )
    else:
        context_text = explicit_context or trusted_context
        context_source = (
            "explicit"
            if explicit_context
            else (_text(header_context.get("profileSource")) or ("fallback" if context_text else ""))
        )
        world_context = explicit_world_context or trusted_world_context
        world_context_source = (
            "explicit"
            if explicit_world_context
            else (_text(header_context.get("worldContextSource")) or ("fallback" if world_context else ""))
        )
    context_label = ""
    context_kind = "profile"
    if context_text:
        if chat_mode == "world":
            context_kind = "peerWorldMembershipProfile"
            context_label = "Peer · World"
        elif chat_mode == "direct":
            context_kind = "peerGlobalProfile"
            context_label = "Peer · Profile"
        else:
            context_label = "Peer Profile"

    context_blocks: list[TranscriptContextBlock] = []
    if context_text:
        context_blocks.append(
            TranscriptContextBlock(
                kind=context_kind,
                label=context_label,
                text=context_text,
                source=context_source,
            )
        )
    if chat_mode == "world" and world_context:
        context_blocks.append(
            TranscriptContextBlock(
                kind="worldContext",
                label="World Context",
                text=world_context,
                source=world_context_source,
            )
        )

    report_type = "full" if source_mode == "stored" else (_text(args.get("reportType")) or "")
    explicit_initiated_by = _text(args.get("initiatedBy")) or ""
    request_direction = _text((source_summary or {}).get("requestDirection")) or ""
    trusted_initiated_by = {
        "inbound": "peer",
        "outbound": "local",
    }.get(request_direction, "")
    initiated_by = (
        trusted_initiated_by or explicit_initiated_by
        if source_mode == "stored"
        else explicit_initiated_by
    )
    return TranscriptHeader(
        chat_mode=chat_mode,
        report_type=report_type,
        initiated_by=initiated_by,
        topic=_public_header_value(args.get("topic")) or title,
        world_name=world_name,
        local_identity=local_identity,
        peer_identity=peer_identity,
        context_label=context_label,
        context_text=context_text,
        context_source=context_source,
        context_blocks=tuple(context_blocks),
        date_label=_transcript_date_label(messages, source_summary),
        message_count=len(messages),
    )


def _message_participant_identity(messages: list[TranscriptMessage], side: str) -> str:
    for message in messages:
        if message.side == side:
            value = _public_header_value(message.participant_label)
            if value:
                return value
    return ""


def _transcript_date_label(
    messages: list[TranscriptMessage],
    source_summary: dict | None = None,
) -> str:
    dates: list[str] = []
    candidates = [message.created_at for message in messages]
    if not any(candidates) and isinstance(source_summary, dict):
        candidates.extend([source_summary.get("firstSeenAt"), source_summary.get("lastSeenAt")])
    for candidate in candidates:
        match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", str(candidate or ""))
        if match:
            value = match.group(0)
            if value not in dates:
                dates.append(value)
    if not dates:
        return ""
    first, last = dates[0], dates[-1]
    if first == last:
        return first[5:]
    if first[:4] == last[:4]:
        return f"{first[5:]}–{last[5:]}"
    return f"{first}–{last}"


def _semantic_header_title(peer_name: str, world_name: str) -> str:
    if peer_name and world_name:
        return f"{peer_name} — {world_name}"
    return peer_name or world_name or "Claworld conversation"


def _display_name(identity: str) -> str:
    return _public_header_value(str(identity or "").split("#", 1)[0])


def _public_header_value(value: Any) -> str:
    normalized = _text(value) or ""
    if re.search(r"(?i)(?:^|[\s(])(?:agt|req|wld|dlv|conversation|management)[_:-][a-z0-9]", normalized):
        return ""
    return normalized


def _extract_transcript_header_context(raw_messages: list, source_summary: dict | None = None) -> dict:
    merged: dict[str, str] = {}
    for raw in raw_messages:
        if not isinstance(raw, dict):
            continue
        candidates = [
            (_text(raw.get("contextText")), "contextText"),
            (_text(raw.get("untrustedContext")), "untrustedContext"),
        ]
        if _text(raw.get("deliveryType")) == "kickoff":
            candidates.append((_text(raw.get("commandText")), "rawKickoffText"))
        for candidate, source in candidates:
            _merge_header_context_candidate(merged, candidate, source)
        world_id = _text(raw.get("worldId"))
        if world_id:
            merged.setdefault("worldId", world_id)
            merged.setdefault("conversationMode", "world")
        from_display = _text(raw.get("fromDisplayIdentity"))
        if from_display and "peerIdentity" not in merged:
            merged["peerIdentity"] = from_display
    source_summary = source_summary if isinstance(source_summary, dict) else {}
    source_world_id = _text(source_summary.get("worldId"))
    if source_world_id:
        merged.setdefault("worldId", source_world_id)
        merged.setdefault("conversationMode", "world")
    conversation_key = _text(source_summary.get("conversationKey")) or ""
    if "conversationMode" not in merged and re.search(
        r"(?:^|[:/_-])direct(?:$|[:/_-])",
        conversation_key,
        flags=re.IGNORECASE,
    ):
        merged["conversationMode"] = "direct"
    profile, source = _select_header_profile(merged)
    return {
        key: value
        for key, value in {
            "peerId": merged.get("peerId"),
            "peerIdentity": merged.get("peerIdentity"),
            "localIdentity": merged.get("localIdentity"),
            "conversationMode": merged.get("conversationMode"),
            "worldName": merged.get("worldName"),
            "worldId": merged.get("worldId"),
            "peerGlobalProfile": merged.get("globalProfile"),
            "peerGlobalProfileSource": merged.get("globalProfileSource"),
            "peerWorldProfile": merged.get("worldProfile"),
            "peerWorldProfileSource": merged.get("worldProfileSource"),
            "worldContext": merged.get("worldContext"),
            "worldContextSource": merged.get("worldContextSource"),
            "peerProfile": profile,
            "profileSource": source,
        }.items()
        if value
    }


def _merge_header_context_candidate(merged: dict[str, str], text: str | None, source: str) -> None:
    if not text:
        return
    parsed = _parse_header_context_candidate(text, source)
    for key, value in parsed.items():
        normalized = _text(value)
        if normalized and _should_merge_header_value(merged, parsed, key, normalized):
            merged[key] = normalized


def _should_merge_header_value(merged: dict[str, str], parsed: dict[str, str], key: str, value: str) -> bool:
    source_priority_keys = {
        "globalProfile": "globalProfileSource",
        "worldProfile": "worldProfileSource",
        "worldContext": "worldContextSource",
        "globalProfileSource": "globalProfileSource",
        "worldProfileSource": "worldProfileSource",
        "worldContextSource": "worldContextSource",
    }
    source_key = source_priority_keys.get(key)
    if not source_key:
        return True
    current_source = merged.get(source_key)
    incoming_source = value if key.endswith("Source") else parsed.get(source_key)
    return _header_profile_source_priority(incoming_source) >= _header_profile_source_priority(current_source)


def _header_profile_source_priority(source: str | None) -> int:
    return {
        "rawKickoffText": 4,
        "contextText": 3,
        "untrustedContext": 2,
        "transcript": 1,
    }.get(_text(source), 0)


def _parse_header_context_candidate(text: str, source: str) -> dict[str, str]:
    value = str(text or "").strip()
    if not value:
        return {}
    value = _queued_turn_background(value)
    parsed: dict[str, str] = {}
    mode = _extract_conversation_mode(value)
    if mode:
        parsed["conversationMode"] = mode
    world_name, world_id = _extract_world_label(value)
    if world_name:
        parsed["worldName"] = world_name
    if world_id:
        parsed["worldId"] = world_id
    world_section = _markdown_section(value, "World Facts", 2)
    world_context = _markdown_named_code_block(world_section, "World Context", 3)
    if world_context:
        parsed["worldContext"] = _squash_whitespace(world_context)
        parsed["worldContextSource"] = source

    local_section = _markdown_section(value, "You", 2)
    if local_section:
        identity = _extract_identity(local_section)
        if identity:
            parsed["localIdentity"] = identity

    peer_section = _markdown_section(value, "Peer", 2)
    if peer_section:
        identity = _extract_identity(peer_section)
        if identity:
            parsed["peerIdentity"] = identity
        global_profile = _markdown_named_code_block(peer_section, "Global Profile", 3)
        world_profile = _markdown_named_code_block(peer_section, "World Membership Profile", 3)
        if global_profile:
            parsed["globalProfile"] = _squash_whitespace(global_profile)
            parsed["globalProfileSource"] = source
        if world_profile:
            parsed["worldProfile"] = _squash_whitespace(world_profile)
            parsed["worldProfileSource"] = source
    if local_section or peer_section:
        return parsed

    if source == "untrustedContext":
        plain_profile = _plain_profile_candidate(value)
        if plain_profile:
            parsed["globalProfile"] = plain_profile
            parsed["globalProfileSource"] = source
    return parsed


def _queued_turn_background(text: str) -> str:
    """Unwrap a backend Background document from a recognized queued-turn shell."""

    queued_section = _markdown_section(text, "Earlier Queued Turns", 2)
    if not queued_section:
        return text
    outer_fence = re.compile(
        r"(?ms)^\s*(?P<fence>`{4,}|~{4,})[^\r\n]*\r?\n"
        r"(?P<body>.*?)^\s*(?P=fence)\s*$"
    )
    for match in outer_fence.finditer(queued_section):
        body = match.group("body").strip()
        headings = _markdown_headings(body)
        titles = {(level, title.casefold()) for level, title, _start, _end in headings}
        if (1, "background") in titles and (
            (2, "conversation facts") in titles
            or ((2, "you") in titles and (2, "peer") in titles)
        ):
            return body
    return text


def _extract_conversation_mode(text: str) -> str:
    match = re.search(r"(?im)^\s*-\s*Mode:\s*`?([A-Za-z_-]+)`?\s*$", text)
    if not match:
        match = re.search(r"(?im)\bconversation[_\s-]*mode\b\s*[:=]\s*`?([A-Za-z_-]+)`?", text)
    value = _text(match.group(1).lower() if match else None)
    return value if value in {"world", "direct"} else ""


def _extract_world_label(text: str) -> tuple[str, str]:
    match = re.search(r"(?im)^\s*-\s*World:\s*([^\n(`]+?)\s*(?:\(`([^`]+)`\))?\s*$", text)
    if not match:
        return "", ""
    return _text(match.group(1)) or "", _text(match.group(2)) or ""


def _markdown_section(text: str, title: str, level: int) -> str:
    headings = _markdown_headings(text)
    target_index = next(
        (
            index
            for index, (heading_level, heading_title, _start, _end) in enumerate(headings)
            if heading_level == level and heading_title.casefold() == title.casefold()
        ),
        None,
    )
    if target_index is None:
        return ""
    _heading_level, _heading_title, _heading_start, start = headings[target_index]
    end = next(
        (
            heading_start
            for heading_level, _heading_title, heading_start, _heading_end in headings[target_index + 1 :]
            if heading_level <= level
        ),
        len(text),
    )
    return text[start:end].strip()


def _markdown_headings(text: str) -> list[tuple[int, str, int, int]]:
    """Return Markdown headings outside fenced code blocks with source spans."""

    headings: list[tuple[int, str, int, int]] = []
    active_fence = ""
    active_fence_length = 0
    offset = 0
    for line in str(text or "").splitlines(keepends=True):
        line_without_ending = line.rstrip("\r\n")
        fence_match = re.match(r"^\s*(`{3,}|~{3,})", line_without_ending)
        if fence_match:
            marker = fence_match.group(1)
            if not active_fence:
                active_fence = marker[0]
                active_fence_length = len(marker)
            elif marker[0] == active_fence and len(marker) >= active_fence_length:
                active_fence = ""
                active_fence_length = 0
            offset += len(line)
            continue
        if not active_fence:
            heading_match = re.match(
                r"^\s*(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$",
                line_without_ending,
            )
            if heading_match:
                headings.append(
                    (
                        len(heading_match.group(1)),
                        heading_match.group(2).strip(),
                        offset,
                        offset + len(line),
                    )
                )
        offset += len(line)
    return headings


def _markdown_named_code_block(text: str, title: str, level: int) -> str:
    section = _markdown_section(text, title, level)
    if not section:
        return ""
    match = re.search(r"```[^\n]*\n(.*?)\n```", section, flags=re.DOTALL)
    if match:
        return match.group(1).strip()
    lines = [line.strip() for line in section.splitlines() if line.strip() and not line.strip().startswith("#")]
    return "\n".join(lines).strip()


def _extract_identity(text: str) -> str:
    match = re.search(r"(?im)^\s*-\s*Identity:\s*`?([^`\n]+)`?\s*$", text)
    return _text(match.group(1)) if match else ""


def _plain_profile_candidate(text: str) -> str:
    value = _squash_whitespace(text)
    if not value or "# " in value or "```" in value:
        return ""
    if display_cols(value) > 420:
        return ""
    return value


def _select_header_profile(values: dict[str, str]) -> tuple[str, str]:
    mode = values.get("conversationMode")
    if mode == "world" and values.get("worldProfile"):
        return values["worldProfile"], values.get("worldProfileSource", "transcript")
    if mode == "direct" and values.get("globalProfile"):
        return values["globalProfile"], values.get("globalProfileSource", "transcript")
    if values.get("worldProfile"):
        return values["worldProfile"], values.get("worldProfileSource", "transcript")
    if values.get("globalProfile"):
        return values["globalProfile"], values.get("globalProfileSource", "transcript")
    return "", ""


def _format_time_marker(value: Any) -> str:
    ts = _parse_timeish(value)
    if ts is not None:
        try:
            return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
        except Exception:
            pass
    text = str(value or "").strip()
    match = re.search(r"(?:(\d{4})[-/])?(\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})", text)
    if match:
        return f"{int(match.group(2)):02d}-{int(match.group(3)):02d} {int(match.group(4)):02d}:{match.group(5)}"
    return ""


CONTROL_PATTERNS = (
    (re.compile(r"\[\[?\s*request[_\s-]*(?:conversation[_\s-]*)?end\s*\]?\]?", re.IGNORECASE), "request end"),
    (re.compile(r"\[\s*requeset\s+end\s*\]", re.IGNORECASE), "request end"),
    (re.compile(r"\[\[?\s*end\s*\]?\]?", re.IGNORECASE), "request end"),
    (re.compile(r"\[\[?\s*like\s*\]?\]?", re.IGNORECASE), "like"),
    (re.compile(r"\[\[?\s*dislike\s*\]?\]?", re.IGNORECASE), "dislike"),
)
GENERIC_CONTROL_PATTERN = re.compile(r"\[\[\s*([A-Za-z0-9][A-Za-z0-9 _-]{0,24})\s*\]\]")


def _extract_control_tags(text: str) -> tuple[str, list[str]]:
    cleaned = str(text or "")
    matches: list[tuple[int, int, str]] = []
    claimed_spans: list[tuple[int, int]] = []
    for pattern, label in CONTROL_PATTERNS:
        for match in pattern.finditer(cleaned):
            _record_tag_match(matches, claimed_spans, match.start(), match.end(), label)
    for match in GENERIC_CONTROL_PATTERN.finditer(cleaned):
        label = _normalize_tag_label(match.group(1))
        if label:
            _record_tag_match(matches, claimed_spans, match.start(), match.end(), label)
    tags: list[str] = []
    for _start, _end, label in sorted(matches, key=lambda item: item[0]):
        if label not in tags:
            tags.append(label)
    return _squash_whitespace(_remove_spans(cleaned, [(start, end) for start, end, _label in matches])), tags


def _record_tag_match(matches: list[tuple[int, int, str]], claimed_spans: list[tuple[int, int]], start: int, end: int, label: str) -> None:
    if any(start < claimed_end and end > claimed_start for claimed_start, claimed_end in claimed_spans):
        return
    matches.append((start, end, label))
    claimed_spans.append((start, end))


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text
    pieces: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        pieces.append(text[cursor:start])
        cursor = max(cursor, end)
    pieces.append(text[cursor:])
    return "".join(pieces)


def _normalize_tag_label(value: str) -> str:
    label = _squash_whitespace(str(value or "").replace("_", " ").replace("-", " ")).lower()
    return label[:24].strip()


def _redact_text(text: str) -> str:
    redacted = str(text or "")
    redacted = re.sub(
        r"(?i)\b(api[_-]?key|app[_-]?token|access[_-]?token|refresh[_-]?token|secret|password|authorization|bearer)\b\s*[:=]\s*[^\s,;]+",
        lambda m: f"{m.group(1)}=[redacted]",
        redacted,
    )
    redacted = re.sub(r"\b(?:sk|rk|pk|ghp|glpat)-[A-Za-z0-9_\-]{12,}\b", "[redacted-token]", redacted)
    redacted = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[redacted-email]", redacted)
    redacted = re.sub(r"(?<!\d)(?:\+?\d[\d\s().-]{8,}\d)(?!\d)", "[redacted-phone]", redacted)
    return redacted


def _strip_internal_markup(text: str) -> str:
    cleaned = str(text or "")
    cleaned = re.sub(r"```(?:json|text|markdown)?", "", cleaned)
    cleaned = cleaned.replace("```", "")
    internal_markers = (
        "Routing metadata:",
        "Claworld live conversation rules:",
        "Backend-authored Claworld context:",
        "Backend-authored Claworld command:",
        "Relay untrusted context:",
    )
    for marker in internal_markers:
        if marker in cleaned:
            cleaned = cleaned.split(marker, 1)[0]
    return _squash_whitespace(cleaned)


def _flatten_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") in {"text", "input_text", "output_text"} and item.get("text") is not None:
                    parts.append(str(item.get("text")))
                elif item.get("content") is not None and isinstance(item.get("content"), str):
                    parts.append(str(item.get("content")))
        return "\n".join(parts)
    if isinstance(content, dict):
        for key in ("text", "content", "message", "body"):
            if isinstance(content.get(key), str):
                return str(content.get(key))
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    return str(content)


def _format_timestamp(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ""
    text = str(value).strip()
    if not text:
        return ""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value).strip()


def _parse_timeish(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text.replace("Z", "+0000"), fmt).timestamp()
        except Exception:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _squash_whitespace(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in str(text or "").splitlines()]
    compact: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if not blank and compact:
                compact.append("")
            blank = True
        else:
            compact.append(line)
            blank = False
    return "\n".join(compact).strip()


def _output_dirs() -> dict[str, Path]:
    base = hermes_home_path()
    image_dir = base / "cache" / "images" / "claworld_reports"
    document_dir = base / "cache" / "documents" / "claworld_reports"
    image_dir.mkdir(parents=True, exist_ok=True)
    document_dir.mkdir(parents=True, exist_ok=True)
    return {"images": image_dir, "documents": document_dir}


def _artifact_id(source: dict, selection: dict, style_name: str) -> str:
    now = datetime.now().strftime("%Y%m%d-%H%M%S")
    style_slug = re.sub(r"[^a-z0-9]+", "-", style_name.lower()).strip("-") or "style"
    digest = hashlib.sha256(
        json.dumps({"source": source, "selection": selection, "style": style_name, "now": now}, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:10]
    return f"claworld-transcript-{style_slug}-{now}-{digest}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _avatar_text(label: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]", "", str(label or "A"))
    if clean:
        return clean[:2].upper()
    visible = "".join(ch for ch in str(label or "") if not ch.isspace() and unicodedata.category(ch)[0] in {"L", "N"})
    return (visible[:2] or "A").upper()


def _text(value: Any, default: str | None = None) -> str | None:
    if value is None:
        return default
    normalized = str(value).strip()
    return normalized or default


def _int(value: Any, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
