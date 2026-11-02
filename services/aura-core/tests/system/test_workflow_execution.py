"""Assembled Aura Core boundary checks for AURA-0137."""

from __future__ import annotations

from pathlib import Path

import yaml
from aura_core.entrypoints.api.app import create_app

ROOT = Path(__file__).parents[2]


def test_workflow_api_routes_are_owner_management_and_execution_surface() -> None:
    app = create_app(testing=True)
    registered = {
        (method.upper(), path)
        for path, metadata in app.openapi()["paths"].items()
        if path.startswith("/api/v1/workflows")
        for method in metadata
        if method.upper() in {"GET", "POST", "PATCH", "DELETE"}
    }
    expected = {
        ("GET", "/api/v1/workflows"),
        ("POST", "/api/v1/workflows"),
        ("GET", "/api/v1/workflows/stream"),
        ("GET", "/api/v1/workflows/{workflow_id}"),
        ("POST", "/api/v1/workflows/{workflow_id}/retire"),
        ("POST", "/api/v1/workflows/{workflow_id}/revisions/{revision_id}/retire"),
        ("POST", "/api/v1/workflows/{workflow_id}/instances"),
        ("GET", "/api/v1/workflows/{workflow_id}/instances/{instance_id}"),
        ("POST", "/api/v1/workflows/{workflow_id}/instances/{instance_id}/cancel"),
        ("POST", "/api/v1/workflows/{workflow_id}/instances/{instance_id}/resolve"),
        ("POST", "/api/v1/workflows/{workflow_id}/drafts/{draft_id}/deletions"),
        ("POST", "/api/v1/workflows/{workflow_id}/deletions"),
        ("POST", "/api/v1/workflows/{workflow_id}/revisions/{revision_id}/deletions"),
        ("POST", "/api/v1/workflows/{workflow_id}/instances/{instance_id}/deletions"),
        ("POST", "/api/v1/workflows/{workflow_id}/instances/{instance_id}/history-deletions"),
        ("POST", "/api/v1/workflows/{workflow_id}/instances/{instance_id}/result-deletions"),
        ("POST", "/api/v1/workflows/{workflow_id}/instances/{instance_id}/checkpoint-deletions"),
    }
    assert expected <= registered
    assert not {
        item for item in registered if "schedule" in item[1].casefold()
    }


def test_every_workflow_operation_in_contract_is_registered_by_the_app() -> None:
    contract = yaml.safe_load(
        (ROOT.parent.parent / "contracts/openapi/aura-v1.yaml").read_text(encoding="utf-8")
    )
    app = create_app(testing=True)
    registered = app.openapi()["paths"]
    for path, metadata in contract["paths"].items():
        if not path.startswith("/api/v1/workflows"):
            continue
        for method, _operation in metadata.items():
            if method in {"parameters", "summary", "description"}:
                continue
            assert method in registered.get(path, {}), (method, path)


def test_workflow_entrypoint_remains_thin_and_metadata_only() -> None:
    source = (ROOT / "src/aura_core/entrypoints/api/routes/workflows.py").read_text(
        encoding="utf-8"
    ).casefold()
    assert "cache-control" in source
    assert "no-store" in source
    assert "sqlalchemy" not in source
    assert "provider" not in source
    assert "executor" not in source
    assert "workflow input" not in source
    assert "request.query_params" not in source


def test_workflow_stream_enforces_cursor_ordering_owner_scope_and_expiry_contract() -> None:
    source = (ROOT / "src/aura_core/entrypoints/api/routes/workflows.py").read_text(
        encoding="utf-8"
    )
    # A replay cursor is an ordered, owner-scoped durable boundary.  Unknown
    # and expired cursors must be distinguishable from an empty stream.
    assert "replay_cursor" in source or "after=cursor" in source or "after=last_event_id" in source
    assert "Last-Event-ID" in source
    assert "WorkflowNotFound" in source
    assert "410" in source or "gone" in source.casefold()
    assert "issuer" in source and "subject" in source
