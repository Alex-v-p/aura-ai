"""Short-lived Ed25519 request envelopes for isolated research providers."""

from __future__ import annotations

import base64
import binascii
import json
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import (
    BrowserFetchRequest,
    CancelRequest,
    HealthRequest,
    LimitsRequest,
    ProviderRequestPayload,
    SearchRequest,
    Signature,
    StaticFetchRequest,
)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    # The wire contract is canonical, unpadded base64url.  Python's decoder
    # otherwise accepts '+'/'/' and silently ignores malformed characters.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value) or len(value) % 4 == 1:
        raise ValueError("invalid base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid base64url") from exc
    if _b64(decoded) != value:
        raise ValueError("non-canonical base64url")
    return decoded


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


class SignedEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = Field(default="research-provider.v1", alias="schemaVersion")
    envelope_type: str = Field(default="request", alias="envelopeType")
    request_id: UUID = Field(alias="requestId")
    replay_id: UUID = Field(alias="replayId")
    audience: Literal["searxng", "static_fetcher", "browser_fetcher"]
    method: Literal["search", "fetch", "cancel", "limits", "health"]
    issued_at: datetime = Field(alias="issuedAt")
    expires_at: datetime = Field(alias="expiresAt")
    ttl_ms: int = Field(alias="ttlMs", ge=100, le=60_000)
    payload: ProviderRequestPayload
    signature: Signature

    def unsigned(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude={"signature"})

    @field_validator("issued_at", "expires_at", mode="before")
    @classmethod
    def require_utc_timestamp(cls, value: object) -> object:
        if isinstance(value, str) and not value.endswith("Z"):
            raise ValueError("provider timestamps must use UTC Z notation")
        return value

    @model_validator(mode="after")
    def validate_method_payload_binding(self) -> SignedEnvelope:
        valid = {
            "search": ("searxng", SearchRequest),
            "fetch": (
                ("static_fetcher", "browser_fetcher"),
                (StaticFetchRequest, BrowserFetchRequest),
            ),
            "cancel": (("static_fetcher", "browser_fetcher"), CancelRequest),
            "limits": (("searxng", "static_fetcher", "browser_fetcher"), LimitsRequest),
            "health": (("searxng", "static_fetcher", "browser_fetcher"), HealthRequest),
        }
        audiences, payload_types = valid[self.method]
        if isinstance(audiences, str):
            audiences = (audiences,)
        if isinstance(payload_types, tuple):
            accepted_types = payload_types
        else:
            accepted_types = (payload_types,)
        if self.audience not in audiences or not isinstance(self.payload, accepted_types):
            raise ValueError("provider envelope method and payload do not match audience")
        return self


class EnvelopeVerificationError(ValueError):
    """Bounded provider-envelope validation failure."""

    def __init__(self, code: str = "invalid_envelope") -> None:
        super().__init__(code)
        self.code = code


class ReplayCache:
    """Small bounded replay cache; eviction is safe because expiry is short."""

    def __init__(self, *, capacity: int = 4096) -> None:
        if capacity < 1:
            raise ValueError("replay cache capacity must be positive")
        self._capacity = capacity
        self._items: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def claim(self, request_id: UUID, *, expires_at: datetime, now: float | None = None) -> bool:
        key = str(request_id)
        current = time.time() if now is None else now
        deadline = expires_at.timestamp()
        with self._lock:
            expired = [item for item, value in self._items.items() if value <= current]
            for item in expired:
                self._items.pop(item, None)
            if deadline <= current or key in self._items:
                return False
            self._items[key] = deadline
            self._items.move_to_end(key)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)
            return True


