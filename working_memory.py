"""Local .claworld working-memory contract for Hermes."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

CONTEXT_DIR = "context"
JOURNAL_DIR = "journal"
REPORTS_DIR = "reports"
SESSIONS_DIR = "sessions"

FILES = {
    "index": "INDEX.md",
    "now": "context/NOW.md",
    "profile": "context/PROFILE.md",
    "memory": "context/MEMORY.md",
}

MAX_BOOTSTRAP_FILE_CHARS = 12000
MAX_BOOTSTRAP_TOTAL_CHARS = 60000
MAX_MANAGEMENT_MEMORY_PREVIEW_FILE_CHARS = 400
MAX_MANAGEMENT_MEMORY_PREVIEW_TOTAL_CHARS = 1800
ROLE_BOOTSTRAP_FILES = {
    "conversation": ("context/NOW.md", "context/MEMORY.md", "context/PROFILE.md"),
}
MANAGEMENT_MEMORY_PREVIEW_FILES = ("context/PROFILE.md", "context/MEMORY.md", "context/NOW.md")


def _text(value) -> str:
    if value is None:
        return ""
    normalized = str(value).strip()
    return normalized


def _build_delivery_entry(envelope) -> dict | None:
    delivery_id = _text(getattr(envelope, "delivery_id", None))
    if not delivery_id:
        return None
    payload = getattr(envelope, "payload", {}) or {}
    metadata = getattr(envelope, "metadata", {}) or {}
    entry = {
        "deliveryId": delivery_id,
        "fromAgentId": _text(metadata.get("fromAgentId")) or None,
        "fromAgentCode": _text(metadata.get("fromAgentCode")) or None,
        "fromDisplayIdentity": _text(metadata.get("fromDisplayIdentity")) or None,
        "deliveryType": _text(metadata.get("deliveryType")) or None,
        "commandText": _text(payload.get("commandText")) or None,
        "contextText": _text(payload.get("contextText")) or None,
        "createdAt": _text(getattr(envelope, "created_at", None)) or None,
        "turnCreatedAt": _text(getattr(envelope, "turn_created_at", None)) or None,
    }
    return {k: v for k, v in entry.items() if v is not None}


TEMPLATES = {
    "INDEX.md": """# Claworld Working Memory

This directory is the private working memory for Claworld.

## Read Order
- `context/NOW.md` for current Claworld focus, active worlds, and recent progress.
- `context/MEMORY.md` for durable Claworld facts and decisions.
- `context/PROFILE.md` for user preferences and profile hints relevant to Claworld.
- `journal/YYYY-MM-DD.md` for append-only structured event indexes.
- `reports/` for generated local progress reports.

## Rules
- Do not load raw Claworld transcripts by default.
- Prefer short summaries and references over raw chat history.
- `context/PROFILE.md` and `context/MEMORY.md` are updated through reviewed maintenance.
""",
    "context/NOW.md": """# Claworld Now

## Active Goals
- none

## Pending Approvals
- none

## Watched People And Worlds
- none

## Open Conversations
- none

## Recent Changes
- none

## Closed Recently
- none
""",
    "context/PROFILE.md": """# Claworld Profile

## Identity And Background
- unknown

## Goals And Interests
- unknown

## Social Style
- unknown

## Autonomy Policy
- unknown

## Contact And Notification Preferences
- unknown

## Privacy And Sensitive Boundaries
- unknown

## World And People Preferences
- unknown

## Explicit Do-Not Rules
- unknown
""",
    "context/MEMORY.md": """# Claworld Memory

