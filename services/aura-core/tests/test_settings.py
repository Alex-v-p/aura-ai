"""AURA-0073 and AURA-0079 configuration checks."""

from pathlib import Path
from typing import Any, cast

import pytest
from aura_core.platform.auth import Settings


def test_document_embedding_defaults_are_qwen_and_vector_compatible() -> None:
    settings = Settings()
    assert settings.document_embedding_model == "qwen3-embedding:4b"
    assert settings.document_embedding_dimension == 768


def test_document_embedding_dimension_is_bounded() -> None:
    for value in (0, -1, 768 + 1, 16_385):
        with pytest.raises(ValueError):
            Settings(**cast(Any, {"document_embedding_dimension": value}))


def test_document_embedding_dimension_accepts_string_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AURA_DOCUMENT_EMBEDDING_DIMENSION", "768")

    settings = Settings()

    assert settings.document_embedding_dimension == 768
    assert isinstance(settings.document_embedding_dimension, int)


def test_document_embedding_dimension_rejects_incompatible_string_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AURA_DOCUMENT_EMBEDDING_DIMENSION", "1024")

    with pytest.raises(ValueError, match="exactly 768"):
        Settings()


def test_document_embedding_dimension_cannot_silently_diverge_from_vector_index() -> None:
    """The document index is vector(768), so other valid Ollama widths fail closed."""

    for value in (384, 767, 769, 1024, 1536, 16_384):
        with pytest.raises(ValueError):
            Settings(**cast(Any, {"document_embedding_dimension": value}))


def test_document_embedding_configuration_is_independent_from_chat_defaults() -> None:
    settings = Settings(default_model="qwen3:8b")
    assert settings.default_model == "qwen3:8b"
    assert settings.document_embedding_model == "qwen3-embedding:4b"


def test_aura0079_core_telemetry_export_is_disabled_by_default() -> None:
    settings = Settings()
    assert settings.telemetry_otlp_enabled is False
    assert settings.telemetry_otlp_credential == ""


def test_aura0079_core_telemetry_export_accepts_bounded_runtime_settings() -> None:
    settings = Settings(
        telemetry_otlp_enabled=True,
        telemetry_otlp_endpoint="http://observatory-collector:4318/v1/observations",
        telemetry_otlp_queue_capacity=64,
        telemetry_otlp_max_attempts=4,
        telemetry_otlp_timeout_seconds=3,
        telemetry_deployment_id="deploy-a",
        telemetry_git_commit="0123456789abcdef",
        telemetry_host_id="host-a",
        role="worker",
    )
    assert settings.telemetry_otlp_enabled is True
    assert settings.telemetry_otlp_queue_capacity == 64
    assert settings.telemetry_otlp_max_attempts == 4
    assert settings.telemetry_otlp_timeout_seconds == 3
    assert settings.role == "worker"


def test_aura0088_core_telemetry_export_accepts_optional_v2_deployment_markers() -> None:
    settings = Settings(
        telemetry_image_version="aura-core-2026.10.18",
        telemetry_configuration_version="config-42",
    )

    assert settings.telemetry_image_version == "aura-core-2026.10.18"
    assert settings.telemetry_configuration_version == "config-42"


@pytest.mark.parametrize(
    "field",
    ("telemetry_image_version", "telemetry_configuration_version"),
)
def test_aura0088_core_telemetry_rejects_untrusted_v2_deployment_markers(field: str) -> None:
    for value in (
        "https://private.example",
        "/var/run/aura",
        "owner-secret",
        "identity-config",
        "api-key-config",
        "content-hash-abc",
        "a value",
    ):
        with pytest.raises(ValueError):
            Settings(**cast(Any, {field: value}))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("telemetry_otlp_queue_capacity", 0),
        ("telemetry_otlp_queue_capacity", 1025),
        ("telemetry_otlp_max_attempts", 0),
        ("telemetry_otlp_max_attempts", 6),
        ("telemetry_otlp_timeout_seconds", 0),
        ("telemetry_otlp_timeout_seconds", 31),
    ),
)
def test_aura0079_core_telemetry_export_settings_reject_unbounded_values(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        Settings(**cast(Any, {field: value}))


def test_aura0079_core_telemetry_credential_comes_from_file_without_repr_or_inline_requirement(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "collector-credential"
    secret.write_text("collector-secret\n", encoding="utf-8")

    settings = Settings(telemetry_otlp_credential_file=str(secret))

    assert settings.telemetry_otlp_credential == "collector-secret"
    assert "collector-secret" not in repr(settings)


@pytest.mark.parametrize(
    "overrides",
    (
        {"telemetry_deployment_id": "https://private.example"},
        {"telemetry_deployment_id": "/var/run/aura"},
        {"telemetry_host_id": "owner@example.com"},
        {"telemetry_host_id": "x" * 129},
        {"telemetry_git_commit": "not-a-git-commit"},
        {"environment": "Owner"},
        {"telemetry_otlp_endpoint": "http://user:password@collector:4318"},
    ),
)
def test_aura0079_telemetry_metadata_rejects_private_or_malformed_values(
    overrides: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        Settings(**cast(Any, overrides))