class EnvelopeSigner:
    """Core-only signer. Providers must receive ``public_key`` only."""

    def __init__(self, key: Ed25519PrivateKey, *, issuer: str = "aura-core") -> None:
        self._key = key
        self.issuer = issuer

    @classmethod
    def generate(cls, *, issuer: str = "aura-core") -> EnvelopeSigner:
        return cls(Ed25519PrivateKey.generate(), issuer=issuer)

    @classmethod
    def from_private_bytes(cls, value: bytes, *, issuer: str = "aura-core") -> EnvelopeSigner:
        if len(value) != 32:
            raise ValueError("Ed25519 private key must contain 32 bytes")
        return cls(Ed25519PrivateKey.from_private_bytes(value), issuer=issuer)

    @property
    def public_key(self) -> str:
        return _b64(self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))

    def private_bytes(self) -> bytes:
        return self._key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())

    def sign(
        self,
        *,
        version: str,
        request_id: UUID | None = None,
        replay_id: UUID | None = None,
        audience: str,
        method: str,
        payload: Mapping[str, Any],
        limits: Mapping[str, int] | None = None,
        ttl_seconds: float = 30.0,
        now: datetime | None = None,
    ) -> SignedEnvelope:
        if not 0.1 <= ttl_seconds <= 60:
            raise ValueError("provider envelope TTL must be between 0 and 60 seconds")
        if version != "research-provider.v1":
            raise ValueError("provider envelope schema version is unsupported")
        issued = now or datetime.now(UTC)
        expires = issued + timedelta(seconds=ttl_seconds)
        normalized_method = method.casefold()
        unsigned = {
            "schemaVersion": "research-provider.v1",
            "envelopeType": "request",
            "requestId": str(request_id or uuid4()),
            "replayId": str(replay_id or uuid4()),
            "audience": audience,
            "method": normalized_method,
            "issuedAt": issued.isoformat().replace("+00:00", "Z"),
            "expiresAt": expires.isoformat().replace("+00:00", "Z"),
            "ttlMs": int(ttl_seconds * 1000),
            "payload": dict(payload),
        }
        # Canonicalise through the closed model first so datetime/UUID JSON
        # normalisation is identical on signing and verification paths.
        normalized = SignedEnvelope.model_validate(
            {
                **unsigned,
                "signature": {
                    "algorithm": "Ed25519",
                    "keyId": self.issuer,
                    "value": "0" * 86,
                },
            }
        )
        signature = _b64(self._key.sign(_canonical(normalized.unsigned())))
        return SignedEnvelope.model_validate(
            {
                **unsigned,
                "signature": {
                    "algorithm": "Ed25519",
                    "keyId": self.issuer,
                    "value": signature,
                },
            }
        )


class EnvelopeVerifier:
    """Provider-side verifier. It has no signing material or caller authority."""

    def __init__(
        self,
        public_key: str,
        *,
        audience: str,
        replay_cache: ReplayCache | None = None,
    ) -> None:
        try:
            self._key = Ed25519PublicKey.from_public_bytes(_unb64(public_key))
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid provider verification key") from exc
        self._audience = audience
        self._replay = replay_cache or ReplayCache()

    def verify(
        self,
        envelope: SignedEnvelope,
        *,
        method: str,
        version: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = now or datetime.now(UTC)
        try:
            self._key.verify(_unb64(envelope.signature.value), _canonical(envelope.unsigned()))
        except ValueError, TypeError:
            raise EnvelopeVerificationError("invalid_signature") from None
        if envelope.schema_version != version:
            raise EnvelopeVerificationError("contract_mismatch")
        if envelope.audience != self._audience or envelope.method != method.casefold():
            raise EnvelopeVerificationError("wrong_audience")
        if envelope.issued_at.tzinfo is None or envelope.expires_at.tzinfo is None:
            raise EnvelopeVerificationError("invalid_timestamp")
        if (
            envelope.issued_at > current + timedelta(seconds=5)
            or envelope.expires_at <= envelope.issued_at
            or envelope.expires_at - envelope.issued_at
            != timedelta(milliseconds=envelope.ttl_ms)
            or envelope.expires_at <= current
        ):
            raise EnvelopeVerificationError("expired")
        if envelope.expires_at - envelope.issued_at > timedelta(seconds=60):
            raise EnvelopeVerificationError("ttl_exceeded")
        if not self._replay.claim(
            envelope.replay_id,
            expires_at=envelope.expires_at,
            now=current.timestamp(),
        ):
            raise EnvelopeVerificationError("replayed")
        return envelope.payload.model_dump(mode="json", by_alias=True)
