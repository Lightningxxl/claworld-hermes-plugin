"""Small authenticated HTTP client for Claworld API calls."""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import ClaworldConfig
from .protocol import normalize_http_base_url

PLUGIN_CLIENT = "hermes-plugin"
PLUGIN_VERSION = "0.1.0"
LEGACY_PLUGIN_VERSION = f"claworld-hermes-plugin/{PLUGIN_VERSION}"
PLUGIN_VERSION_HEADER = "x-claworld-plugin-version"
CLIENT_HEADER = "x-claworld-client"
CLIENT_VERSION_HEADER = "x-claworld-client-version"
CLIENT_CHANNEL_HEADER = "x-claworld-client-channel"
USER_AGENT = f"{LEGACY_PLUGIN_VERSION} hermes-agent"
RETRY_BASE_DELAY_SECONDS = 0.2
RETRY_MAX_DELAY_SECONDS = 1.0
TRANSPORT_ERRORS = (
    urllib.error.URLError,
    http.client.RemoteDisconnected,
    ssl.SSLError,
    TimeoutError,
    ConnectionResetError,
    socket.timeout,
)


class ClaworldHttpError(RuntimeError):
    def __init__(self, status: int, body: Any, message: str = "Claworld HTTP request failed") -> None:
        super().__init__(f"{message}: HTTP {status}")
        self.status = status
        self.body = body


def auth_headers(config: ClaworldConfig, base: dict | None = None) -> dict:
    headers = dict(base or {})
    if not any(name.lower() == "user-agent" for name in headers):
        headers["User-Agent"] = USER_AGENT
    headers[CLIENT_HEADER] = PLUGIN_CLIENT
    headers[CLIENT_VERSION_HEADER] = PLUGIN_VERSION
    headers[CLIENT_CHANNEL_HEADER] = "testing"
    headers[PLUGIN_VERSION_HEADER] = LEGACY_PLUGIN_VERSION
    if config.api_key:
        headers["x-api-key"] = config.api_key
    if config.app_token:
        headers["authorization"] = f"Bearer {config.app_token}"
        headers["x-claworld-app-token"] = config.app_token
    return headers


def request_json(
    config: ClaworldConfig,
    method: str,
    path: str,
    *,
    query: dict | None = None,
    body: dict | None = None,
    timeout: float = 30.0,
) -> dict:
    url = build_url(config, path, query=query)
    data = None
    headers = auth_headers(config, {"accept": "application/json"})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"

    try:
        retry_count = int(config.http_retries)
    except (TypeError, ValueError):
        retry_count = 0
    attempts = max(1, retry_count + 1)
    for attempt in range(attempts):
        request = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
        try:
            with _build_opener(config).open(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as error:
            payload = error.read().decode("utf-8", "replace")
            try:
                body_payload = json.loads(payload) if payload else {}
            except json.JSONDecodeError:
                body_payload = {"message": payload}
            raise ClaworldHttpError(error.code, body_payload) from error
        except TRANSPORT_ERRORS:
            if attempt >= attempts - 1:
                raise
            time.sleep(min(RETRY_BASE_DELAY_SECONDS * (attempt + 1), RETRY_MAX_DELAY_SECONDS))
    return {}


def _build_opener(config: ClaworldConfig) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_proxy_handler(config))


def _proxy_handler(config: ClaworldConfig) -> urllib.request.ProxyHandler:
    if config.http_proxy:
        return urllib.request.ProxyHandler({"http": config.http_proxy, "https": config.http_proxy})
    if config.use_env_proxy:
        return urllib.request.ProxyHandler()
    return urllib.request.ProxyHandler({})


def build_url(config: ClaworldConfig, path: str, *, query: dict | None = None) -> str:
    base = normalize_http_base_url(config.server_url)
    if not path.startswith("/"):
        path = f"/{path}"
    url = f"{base}{path}"
    cleaned = {k: v for k, v in (query or {}).items() if v is not None and str(v) != ""}
    if cleaned:
        url = f"{url}?{urllib.parse.urlencode(cleaned, doseq=True)}"
    return url


def public_error_payload(error: Exception) -> dict:
    if isinstance(error, ClaworldHttpError):
        body = error.body if isinstance(error.body, dict) else {}
        backend_code = body.get("error") or body.get("code")
        backend_message = body.get("message") or body.get("reason")
        extras = {
            "httpStatus": error.status,
            "backendCode": backend_code,
            "backendMessage": backend_message,
            "fieldErrors": body.get("fieldErrors"),
            "requiredAction": body.get("requiredAction"),
            "nextAction": body.get("nextAction"),
            "nextTool": body.get("nextTool"),
            "missingFields": body.get("missingFields"),
            "publicIdentity": body.get("publicIdentity"),
            "agentId": body.get("agentId"),
            "profile": body.get("profile"),
        }
        return {
            "status": "error",
            **{key: value for key, value in extras.items() if value is not None},
            "error": {
                "type": "claworld_http_error",
                "httpStatus": error.status,
                "body": error.body,
                "message": str(error),
            },
        }
    return {
        "status": "error",
        "error": {
            "type": type(error).__name__,
            "message": str(error),
        },
    }
