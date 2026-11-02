"""Parser sandbox resource and privilege-boundary checks."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_parser_service_has_bounded_body_and_alarm_timeout() -> None:
    source = (ROOT / "deploy/docker/document_parser_service.py").read_text()
    assert "MAX_BODY_BYTES = 100_000_000" in source
    assert "PARSE_TIMEOUT_SECONDS = 30" in source
    assert "signal.alarm(PARSE_TIMEOUT_SECONDS)" in source
    assert "signal.alarm(0)" in source
    assert "document parsing failed" in source


def test_parser_service_has_no_application_credentials_or_general_egress_imports() -> None:
    source = (ROOT / "deploy/docker/document_parser_service.py").read_text().casefold()
    assert "database" not in source
    assert "nats" not in source
    assert "redis" not in source
    assert "ollama" not in source
    assert "object_storage" not in source
    assert "requests" not in source
    assert "httpx" not in source

