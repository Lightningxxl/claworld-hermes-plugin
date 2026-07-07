"""Local transcript report rendering for Claworld conversations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ClaworldConfig, hermes_home_path
from .transcript_report_stylekit import display_cols
from .transcript_report_styles import resolve_report_style
from .transcript_report_types import TranscriptMessage
from .working_memory import append_journal, atomic_write_text, read_session_index


DEFAULT_WIDTH = 720
DEFAULT_MAX_TURNS = 10
DEFAULT_MAX_PAGE_HEIGHT = 2600
MAX_TURNS_LIMIT = 80

TIME_SPLIT_SECONDS = 5 * 60


def render_transcript_report(cfg: ClaworldConfig, args: dict) -> dict:
    """Render a local Claworld transcript as BubbleSpec, SVG, and PNG files."""

    root = cfg.memory_root_path()
    source = _load_source_messages(cfg, args or {}, root)
    header_context = _extract_transcript_header_context(source["messages"], cfg, args or {})
    normalized = _normalize_messages(source["messages"], cfg, args or {})
    if not normalized:
        raise ValueError("no visible transcript messages were found for rendering")

    selected, selection = _select_messages(normalized, args or {})
    if not selected:
        raise ValueError("selection did not include any visible transcript messages")

    width = _int(args.get("width"), DEFAULT_WIDTH, minimum=520, maximum=1200)
    max_page_height = _int(args.get("maxPageHeight") or args.get("max_page_height"), DEFAULT_MAX_PAGE_HEIGHT, minimum=900, maximum=8000)
    style = resolve_report_style(_report_style_name(args or {}))
    participants = _participants(selected, cfg, args or {})
    title, subtitle = _header_text(args or {}, source, selection, selected, header_context)
    bubbles = _decorate_selection(selected, selection)
    bubble_spec = {
        "version": "1",
        "kind": "claworld.transcript_report",
        "scene": {
            "title": title,
            "subtitle": subtitle,
            "peerId": title,
            "peerProfile": subtitle,
            "peerProfileSource": header_context.get("profileSource", "fallback"),
            "generatedAt": _iso_now(),
            "timezone": _text(args.get("timezone"), "local") or "local",
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
    result = {
        "artifactId": artifact_id,
        "status": "ok",
        "source": source["summary"],
        "selection": selection,
        "files": files,
        "pngPath": primary_pngs[0] if primary_pngs else None,
        "pngPaths": primary_pngs,
        "svgPath": source_svgs[0] if source_svgs else None,
        "svgPaths": source_svgs,
        "bubbleSpecPath": str(spec_path),
        "messageCount": len(selected),
        "pages": len(pages),
        "style": style.name,
        "stats": stats,
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


def _load_source_messages(cfg: ClaworldConfig, args: dict, root: Path) -> dict:
    explicit_messages = args.get("messages")
    if isinstance(explicit_messages, list):
        return {
            "messages": explicit_messages,
            "summary": {"kind": "messages", "messageCount": len(explicit_messages)},
        }

    session_id, source_summary = _resolve_source_session_id(args, root)
    if not session_id:
        if source_summary.get("kind") in {"latest_conversation", "latest"}:
            raise ValueError("no locally stored Hermes transcript was found for any indexed Claworld conversation")
        if source_summary.get("kind") == "selector_not_found":
            raise ValueError(f"transcript selector did not match local Claworld session index: {source_summary.get('selectors')}")
        raise ValueError(
            "explicit transcript selector is required: pass sessionId, conversationKey, "
            "localSessionKey, relaySessionKey, chatId, or messages; use sourceKind=latest_conversation only intentionally"
        )
    messages = _load_session_db_messages(session_id)
    return {
        "messages": messages,
        "summary": {**source_summary, "kind": source_summary.get("kind") or "session", "sessionId": session_id, "messageCount": len(messages)},
    }


def _report_style_name(args: dict) -> str:
    return _text(args.get("style"), "claworld-terminal-crt") or "claworld-terminal-crt"


def _resolve_source_session_id(args: dict, root: Path) -> tuple[str | None, dict]:
    session_id = _text(args.get("sessionId"), _text(args.get("hermesSessionId")))
    if session_id:
        return _resolve_session_db_id(session_id) or session_id, {"kind": "sessionId", "requestedSessionId": session_id}

    index = read_session_index(root)
    sessions = index.get("conversationSessions") if isinstance(index.get("conversationSessions"), dict) else {}
    matchers = {
        "conversationKey": _text(args.get("conversationKey")),
        "localSessionKey": _text(args.get("localSessionKey")),
        "relaySessionKey": _text(args.get("relaySessionKey")),
        "chatId": _text(args.get("chatId")),
    }
    for field, expected in matchers.items():
        if not expected:
            continue
        for chat_id, entry in sessions.items():
            if not isinstance(entry, dict):
                continue
            if expected in _entry_selector_values(chat_id, entry, field):
                session_key = _text(entry.get("lastActiveSessionKey"), _text(entry.get("sessionKey")))
                resolved = _resolve_session_db_id(session_key) if session_key else None
                return resolved or session_key, {
                    "kind": field,
                    "chatId": chat_id,
                    "conversationKey": entry.get("conversationKey"),
                    "relaySessionKey": entry.get("relaySessionKey"),
                    "lastActiveSessionKey": session_key,
                }
    provided_selectors = {key: value for key, value in matchers.items() if value}
    if provided_selectors:
        return None, {"kind": "selector_not_found", "selectors": provided_selectors}

    source_kind = _text(args.get("sourceKind"), _text(args.get("source"))) or ""
    if source_kind == "current_session":
        current = _current_session_id()
        if current:
            return current, {"kind": "current_session"}
    if source_kind in {"latest_conversation", "latest"}:
        latest = _latest_resolvable_conversation_entry(sessions)
        if latest:
            chat_id, entry, resolved = latest
            session_key = _text(entry.get("lastActiveSessionKey"), _text(entry.get("sessionKey")))
            return resolved, {
                "kind": "latest_conversation",
                "chatId": chat_id,
                "conversationKey": entry.get("conversationKey"),
                "relaySessionKey": entry.get("relaySessionKey"),
                "lastActiveSessionKey": session_key,
            }
    return None, {"kind": source_kind}


def _entry_selector_values(chat_id: str, entry: dict, field: str) -> set[str]:
    if field == "chatId":
        values = [chat_id]
    elif field == "localSessionKey":
        values = [entry.get("localSessionKey"), entry.get("relaySessionKey")]
    elif field == "relaySessionKey":
        values = [entry.get("relaySessionKey"), entry.get("localSessionKey")]
    else:
        values = [entry.get(field)]
    return {_text(value) for value in values if _text(value)}


def _latest_conversation_entry(sessions: dict) -> tuple[str, dict] | None:
    entries = [(chat_id, entry) for chat_id, entry in sessions.items() if isinstance(entry, dict)]
    if not entries:
        return None
    entries.sort(key=lambda item: _text(item[1].get("updatedAt"), "") or "", reverse=True)
    return entries[0]


def _latest_resolvable_conversation_entry(sessions: dict) -> tuple[str, dict, str] | None:
    entries = [(chat_id, entry) for chat_id, entry in sessions.items() if isinstance(entry, dict)]
    entries.sort(key=lambda item: _text(item[1].get("updatedAt"), "") or "", reverse=True)
    for chat_id, entry in entries:
        session_key = _text(entry.get("lastActiveSessionKey"), _text(entry.get("sessionKey")))
        resolved = _resolve_session_db_id(session_key) if session_key else None
        if resolved:
            return chat_id, entry, resolved
    return None


def _current_session_id() -> str | None:
    try:
        from gateway.session_context import get_session_env

        return _text(get_session_env("HERMES_SESSION_ID", ""))
    except Exception:
        return _text(os.getenv("HERMES_SESSION_ID"))


def _resolve_session_db_id(session_id_or_key: str | None) -> str | None:
    value = _text(session_id_or_key)
    if not value:
        return None
    try:
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            resolved = _resolve_session_db_id_from_db(db, value)
            if resolved:
                return resolved
        finally:
            close = getattr(db, "close", None)
            if callable(close):
                close()
    except Exception:
        pass
    try:
        data = json.loads((hermes_home_path() / "sessions" / "sessions.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    entry = data.get(value) if isinstance(data, dict) else None
    if isinstance(entry, dict):
        return _text(entry.get("session_id"), _text(entry.get("sessionId")))
    return None


def _resolve_session_db_id_from_db(db: Any, value: str) -> str | None:
    if db.get_session(value):
        return value
    resolver = getattr(db, "resolve_session_id", None)
    if callable(resolver):
        resolved = resolver(value)
        if resolved:
            return resolved
    finder = getattr(db, "find_latest_gateway_session_for_peer", None)
    if callable(finder):
        try:
            row = finder(source="claworld", session_key=value)
            if isinstance(row, dict):
                resolved = _text(row.get("id"))
                if resolved:
                    return resolved
        except Exception:
            pass

    # Historical Claworld conversations may be explicitly ended, so Hermes'
    # recoverable-session helper can intentionally skip them. The transcript
    # renderer still needs to resolve those durable rows by gateway key.
    conn = getattr(db, "_conn", None)
    if conn is None:
        return None
    query = """
        SELECT id FROM sessions
        WHERE session_key = ? OR chat_id = ?
        ORDER BY started_at DESC
        LIMIT 1
    """
    lock = getattr(db, "_lock", None)
    try:
        if lock is not None:
            with lock:
                row = conn.execute(query, (value, value)).fetchone()
        else:
            row = conn.execute(query, (value, value)).fetchone()
    except Exception:
        return None
    if not row:
        return None
    if isinstance(row, dict):
        return _text(row.get("id"))
    try:
        return _text(row["id"])
    except Exception:
        return _text(row[0])


def _load_session_db_messages(session_id: str) -> list[dict]:
    try:
        from hermes_state import SessionDB
    except Exception as exc:
        raise ValueError(f"Hermes SessionDB is unavailable: {exc}") from exc
    db = SessionDB()
    try:
        resolved = _resolve_session_db_id_from_db(db, session_id) or session_id
        if not db.get_session(resolved):
            raise ValueError(f"Hermes session not found: {session_id}")
        return db.get_messages_as_conversation(resolved)
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            close()


def _normalize_messages(raw_messages: list, cfg: ClaworldConfig, args: dict) -> list[TranscriptMessage]:
    local_id = _text(args.get("localAgentId"), cfg.agent_id) or "local-agent"
    peer_id = _text(args.get("peerAgentId"), _text(args.get("targetAgentId"))) or "peer-agent"
    local_label = _text(args.get("localLabel"), local_id) or local_id
    peer_label = _text(args.get("peerLabel"), peer_id) or peer_id
    include_tools = _text(args.get("includeToolCalls"), "none") not in {"", "none", "false"}
    normalized: list[TranscriptMessage] = []
    for idx, raw in enumerate(raw_messages):
        if not isinstance(raw, dict):
            continue
        role = _text(raw.get("role"), _text(raw.get("from"))) or ""
        if role in {"system", "developer"}:
            continue
        if role == "tool" and not include_tools:
            continue
        if raw.get("tool_calls") and not include_tools:
            content_text = _flatten_content(raw.get("content"))
            if not content_text.strip():
                continue
        text = _flatten_content(raw.get("content", raw.get("text")))
        created_at = _format_timestamp(raw.get("timestamp") or raw.get("created_at") or raw.get("createdAt"))
        message_id = _text(raw.get("message_id"), _text(raw.get("id"), f"msg-{idx + 1}")) or f"msg-{idx + 1}"
        if role == "user":
            extracted = _extract_claworld_peer_message(text)
            if extracted is not None:
                text = extracted["text"]
                created_at = created_at or extracted.get("createdAt", "")
            side = "left"
            participant_id = peer_id
            participant_label = peer_label
        elif role == "assistant":
            if _is_no_reply(text):
                continue
            side = "right"
            participant_id = local_id
            participant_label = local_label
        else:
            side = "left" if role not in {"assistant", "local"} else "right"
            participant_id = _text(raw.get("participant_id"), peer_id if side == "left" else local_id) or peer_id
            participant_label = _text(raw.get("author"), _text(raw.get("name"), participant_id)) or participant_id
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
                ends_segment="request end" in tags,
            )
        )
    return normalized


def _select_messages(messages: list[TranscriptMessage], args: dict) -> tuple[list[TranscriptMessage], dict]:
    segments = _segment_messages(messages, _int(args.get("segmentGapMinutes"), 240, minimum=1, maximum=10080))
    segment_index_arg = args.get("segmentIndex")
    if segment_index_arg is not None:
        segment_index = max(0, min(_int(segment_index_arg, len(segments) - 1), len(segments) - 1))
    else:
        segment_index = len(segments) - 1
    segment = list(segments[segment_index]) if segments else []
    total = len(segment)
    max_turns = _int(args.get("maxTurns") or args.get("max_turns"), DEFAULT_MAX_TURNS, minimum=1, maximum=MAX_TURNS_LIMIT)
    start_arg = args.get("startTurn") or args.get("start_turn")
    end_arg = args.get("endTurn") or args.get("end_turn")
    if start_arg is not None:
        start = max(0, _int(start_arg, 1) - 1)
    elif total > max_turns:
        start = max(0, total - max_turns)
    else:
        start = 0
    if end_arg is not None:
        end = min(total, _int(end_arg, total))
    else:
        end = min(total, start + max_turns)
    if end - start > max_turns:
        end = start + max_turns
    selected = segment[start:end]
    selection = {
        "segmentIndex": segment_index,
        "segmentCount": len(segments),
        "segmentMessages": total,
        "startTurn": start + 1 if selected else 0,
        "endTurn": end if selected else 0,
        "maxTurns": max_turns,
        "omittedBefore": start,
        "omittedAfter": max(0, total - end),
    }
    return selected, selection


def _segment_messages(messages: list[TranscriptMessage], gap_minutes: int) -> list[list[TranscriptMessage]]:
    if not messages:
        return []
    segments: list[list[TranscriptMessage]] = [[]]
    previous_ts: float | None = None
    previous_ended = False
    for message in messages:
        ts = _parse_timeish(message.created_at)
        gap_boundary = previous_ts is not None and ts is not None and (ts - previous_ts) > gap_minutes * 60
        if segments[-1] and (previous_ended or gap_boundary):
            segments.append([])
        segments[-1].append(message)
        previous_ts = ts or previous_ts
        previous_ended = message.ends_segment
    return [segment for segment in segments if segment]


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


def _participants(messages: list[TranscriptMessage], cfg: ClaworldConfig, args: dict) -> list[dict[str, str]]:
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


def _subtitle(args: dict, source: dict, selection: dict) -> str:
    explicit = _text(args.get("subtitle"))
    if explicit:
        return explicit
    pieces = []
    conversation_key = source["summary"].get("conversationKey")
    if conversation_key:
        pieces.append(f"conversation {conversation_key}")
    chat_id = source["summary"].get("chatId")
    if chat_id:
        pieces.append(str(chat_id))
    if selection.get("segmentCount", 0) > 1:
        pieces.append(f"segment {selection['segmentIndex'] + 1}/{selection['segmentCount']}")
    turn_range = f"turns {selection.get('startTurn', 0)}-{selection.get('endTurn', 0)}"
    pieces.append(turn_range)
    return " · ".join(pieces)


def _header_text(args: dict, source: dict, selection: dict, messages: list[TranscriptMessage], header_context: dict | None = None) -> tuple[str, str]:
    header_context = header_context or {}
    peer_id = _text(
        header_context.get("peerIdentity"),
        _text(header_context.get("peerId"), _text(args.get("peerAgentId"), _text(args.get("targetAgentId"), _text(args.get("peerId"))))),
    )
    if not peer_id:
        for message in messages:
            if message.side == "left":
                peer_id = message.participant_id or message.participant_label
                break
    peer_id = peer_id or _text(args.get("title"), "peer-agent") or "peer-agent"
    profile = (
        _text(header_context.get("peerProfile"))
        or _text(args.get("peerProfile"))
        or _text(args.get("peerProfileSummary"))
        or _text(args.get("profile"))
        or _text(args.get("subtitle"))
        or _subtitle(args, source, selection)
    )
    return peer_id, profile or "profile: unavailable"


def _extract_transcript_header_context(raw_messages: list, cfg: ClaworldConfig, args: dict) -> dict:
    merged: dict[str, str] = {}
    for raw in raw_messages:
        if not isinstance(raw, dict):
            continue
        text = _flatten_content(raw.get("content", raw.get("text")))
        for candidate in _header_context_candidates(raw, text):
            parsed = _parse_header_context_candidate(candidate["text"], candidate.get("source", "transcript"))
            if not parsed:
                continue
            for key, value in parsed.items():
                normalized = _text(value)
                if normalized:
                    if not _should_merge_header_value(merged, parsed, key, normalized):
                        continue
                    merged[key] = normalized
    profile, source = _select_header_profile(merged)
    result = {
        key: value
        for key, value in {
            "peerId": merged.get("peerId"),
            "peerIdentity": merged.get("peerIdentity"),
            "conversationMode": merged.get("conversationMode"),
            "worldName": merged.get("worldName"),
            "worldId": merged.get("worldId"),
            "peerProfile": profile,
            "profileSource": source,
        }.items()
        if value
    }
    return result


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


def _header_context_candidates(raw: dict, text: str) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for heading, source in (
        ("Backend-authored Claworld context:", "contextText"),
        ("Relay untrusted context:", "untrustedContext"),
    ):
        for section in _extract_sections_until_markers(text, heading):
            candidates.append({"source": source, "text": section})
    if "# Background" in text and "## Participant Facts" in text:
        candidates.append({"source": "rawKickoffText", "text": text})

    payloads = _payload_dicts(raw)
    for payload in payloads:
        for key, source in (("contextText", "contextText"), ("untrustedContext", "untrustedContext"), ("visibleText", "rawKickoffText"), ("text", "rawKickoffText")):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append({"source": source, "text": value})
    return candidates


def _payload_dicts(raw: dict) -> list[dict]:
    payloads: list[dict] = []
    for candidate in (raw, raw.get("payload"), raw.get("data")):
        if isinstance(candidate, dict):
            payloads.append(candidate)
            nested = candidate.get("payload")
            if isinstance(nested, dict):
                payloads.append(nested)
    unique: list[dict] = []
    seen: set[int] = set()
    for payload in payloads:
        marker = id(payload)
        if marker not in seen:
            unique.append(payload)
            seen.add(marker)
    return unique


def _extract_sections_until_markers(text: str, heading: str) -> list[str]:
    if not text or heading not in text:
        return []
    stop_markers = (
        "Backend-authored Claworld context:",
        "Relay untrusted context:",
        "Backend-authored Claworld command:",
        "Peer-visible Claworld message:",
        "Inbound Claworld payload content:",
        "Claworld live conversation rules:",
    )
    sections: list[str] = []
    start = 0
    while True:
        found = text.find(heading, start)
        if found < 0:
            break
        content_start = found + len(heading)
        stops = [pos for marker in stop_markers if marker != heading for pos in [text.find(marker, content_start)] if pos >= 0]
        content_end = min(stops) if stops else len(text)
        section = _unwrap_header_context_fence(text[content_start:content_end])
        if section:
            sections.append(section)
        start = content_end
    return sections


def _unwrap_header_context_fence(text: str) -> str:
    lines = str(text or "").strip().splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and re.fullmatch(r"```(?:text|markdown|md|json)?", lines[0].strip(), flags=re.IGNORECASE):
        lines = lines[1:]
    return "\n".join(lines).strip()


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


def _extract_claworld_peer_message(text: str) -> dict | None:
    if "Claworld delivery received." not in text:
        return None
    extracted = _extract_fenced_block(text, "Peer-visible Claworld message:")
    if extracted is None:
        extracted = _extract_fenced_block(text, "Inbound Claworld payload content:")
    if extracted is None:
        return None
    created_at = None
    for key in ("turn_created_at", "created_at"):
        match = re.search(rf"^\s*-\s*{key}=([^\n]+)$", text, flags=re.MULTILINE)
        if match:
            created_at = match.group(1).strip()
            break
    return {"text": extracted.strip(), "createdAt": _format_timestamp(created_at)}


def _extract_fenced_block(text: str, heading: str) -> str | None:
    pattern = re.compile(re.escape(heading) + r"\s*\n\s*```(?:text)?\s*\n(.*?)\n```", re.DOTALL)
    match = pattern.search(text)
    return match.group(1) if match else None


CONTROL_PATTERNS = (
    (re.compile(r"\[\[?\s*request[_\s-]*(?:conversation[_\s-]*)?end\s*\]?\]?", re.IGNORECASE), "request end"),
    (re.compile(r"\[\s*requeset\s+end\s*\]", re.IGNORECASE), "request end"),
    (re.compile(r"\[\[?\s*like\s*\]?\]?", re.IGNORECASE), "like"),
    (re.compile(r"\[\[?\s*dislike\s*\]?\]?", re.IGNORECASE), "dislike"),
)


def _extract_control_tags(text: str) -> tuple[str, list[str]]:
    tags: list[str] = []
    cleaned = str(text or "")
    for pattern, label in CONTROL_PATTERNS:
        if pattern.search(cleaned) and label not in tags:
            tags.append(label)
        cleaned = pattern.sub("", cleaned)
    return _squash_whitespace(cleaned), tags


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


def _is_no_reply(text: str) -> bool:
    return str(text or "").strip() == "NO_REPLY"


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
