"""Provider-neutral model selection."""

from collections.abc import Sequence

from aura_core.runtime.models.ports import ModelDescriptor


def select_model(
    models: Sequence[ModelDescriptor], requested: str, default: str | None
) -> ModelDescriptor:
    for model in models:
        if model.id == requested:
            if not model.selectable:
                raise ValueError(model.disabled_reason or "model is not selectable")
            return model
    if default is not None and requested == default:
        raise ValueError("configured default model is unavailable")
    raise ValueError("model is unavailable")
