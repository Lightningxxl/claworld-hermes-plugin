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
from .transcript_report_types import TranscriptMessage
from .working_memory import append_journal, atomic_write_text, read_session_index


DEFAULT_WIDTH = 720
DEFAULT_MAX_PAGE_HEIGHT = 2600

TIME_SPLIT_SECONDS = 5 * 60

TOP_LEVEL_RENDER_FIELDS = {"mode", "stored", "manual", "style", "maxPageHeight"}
MANUAL_RENDER_FIELDS = {"messages", "title", "peerProfile", "localLabel", "peerLabel"}
REQUIRED_MANUAL_RENDER_FIELDS = {"messages", "title", "peerProfile", "localLabel", "peerLabel"}
STORED_RENDER_FIELDS = {"chatRequestId", "title", "peerProfile", "localLabel", "peerLabel"}
MANUAL_MESSAGE_FIELDS = {"from", "text", "createdAt"}


def render_transcript_report(cfg: ClaworldConfig, args: dict) -> dict:
    """Render a local Claworld transcript as BubbleSpec, SVG, and PNG files."""

    request = _normalize_render_request(args or {})
    render_args = request["renderArgs"]
    root = cfg.memory_root_path()
    source = _load_source_messages(request, root)
    header_context = _extract_transcript_header_context(source["messages"])
    normalized = _normalize_messages(source["messages"], cfg, render_args, header_context)
    if not normalized:
        raise ValueError("no visible transcript messages were found for rendering")

    selected = normalized
    selection = _selection_summary(request, len(selected))

    width = DEFAULT_WIDTH
    max_page_height = _int(render_args.get("maxPageHeight"), DEFAULT_MAX_PAGE_HEIGHT, minimum=900, maximum=8000)
    style = resolve_report_style(_report_style_name(render_args))
    participants = _participants(selected)
    title, subtitle = _header_text(render_args, selected, header_context)
    bubbles = _decorate_selection(selected, selection)
    bubble_spec = {
        "version": "1",
        "kind": "claworld.transcript_report",
        "scene": {
            "title": title,
            "subtitle": subtitle,
            "peerId": title,
            "peerProfile": subtitle,
            "peerProfileSource": (
                "explicit"
                if _public_header_value(render_args.get("peerProfile"))
                else header_context.get("profileSource", "fallback")
            ),
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
    pages = style.paginate(measured, width, max_page_height, title, subtitle)
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
            "primaryMedia": f"MEDIA:{primary_pngs[0]}" if primary_pngs else None,
            "primaryMediaBatch": "\n".join(f"MEDIA:{path}" for path in primary_pngs),
            "sourceSvgDocument": f"[[as_document]]\nMEDIA:{source_svgs[0]}" if source_svgs else None,
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


def _artifact_page(item: dict) -> dict:
    return {
        "page": item["page"],
        "format": item["format"],
        "path": item["path"],
        "width": item["width"],
        "height": item["height"],
        "sha256": item["sha256"],
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
        stored = args.get("stored")
        if not isinstance(stored, dict):
            raise ValueError("stored must be an object when mode=stored")
        _reject_unknown_nested("stored", stored, STORED_RENDER_FIELDS)
        chat_request_id = _text(stored.get("chatRequestId"))
        if not chat_request_id:
            raise ValueError("stored.chatRequestId is required when mode=stored")
        for key in ("title", "peerProfile", "localLabel", "peerLabel"):
            render_args[key] = stored.get(key)
        return {
            "mode": mode,
            "chatRequestId": chat_request_id,
            "renderArgs": render_args,
        }

    if args.get("stored") is not None:
        raise ValueError("stored must not be provided when mode=manual")
    manual = args.get("manual")
    if not isinstance(manual, dict):
        raise ValueError("manual must be an object when mode=manual")
    _reject_unknown_nested("manual", manual, MANUAL_RENDER_FIELDS)
    for key in sorted(REQUIRED_MANUAL_RENDER_FIELDS - {"messages"}):
        if not _text(manual.get(key)):
            raise ValueError(f"manual.{key} is required when mode=manual")
    messages = manual.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("manual.messages must be a non-empty array when mode=manual")
    _validate_manual_messages(messages)
    for key in ("title", "peerProfile", "localLabel", "peerLabel"):
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
        if not _text(message.get("createdAt")):
            raise ValueError(f"manual.messages[{idx}].createdAt is required")


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
    local_identity = _text(header_context.get("localIdentity"))
    peer_identity = _text(header_context.get("peerIdentity"), _text(header_context.get("peerId")))
    local_id = _text(local_identity, cfg.agent_id) or "local-agent"
    peer_id = peer_identity or "peer-agent"
    local_label = _public_header_value(args.get("localLabel")) or _public_header_value(local_identity) or "Me"
    peer_label = _public_header_value(args.get("peerLabel")) or _public_header_value(peer_identity) or "Peer"
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
    explicit_title = _public_header_value(args.get("title"))
    peer_identity = _public_header_value(header_context.get("peerIdentity")) or _public_header_value(
        header_context.get("peerId")
    )
    if not peer_identity:
        for message in messages:
            if message.side == "left":
                peer_identity = _public_header_value(message.participant_label)
                break
    peer_name = _display_name(peer_identity)
    world_name = _public_header_value(header_context.get("worldName"))
    title = explicit_title or _semantic_header_title(peer_name, world_name)

    explicit_profile = _public_header_value(args.get("peerProfile"))
    if explicit_profile:
        subtitle = explicit_profile
    else:
        profile = _public_header_value(header_context.get("peerProfile"))
        subtitle_parts = [part for part in (peer_identity, profile) if part]
        if not subtitle_parts and world_name:
            subtitle_parts.append(world_name)
        subtitle = " · ".join(subtitle_parts) or "Conversation transcript"
    return title, subtitle


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


def _extract_transcript_header_context(raw_messages: list) -> dict:
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
        from_display = _text(raw.get("fromDisplayIdentity"))
        if from_display and "peerIdentity" not in merged:
            merged["peerIdentity"] = from_display
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
        "globalProfileSource": "globalProfileSource",
        "worldProfileSource": "worldProfileSource",
    }
    source_key = source_priority_keys.get(key)
    if not source_key:
        return True
    current_source = merged.get(source_key)
    incoming_source = value if key.endswith("Source") else parsed.get(source_key)
    return _header_profile_source_priority(incoming_source) >= _header_profile_source_priority(current_source)


def _header_profile_source_priority(source: str | None) -> int:
    return {
        "contextText": 4,
        "untrustedContext": 3,
        "rawKickoffText": 2,
        "transcript": 1,
    }.get(_text(source), 0)


def _parse_header_context_candidate(text: str, source: str) -> dict[str, str]:
    value = str(text or "").strip()
    if not value:
        return {}
    parsed: dict[str, str] = {}
    mode = _extract_conversation_mode(value)
    if mode:
        parsed["conversationMode"] = mode
    world_name, world_id = _extract_world_label(value)
    if world_name:
        parsed["worldName"] = world_name
    if world_id:
        parsed["worldId"] = world_id

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
    hashes = "#" * level
    pattern = re.compile(rf"(?im)^{re.escape(hashes)}\s+{re.escape(title)}\s*$")
    match = pattern.search(text)
    if not match:
        return ""
    start = match.end()
    next_heading = re.search(rf"(?m)^#{{1,{level}}}\s+", text[start:])
    end = start + next_heading.start() if next_heading else len(text)
    return text[start:end].strip()


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
