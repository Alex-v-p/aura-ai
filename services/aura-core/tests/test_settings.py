"""AURA-0073 document embedding configuration checks."""

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
