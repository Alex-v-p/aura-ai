"""Closed, disabled-by-default Google Calendar OAuth platform boundary.

This module deliberately contains configuration and policy only.  It does not
perform OAuth, HTTP, encryption, persistence, or provider discovery.  Those
responsibilities belong to later, explicitly owned work items.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, SupportsIndex, cast
from urllib.parse import urlsplit, urlunsplit

GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_TOKEN_REVOCATION_ENDPOINT = "https://oauth2.googleapis.com/revoke"
GOOGLE_CALENDAR_API_ENDPOINT = "https://www.googleapis.com/calendar/v3"
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
GOOGLE_CALENDAR_CALLBACK_PATH = "/api/v1/integrations/calendar/callback"

# Public aliases make the fixed nature of these values obvious to adapters and
# tests without offering a configuration escape hatch.
AUTHORIZATION_ENDPOINT = GOOGLE_AUTHORIZATION_ENDPOINT
TOKEN_ENDPOINT = GOOGLE_TOKEN_ENDPOINT
REVOCATION_ENDPOINT = GOOGLE_TOKEN_REVOCATION_ENDPOINT
CALENDAR_API_ENDPOINT = GOOGLE_CALENDAR_API_ENDPOINT
CALENDAR_SCOPE = GOOGLE_CALENDAR_SCOPE
CALENDAR_CALLBACK_PATH = GOOGLE_CALENDAR_CALLBACK_PATH

CredentialReadiness = Literal["disabled", "ready", "missing_configuration", "invalid_configuration"]
_SAFE_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_KEY_RING_BYTES = 64 * 1024
_MAX_KEYS = 8
_MAX_CLIENT_ID = 512
_LOCAL_ORIGIN = "http://aura.localhost:4200"


class CalendarOAuthConfigurationError(ValueError):
    """Raised for invalid configuration without including secret details."""


class CalendarOAuthConfigurationMissing(CalendarOAuthConfigurationError):
    """Raised when an enabled file-backed secret is not present."""


class SecretValue:
    """Opaque, repr-safe secret held only for the API process."""

    __slots__ = ("__value",)

    def __init__(self, value: str = "") -> None:
        if type(value) is not str:
            raise TypeError("secret value must be text")
        object.__setattr__(self, "_SecretValue__value", value)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("SecretValue is immutable")

    def __repr__(self) -> str:
        return "SecretValue(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"

    @property
    def value(self) -> str:
        return object.__getattribute__(self, "_SecretValue__value")

    def __reduce__(self) -> tuple[Any, ...]:
        raise TypeError("SecretValue serialization is prohibited")

    def __reduce_ex__(self, protocol: SupportsIndex) -> tuple[Any, ...]:
        del protocol
        raise TypeError("SecretValue serialization is prohibited")

    def __copy__(self) -> SecretValue:
        raise TypeError("SecretValue copying is prohibited")

    def __deepcopy__(self, memo: dict[int, object]) -> SecretValue:
        del memo
        raise TypeError("SecretValue copying is prohibited")


class KeyMaterial:
    """Opaque, bounded key material exposed only through the key adapter seam."""

    __slots__ = ("__identifier", "__bytes", "__status")

    def __init__(
        self, identifier: str, key_bytes: bytes, status: Literal["active", "decrypt_only"]
    ) -> None:
        if (
            type(identifier) is not str
            or type(key_bytes) is not bytes
            or not _SAFE_KEY_ID.fullmatch(identifier)
            or len(key_bytes) != 32
        ):
            raise CalendarOAuthConfigurationError("invalid calendar encryption key material")
        if status not in {"active", "decrypt_only"}:
            raise CalendarOAuthConfigurationError("invalid calendar encryption key material")
        object.__setattr__(self, "_KeyMaterial__identifier", identifier)
        object.__setattr__(self, "_KeyMaterial__bytes", bytes(key_bytes))
        object.__setattr__(self, "_KeyMaterial__status", status)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("KeyMaterial is immutable")

    @property
    def identifier(self) -> str:
        return object.__getattribute__(self, "_KeyMaterial__identifier")

    @property
    def status(self) -> Literal["active", "decrypt_only"]:
        return object.__getattribute__(self, "_KeyMaterial__status")

    @property
    def key_bytes(self) -> bytes:
        """Return the key to the future encryption adapter; no crypto occurs here."""

        return object.__getattribute__(self, "_KeyMaterial__bytes")

    def __repr__(self) -> str:
        return f"KeyMaterial(id={self.identifier!r}, status={self.status!r}, length=32)"

    def __reduce__(self) -> tuple[Any, ...]:
        raise TypeError("KeyMaterial serialization is prohibited")

    def __reduce_ex__(self, protocol: SupportsIndex) -> tuple[Any, ...]:
        del protocol
        raise TypeError("KeyMaterial serialization is prohibited")

    def __copy__(self) -> KeyMaterial:
        raise TypeError("KeyMaterial copying is prohibited")

    def __deepcopy__(self, memo: dict[int, object]) -> KeyMaterial:
        del memo
        raise TypeError("KeyMaterial copying is prohibited")


class CredentialKeyRing:
    """Opaque, immutable key-ring wrapper with no dataclass serialization surface."""

    __slots__ = ("__active_key_id", "__keys", "__version")

    def __init__(
        self, active_key_id: str, keys: Mapping[str, KeyMaterial], version: int = 1
    ) -> None:
        if type(version) is not int or version != 1:
            raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
        if type(active_key_id) is not str or not _SAFE_KEY_ID.fullmatch(active_key_id):
            raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
        if not 1 <= len(keys) <= _MAX_KEYS:
            raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
        typed_keys = cast(Mapping[object, object], keys)
        if any(
            not isinstance(key_id, str) or not isinstance(key, KeyMaterial)
            for key_id, key in typed_keys.items()
        ):
            raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
        object.__setattr__(self, "_CredentialKeyRing__active_key_id", active_key_id)
        object.__setattr__(self, "_CredentialKeyRing__keys", MappingProxyType(dict(keys)))
        object.__setattr__(self, "_CredentialKeyRing__version", version)
        active = self.__keys.get(self.active_key_id)
        if active is None or active.status != "active":
            raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
        if sum(key.status == "active" for key in self.__keys.values()) != 1:
            raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
        if any(key_id != key.identifier for key_id, key in self.__keys.items()):
            raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("CredentialKeyRing is immutable")

    @property
    def active_key_id(self) -> str:
        return object.__getattribute__(self, "_CredentialKeyRing__active_key_id")

    @property
    def version(self) -> int:
        return object.__getattribute__(self, "_CredentialKeyRing__version")

    @property
    def keys(self) -> Mapping[str, KeyMaterial]:
        return object.__getattribute__(self, "_CredentialKeyRing__keys")

    @property
    def active(self) -> KeyMaterial:
        return self.keys[self.active_key_id]

    def key_for_decryption(self, key_id: str) -> KeyMaterial | None:
        return self.keys.get(key_id)

    def __repr__(self) -> str:
        return (
            f"CredentialKeyRing(active_key_id={self.active_key_id!r}, "
            f"key_count={len(self.keys)}, version={self.version})"
        )

    def __reduce__(self) -> tuple[Any, ...]:
        raise TypeError("CredentialKeyRing serialization is prohibited")

    def __reduce_ex__(self, protocol: SupportsIndex) -> tuple[Any, ...]:
        del protocol
        raise TypeError("CredentialKeyRing serialization is prohibited")

    def __copy__(self) -> CredentialKeyRing:
        raise TypeError("CredentialKeyRing copying is prohibited")

    def __deepcopy__(self, memo: dict[int, object]) -> CredentialKeyRing:
        del memo
        raise TypeError("CredentialKeyRing copying is prohibited")


@dataclass(frozen=True, slots=True)
class CredentialEncryptionPolicy:
    algorithm: Literal["AES-256-GCM"] = "AES-256-GCM"
    nonce_bytes: int = 12
    associated_data_fields: tuple[str, str] = ("owner_id", "connection_id")
    stores_key_version: bool = True

    def __repr__(self) -> str:
        return (
            "CredentialEncryptionPolicy(algorithm='AES-256-GCM', nonce_bytes=12, "
            "associated_data_fields=('owner_id', 'connection_id'), stores_key_version=True)"
        )


CREDENTIAL_ENCRYPTION_POLICY = CredentialEncryptionPolicy()


@dataclass(frozen=True, slots=True)
class CalendarOAuthReadiness:
    state: CredentialReadiness

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    def __repr__(self) -> str:
        return f"CalendarOAuthReadiness(state={self.state!r})"


@dataclass(frozen=True, slots=True)
class GoogleCalendarOAuthConfig:
    """Validated provider-neutral settings for the API-owned OAuth boundary."""

    enabled: bool = False
    client_id: str = ""
    client_secret: SecretValue | str = SecretValue()
    client_secret_file: str = ""
    key_ring_file: str = ""
    public_origin: str = "https://aura.example.invalid"
    environment: str = "development"
    callback_uri: str = ""
    key_ring: CredentialKeyRing | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.client_secret, SecretValue):
            raise CalendarOAuthConfigurationError("calendar OAuth secret must be file-backed")
        if len(self.client_id) > _MAX_CLIENT_ID:
            raise CalendarOAuthConfigurationError("invalid calendar OAuth configuration")
        if self.callback_uri and self.callback_uri != build_callback_uri(
            self.public_origin, self.environment
        ):
            raise CalendarOAuthConfigurationError("invalid calendar OAuth configuration")

    @classmethod
    def from_settings(cls, settings: object) -> GoogleCalendarOAuthConfig:
        def get(name: str, default: Any = "") -> Any:
            return getattr(settings, name, default)

        client_secret = get("google_calendar_client_secret", "") or ""
        client_secret_file = get("google_calendar_client_secret_file", "") or ""
        key_ring_file = (
            get("google_calendar_key_ring_file", "")
            or get("provider_credential_key_ring_file", "")
            or ""
        )
        if not isinstance(client_secret, str) or not isinstance(client_secret_file, str):
            raise CalendarOAuthConfigurationError("invalid calendar OAuth configuration")
        if client_secret:
            # Inline provider secrets are never accepted, even when disabled.
            raise CalendarOAuthConfigurationError("calendar OAuth secret must be file-backed")
        origin = str(get("public_origin", ""))
        environment = str(get("environment", "development"))
        config = cls(
            enabled=bool(get("google_calendar_enabled", False)),
            client_id=str(get("google_calendar_client_id", "") or ""),
            client_secret=SecretValue(),
            client_secret_file=client_secret_file,
            key_ring_file=str(key_ring_file),
            public_origin=origin,
            environment=environment,
            callback_uri=str(get("google_calendar_callback_uri", "") or ""),
        )
        if config.enabled:
            config.validate()
        return config

    def validate(self) -> GoogleCalendarOAuthConfig:
        _validate_public_origin(self.public_origin, self.environment)
        if not self.client_id.strip() or not self.client_secret_file or not self.key_ring_file:
            raise CalendarOAuthConfigurationMissing("calendar OAuth configuration is incomplete")
        if len(self.client_id) > _MAX_CLIENT_ID:
            raise CalendarOAuthConfigurationError("invalid calendar OAuth configuration")
        secret = _read_secret_file(self.client_secret_file)
        ring = load_key_ring(self.key_ring_file)
        object.__setattr__(self, "client_secret", SecretValue(secret))
        object.__setattr__(self, "key_ring", ring)
        object.__setattr__(
            self, "callback_uri", build_callback_uri(self.public_origin, self.environment)
        )
        return self

    def readiness(self) -> CalendarOAuthReadiness:
        if not self.enabled:
            return CalendarOAuthReadiness("disabled")
        try:
            self.validate()
        except CalendarOAuthConfigurationMissing:
            return CalendarOAuthReadiness("missing_configuration")
        except CalendarOAuthConfigurationError:
            return CalendarOAuthReadiness("invalid_configuration")
        return CalendarOAuthReadiness("ready")

    def __repr__(self) -> str:
        return (
            "GoogleCalendarOAuthConfig("
            f"enabled={self.enabled!r}, client_id_present={bool(self.client_id)!r}, "
            f"public_origin_configured={bool(self.public_origin)!r}, "
            f"callback_path={GOOGLE_CALENDAR_CALLBACK_PATH!r})"
        )


def build_callback_uri(public_origin: str, environment: str = "production") -> str:
    """Build the only accepted provider callback URI from the stable origin."""

    _validate_public_origin(public_origin, environment)
    return f"{public_origin.rstrip('/')}{GOOGLE_CALENDAR_CALLBACK_PATH}"


def validate_callback_uri(uri: str, public_origin: str, environment: str = "production") -> bool:
    """Return whether ``uri`` is exactly the configured callback URI."""

    try:
        return uri == build_callback_uri(public_origin, environment)
    except CalendarOAuthConfigurationError:
        return False


def validate_calendar_callback(
    uri: str, public_origin: str, environment: str = "production"
) -> None:
    if not validate_callback_uri(uri, public_origin, environment):
        raise CalendarOAuthConfigurationError("calendar callback rejected")


def fixed_endpoints() -> Mapping[str, str]:
    return MappingProxyType(
        {
            "authorization": GOOGLE_AUTHORIZATION_ENDPOINT,
            "token": GOOGLE_TOKEN_ENDPOINT,
            "revocation": GOOGLE_TOKEN_REVOCATION_ENDPOINT,
            "calendar_api": GOOGLE_CALENDAR_API_ENDPOINT,
        }
    )


def load_key_ring(path: str | os.PathLike[str]) -> CredentialKeyRing:
    """Read and validate a bounded JSON key ring without returning raw JSON."""

    payload = _read_file(path, _MAX_KEY_RING_BYTES)
    try:
        document_value: Any = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, CalendarOAuthConfigurationError) as exc:
        raise CalendarOAuthConfigurationError("invalid calendar encryption key ring") from exc
    if not isinstance(document_value, dict):
        raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
    document = cast(dict[str, Any], document_value)
    if set(document) - {"version", "active_key_id", "active", "keys"}:
        raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
    version = document.get("version", 1)
    if version != 1 or ("active_key_id" in document and "active" in document):
        raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
    keys_value = document.get("keys")
    active_value = document.get("active_key_id", document.get("active"))
    if not isinstance(active_value, str) or not isinstance(keys_value, (dict, list)):
        raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
    entries: list[tuple[str, dict[str, Any]]] = []
    if isinstance(keys_value, dict):
        for identifier_value, value_value in cast(dict[Any, Any], keys_value).items():
            identifier = identifier_value
            value = value_value
            if not isinstance(identifier, str) or not isinstance(value, (dict, str)):
                raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
            entries.append(
                (
                    identifier,
                    {"key": value}
                    if isinstance(value, str)
                    else cast(dict[str, Any], value),
                )
            )
    else:
        for value_value in cast(list[Any], keys_value):
            value = value_value
            if not isinstance(value, dict):
                raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
            value_map = cast(dict[str, Any], value)
            if not isinstance(value_map.get("id"), str):
                raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
            entries.append((value_map["id"], value_map))
    if (
        not entries
        or len(entries) > _MAX_KEYS
        or len({item[0] for item in entries}) != len(entries)
    ):
        raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
    parsed: dict[str, KeyMaterial] = {}
    for identifier, value in entries:
        if not _SAFE_KEY_ID.fullmatch(identifier):
            raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
        if set(value) - {"id", "status", "key"}:
            raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
        if "id" in value and value["id"] != identifier:
            raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
        status = value.get("status")
        if status is None:
            status = "active" if identifier == active_value else "decrypt_only"
        if status not in {"active", "decrypt_only"}:
            raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
        encoded = value.get("key")
        if not isinstance(encoded, str):
            raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
        key_bytes = _decode_key(encoded)
        parsed[identifier] = KeyMaterial(identifier, key_bytes, status)
    return CredentialKeyRing(active_value, MappingProxyType(parsed), version)


def calendar_oauth_readiness(settings: object) -> CalendarOAuthReadiness:
    """Return content-free readiness for a Settings-like object."""

    try:
        config = GoogleCalendarOAuthConfig.from_settings(settings)
    except CalendarOAuthConfigurationMissing:
        return CalendarOAuthReadiness("missing_configuration")
    except CalendarOAuthConfigurationError:
        return CalendarOAuthReadiness("invalid_configuration")
    return config.readiness()


def _validate_public_origin(origin: str, environment: str) -> None:
    if type(origin) is not str or any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in origin
    ):
        raise CalendarOAuthConfigurationError("invalid calendar public origin")
    try:
        parsed = urlsplit(origin)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise CalendarOAuthConfigurationError("invalid calendar public origin") from exc
    if not hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CalendarOAuthConfigurationError("invalid calendar public origin")
    if parsed.path not in {"", "/"}:
        raise CalendarOAuthConfigurationError("invalid calendar public origin")
    canonical_origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    if canonical_origin != origin:
        raise CalendarOAuthConfigurationError("invalid calendar public origin")
    normalized_environment = environment.casefold()
    if normalized_environment in {"development", "dev", "test", "local"}:
        if origin != _LOCAL_ORIGIN:
            raise CalendarOAuthConfigurationError("invalid calendar public origin")
        return
    if parsed.scheme != "https" or port is not None:
        raise CalendarOAuthConfigurationError("invalid calendar public origin")
    lowered = hostname.casefold().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost") or lowered in {"127.0.0.1", "::1"}:
        raise CalendarOAuthConfigurationError("invalid calendar public origin")


def _read_secret_file(path: str | os.PathLike[str]) -> str:
    payload = _read_file(path, 16 * 1024)
    secret = payload.rstrip("\r\n")
    if not secret or any(ord(character) < 0x20 for character in secret):
        raise CalendarOAuthConfigurationError("invalid calendar secret configuration")
    return secret


def _read_file(path: str | os.PathLike[str], limit: int) -> str:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CalendarOAuthConfigurationError("invalid calendar secret configuration")
        if metadata.st_mode & 0o022:
            raise CalendarOAuthConfigurationError("invalid calendar secret configuration")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = None
            data = stream.read(limit + 1)
    except CalendarOAuthConfigurationError:
        raise
    except FileNotFoundError as exc:
        raise CalendarOAuthConfigurationMissing(
            "calendar secret configuration unavailable"
        ) from exc
    except OSError as exc:
        # Deliberately omit the path and OS error from the public failure.
        raise CalendarOAuthConfigurationError("calendar secret configuration unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(data) > limit:
        raise CalendarOAuthConfigurationError("calendar secret configuration is too large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CalendarOAuthConfigurationError("invalid calendar secret configuration") from exc


def _decode_key(value: str) -> bytes:
    try:
        candidate = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise CalendarOAuthConfigurationError("invalid calendar encryption key ring") from exc
    try:
        decoded = base64.b64decode(
            candidate + b"=" * (-len(candidate) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error) as exc:
        decoded = b""
        try:
            if len(value) == 64:
                decoded = bytes.fromhex(value)
        except ValueError:
            pass
        if not decoded:
            raise CalendarOAuthConfigurationError("invalid calendar encryption key ring") from exc
    if len(decoded) != 32:
        raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
    return decoded


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CalendarOAuthConfigurationError("invalid calendar encryption key ring")
        result[key] = value
    return result


CalendarOAuthConfig = GoogleCalendarOAuthConfig
GoogleCalendarOAuthSettings = GoogleCalendarOAuthConfig
CalendarKeyRing = CredentialKeyRing
OAuthReadiness = CalendarOAuthReadiness
expected_callback_uri = build_callback_uri
load_credential_key_ring = load_key_ring


__all__ = [
    "AUTHORIZATION_ENDPOINT",
    "CALENDAR_API_ENDPOINT",
    "CALENDAR_CALLBACK_PATH",
    "CALENDAR_SCOPE",
    "CREDENTIAL_ENCRYPTION_POLICY",
    "CredentialEncryptionPolicy",
    "CredentialKeyRing",
    "CalendarKeyRing",
    "CalendarOAuthConfig",
    "CalendarOAuthConfigurationError",
    "CalendarOAuthConfigurationMissing",
    "CalendarOAuthReadiness",
    "GOOGLE_AUTHORIZATION_ENDPOINT",
    "GOOGLE_CALENDAR_API_ENDPOINT",
    "GOOGLE_CALENDAR_CALLBACK_PATH",
    "GOOGLE_CALENDAR_SCOPE",
    "GOOGLE_TOKEN_ENDPOINT",
    "GOOGLE_TOKEN_REVOCATION_ENDPOINT",
    "GoogleCalendarOAuthConfig",
    "GoogleCalendarOAuthSettings",
    "KeyMaterial",
    "OAuthReadiness",
    "REVOCATION_ENDPOINT",
    "SecretValue",
    "TOKEN_ENDPOINT",
    "build_callback_uri",
    "calendar_oauth_readiness",
    "expected_callback_uri",
    "fixed_endpoints",
    "load_credential_key_ring",
    "load_key_ring",
    "validate_calendar_callback",
    "validate_callback_uri",
]
