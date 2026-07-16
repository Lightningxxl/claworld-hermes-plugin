"""Small authenticated HTTP client for Claworld API calls."""

from __future__ import annotations

import http.client
import hashlib
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import ClaworldConfig
from .protocol import normalize_http_base_url
from .version import PLUGIN_CLIENT, PLUGIN_VERSION, USER_AGENT, infer_client_channel

CLIENT_HEADER = "x-claworld-client"
CLIENT_VERSION_HEADER = "x-claworld-client-version"
CLIENT_CHANNEL_HEADER = "x-claworld-client-channel"
RETRY_BASE_DELAY_SECONDS = 0.2
RETRY_MAX_DELAY_SECONDS = 1.0
TRANSPORT_RETRY_METHODS = frozenset({"GET", "HEAD"})
TRANSPORT_ERRORS = (
    urllib.error.URLError,
    http.client.RemoteDisconnected,
    ssl.SSLError,
    TimeoutError,
    ConnectionResetError,
    socket.timeout,
)
SHARE_CARD_MAX_BYTES = 10 * 1024 * 1024
SHARE_CARD_EXTENSIONS = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


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
    headers[CLIENT_CHANNEL_HEADER] = infer_client_channel()
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
    normalized_method = str(method or "GET").strip().upper() or "GET"
    data = None
    headers = auth_headers(config, {"accept": "application/json"})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"

    retry_count = 0
    if normalized_method in TRANSPORT_RETRY_METHODS:
        try:
            retry_count = int(config.http_retries)
        except (TypeError, ValueError):
            retry_count = 0
    attempts = max(1, retry_count + 1)
    for attempt in range(attempts):
        request = urllib.request.Request(url, data=data, method=normalized_method, headers=headers)
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


def download_share_card(
    config: ClaworldConfig,
    image_url: str,
    destination_dir: Path,
    *,
    timeout: float = 30.0,
    max_bytes: int = SHARE_CARD_MAX_BYTES,
) -> Path:
    """Download a backend-issued share-card image into Hermes media cache."""

    parsed = urllib.parse.urlparse(str(image_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("share-card image URL must use http or https")

    request = urllib.request.Request(
        image_url,
        method="GET",
        headers={"accept": "image/*", "user-agent": USER_AGENT},
    )
    with _build_opener(config).open(request, timeout=timeout) as response:
        content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if content_type not in SHARE_CARD_EXTENSIONS:
            raise ValueError(f"share-card response is not a supported image: {content_type or 'unknown'}")
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    raise ValueError("share-card image exceeds the delivery size limit")
            except ValueError as exc:
                if "exceeds" in str(exc):
                    raise

        chunks = []
        total = 0
        while True:
            chunk = response.read(min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("share-card image exceeds the delivery size limit")
            chunks.append(chunk)

    if not chunks:
        raise ValueError("share-card image response was empty")

    destination_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:20]
    path = destination_dir / f"claworld-share-card-{digest}{SHARE_CARD_EXTENSIONS[content_type]}"
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_bytes(b"".join(chunks))
    temporary.replace(path)
    return path


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
