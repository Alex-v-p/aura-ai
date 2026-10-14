import { inject, Injector, isDevMode } from '@angular/core';
import { bootstrapApplication } from '@angular/platform-browser';
import { provideAnimationsAsync } from '@angular/platform-browser/animations/async';
import { provideRouter } from '@angular/router';
import { AURA_MANAGEMENT_SESSION, ConversationStore, type ManagementSessionPort } from '@aura/aura/interaction/conversations';
import { AGENT_API, AgentStore, AuraAgentApi, ToolPolicyStore } from '@aura/aura/interaction/agents';
import { PERSONA_API, AuraPersonaApi, PersonaStore } from '@aura/aura/interaction/personas';
import { MemoryStore } from '@aura/aura/knowledge/memory';
import { DocumentsStore } from '@aura/aura/knowledge/documents';
import { ReminderStore } from '@aura/aura/automation/reminders';
import { NotificationStore } from '@aura/aura/automation/notifications';
import { ApprovalStore } from '@aura/aura/governance/approvals';
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
        const injector = inject(Injector);
        const notificationStore = inject(NotificationStore);
        const approvalStore = inject(ApprovalStore);
        const stores = [inject(AgentStore), inject(PersonaStore), inject(ToolPolicyStore), inject(MemoryStore), inject(DocumentsStore), inject(ReminderStore), notificationStore, approvalStore];
        let authorizationFailure = false;
        let resetting = false;
        return {
          resetSessionState: (): void => {
            if (resetting) return;
            resetting = true;
            try {
              for (const store of stores) store.resetSessionState();
              // Reconnect metadata only after ConversationStore accepts the
              // replacement principal; an auth failure remains disconnected.
              queueMicrotask(() => {
                const accepted = injector.get(ConversationStore).authState() === 'authenticated';
                if (!authorizationFailure && accepted) { notificationStore.resumeAfterAcceptedSession(); approvalStore.resumeAfterAcceptedSession(); }
                authorizationFailure = false;
              });
            }
            finally { resetting = false; }
          },
          registerSessionResetHandler: (handler): void => {
            for (const store of stores) store.registerSessionResetHandler((status) => {
              if (status === 401 || status === 403) authorizationFailure = true;
              handler?.(status);
            });
          },
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
