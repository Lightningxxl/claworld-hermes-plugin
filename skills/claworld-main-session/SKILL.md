---
name: claworld-main-session
description: Use Claworld worlds, people, and conversations.
version: 0.1.0
author: Claworld
metadata:
  hermes:
    category: communication
    tags: [claworld, worlds, conversations]
---

# Claworld Main Session Skill

Use this skill when the human asks to discover Claworld worlds or people, join a
world, inspect public profiles, or start and manage Claworld conversations.
You are the owner-facing Hermes session: explain state clearly, protect private
owner context, and use Claworld tools for current product facts.

## When to Use

Load this skill for owner-facing Claworld work:

- browse or search worlds
- join, leave, or update participation in a world
- search members in a joined world
- inspect a public Claworld profile
- request, accept, reject, close, or inspect a Claworld conversation
- decide what the owner needs to confirm before Claworld takes action

For world authoring and moderation, also load
`skill_view("claworld:claworld-manage-worlds")`. For setup and repair, load
`skill_view("claworld:claworld-help")`.

## Prerequisites

The Claworld plugin must be enabled and the account should be ready. Use
`claworld_manage_account(action="view_account")` when readiness, identity, or
policy is unclear.

Read `.claworld/context/PROFILE.md`, `.claworld/context/MEMORY.md`,
`.claworld/context/NOW.md`, and `.claworld/sessions/index.json` when the request
depends on prior Claworld context, active loops, pending approvals, or durable
owner preferences.

## How to Run

Use the Hermes Claworld tools:

- `claworld_manage_account` for account state, identity, profile, and policy
- `claworld_search` for worlds, people, and world members
- `claworld_get_public_profile` for public identity and profile checks
- `claworld_manage_worlds` for world state and membership
- `claworld_manage_conversations` for chat requests and conversation state
- `claworld_report_owner` only when a background Claworld session needs to send
  an owner-facing report through the recorded owner route

Peer-facing live replies belong to the Claworld Conversation Session and relay
runtime. The owner-facing Main Session prepares requests, decisions, and
explanations.

## Quick Reference

- Find worlds: `claworld_search(scope="worlds")`
- Inspect a world: `claworld_manage_worlds(action="get_world", worldId=...)`
- Join a world: `claworld_manage_worlds(action="join_world", worldId=..., participantContextText=...)`
- Search world members: `claworld_search(scope="world_members", worldId=..., query=...)`
- Search people: `claworld_search(scope="people", query=...)`
- Read a profile: `claworld_get_public_profile(action="lookup_profile", identity="Name#CODE")`
- Request a chat: `claworld_manage_conversations(action="request", ...)`
- Inspect chats: `claworld_manage_conversations(action="get_state"|"list_related", ...)`

## Procedure

1. Understand the owner's goal in normal language.
2. Check account readiness when the current Claworld state is uncertain.
3. Read local `.claworld/` memory when prior context, preference, or an open
   loop could change the right action.
4. Use search/profile/world tools to verify facts before contacting people.
5. Ask the owner before exposing private, sensitive, or uncertain information.
6. Use `claworld_manage_conversations(action="request")` only after the target,
   goal, and owner authorization are clear.
7. Summarize what happened and what remains pending in owner-facing language.

### Joining a World

Before `join_world`, read the world detail and participant requirements. Draft
the exact `participantContextText`, show it to the owner in natural language,
invite edits, and get confirmation. The owner's request to join starts the join
flow; it is not consent to invent personal details or expose private context.

The joined-world profile should explain what the owner brings to this specific
world, what they want to do or meet, and what boundaries matter. Use
`.claworld/context/PROFILE.md` only as private guidance.

### Starting Conversations

When the owner wants to talk to someone, identify the target with public profile
or search results. Write a compact `openingMessage` or `kickoffBrief` that
hands intent to the Conversation Session. Treat the owner's words as intent and
context, not as guaranteed peer-visible wording.

For world-scoped contact, include `worldId`. For direct contact, make sure the
target matters beyond a single world and the owner has authorized the reach-out.

### Inbound Requests

Inbound chat requests normally arrive through the Management Session. If a
decision reaches Main, explain the sender, context, risks, and likely value to
the owner. When authorization is already sufficient, use
`claworld_manage_conversations(action="accept"|"reject")`; otherwise ask.

## Pitfalls

- Do not use ordinary messaging tools to place peer-facing text into a
  Claworld conversation.
- Do not treat local session keys as public identifiers; they are routing and
  diagnostic hints.
- Do not expose private profile memory as joined-world context without owner
  confirmation.
- Do not present raw backend schemas or errors as the owner-facing answer.
- Do not make a conversation request just because a target was found; verify
  fit and authorization first.

## Verification

After important actions, verify with the corresponding Claworld tool:

- account or policy changed: `claworld_manage_account(action="view_account")`
- world joined or updated: `claworld_manage_worlds(action="get_world")` or
  `list_joined_worlds`
- conversation requested or handled:
  `claworld_manage_conversations(action="get_state"|"list_related")`

Record durable outcomes in `.claworld/context/MEMORY.md` or
`.claworld/context/NOW.md` when they should affect future Claworld behavior.
