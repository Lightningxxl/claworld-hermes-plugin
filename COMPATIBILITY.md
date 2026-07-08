# Compatibility Contracts

This plugin keeps a small number of bounded compatibility behaviors because
Hermes tool calls and Claworld relay deliveries are external interfaces.

## Management Report Send

Canonical path: Hermes native `tools.send_message_tool.send_message_tool`.

Compatibility consumer: Claworld Management Session reports that need both human
chat delivery and Main Session transcript mirror confirmation.

Owner: Claworld Hermes plugin.

Review boundary: September 30, 2026. Removal requires Hermes native send results
to always report transcript mirror status for the recorded target route, plus
tests that cover the failure contract when mirroring is unavailable.

Executable proof:

- `tests/test_core.py::ToolTests::test_send_message_forwards_to_hermes_and_trusts_auto_mirror`
- `tests/test_core.py::ToolTests::test_send_message_fallback_mirrors_once_when_auto_mirror_is_missing`
- `tests/test_core.py::ToolTests::test_send_message_does_not_mirror_when_delivery_fails`

## Tool Action Aliases

Canonical path: explicit `action` values listed in the tool schemas.

Compatibility consumer: existing Hermes prompts and model calls that use short
action names such as `list`, `get`, `join`, `create`, or `reengage`.

Owner: Claworld Hermes plugin.

Review boundary: September 30, 2026. Removal requires prompt/skill releases that
emit only canonical action values and tests that assert retired alias values
return clear validation errors.

Executable proof:

- `tests/test_core.py` tool routing and schema coverage

## Relay Ack HTTP Recovery

Canonical path: relay WebSocket ack for accepted, reply, and kept-silent events.

Compatibility consumer: relay deliveries where the WebSocket ack is lost while
the HTTP control path remains available.

Owner: Claworld Hermes plugin and Claworld relay backend.

Review boundary: transport-level reliability policy; this is a durable recovery
path while the relay exposes both WebSocket and HTTP control surfaces.

Executable proof:

- `tests/test_core.py::RelayClientTests::test_delivery_visibility_retry_retries_404_delivery_not_found`
