"""Thin FastAPI composition root."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from aura_core.entrypoints.api.errors import install_error_handlers
from aura_core.entrypoints.api.routes import auth, conversations, health, models, runs
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Settings


def create_app(settings: Settings | None = None, *, testing: bool = False) -> FastAPI:
    """Build the API with production adapters by default.

    Tests must opt into the in-memory composition explicitly.  This prevents a
    production process from silently accepting writes that disappear on a
    restart when database or broker configuration is missing.
    """

    state = AppState(settings, testing=testing)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        del app
        await state.startup()
        try:
            yield
        finally:
            await state.shutdown()

    app = FastAPI(title="Aura Core API", version="1.0.0", lifespan=lifespan)
    install_error_handlers(app)
    app.state.aura = state
    app.include_router(auth.router)
    app.include_router(models.router)
    app.include_router(conversations.router)
    app.include_router(runs.router)
    app.include_router(health.router)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("aura_core.entrypoints.api.app:app", host="0.0.0.0", port=8000)
