"""Disposable full-stack notification seam checks.

The live browser/API flow is exercised by the repository Playwright suite.  These
checks keep the disposable seam explicit and do not contact any external service.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[4]


def test_full_stack_harness_mentions_notification_reconciliation() -> None:
    harness = (ROOT / "deploy/compose/run-full-stack-e2e.sh").read_text(encoding="utf-8")
    assert "notification" in harness.lower()
    assert "last-event-id" in harness.lower() or "reconnect" in harness.lower()


def test_disposable_topology_exposes_only_core_scheduler_database_access() -> None:
    compose = (ROOT / "deploy/compose/e2e/local-aura.yaml").read_text(encoding="utf-8")
    base_compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    lowered = compose.lower()
    assert "aura-core-scheduler" in lowered
    base_lowered = "\n".join(
        line for line in base_compose.lower().splitlines() if not line.lstrip().startswith("#")
    )
    scheduler_start = base_lowered.index("aura-core-scheduler")
    scheduler_end = base_lowered.index("\n  aura-core-migrate:", scheduler_start)
    scheduler_section = base_lowered[scheduler_start:scheduler_end]
    assert "database" in scheduler_section
    for forbidden in ("ollama", "nats", "valkey", "oidc"):
        assert forbidden not in scheduler_section
