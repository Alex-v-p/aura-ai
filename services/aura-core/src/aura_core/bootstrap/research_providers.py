"""Composition root for governed research provider ports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aura_core.platform.research_provider_settings import ResearchProviderSettings
from aura_core.providers.research.client import (
    BrowserFetchProvider,
    ProviderMetrics,
    ResearchProviderHealth,
    SearchProvider,
    StaticFetchProvider,
)
from aura_core.providers.research.signing import EnvelopeSigner


@dataclass(frozen=True, slots=True)
class ResearchProviderClients:
    """Provider ports plus the admission capability snapshot."""

    settings: ResearchProviderSettings
    signer: EnvelopeSigner
    search: SearchProvider
    static_fetch: StaticFetchProvider
    browser_fetch: BrowserFetchProvider

    @property
    def admission_enabled(self) -> bool:
        return self.settings.admission_enabled

    async def health(self) -> tuple[ResearchProviderHealth, ...]:
        return (
            await self.search.health(),
            await self.static_fetch.health(),
            await self.browser_fetch.health(),
        )

    async def aclose(self) -> None:
        await self.search.aclose()
        await self.static_fetch.aclose()
        await self.browser_fetch.aclose()


def _coerce_settings(settings: Any | None) -> ResearchProviderSettings:
    if settings is None:
        return ResearchProviderSettings()
    if isinstance(settings, ResearchProviderSettings):
        return settings
    # Keep the general Core Settings object out of the provider package. The
    # bootstrap seam reads only its public provider-prefixed attributes.
    values: dict[str, Any] = {}
    for name in ResearchProviderSettings.model_fields:
        if hasattr(settings, name):
            values[name] = getattr(settings, name)
    return ResearchProviderSettings(**values)


def build_research_provider_clients(
    settings: ResearchProviderSettings | Any | None = None,
    *,
    telemetry: ProviderMetrics | None = None,
) -> ResearchProviderClients:
    config = _coerce_settings(settings)
    key = config.key_bytes
    signer = EnvelopeSigner.from_private_bytes(key) if key else EnvelopeSigner.generate()
    return ResearchProviderClients(
        settings=config,
        signer=signer,
        search=SearchProvider(
            config.search_endpoint,
            signer,
            enabled=config.search_enabled,
            timeout_seconds=config.search_deadline_seconds,
            telemetry=telemetry,
        ),
        static_fetch=StaticFetchProvider(
            config.static_fetch_endpoint,
            signer,
            enabled=config.static_fetch_enabled,
            timeout_seconds=config.static_fetch_deadline_seconds,
            telemetry=telemetry,
        ),
        browser_fetch=BrowserFetchProvider(
            config.browser_fetch_endpoint,
            signer,
            enabled=config.browser_fetch_enabled,
            timeout_seconds=config.browser_fetch_deadline_seconds,
            telemetry=telemetry,
        ),
    )


def load_research_provider_settings() -> ResearchProviderSettings:
    """Load the non-network provider capability snapshot for API admission."""
    try:
        return ResearchProviderSettings.from_environment()
    except (OSError, ValueError):
        return ResearchProviderSettings()


# Composition callers use either name depending on whether they build a UoW
# or an application worker; both are the same public seam.
compose_research_providers = build_research_provider_clients
research_provider_clients = build_research_provider_clients
