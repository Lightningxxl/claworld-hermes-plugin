"""Local-only durable profile selection for trusted group projection.

Hermes profile names choose a local credential set.  They are deliberately
never part of the relay-authored projection route or its authority digest.
This store binds that local choice to the immutable backend binding id while
also retaining the initiator's exact captured origin profile across restarts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


_ORIGIN_PROFILE_SCHEMA = "claworld.local-projection-origin-profile.v1"
_BINDING_PROFILE_SCHEMA = "claworld.local-projection-binding-profile.v1"
_ROUTE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BINDING_ROLES = frozenset({"initiator_exact", "peer_default"})


class ProjectionProfileSelectionError(RuntimeError, ValueError):
    """Raised when a local profile choice is missing, invalid, or mutable."""


@dataclass(frozen=True)
class ProjectionBindingProfile:
    projection_binding_id: str
    route_digest: str
    profile: str
    role: str
    channel_identity_binding_id: str | None = None


class ProjectionProfileStore:
    """Permission-restricted immutable local profile selections."""

    def __init__(self, memory_root: Path | str) -> None:
        self.root = Path(memory_root).expanduser() / "runtime" / "projections" / "profiles"
        self.origin_dir = self.root / "origins"
        self.binding_dir = self.root / "bindings"
        self._lock = threading.RLock()
        for path in (self.root, self.origin_dir, self.binding_dir):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)

    def remember_origin_profile(self, route_digest: str, profile: str) -> str:
        """Persist the initiating turn's exact captured profile by route digest."""

        digest = _route_digest(route_digest)
        normalized_profile = _profile(profile)
        payload = {
            "schema": _ORIGIN_PROFILE_SCHEMA,
            "routeDigest": digest,
            "profile": normalized_profile,
        }
        self._save_immutable(self.origin_dir, digest, payload)
        return normalized_profile

    def load_origin_profile(self, route_digest: str) -> str | None:
        digest = _route_digest(route_digest)
        payload = self._load(self.origin_dir, digest)
        if payload is None:
            return None
        _assert_keys(
            payload,
            schema=_ORIGIN_PROFILE_SCHEMA,
            route_digest=digest,
        )
        return _profile(payload.get("profile"))

    def bind_profile(
        self,
        *,
        projection_binding_id: str,
        route_digest: str,
        profile: str,
        role: str,
        channel_identity_binding_id: str | None = None,
    ) -> ProjectionBindingProfile:
        """Immutably bind one relay binding to one explicit local profile."""

        binding_id = _plain(projection_binding_id, "projectionBindingId", 512)
        digest = _route_digest(route_digest)
        normalized_profile = _profile(profile)
        normalized_role = _plain(role, "role", 64)
        normalized_identity_id = _optional_plain(
            channel_identity_binding_id,
            "channelIdentityBindingId",
            512,
        )
        if normalized_role not in _BINDING_ROLES:
            raise ProjectionProfileSelectionError("invalid projection profile binding role")
        payload = {
            "schema": _BINDING_PROFILE_SCHEMA,
            "projectionBindingId": binding_id,
            "routeDigest": digest,
            "profile": normalized_profile,
            "role": normalized_role,
            **(
                {"channelIdentityBindingId": normalized_identity_id}
                if normalized_identity_id
                else {}
            ),
        }
        saved = self._save_binding_profile(binding_id, payload)
        return _binding_profile(saved)

    def load_binding_profile(
        self,
        projection_binding_id: str,
    ) -> ProjectionBindingProfile | None:
        binding_id = _plain(projection_binding_id, "projectionBindingId", 512)
        payload = self._load(self.binding_dir, binding_id)
        if payload is None:
            return None
        selection = _binding_profile(payload)
        if selection.projection_binding_id != binding_id:
            raise ProjectionProfileSelectionError(
                "projection binding profile identity does not match its local key"
            )
        return selection

    def _save_immutable(
        self,
        directory: Path,
        key: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        path = _record_path(directory, key)
        with self._lock:
            existing = _read_payload(path)
            if existing is not None:
                existing_core = {
                    field: existing.get(field)
                    for field in payload
                }
                if existing_core != dict(payload):
                    raise ProjectionProfileSelectionError(
                        "local projection profile selection is immutable"
                    )
                return existing
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            stored = {**dict(payload), "createdAt": now, "updatedAt": now}
            _atomic_write(path, stored)
            return stored

    def _save_binding_profile(
        self,
        binding_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        path = _record_path(self.binding_dir, binding_id)
        with self._lock:
            existing = _read_payload(path)
            if existing is None:
                now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                stored = {**dict(payload), "createdAt": now, "updatedAt": now}
                _atomic_write(path, stored)
                return stored

            immutable_fields = (
                "schema",
                "projectionBindingId",
                "routeDigest",
                "profile",
                "role",
            )
            if any(existing.get(field) != payload.get(field) for field in immutable_fields):
                raise ProjectionProfileSelectionError(
                    "local projection binding profile selection is immutable"
                )
            current_identity = _optional_plain(
                existing.get("channelIdentityBindingId"),
                "channelIdentityBindingId",
                512,
            )
            proposed_identity = _optional_plain(
                payload.get("channelIdentityBindingId"),
                "channelIdentityBindingId",
                512,
            )
            if current_identity:
                if proposed_identity and proposed_identity != current_identity:
                    raise ProjectionProfileSelectionError(
                        "local projection channel identity binding is immutable"
                    )
                return existing
            if not proposed_identity:
                return existing
            updated = {
                **existing,
                "channelIdentityBindingId": proposed_identity,
                "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            _atomic_write(path, updated)
            return updated

    def _load(self, directory: Path, key: str) -> dict[str, Any] | None:
        with self._lock:
            return _read_payload(_record_path(directory, key))


def _binding_profile(payload: Mapping[str, Any]) -> ProjectionBindingProfile:
    if payload.get("schema") != _BINDING_PROFILE_SCHEMA:
        raise ProjectionProfileSelectionError("invalid local projection binding profile schema")
    role = _plain(payload.get("role"), "role", 64)
    if role not in _BINDING_ROLES:
        raise ProjectionProfileSelectionError("invalid projection profile binding role")
    return ProjectionBindingProfile(
        projection_binding_id=_plain(
            payload.get("projectionBindingId"),
            "projectionBindingId",
            512,
        ),
        route_digest=_route_digest(payload.get("routeDigest")),
        profile=_profile(payload.get("profile")),
        role=role,
        channel_identity_binding_id=_optional_plain(
            payload.get("channelIdentityBindingId"),
            "channelIdentityBindingId",
            512,
        ),
    )


def _assert_keys(
    payload: Mapping[str, Any],
    *,
    schema: str,
    route_digest: str,
) -> None:
    if payload.get("schema") != schema:
        raise ProjectionProfileSelectionError("invalid local origin profile schema")
    if _route_digest(payload.get("routeDigest")) != route_digest:
        raise ProjectionProfileSelectionError("local origin profile route digest mismatch")


def _profile(value: Any) -> str:
    profile = _plain(value, "profile", 128)
    if any(ord(char) < 32 or ord(char) == 127 for char in profile):
        raise ProjectionProfileSelectionError("profile contains control characters")
    return profile


def _route_digest(value: Any) -> str:
    digest = _plain(value, "routeDigest", 96)
    if not _ROUTE_DIGEST_RE.fullmatch(digest):
        raise ProjectionProfileSelectionError("invalid projection route digest")
    return digest


def _plain(value: Any, field: str, max_chars: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > max_chars:
        raise ProjectionProfileSelectionError(f"invalid local projection {field}")
    return normalized


def _optional_plain(value: Any, field: str, max_chars: int) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return _plain(value, field, max_chars)


def _record_path(directory: Path, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return directory / f"{digest}.json"


def _read_payload(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ProjectionProfileSelectionError(
            f"invalid local projection profile file: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProjectionProfileSelectionError("local projection profile record is not an object")
    return payload


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)


__all__ = [
    "ProjectionBindingProfile",
    "ProjectionProfileSelectionError",
    "ProjectionProfileStore",
]
