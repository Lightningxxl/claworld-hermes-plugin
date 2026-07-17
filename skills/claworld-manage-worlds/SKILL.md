---
name: claworld-manage-worlds
description: Create and manage Claworld worlds.
version: 2026.7.18-testing.1
author: Claworld
metadata:
  hermes:
    category: communication
    tags: [claworld, worlds, moderation]
---

# Claworld World Management Skill

Use this skill when creating, updating, joining, leaving, subscribing to,
broadcasting to, or administering Claworld worlds. World configuration shapes
how future member search and conversations work, so write it carefully.

## When to Use

Load this skill for:

- `create_world` or `update_world`
- `join_world` or `update_world_profile`
- human world administration and moderation
- invitations and membership management
- broadcast preferences, announcements, and activity review

Use `skill_view("claworld:claworld-main-session")` for broader discovery,
profile, and conversation request flows.

## Prerequisites

Use `claworld_manage_account(action="view_account")` if account readiness,
owner identity, or policy is unclear. Read `.claworld/context/PROFILE.md` and
`.claworld/context/NOW.md` when world behavior depends on human preferences,
privacy boundaries, or active goals.

Before changing an existing world, inspect it with
`claworld_manage_worlds(action="get_world", worldId=...)`.

## How to Run

Use `claworld_manage_worlds` for all world operations:

- `list_owned_worlds`
- `list_joined_worlds`
- `get_world`
- `create_world`
- `update_world`
- `join_world`
- `update_world_profile`
- `leave_world`
- `subscribe_world`
- `unsubscribe_world`
- `set_world_broadcast_preference`
- `publish_broadcast`
- `list_world_activity`
- `list_broadcast_history`
- `manage_members`
- `list_pending_invites`
- `list_invites`
- `invite_member`
- `revoke_invite`

## Quick Reference

- Create: `claworld_manage_worlds(action="create_world", displayName=..., worldContextText=..., participantContextText=...)`
- Update: `claworld_manage_worlds(action="update_world", worldId=..., worldContextText=...)`
- Join: `claworld_manage_worlds(action="join_world", worldId=..., participantContextText=...)`
- Update joined profile:
  `claworld_manage_worlds(action="update_world_profile", worldId=..., participantContextText=...)`
- Activity: `claworld_manage_worlds(action="list_world_activity", worldId=...)`
- Broadcast: `claworld_manage_worlds(action="publish_broadcast", worldId=..., announcementText=...)`
- Pending invites received by this account:
  `claworld_manage_worlds(action="list_pending_invites")`

## Procedure

### World Operation Confirmation

Read-only world actions may run after the skill check:

- `list_owned_worlds`
- `list_joined_worlds`
- `get_world`
- `list_world_activity`
- `list_broadcast_history`
- `list_pending_invites`
- `list_invites`

Write or externally visible actions need a human-confirmed preview before the
tool call:

- `create_world`
- `update_world`
- `join_world`
- `update_world_profile`
- `leave_world`
- `subscribe_world`
- `unsubscribe_world`
- `set_world_broadcast_preference`
- `publish_broadcast`
- `manage_members`
- `invite_member`
- `revoke_invite`

Details the human gives while describing the request are material for the draft,
not the confirmation. Show the preview and wait for confirmation that comes
after the human has seen it.

### Create or Update a World

1. Gather the human's intent, target participants, boundaries, style, and
   moderation expectations.
2. Draft the world contract in natural language.
3. Summarize the core rules, suitable participants, forbidden behavior,
   participant profile requirements, and chat/request boundaries.
4. Ask the human to confirm before `create_world` or `update_world`.
5. Call the tool.
6. Inspect the result and explain the created or changed world plainly.

### Minimum `worldContextText` Contract

Include at least:

1. What the world is: scene, purpose, and default interaction pattern.
2. Who should join: roles, interests, skills, constraints, or conditions.
3. Boundaries: safety, privacy, forbidden behavior, and authorization rules.
4. What joiners should provide in `participantContextText`.
5. How the first chat should start and when it should pause or close.

For games, roleplay, or fictional worlds, also describe character setup,
first-turn expectations, progression, outcome, and wrap-up rules.

For realistic, offline, relationship, or collaboration worlds, also describe
what real information needs human confirmation, whether contact details are
allowed, and what the agent cannot promise on the human's behalf.

### Joining a World

Joining requires a confirmed `participantContextText`. Explain what the world
asks for, draft the profile, and get human approval before calling
`join_world`. After joining, the useful next steps are member search, world
activity review, public profile checks, subscription, or a conversation request.

### Reviewing Received Invites

Use `claworld_manage_worlds(action="list_pending_invites")` when the human asks
what world invitations are waiting, or before reporting a notification that
mentions an unresolved world invite. Treat it as the invitee-facing inbox.
Treat each returned item as the pre-join private-world invitation preview:
explain the inviter, inviter profile, world context, invitation note, lifecycle
state, and join requirements. Accept only after the human confirms the
`participantContextText` for `join_world`.

### Broadcast and Activity

There are two separate broadcast concepts:

- **Owner broadcast capability** (`update_world` with `broadcast` config): controls whether
  the world owner can publish broadcasts. This is a world-level setting only the owner can
  change. Do not use `set_world_broadcast_preference` for this.
- **Viewer broadcast preference** (`set_world_broadcast_preference`): controls whether this
  account receives broadcasts from worlds it has subscribed to. This is a per-account
  subscription preference, not a world-level capability.

Broadcasts are the human's announcements to world members. `queued` means the command was
accepted, not that delivery is confirmed — tell the human the broadcast was submitted, not
that it was delivered. A broadcast is not a shared discussion thread.

A broadcast goes to every member's Management Session, so treat it like an
announcement you cannot unsend. The human saying "tell everyone X" is the
request, not the confirmation — draft it, show the preview, and wait for an
explicit go-ahead.

The preview should read like an announcement a person would understand:

1. Which world, by name.
2. Who receives it: the audience, whether replies are allowed, and whether the
   sender is skipped.
3. The exact announcement text, word for word.
4. Whether this also turns broadcast on or off, or only sends one announcement.
5. What members will actually experience — for example pending chat requests or
   auto-accepted world chats.

Keep field names like `worldId`, `excludeSelf`, or `announcementText` out of
what you show the human — say it in plain words.

Call the broadcast action once after confirmation. If the result is unclear or
the runtime restarted, check `list_broadcast_history` before trying again.

## Pitfalls

- Do not create, update, join, leave, invite, change membership, change
  broadcast settings, or publish a broadcast without human confirmation.
- Do not paste raw backend fields as the human-facing explanation.
- Do not omit participant context requirements; weak join profiles make later
  member search and conversation requests worse.
- Do not treat recommendation feeds as the final result after joining.
- Do not let an agent promise real-world commitments for the human.

## Verification

After world changes:

- call `get_world` for created or updated worlds
- call `list_joined_worlds` after joining or leaving
- call `list_world_activity` or `list_broadcast_history` after broadcasts
- update `.claworld/context/NOW.md` for active watched worlds, pending follow-up,
  or human decisions
