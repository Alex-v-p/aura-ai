"""Deterministic, test-only OIDC and Ollama HTTP providers for Compose E2E."""

import asyncio
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode

from authlib.jose import JsonWebKey, jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_private_pem = _private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
_key = JsonWebKey.import_key(_private_pem, {"kid": "aura-e2e", "use": "sig", "alg": "RS256"})
_codes: dict[str, dict[str, str]] = {}


def kind() -> str:
    return os.environ.get("AURA_E2E_PROVIDER", "oidc")


def issuer() -> str:
    return os.environ.get("AURA_E2E_OIDC_ISSUER", "http://aura-e2e-oidc:8081").rstrip("/")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/.well-known/openid-configuration")
async def discovery() -> dict[str, str]:
    if kind() != "oidc":
        raise HTTPException(status_code=404)
    base = issuer()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "jwks_uri": f"{base}/jwks",
    }


@app.get("/jwks")
async def jwks() -> dict[str, list[dict[str, Any]]]:
    if kind() != "oidc":
        raise HTTPException(status_code=404)
    return {"keys": [_key.as_dict(is_private=False)]}


@app.get("/authorize")
async def authorize(
    response_type: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    nonce: str,
    scope: str,
) -> HTMLResponse:
    if kind() != "oidc" or response_type != "code" or "openid" not in scope.split():
        raise HTTPException(status_code=400)
    hidden = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "nonce": nonce,
        }
    )
    return HTMLResponse(
        "<form method='post' action='/approve?" + hidden + "'>"
        "<label>Username <input name='username' autocomplete='username'></label>"
        "<label>Password <input type='password' name='password' "
        "autocomplete='current-password'></label>"
        "<button type='submit'>Sign in</button></form>"
    )


@app.post("/approve")
async def approve(
    request: Request,
    client_id: str,
    redirect_uri: str,
    state: str,
    nonce: str,
) -> RedirectResponse:
    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    username = form.get("username", [""])[0]
    password = form.get("password", [""])[0]
    if kind() != "oidc" or username != "owner" or password != "fixture-password":
        raise HTTPException(status_code=401, detail="invalid deterministic fixture credentials")
    code = secrets.token_urlsafe(24)
    _codes[code] = {"client_id": client_id, "redirect_uri": redirect_uri, "nonce": nonce}
    return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", 302)


@app.post("/token")
async def token(request: Request) -> dict[str, str]:
    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    grant_type = form.get("grant_type", [""])[0]
    code = form.get("code", [""])[0]
    redirect_uri = form.get("redirect_uri", [""])[0]
    pending = _codes.pop(code, None)
    if kind() != "oidc" or grant_type != "authorization_code" or pending is None:
        raise HTTPException(status_code=400)
    if pending["redirect_uri"] != redirect_uri:
        raise HTTPException(status_code=400)
    now = datetime.now(UTC)
    identity_token = jwt.encode(
        {"alg": "RS256", "kid": "aura-e2e"},
        {
            "iss": issuer(),
            "sub": "owner",
            "aud": pending["client_id"],
            "nonce": pending["nonce"],
            "name": "Fixture Owner",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        },
        _private_pem,
    ).decode("utf-8")
    return {"access_token": "fixture-access", "token_type": "Bearer", "id_token": identity_token}


@app.get("/api/tags")
async def tags() -> dict[str, list[dict[str, str]]]:
    if kind() != "ollama":
        raise HTTPException(status_code=404)
    return {
        "models": [
            {"name": "fixture-chat"},
            {"name": "fixture-chat-2"},
            {"name": "embed"},
        ]
    }


@app.post("/api/show")
async def show(body: dict[str, Any]) -> dict[str, list[str]]:
    if kind() != "ollama":
        raise HTTPException(status_code=404)
    return {"capabilities": ["embedding"] if body.get("name") == "embed" else ["chat"]}


@app.post("/api/chat")
async def chat(body: dict[str, Any]) -> StreamingResponse:
    if kind() != "ollama":
        raise HTTPException(status_code=404)
    messages = body.get("messages", [])
    content = next(
        (item.get("content", "") for item in reversed(messages) if item.get("role") == "user"), ""
    )

    async def stream() -> Any:
        yield json.dumps({"message": {"content": "Fixture reply: "}}) + "\n"
        if isinstance(content, str) and content.startswith("background-run-"):
            await asyncio.sleep(4)
        yield json.dumps({"message": {"content": content}, "done": True}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")
