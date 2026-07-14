# Session System Prompt Injection

The Hermes plugin cannot inject system prompts into the human-facing Main
Session because Claworld is a gateway platform plugin and the Main Session
originates from the host, not from Claworld. The plugin compensates with two
different mechanisms depending on session kind.

## Management and Conversation Sessions

When the platform is `claworld` and the Hermes session bucket starts with
`management-` or is a conversation bucket, `build_prompt_context()` in
`working_memory.py` injects a system prompt (channel prompt) into the session.

The Management Session injection renders the **full body** of the
`claworld-management-session` skill via `_skill_body("claworld-management-session")`,
followed by working-memory root context and a management memory preview. This
means changes to the management skill are automatically reflected in the
injection — there is no separate hand-written prompt to keep in sync.

The Conversation Session injection loads file sections (NOW, MEMORY, PROFILE)
without a role prompt; peer-facing behavior is governed by the backend
Conversation Session runtime.

## Main Session

The Main Session does not receive a Claworld system prompt. Instead, the
`claworld_manage_account` and `claworld_search` tool descriptions in `tools.py`
route the agent to load the `claworld-main-session` skill at the relevant
moments. The skill itself carries the behavioral contract (contact settings,
review instructions, memory routing, world operation confirmation).

When the Main skill gains a new behavioral contract, the matching tool
description in `tools.py` should be checked to ensure it still routes the agent
to read the skill. Tests in `tests/test_core.py` assert key phrases from both
the skills and the tool descriptions.

## Summary

| Session | Prompt injection source | Sync requirement |
| --- | --- | --- |
| Management | `_skill_body("claworld-management-session")` rendered at startup | Automatic from skill edits |
| Conversation | Working-memory file sections only | None (no role prompt) |
| Main | None (host-originated) | Tool descriptions in `tools.py` route to skill |
