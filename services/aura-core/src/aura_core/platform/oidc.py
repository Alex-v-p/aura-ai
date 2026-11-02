"""Provider-independent validation rules for OIDC callback claims.

The network token exchange is composed at the entrypoint; these deterministic
checks are kept reusable and make issuer/audience/nonce/owner enforcement explicit.
"""

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlencode

import httpx

from aura_core.domains.governance.identity.application import IdentityValidationError
from aura_core.domains.governance.identity.models import ProviderTokens, ValidatedIdentity


class OIDCValidationError(IdentityValidationError):
    pass


def validate_claims(
    claims: Mapping[str, object],
    *,
    expected_issuer: str,
    expected_audience: str,
    expected_nonce: str,
    owner_subject: str,
) -> ValidatedIdentity:
    issuer = claims.get("iss")
    subject = claims.get("sub")
    audience = claims.get("aud")
    nonce = claims.get("nonce")
    expires_at = claims.get("exp")
    issued_at = claims.get("iat")
    if issuer != expected_issuer:
        raise OIDCValidationError("issuer mismatch")
    if not isinstance(subject, str) or not subject:
        raise OIDCValidationError("subject missing")
    audiences = cast(list[object], audience) if isinstance(audience, list) else [audience]
    if expected_audience not in audiences:
        raise OIDCValidationError("audience mismatch")
    if len(audiences) > 1 and claims.get("azp") != expected_audience:
        raise OIDCValidationError("authorized party mismatch")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        raise OIDCValidationError("expiration missing")
    if not isinstance(issued_at, (int, float)) or isinstance(issued_at, bool):
        raise OIDCValidationError("issued-at missing")
    if expires_at <= datetime.now(UTC).timestamp():
        raise OIDCValidationError("identity token expired")
    if nonce != expected_nonce:
        raise OIDCValidationError("nonce mismatch")
    if owner_subject and subject != owner_subject:
        raise OIDCValidationError("owner authorization required")
    display = claims.get("name")
    return ValidatedIdentity(
        expected_issuer, subject, display if isinstance(display, str) else None
    )


async def exchange_code(
    *,
    issuer: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    expected_nonce: str,
    audience: str,
    owner_subject: str,
) -> ValidatedIdentity:
    """Exchange and cryptographically validate an OIDC authorization code.

    Authlib performs JWT signature/JWKS validation; discovery and token exchange
    remain here so the API does not depend on an Authentik service name or API.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            discovery_response = await client.get(
                f"{issuer.rstrip('/')}/.well-known/openid-configuration"
            )
            discovery_response.raise_for_status()
            discovery = discovery_response.json()
            token_response = await client.post(
                discovery["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                auth=(client_id, client_secret),
            )
            token_response.raise_for_status()
            tokens = token_response.json()
            jwks_response = await client.get(discovery["jwks_uri"])
            jwks_response.raise_for_status()
            jwks = jwks_response.json()
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise OIDCValidationError("OIDC exchange failed") from exc
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str):
        raise OIDCValidationError("OIDC identity token missing")
    try:
        from authlib.jose import JsonWebToken

        decoder: Any = JsonWebToken(["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"])
        claims: Any = decoder.decode(id_token, jwks)
        claims.validate()
        claim_map = dict(claims)
    except Exception as exc:
        raise OIDCValidationError("OIDC identity token invalid") from exc
    identity = validate_claims(
        claim_map,
        expected_issuer=issuer,
        expected_audience=audience,
        expected_nonce=expected_nonce,
        owner_subject=owner_subject,
    )
    provider_tokens = ProviderTokens(
        tokens.get("access_token") if isinstance(tokens.get("access_token"), str) else None,
        tokens.get("refresh_token") if isinstance(tokens.get("refresh_token"), str) else None,
        tokens.get("token_type") if isinstance(tokens.get("token_type"), str) else None,
    )
    if provider_tokens.access_token is None and provider_tokens.refresh_token is None:
        return identity
    return replace(identity, provider_tokens=provider_tokens)


class OidcClient:
    """Configured OIDC provider adapter for the identity application service."""

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        audience: str,
        owner_subject: str,
    ) -> None:
        self.issuer = issuer
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.audience = audience
        self.owner_subject = owner_subject

    async def authorization_url(self, *, state: str, nonce: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"
                )
                response.raise_for_status()
                endpoint = response.json()["authorization_endpoint"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise OIDCValidationError("OIDC discovery failed") from exc
        if not isinstance(endpoint, str) or not endpoint:
            raise OIDCValidationError("OIDC authorization endpoint missing")
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": "openid profile",
                "state": state,
                "nonce": nonce,
            }
        )
        separator = "&" if "?" in endpoint else "?"
        return f"{endpoint}{separator}{query}"

    async def exchange(self, *, code: str, expected_nonce: str) -> ValidatedIdentity:
        return await exchange_code(
            issuer=self.issuer,
            client_id=self.client_id,
            client_secret=self.client_secret,
            code=code,
            redirect_uri=self.redirect_uri,
            expected_nonce=expected_nonce,
            audience=self.audience,
            owner_subject=self.owner_subject,
        )
