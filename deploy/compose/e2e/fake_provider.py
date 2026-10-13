"""Deterministic, test-only OIDC and Ollama HTTP providers for Compose E2E."""

import asyncio
import hashlib
import json
import os
import re
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
# The marker is intentionally explicit and test-only.  Keeping the seen set in
# the provider process makes retries of the same marked prompt deterministic:
# native tool selection can create one proposal, while resumed tools-disabled
# synthesis can never create a second one.
_REMINDER_SERIES_MARKER = "AURA_E2E_REMINDER_SERIES_CREATE"
_series_markers_seen: set[str] = set()
_DOCUMENT_SEARCH_MARKER = "AURA_E2E_DOCUMENT_SEARCH_CITATION"
_document_markers_seen: set[str] = set()
_EMBEDDING_DIMENSION = 768


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
            {
                "name": "fixture-chat",
                "digest": "5aab3664f58aecf3728756feafd39dfc723fdfd43dff233a622eea824574d18b",
                "modified_at": "2026-10-09T00:00:00Z",
            },
            {
                "name": "fixture-chat-2",
                "digest": "2da0d79fc1a6e1cb9edef7bc5a3c41ee4e31df2eb8f53f35487639a0f9ed8951",
                "modified_at": "2026-10-09T00:00:00Z",
            },
            {
                "name": "embed",
                "digest": "8a1eed22144c3f04485a69eea62bfd3fa1026de39dc252b77ba9b1f3f08b00ec",
                "modified_at": "2026-10-09T00:00:00Z",
            },
            {
                "name": "nomic-embed-text",
                "digest": "9b6e3f6f9c7d4a8b1e2d3c4f5a6b7c8d9e0f112233445566778899aabbccdde0",
                "modified_at": "2026-10-09T00:00:00Z",
            },
        ]
    }


@app.post("/api/show")
async def show(body: dict[str, Any]) -> dict[str, list[str]]:
    if kind() != "ollama":
        raise HTTPException(status_code=404)
    name = body.get("name")
    if name in {"embed", "nomic-embed-text"}:
        return {"capabilities": ["embedding"]}
    if name == "fixture-chat":
        return {"capabilities": ["chat", "tools", "tool_calling"]}
    return {"capabilities": ["chat"]}


@app.post("/api/embed")
async def embed(body: dict[str, Any]) -> dict[str, list[list[float]] | str]:
    if kind() != "ollama":
        raise HTTPException(status_code=404)
    model = body.get("model")
    value = body.get("input")
    if model != "nomic-embed-text" or not isinstance(value, str) or not value:
        raise HTTPException(status_code=400)
    vector: list[float] = []
    block = 0
    while len(vector) < _EMBEDDING_DIMENSION:
        digest = hashlib.sha256(f"{model}:{block}:{value}".encode()).digest()
        vector.extend(((byte / 255.0) * 2.0) - 1.0 for byte in digest)
        block += 1
    vector = vector[:_EMBEDDING_DIMENSION]
    return {"embeddings": [vector], "model_revision": "fixture-embedding-v1"}


@app.post("/api/chat")
async def chat(body: dict[str, Any]) -> StreamingResponse:
    if kind() != "ollama":
        raise HTTPException(status_code=404)
    messages = body.get("messages", [])
    content = next(
        (item.get("content", "") for item in reversed(messages) if item.get("role") == "user"), ""
    )
    tools_enabled = isinstance(body.get("tools"), list) and bool(body.get("tools"))
    all_content = "\n".join(
        str(item.get("content", "")) for item in messages if isinstance(item, dict)
    )

    # Ollama's native tool protocol must not mix text and a tool call in the
    # same selection stream.  The marker is used only by the real browser
    # approval flow; ordinary fixture prompts retain the old text behavior.
    marked = isinstance(content, str) and _REMINDER_SERIES_MARKER in content
    if marked and tools_enabled and content not in _series_markers_seen:
        _series_markers_seen.add(content)
        # The value is deliberately naive: Core interprets conversational
        # wall-clock input through the run's pinned Europe/Brussels timezone.
        local_start = (datetime.now(UTC) + timedelta(days=30)).replace(tzinfo=None)
        recurrence = {"frequency": "daily", "interval": 1}

        async def tool_stream() -> Any:
            yield json.dumps(
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "reminder.series.create",
                                    "arguments": {
                                        "content": "Fixture recurring reminder",
                                        "localStart": local_start.isoformat(timespec="seconds"),
                                        "recurrence": recurrence,
                                    },
                                }
                            }
                        ]
                    }
                }
            ) + "\n"
            yield json.dumps({"message": {}, "done": True}) + "\n"

        return StreamingResponse(tool_stream(), media_type="application/x-ndjson")

    document_marked = _DOCUMENT_SEARCH_MARKER in all_content
    if document_marked and tools_enabled and content not in _document_markers_seen:
        _document_markers_seen.add(content)

        async def document_tool_stream() -> Any:
            yield json.dumps(
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "document.search",
                                    "arguments": {"query": _DOCUMENT_SEARCH_MARKER},
                                }
                            }
                        ]
                    }
                }
            ) + "\n"
            yield json.dumps({"message": {}, "done": True}) + "\n"

        return StreamingResponse(document_tool_stream(), media_type="application/x-ndjson")

    if document_marked and not tools_enabled:
        evidence_match = re.search(
            r"evidenceId=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            all_content,
            flags=re.IGNORECASE,
        )
        marker = evidence_match.group(1) if evidence_match else "invalid-evidence"

        async def document_synthesis_stream() -> Any:
            yield json.dumps(
                {"message": {"content": f"Fixture grounded answer [[document:{marker}]]."}}
            ) + "\n"
            yield json.dumps({"message": {}, "done": True}) + "\n"

        return StreamingResponse(document_synthesis_stream(), media_type="application/x-ndjson")

    async def stream() -> Any:
        # The resumed approval path deliberately omits tools.  It must receive
        # ordinary text synthesis even for the marked prompt.
        yield json.dumps({"message": {"content": "Fixture reply: "}}) + "\n"
        if isinstance(content, str) and content.startswith("background-run-"):
            await asyncio.sleep(4)
        yield json.dumps({"message": {"content": content}, "done": True}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")
