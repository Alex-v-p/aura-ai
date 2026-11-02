from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from aura_core.domains.governance.identity.application import (
    LoginConfiguration,
    LoginService,
)
from aura_core.domains.governance.identity.models import ValidatedIdentity
from aura_core.domains.interaction.conversations.public import ConversationStore
from aura_core.platform.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    SessionService,
    Settings,
)
from aura_core.platform.oidc import OIDCValidationError, validate_claims


def test_oidc_claims_require_issuer_audience_nonce_and_owner() -> None:
    identity = validate_claims(
        {
            "iss": "https://auth.example",
            "sub": "owner",
            "aud": ["aura"],
            "nonce": "n",
            "name": "Owner",
            "exp": (datetime.now(UTC) + timedelta(minutes=5)).timestamp(),
            "iat": datetime.now(UTC).timestamp(),
        },
        expected_issuer="https://auth.example",
        expected_audience="aura",
        expected_nonce="n",
        owner_subject="owner",
    )
    assert identity.subject == "owner"


def test_oidc_claim_mismatch_is_rejected() -> None:
    with pytest.raises(OIDCValidationError):
        validate_claims(
            {
                "iss": "https://evil.example",
                "sub": "owner",
                "aud": "aura",
                "nonce": "n",
                "exp": (datetime.now(UTC) + timedelta(minutes=5)).timestamp(),
                "iat": datetime.now(UTC).timestamp(),
            },
            expected_issuer="https://auth.example",
            expected_audience="aura",
            expected_nonce="n",
            owner_subject="owner",
        )


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "aura"},
        {"aud": "aura", "exp": 4_102_444_800},
        {"aud": ["aura", "other"], "exp": 4_102_444_800, "iat": 1_700_000_000},
        {
            "aud": ["aura", "other"],
            "azp": "other",
            "exp": 4_102_444_800,
            "iat": 1_700_000_000,
        },
    ],
)
def test_oidc_requires_temporal_claims_and_authorized_party(
    claims: dict[str, object],
) -> None:
    with pytest.raises(OIDCValidationError):
        validate_claims(
            {
                "iss": "https://auth.example",
                "sub": "owner",
                "nonce": "n",
                **claims,
            },
            expected_issuer="https://auth.example",
            expected_audience="aura",
            expected_nonce="n",
            owner_subject="owner",
        )


def test_oidc_accepts_matching_authorized_party_for_multiple_audiences() -> None:
    identity = validate_claims(
        {
            "iss": "https://auth.example",
            "sub": "owner",
            "aud": ["aura", "other"],
            "azp": "aura",
            "nonce": "n",
            "exp": 4_102_444_800,
            "iat": 1_700_000_000,
        },
        expected_issuer="https://auth.example",
        expected_audience="aura",
        expected_nonce="n",
        owner_subject="owner",
    )
    assert identity.subject == "owner"


@pytest.mark.asyncio
async def test_login_logout_and_denials_write_metadata_only_audit() -> None:
    class Provider:
        async def authorization_url(self, *, state: str, nonce: str) -> str:
            return f"https://identity.example/authorize?state={state}&nonce={nonce}"

        async def exchange(self, *, code: str, expected_nonce: str) -> ValidatedIdentity:
            assert code == "secret-code"
            assert expected_nonce
            return ValidatedIdentity("https://identity.example", "owner", "Owner")

    audit = ConversationStore()
    sessions = SessionService(MemorySessionBackend(), Settings())
    service = LoginService(
        LoginConfiguration(
            "https://identity.example",
            "aura-web",
            "https://aura.example/api/v1/auth/callback",
            True,
        ),
        MemoryLoginStateBackend(),
        sessions,
        Provider(),
        audit,
    )

    with pytest.raises(ValueError):
        await service.start("//untrusted.example")
    with pytest.raises(ValueError):
        await service.complete("secret-code", "missing", None)
    started = await service.start("/chat")
    state = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
    completed = await service.complete("secret-code", state, None)
    await service.logout(completed.session_id)

    assert [(entry["action"], entry["outcome"]) for entry in audit.auth_audit] == [
        ("auth.login", "denied"),
        ("auth.login", "denied"),
        ("auth.login", "started"),
        ("auth.login", "succeeded"),
        ("auth.logout", "succeeded"),
    ]
    serialized = repr(audit.auth_audit)
    assert "secret-code" not in serialized
    assert state not in serialized
