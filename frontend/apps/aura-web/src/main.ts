import { inject, isDevMode } from '@angular/core';
import { bootstrapApplication } from '@angular/platform-browser';
import { provideAnimationsAsync } from '@angular/platform-browser/animations/async';
import { provideRouter } from '@angular/router';
import { AURA_MANAGEMENT_SESSION, type ManagementSessionPort } from '@aura/aura/interaction/conversations';
import { AGENT_API, AgentStore, AuraAgentApi, ToolPolicyStore } from '@aura/aura/interaction/agents';
import { PERSONA_API, AuraPersonaApi, PersonaStore } from '@aura/aura/interaction/personas';
import { MemoryStore } from '@aura/aura/knowledge/memory';
import { AppComponent } from './app/app.component';
import { routes } from './app/app.routes';

bootstrapApplication(AppComponent, {
  providers: [
    provideRouter(routes),
    provideAnimationsAsync(),
    { provide: AGENT_API, useClass: AuraAgentApi },
    { provide: PERSONA_API, useClass: AuraPersonaApi },
    {
      provide: AURA_MANAGEMENT_SESSION,
      useFactory: (): ManagementSessionPort => {
        const stores = [inject(AgentStore), inject(PersonaStore), inject(ToolPolicyStore), inject(MemoryStore)];
        let resetting = false;
        return {
          resetSessionState: (): void => {
            if (resetting) return;
            resetting = true;
            try { for (const store of stores) store.resetSessionState(); }
            finally { resetting = false; }
          },
          registerSessionResetHandler: (handler): void => { for (const store of stores) store.registerSessionResetHandler(handler); },
        };
      },
    },
  ],
})
  .then(() => {
    if (!isDevMode() && 'serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').catch((error: unknown) => console.warn('Aura service worker unavailable', error));
    }
  })
  .catch((error: unknown) => console.error(error));
