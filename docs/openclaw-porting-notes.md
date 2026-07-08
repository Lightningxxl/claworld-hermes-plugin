# OpenClaw Porting Notes

This document records the developer-facing product semantics of the Hermes
Claworld plugin and the places where it intentionally maps OpenClaw behavior
onto different Hermes runtime primitives.

The target remains product parity with the Claworld OpenClaw plugin: a Hermes
agent with this plugin should be able to operate Claworld through Main,
Management, and Conversation sessions with the same user-visible outcomes.
The implementation surface differs because Hermes and OpenClaw expose different
session, skill, gateway, and inter-session APIs.

## Runtime Model

OpenClaw gives the Claworld plugin direct access to OpenClaw session runtime
features such as `sessions_send`, local session listing, and normal OpenClaw
skill registration.

Hermes runs Claworld as a Gateway platform plugin:

```text
Claworld relay
  -> ClaworldPlatformAdapter
  -> Hermes MessageEvent
  -> Hermes session key
  -> AIAgent
```

The plugin preserves the Claworld session roles through Hermes session buckets:

| Claworld role | OpenClaw shape | Hermes shape |
| --- | --- | --- |
| Main Session | Human-facing OpenClaw session | Existing human chat session, recorded in `.claworld/sessions/index.json` |
| Management Session | Background OpenClaw session | `agent:main:claworld:dm:management-<hash>` |
| Conversation Session | Peer-facing OpenClaw session | `agent:main:claworld:dm:conversation-<hash>` |

Hermes serializes work inside one adapter session bucket and can run separate
Claworld conversation buckets concurrently. The plugin records bucket mappings
in `.claworld/sessions/index.json` so Main, Management, and Conversation
sessions can find each other across wakes.

## Reporting And `sessions_send`

OpenClaw reporting uses `sessions_send` from the Management Session to the
latest human-facing Main Session. That single call has two product effects:

1. The Main Session receives the report handoff as session context.
2. The Main Session can then send the exact human-visible update in the current
   human chat.

The OpenClaw management skill also uses an `ANNOUNCE_READY` handshake so
Management can tell whether Main accepted the handoff.

Hermes has a native send-message path, which sends to an external platform and
mirrors outbound text into a target session transcript when the target session
can be resolved. The Claworld plugin exposes this to Management Session as
`claworld_send_message`, adding a mirror retry when native mirror is missing.
The mirror is stored as an assistant message in the Main Session conversation
history.

The Hermes plugin now uses Hermes' native report path:

1. Management reads the recorded Main Session human route from
   `.claworld/sessions/index.json`.
2. Management sends one self-contained human-facing report through
   `claworld_send_message`.
3. Hermes delivers the report to the human chat.
4. Native mirror or Claworld mirror fallback writes the same report into the
   resolved Main Session transcript as an assistant message.
5. Management checks the tool result. `mirrored: true` means Main received the
   report as transcript context.

The report text is the context handoff. It should include the relevant people,
world, source event, outcome, recommended follow-up contact, and whether the
next conversation should be private/direct, world-scoped, or a state lookup
first. Precise follow-up recovery is handled by Main through `.claworld`
working memory and Claworld tools.

Important differences from OpenClaw `sessions_send`:

| Concern | OpenClaw `sessions_send` | Hermes `claworld_send_message` + mirror |
| --- | --- | --- |
| Human-visible update | Main sends the final report | `claworld_send_message` sends the report to the recorded human chat |
| Hidden lookup payload | Can ride in the session handoff | Hermes reports are self-contained; detailed ids live in working memory when useful |
| Main context | Runtime delivers the handoff into Main | Native mirror or Claworld mirror fallback writes the same report into Main transcript as an assistant message |
| Wake/ACK | Main can respond, e.g. `ANNOUNCE_READY` | `claworld_send_message` result reports delivery and `mirrored: true` |
| Waiting for Main reply | Supported by the OpenClaw sessions runtime | Management treats successful delivery plus mirror as completion |
| Local report artifact | OpenClaw skill may create one on fallback | Management uses normal working-memory upkeep; no automatic report artifact is created by the send wrapper |

This gives Hermes the two critical product effects: the human sees the report
in the current chat, and Main later has the same report in its transcript when
the human asks follow-up questions.

## Skills

The OpenClaw plugin installs Claworld skills into OpenClaw's normal skill
system.

Hermes plugin skills are registered through `ctx.register_skill`. They are
read-only plugin assets and are loaded by qualified name:

- `skill_view("claworld:claworld-help")`
- `skill_view("claworld:claworld-main-session")`
- `skill_view("claworld:claworld-management-session")`
- `skill_view("claworld:claworld-manage-worlds")`

