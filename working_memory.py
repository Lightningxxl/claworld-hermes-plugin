"""Local .claworld working-memory contract for Hermes."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
    }


def read_session_index(root: Path) -> dict:
    ensure_working_memory(root)
    path = root / SESSIONS_DIR / "index.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = empty_session_index()
    return data if isinstance(data, dict) else empty_session_index()


def write_session_index(root: Path, data: dict) -> None:
    data["updatedAt"] = iso_now()
    atomic_write_json(root / SESSIONS_DIR / "index.json", data)


def record_claworld_route(root: Path, route, hermes_session_key: str, envelope) -> None:
    data = read_session_index(root)
    now = iso_now()
    if route.session_kind == "management":
        data["management"] = {
            "lastActiveSessionKey": hermes_session_key,
            "chatId": route.chat_id,
            "relaySessionKey": route.relay_session_key,
            "targetAgentId": envelope.target_agent_id,
            "updatedAt": now,
        }
    else:
        sessions = data.setdefault("conversationSessions", {})
        sessions[route.chat_id] = {
            "lastActiveSessionKey": hermes_session_key,
            "chatId": route.chat_id,
            "relaySessionKey": route.relay_session_key,
            "conversationKey": route.conversation_key,
            "targetAgentId": envelope.target_agent_id,
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


def build_prompt_context(root: Path, platform: str = "", chat_id: str = "", max_chars: int = 60000) -> str:
    ensure_working_memory(root)
    role = "main"
    if platform == "claworld" and chat_id.startswith("management-"):
        role = "management"
    elif platform == "claworld":
        role = "conversation"

    intro = {
        "main": MAIN_CONTEXT,
        "management": MANAGEMENT_CONTEXT,
        "conversation": CONVERSATION_CONTEXT,
    }[role]

    files = ["context/NOW.md", "context/MEMORY.md", "context/PROFILE.md"]
    parts = [intro.format(root=str(root))]
    session_context = render_session_context(read_session_index(root), role=role, platform=platform, chat_id=chat_id)
    if session_context:
        parts.append(session_context)
    for relative in files:
        path = root / relative
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        parts.append(f"## {relative}\n\n{content.strip()}")
    rendered = "\n\n".join(part for part in parts if part.strip())
    return rendered[:max_chars]


def render_session_context(data: dict, *, role: str, platform: str = "", chat_id: str = "", max_chars: int = 12000) -> str:
    if not isinstance(data, dict):
        return ""
    sessions = data.get("conversationSessions") if isinstance(data.get("conversationSessions"), dict) else {}
    recent_sessions = sorted(
        sessions.values(),
        key=lambda item: item.get("updatedAt", "") if isinstance(item, dict) else "",
        reverse=True,
    )[:12]
    current_conversation = sessions.get(chat_id) if chat_id else None
    summary = {
        "schema": data.get("schema"),
        "updatedAt": data.get("updatedAt"),
        "currentHermesContext": {"role": role, "platform": platform or None, "chatId": chat_id or None},
        "main": data.get("main") or {},
        "management": data.get("management") or {},
        "currentConversation": current_conversation or {},
        "recentConversationSessions": recent_sessions,
    }
    payload = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    if len(payload) > max_chars:
        payload = payload[: max_chars - 32] + "\n... truncated ..."
    return "\n".join(["## sessions/index.json summary", "", "```json", payload, "```"])


MAIN_CONTEXT = """# About Claworld

Claworld is a social app connected to this Hermes agent. Use `.claworld/` as private working memory.

Working memory root: `{root}`

- Read `context/MEMORY.md` for durable Claworld facts.
- Read `context/NOW.md` for active Claworld focus and pending approvals.
- Read `context/PROFILE.md` for owner preferences and autonomy policy.
- Read `sessions/index.json` before reasoning about known Claworld sessions.
- For substantive Claworld owner-facing work, load `skill_view("claworld:claworld-main-session")`.
- For setup or repair, load `skill_view("claworld:claworld-help")`.
- Use Claworld tools for current product facts.
- Peer-facing messages belong to Claworld conversation routing; keep owner-facing reports readable and concise."""


MANAGEMENT_CONTEXT = """# Claworld Management Session

You are the private Claworld Management Session for this account.

Working memory root: `{root}`

- Start by loading `skill_view("claworld:claworld-management-session")` before deciding what to do.
- Handle Claworld notifications, lifecycle events, proactive work, local memory, and report handoffs.
- Read PROFILE, MEMORY, NOW, journal, and sessions/index.json before deciding.
- Conversation Sessions handle live peer-facing Claworld chat.
- Ask or report to the owner through the owner report tool when the owner needs visibility or a decision."""


CONVERSATION_CONTEXT = """# Claworld Conversation Session

You are a peer-facing Claworld Conversation Session.

Working memory root: `{root}`

- Respond to the current Claworld peer conversation only.
- Use `skill_view("claworld:claworld-main-session")` only when you need broader Claworld product rules; keep ordinary live replies short and direct.
- Read NOW, MEMORY, and PROFILE for bounded shared context.
- Submit observations through Claworld tools or reports; durable memory commits are handled by Management/Main."""


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
