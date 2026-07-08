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
    "main": ("context/MEMORY.md",),
    "conversation": ("context/NOW.md", "context/MEMORY.md", "context/PROFILE.md"),
}
MANAGEMENT_MEMORY_PREVIEW_FILES = ("context/PROFILE.md", "context/MEMORY.md", "context/NOW.md")


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

    parts = [_role_prompt(role, root)]
    if role == "main":
        session_context = render_session_context(read_session_index(root), role=role, platform=platform, chat_id=chat_id)
        if session_context:
            parts.append(session_context)
    if role == "conversation":
        title = "# Claworld Conversation Startup Context"
        file_sections = [_file_section(root, relative) for relative in ROLE_BOOTSTRAP_FILES[role]]
        parts.append("\n\n".join([title, *file_sections]))
    else:
        for relative in ROLE_BOOTSTRAP_FILES[role]:
            parts.append(_file_section(root, relative))
    rendered = "\n\n".join(part for part in parts if part.strip())
    return rendered[:max_chars]


def _role_prompt(role: str, root: Path) -> str:
    if role == "management":
        return _skill_body("claworld-management-session")
    if role == "conversation":
        return ""
    return MAIN_CONTEXT.format(root=str(root))


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
- Read `context/PROFILE.md` for the human's preferences and autonomy policy.
- Read `sessions/index.json` before reasoning about known Claworld sessions.
- Canonical Claworld guidance lives in plugin-qualified skills. Use these `claworld:...` skill names even when local/user-authored Claworld notes also exist.
- For Claworld work with the human — browsing worlds, joining, talking to people, managing preferences — load `skill_view("claworld:claworld-main-session")`.
- For setup or repair, load `skill_view("claworld:claworld-help")`.
- Use Claworld tools for current product facts.
- Peer-facing messages belong to Claworld conversation routing; keep reports to the human readable and concise."""


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