## Memories
- none
""",
}


def ensure_working_memory(root: Path) -> dict:
    root = root.expanduser()
    for relative in ("", CONTEXT_DIR, JOURNAL_DIR, REPORTS_DIR, SESSIONS_DIR):
        (root / relative).mkdir(parents=True, exist_ok=True)

    created: list[str] = []
    preserved: list[str] = []
    for relative, content in TEMPLATES.items():
        path = root / relative
        if path.exists():
            preserved.append(relative)
            continue
        atomic_write_text(path, content)
        created.append(relative)

    sessions = root / SESSIONS_DIR / "index.json"
    if not sessions.exists():
        atomic_write_json(sessions, empty_session_index())
        created.append(f"{SESSIONS_DIR}/index.json")

    return {"root": str(root), "created": created, "preserved": preserved}


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            if not content.endswith("\n"):
                handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def atomic_write_json(path: Path, payload: dict) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True))


def empty_session_index() -> dict:
    now = iso_now()
    return {
        "schema": "claworld.sessions.v1",
        "createdAt": now,
        "updatedAt": now,
        "main": {},
        "management": {},
        "conversationSessions": {},
        "conversationEpisodes": {},
    }


def read_session_index(root: Path) -> dict:
    ensure_working_memory(root)
    path = root / SESSIONS_DIR / "index.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = empty_session_index()
    if not isinstance(data, dict):
        data = empty_session_index()
    data.setdefault("schema", "claworld.sessions.v1")
    data.setdefault("createdAt", iso_now())
    data.setdefault("updatedAt", iso_now())
    data.setdefault("main", {})
    data.setdefault("management", {})
    data.setdefault("conversationSessions", {})
    data.setdefault("conversationEpisodes", {})
    return data


def write_session_index(root: Path, data: dict) -> None:
    data["updatedAt"] = iso_now()
    atomic_write_json(root / SESSIONS_DIR / "index.json", data)


def record_claworld_route(root: Path, route, hermes_session_key: str, envelope) -> None:
    data = read_session_index(root)
    now = iso_now()
    chat_request_id = _text(getattr(envelope, "chat_request_id", None))
    if route.session_kind == "management":
        data["management"] = {
            "lastActiveSessionKey": hermes_session_key,
            "chatId": route.chat_id,
            "relaySessionKey": route.relay_session_key,
            "targetAgentId": envelope.target_agent_id,
            **({"lastChatRequestId": chat_request_id} if chat_request_id else {}),
            "updatedAt": now,
        }
    else:
        sessions = data.setdefault("conversationSessions", {})
        existing = sessions.get(route.chat_id) if isinstance(sessions.get(route.chat_id), dict) else {}
        chat_request_ids = list(existing.get("chatRequestIds") or [])
        if chat_request_id and chat_request_id not in chat_request_ids:
            chat_request_ids.append(chat_request_id)
        sessions[route.chat_id] = {
            **existing,
            "lastActiveSessionKey": hermes_session_key,
            "chatId": route.chat_id,
            "relaySessionKey": route.relay_session_key,
            "conversationKey": route.conversation_key,
            "targetAgentId": envelope.target_agent_id,
            "chatRequestIds": chat_request_ids,
            **({"lastChatRequestId": chat_request_id} if chat_request_id else {}),
            "updatedAt": now,
        }
        if chat_request_id:
            episodes = data.setdefault("conversationEpisodes", {})
            previous = episodes.get(chat_request_id) if isinstance(episodes.get(chat_request_id), dict) else {}
            delivery_ids = list(previous.get("deliveryIds") or [])
            if envelope.delivery_id and envelope.delivery_id not in delivery_ids:
                delivery_ids.append(envelope.delivery_id)
            deliveries = list(previous.get("deliveries") or [])
            delivery_entry = _build_delivery_entry(envelope)
            if delivery_entry and not any(d.get("deliveryId") == envelope.delivery_id for d in deliveries):
                deliveries.append(delivery_entry)
            from_agent_code = _text(envelope.metadata.get("fromAgentCode"))
            from_display_identity = _text(envelope.metadata.get("fromDisplayIdentity"))
            episodes[chat_request_id] = {
                **previous,
                "chatRequestId": chat_request_id,
                "chatId": route.chat_id,
                "lastActiveSessionKey": hermes_session_key,
                "relaySessionKey": route.relay_session_key,
                "conversationKey": route.conversation_key,
                "targetAgentId": envelope.target_agent_id,
                **({"fromAgentCode": from_agent_code} if from_agent_code else {}),
                **({"fromDisplayIdentity": from_display_identity} if from_display_identity else {}),
                "firstSeenAt": previous.get("firstSeenAt") or _text(getattr(envelope, "created_at", None)) or now,
                "lastSeenAt": _text(getattr(envelope, "turn_created_at", None)) or _text(getattr(envelope, "updated_at", None)) or _text(getattr(envelope, "created_at", None)) or now,
                "deliveryIds": delivery_ids,
                "deliveryCount": len(delivery_ids),
                "deliveries": deliveries,
                "updatedAt": now,
            }
    write_session_index(root, data)


def record_owner_route_from_context(root: Path) -> dict | None:
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return None

    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
    if not platform or not chat_id or platform == "claworld":
        return None

    route = {
        "platform": platform,
        "chatId": chat_id,
        "chatName": get_session_env("HERMES_SESSION_CHAT_NAME", ""),
        "threadId": get_session_env("HERMES_SESSION_THREAD_ID", "") or None,
        "userId": get_session_env("HERMES_SESSION_USER_ID", "") or None,
        "userName": get_session_env("HERMES_SESSION_USER_NAME", "") or None,
        "sessionKey": get_session_env("HERMES_SESSION_KEY", "") or None,
        "sessionId": get_session_env("HERMES_SESSION_ID", "") or None,
        "updatedAt": iso_now(),
    }
    data = read_session_index(root)
    data["main"] = route
    write_session_index(root, data)
    return route


def append_journal(root: Path, event: dict) -> Path:
    ensure_working_memory(root)
    day = datetime.now().strftime("%Y-%m-%d")
    path = root / JOURNAL_DIR / f"{day}.md"
    header = f"# Claworld Journal {day}\n\n"
    entry = "\n".join(
        [
            f"## {iso_now()} {event.get('kind', 'event')}",
            "",
            "```json",
            json.dumps(event, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )
    if path.exists():
        with path.open("a", encoding="utf-8") as handle:
            handle.write(entry)
    else:
        atomic_write_text(path, header + entry)
    return path


def write_report(root: Path, text: str, metadata: dict | None = None) -> Path:
    ensure_working_memory(root)
    name = datetime.now().strftime("REPORT-%Y%m%d-%H%M%S.md")
    path = root / REPORTS_DIR / name
    lines = ["# Claworld Owner Report", "", text.strip(), ""]
    if metadata:
        lines.extend(["## Metadata", "", "```json", json.dumps(metadata, indent=2, sort_keys=True), "```", ""])
    atomic_write_text(path, "\n".join(lines))
    return path


def build_prompt_context(root: Path, platform: str = "", chat_id: str = "", max_chars: int = MAX_BOOTSTRAP_TOTAL_CHARS) -> str:
    ensure_working_memory(root)
    role = "main"
    if platform == "claworld" and chat_id.startswith("management-"):
        role = "management"
    elif platform == "claworld":
        role = "conversation"

    if role == "management":
        rendered = "\n\n".join(
            part
            for part in (
                _role_prompt(role, root),
                _working_memory_root_context(root),
                _management_memory_preview(root),
            )
            if part.strip()
        )
        return rendered[:max_chars]

    if role == "conversation":
        title = "# Claworld Conversation Startup Context"
        file_sections = [_file_section(root, relative) for relative in ROLE_BOOTSTRAP_FILES[role]]
        rendered = "\n\n".join([title, *file_sections])
        return rendered[:max_chars]

    return ""


def _role_prompt(role: str, root: Path) -> str:
    if role == "management":
        return _skill_body("claworld-management-session")
    return ""


def _file_section(root: Path, relative: str, max_chars: int = MAX_BOOTSTRAP_FILE_CHARS) -> str:
    path = root / relative
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    content = content.strip()
    if len(content) > max_chars:
        note = "\n_(Truncated to the per-file Claworld bootstrap budget.)_"
        content = content[: max_chars - len(note)].rstrip() + note
    return f"## `.claworld/{relative}`\n{content}"


def _working_memory_root_context(root: Path) -> str:
    root = root.expanduser()
    return "\n".join(
        [
            "# Claworld Working Memory Root",
            "",
            f"Configured root: `{root}`",
            "",
            "Use this configured root for all Claworld working-memory reads and writes.",
            f"- NOW: `{root / 'context' / 'NOW.md'}`",
            f"- MEMORY: `{root / 'context' / 'MEMORY.md'}`",
            f"- PROFILE: `{root / 'context' / 'PROFILE.md'}`",
            f"- sessions index: `{root / 'sessions' / 'index.json'}`",
        ]
    )


def _management_memory_preview(root: Path) -> str:
    root = root.expanduser()
    parts = [
        "# Claworld Working Memory Startup Preview",
        "",
        (
            "This is a short, truncated startup index for Management Session. "
            "Treat it as Claworld operating memory, not communication style or "
            "the full source of truth. Before any important decision, read the "
            "full files under the configured Claworld working-memory root."
        ),
        "",
        (
            f"Full files: `{root / 'context' / 'PROFILE.md'}`, "
            f"`{root / 'context' / 'MEMORY.md'}`, `{root / 'context' / 'NOW.md'}`."
        ),
    ]
    for relative in MANAGEMENT_MEMORY_PREVIEW_FILES:
        parts.extend(["", f"### `.claworld/{relative}`", _file_preview(root, relative)])
    rendered = "\n".join(parts).strip()
    if len(rendered) <= MAX_MANAGEMENT_MEMORY_PREVIEW_TOTAL_CHARS:
        return rendered
    note = "\n\n_(Preview truncated; read full `.claworld/context/` files before acting.)_"
    return rendered[: MAX_MANAGEMENT_MEMORY_PREVIEW_TOTAL_CHARS - len(note)].rstrip() + note


def _file_preview(root: Path, relative: str, max_chars: int = MAX_MANAGEMENT_MEMORY_PREVIEW_FILE_CHARS) -> str:
    path = root / relative
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    content = content.strip()
    if not content:
        return "(empty or missing)"
    if len(content) <= max_chars:
        return content
    note = "\n_(Truncated preview; read the full file before acting.)_"
    return content[: max_chars - len(note)].rstrip() + note


def _skill_body(skill_name: str) -> str:
    skill_path = Path(__file__).resolve().parent / "skills" / skill_name / "SKILL.md"
    text = skill_path.read_text(encoding="utf-8")
    if text.startswith("---"):
        marker = text.find("\n---", 3)
        if marker != -1:
            text = text[marker + len("\n---") :]
    return text.strip()


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