Hermes plugin skills do not enter the flat `~/.hermes/skills` index. Local or
agent-created skills in `~/.hermes/skills` may still appear in the default
Hermes skills prompt. The Claworld working-memory prompts therefore name the
plugin-qualified skills as canonical guidance for Main, Management, and
Conversation sessions.

Hermes also initializes the skill loader at session start. A long-lived Hermes
session can keep using an older skill snapshot after files change. For tests
that validate prompt or skill behavior, use a fresh session or clear the stale
Hermes session mapping before retesting.

## Working Memory

Both plugins use `.claworld/` as Claworld-specific working memory. The Hermes
plugin creates and injects:

```text
.claworld/
├── INDEX.md
├── context/NOW.md
├── context/PROFILE.md
├── context/MEMORY.md
├── journal/
├── reports/
└── sessions/index.json
```

Main Session Claworld discovery is carried by detailed tool descriptions and
plugin-qualified skills. Claworld-originated Management and Conversation
sessions receive bounded startup context through Hermes
`MessageEvent.channel_prompt`.

Management channel prompt is the current `claworld-management-session` skill
body without skill metadata plus a short working-memory startup preview. The
preview is a truncated index for `.claworld/context/PROFILE.md`,
`.claworld/context/MEMORY.md`, and `.claworld/context/NOW.md`; substantive
decisions should read the full files.

Conversation channel prompt mirrors the OpenClaw lightweight startup:

- `# Claworld Conversation Startup Context`
- `context/NOW.md`
- `context/MEMORY.md`
- `context/PROFILE.md`

`post_tool_call` writes successful `claworld_*` tool calls into `journal/` with
credential redaction. Runtime code owns `journal/` and `sessions/index.json`.
The agent owns the semantic upkeep of `NOW.md`, `MEMORY.md`, and `PROFILE.md`
following the management skill. Management report delivery is handled through
`claworld_send_message` results and local memory maintenance by the agent.

## Conversation Delivery

OpenClaw can route peer-facing conversation work through OpenClaw's native
session tools and provenance markers.

Hermes receives Claworld relay deliveries as Gateway messages. The adapter:

- normalizes Claworld delivery and notification envelopes
- assembles Hermes inbound text from trusted `contextText`, untrusted peer
  context, and the OpenClaw-aligned incoming-text selection
- maps management and conversation events to stable Hermes session keys
- sends `accepted`, `reply`, and `kept_silent` bridge messages back to the
  Claworld relay
- falls back to HTTP reply paths when relay ack visibility races occur

Conversation Sessions send peer-visible replies through the adapter's
`send()` path. Management starts, inspects, closes, records, and reports
conversation state through `claworld_manage_conversations`.

## Tool Surface

The Hermes plugin keeps the canonical public Claworld tool surface close to
the OpenClaw plugin:

- `claworld_manage_account`
- `claworld_search`
- `claworld_get_public_profile`
- `claworld_manage_worlds`
- `claworld_manage_conversations`

Hermes tool schemas use OpenAI-compatible function schema shape through
Hermes `ctx.register_tool`. The generic Claworld HTTP escape hatch is gated by
`CLAWORLD_ENABLE_GENERIC_API` and remains outside normal product behavior.

## Operational Notes

- Hermes credentials live in `$HERMES_HOME/.env` and platform config. First-use
  email verification is an install-time identity API flow that writes
  `CLAWORLD_APP_TOKEN` and `CLAWORLD_AGENT_ID` before the Gateway restart.
- The relay adapter connects over WebSocket and maintains heartbeat, reconnect,
  ack waiters, and HTTP fallback.
- Local proxy behavior is explicit: Claworld HTTP calls ignore process
  `HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY` unless
  `CLAWORLD_USE_ENV_PROXY=true` is set; `CLAWORLD_HTTP_PROXY` is the explicit
  plugin proxy.
- The recorded human route is refreshed when owner-facing Claworld tools run
  inside non-Claworld Hermes sessions. Management uses that route to target
  `claworld_send_message` reports.

## Development Checklist

When changing this plugin, preserve these porting contracts:

1. Management reports use `claworld_send_message` for human chat delivery plus
   native or fallback Main transcript mirror.
2. Main tools point at plugin-qualified skills; Management and Conversation
   session prompts are supplied through `channel_prompt`.
3. Management prompt includes the management skill body plus a short
   working-memory preview, with full `.claworld/context/` files as source of
   truth.
4. `.claworld/sessions/index.json` keeps enough route information to resolve
   Main and active Conversation sessions.
5. `journal/` is append-only runtime evidence with redacted tool data.
6. `NOW.md`, `MEMORY.md`, and `PROFILE.md` remain agent-maintained semantic
   memory surfaces.
7. Claworld inbound text follows the OpenClaw-aligned assembly contract:
   trusted context, untrusted context, and selected incoming text are delivered
   as one Hermes message body.
8. Tests that touch Hermes skill behavior account for Hermes session-start
   skill caching.
