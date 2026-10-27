"""HTTP boundary evaluations for the owner-private scheduled briefing API."""

from __future__ import annotations

from pathlib import Path

from aura_core.entrypoints.api.app import create_app

ROOT = Path(__file__).parents[2]
EXPECTED_ROUTES = {
    ("GET", "/api/v1/briefing-schedules"),
    ("POST", "/api/v1/briefing-schedules"),
    ("GET", "/api/v1/briefing-schedules/{schedule_id}"),
    ("POST", "/api/v1/briefing-schedules/{schedule_id}/pause"),
    ("POST", "/api/v1/briefing-schedules/{schedule_id}/resume"),
    ("POST", "/api/v1/briefing-schedules/{schedule_id}/deletions"),
    ("GET", "/api/v1/briefing-schedules/{schedule_id}/occurrences"),
    ("GET", "/api/v1/briefing-schedules/{schedule_id}/occurrences/{occurrence_id}"),
    ("POST", "/api/v1/briefing-schedules/{schedule_id}/history-deletions"),
    ("GET", "/api/v1/briefing-results/{result_id}"),
    ("POST", "/api/v1/briefing-results/{result_id}/deletions"),
}


def test_scheduled_briefing_routes_are_exactly_the_authorized_management_surface() -> None:
    app = create_app(testing=True)
    registered = {
        (method.upper(), path)
        for path, metadata in app.openapi()["paths"].items()
        if path.startswith("/api/v1/briefing")
        for method in metadata
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }

    assert EXPECTED_ROUTES <= registered
    assert not {
        (method, path)
        for method, path in registered
        if path.endswith("/occurrences/{occurrence_id}/cancel")
        or path.endswith("/edit")
    }


def test_briefing_routes_are_thin_and_result_responses_are_no_store() -> None:
    source = (
        ROOT / "src/aura_core/entrypoints/api/routes/briefings.py"
    ).read_text(encoding="utf-8")
    lowered = source.lower()
    assert "cache-control" in lowered
    assert "no-store" in lowered
    assert "sqlalchemy" not in lowered
    assert "provider" not in lowered
    assert "memory" not in lowered
    assert "notification" not in lowered


def test_briefing_route_inputs_are_not_private_query_or_url_values() -> None:
    source = (
        ROOT / "src/aura_core/entrypoints/api/routes/briefings.py"
    ).read_text(encoding="utf-8")
    assert "request.query_params" not in source
    assert "content: str" not in source
    assert "provider" not in source.lower()
