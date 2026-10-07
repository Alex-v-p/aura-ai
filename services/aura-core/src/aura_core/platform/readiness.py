"""Technical readiness probe adapters."""

from collections.abc import Awaitable, Callable

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aura_core.bootstrap.health import DependencyCheck


class StaticProbe:
    def __init__(self, ready: bool, detail: str | None = None) -> None:
        self.ready = ready
        self.detail = detail

    async def check(self) -> DependencyCheck:
        return DependencyCheck("ready" if self.ready else "degraded", self.detail)


class UnknownProbe:
    async def check(self) -> DependencyCheck:
        return DependencyCheck("unknown", "test composition")


class DatabaseProbe:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def check(self) -> DependencyCheck:
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return DependencyCheck("ready")
        except Exception:
            return DependencyCheck("unavailable", "database unavailable")


class CallableProbe:
    def __init__(
        self,
        readiness: Callable[[], Awaitable[bool]],
        unavailable_detail: str,
    ) -> None:
        self.readiness = readiness
        self.unavailable_detail = unavailable_detail

    async def check(self) -> DependencyCheck:
        try:
            ready = await self.readiness()
        except Exception:
            ready = False
        return DependencyCheck(
            "ready" if ready else "unavailable",
            None if ready else self.unavailable_detail,
        )


class DiagnosticProbe:
    """Adapter for dependencies that return a sanitized readiness diagnosis."""

    def __init__(
        self,
        readiness: Callable[[], Awaitable[tuple[bool, str | None]]],
        unavailable_detail: str,
    ) -> None:
        self.readiness = readiness
        self.unavailable_detail = unavailable_detail

    async def check(self) -> DependencyCheck:
        try:
            ready, detail = await self.readiness()
        except Exception:
            ready, detail = False, None
        return DependencyCheck(
            "ready" if ready else "unavailable",
            None if ready else detail or self.unavailable_detail,
        )


class OidcDiscoveryProbe:
    def __init__(self, issuer: str) -> None:
        self.issuer = issuer

    async def check(self) -> DependencyCheck:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(
                    f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"
                )
                response.raise_for_status()
            return DependencyCheck("ready")
        except Exception:
            return DependencyCheck("unavailable", "OIDC discovery unavailable")
