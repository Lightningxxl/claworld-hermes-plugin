"""Interactive Hermes setup flow for the Claworld platform plugin."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .config import DEFAULT_CLAWORLD_SERVER_URL, ClaworldConfig, hermes_home_path
from .http_client import ClaworldHttpError, request_json


def interactive_setup() -> None:
    """Run the Claworld setup flow from ``hermes setup gateway``."""

    output = _cli_output()
    output["print_header"]("Claworld")

    existing = ClaworldConfig.load()
    if existing.app_token and existing.agent_id:
        output["print_success"]("Claworld is already configured.")
        if not output["prompt_yes_no"]("Reconfigure Claworld?", False):
            return

    email = _normalize_text(output["prompt"]("Email", password=False))
    if not email:
        output["print_warning"]("Skipped - Claworld setup needs an email address.")
        return

    try:
        started = start_email_verification(email)
    except Exception as exc:
        _print_setup_error(output, "Could not start email verification", exc)
        return

    expires_at = _normalize_text(started.get("expiresAt"))
    output["print_success"]("Verification email sent.")
    if expires_at:
        output["print_info"](f"Code expires at {expires_at}.")

    code = _normalize_text(output["prompt"]("Verification code", password=False))
    if not code:
        output["print_warning"]("Skipped - Claworld setup needs the verification code.")
        return

    try:
        verified = complete_email_verification(email, code)
        persistence = persist_setup_credentials(verified)
    except Exception as exc:
        _print_setup_error(output, "Could not complete email verification", exc)
        return

    agent_id = _normalize_text(verified.get("agentId"))
    output["print_success"]("Claworld configured.")
    if agent_id:
        output["print_info"](f"Agent ID: {agent_id}")
    output["print_info"](f"Credential saved to {persistence['path']}")
    output["print_info"]("Restart the gateway for Claworld to connect: hermes gateway restart")


def start_email_verification(
    email: str,
    *,
    server_url: str = DEFAULT_CLAWORLD_SERVER_URL,
) -> dict:
    """Ask Claworld to send an email verification code."""

    return request_json(
        _identity_request_config(server_url),
        "POST",
        "/v1/identity/email/start",
        body=_drop_empty({"email": email}),
    )


def complete_email_verification(
    email: str,
    code: str,
    *,
    server_url: str = DEFAULT_CLAWORLD_SERVER_URL,
) -> dict:
    """Exchange an email verification code for a Claworld app credential."""

    payload = request_json(
        _identity_request_config(server_url),
        "POST",
        "/v1/identity/email/verify",
        body=_drop_empty({"email": email, "code": code}),
    )
    app_token = _normalize_text(payload.get("appToken"))
    agent_id = _normalize_text(payload.get("agentId"))
    if not app_token or not agent_id:
        raise ValueError("Claworld email verification did not return appToken and agentId")
    return payload


def persist_setup_credentials(verified_payload: dict[str, Any]) -> dict:
    """Persist Claworld setup credentials through Hermes' official env writer."""

    app_token = _normalize_text(verified_payload.get("appToken"))
    agent_id = _normalize_text(verified_payload.get("agentId"))
    if not app_token or not agent_id:
        raise ValueError("Claworld setup cannot persist missing appToken or agentId")

    values = {
        "CLAWORLD_APP_TOKEN": app_token,
        "CLAWORLD_AGENT_ID": agent_id,
    }

    return save_env_values(values)


def save_env_values(values: dict[str, str]) -> dict:
    """Save env values, preferring Hermes' sanitizing atomic writer."""

    cleaned = {key: _normalize_text(value) for key, value in values.items() if _normalize_text(value)}
    try:
        from hermes_cli.config import get_env_path, save_env_value

        for key, value in cleaned.items():
            save_env_value(key, value)
        env_path = Path(get_env_path()).expanduser()
        status = "saved_to_hermes_env"
    except Exception:
        env_path = hermes_home_path() / ".env"
        _write_dotenv_values(env_path, cleaned)
        status = "saved_to_dotenv_fallback"

    for key, value in cleaned.items():
        os.environ[key] = value
    return {"status": status, "path": str(env_path), "restartRequired": True}


def _identity_request_config(server_url: str) -> ClaworldConfig:
    return ClaworldConfig(server_url=_normalize_text(server_url, DEFAULT_CLAWORLD_SERVER_URL))


def _drop_empty(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if _normalize_text(value)}


def _normalize_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _cli_output() -> dict[str, Any]:
    try:
        from hermes_cli.cli_output import (
            print_error,
            print_header,
            print_info,
            print_success,
            print_warning,
            prompt,
            prompt_yes_no,
        )

        return {
            "print_error": print_error,
            "print_header": print_header,
            "print_info": print_info,
            "print_success": print_success,
            "print_warning": print_warning,
            "prompt": prompt,
            "prompt_yes_no": prompt_yes_no,
        }
    except Exception:
        return {
            "print_error": lambda text: print(f"Error: {text}"),
            "print_header": lambda text: print(f"\n{text}"),
            "print_info": print,
            "print_success": print,
            "print_warning": lambda text: print(f"Warning: {text}"),
            "prompt": _fallback_prompt,
            "prompt_yes_no": _fallback_prompt_yes_no,
        }


def _fallback_prompt(question: str, default: str | None = None, password: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{question}{suffix}: ").strip()
    return value or default or ""


def _fallback_prompt_yes_no(question: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    value = input(f"{question} [{suffix}]: ").strip().lower()
    if not value:
        return default
    return value.startswith("y")


def _print_setup_error(output: dict[str, Any], prefix: str, exc: Exception) -> None:
    if isinstance(exc, ClaworldHttpError):
        body = exc.body if isinstance(exc.body, dict) else {}
        message = body.get("message") or body.get("error") or str(exc)
        output["print_error"](f"{prefix}: HTTP {exc.status} {message}")
        return
    output["print_error"](f"{prefix}: {exc}")


def _write_dotenv_values(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    updated_keys = set()
    lines: list[str] = []
    for line in existing:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in values:
            lines.append(f"{key}={_dotenv_value(values[key])}")
            updated_keys.add(key)
        else:
            lines.append(line)
    for key, value in values.items():
        if key not in updated_keys:
            lines.append(f"{key}={_dotenv_value(value)}")

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines).rstrip() + "\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _dotenv_value(value: str) -> str:
    value = str(value).replace("\n", "").replace("\r", "")
    if any(ch.isspace() or ch in {'"', "#", "'"} for ch in value):
        return json.dumps(value)
    return value
