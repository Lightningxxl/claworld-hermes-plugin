"""Claworld Hermes plugin version metadata."""

PLUGIN_CLIENT = "hermes-plugin"
PLUGIN_VERSION = "2026.7.2-testing.1"
PLUGIN_PACKAGE = "claworld-hermes-plugin"


def infer_client_channel(version: str = PLUGIN_VERSION) -> str:
    return "testing" if "-testing." in version else "stable"


USER_AGENT = f"{PLUGIN_PACKAGE}/{PLUGIN_VERSION} hermes-agent"
