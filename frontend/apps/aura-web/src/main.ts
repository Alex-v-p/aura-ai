import { computed, inject, Injector, isDevMode } from '@angular/core';
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
import { DAILY_CALENDAR_AGENDA, type DailyCalendarAgendaProjection, type DailyCalendarAgendaStatus } from '@aura/aura/daily-action';
import { CalendarStore } from '@aura/aura/integrations/calendar';
import { AppComponent } from './app/app.component';
import { routes } from './app/app.routes';

bootstrapApplication(AppComponent, {
  providers: [
    provideRouter(routes),
    provideAnimationsAsync(),
    // The shell composes the calendar library's public agenda projection into
    // Today. The daily-action feature never reaches into calendar internals.
    {
      provide: DAILY_CALENDAR_AGENDA,
      useFactory: (): DailyCalendarAgendaProjection => {
        const calendar = inject(CalendarStore);
        let startedEpoch = -1;
        const start = (): Promise<void> => {
          const epoch = calendar.resetEpoch();
          if (startedEpoch === epoch) return Promise.resolve();
          startedEpoch = epoch;
          return calendar.load().then(() => undefined);
        };
        const status = computed<DailyCalendarAgendaStatus>(() => {
          const error = calendar.error();
          if (calendar.requestState() === 'loading' && !calendar.events()) return 'loading';
          if (calendar.requestState() === 'refreshing') return 'reconnecting';
          if (error?.recovery === 'reauthorize') return 'reauthorization-required';
          if (error?.recovery === 'wait') return 'rate-limited';
          if (error?.status === 403) return 'permission-denied';
          if (calendar.connection()?.status === 'connecting') return 'connecting';
          if (calendar.connection()?.status === 'reauthorization_required') return 'reauthorization-required';
          if (calendar.connection()?.status === 'revocation_uncertain') return 'revocation-uncertain';
          if (calendar.connection()?.status === 'disabled') return 'disabled';
          if (calendar.connection()?.status === 'unavailable') return 'unavailable';
          if (!calendar.connection() || calendar.connection()?.status === 'disconnected') return 'disconnected';
          if (!calendar.connection()?.readsEnabled) return 'disabled';
          if (calendar.requestState() === 'error') return calendar.events() ? 'stale' : 'unavailable';
          if (calendar.agenda().partial) return 'partial';
          if (!calendar.agenda().events.length) return 'empty';
          return 'ready';
        });
        const events = computed(() => calendar.agenda().events.map((event) => ({
          kind: event.kind,
          id: event.id,
          calendarId: event.calendarId,
          title: event.title,
          location: event.location,
          availability: event.availability,
          ...(event.kind === 'timed' ? { startInstant: event.startInstant, endInstant: event.endInstant } : { startDate: event.startDate, endDateExclusive: event.endDateExclusive }),
          eventTimeZone: event.eventTimeZone,
          displayTimeZone: event.displayTimeZone,
          spansMultipleDays: event.spansMultipleDays,
        })));
        return {
          start,
          status,
          events,
          conflicts: computed(() => calendar.agenda().conflicts),
          displayTimeZone: calendar.displayTimeZone,
          visibleDate: calendar.selectedDate,
          notice: calendar.notice,
          retry: (): Promise<void> => calendar.loadAgenda().then(() => undefined),
        };
      },
    },
    { provide: AGENT_API, useClass: AuraAgentApi },
    { provide: PERSONA_API, useClass: AuraPersonaApi },
    {
      provide: AURA_MANAGEMENT_SESSION,
      useFactory: (): ManagementSessionPort => {
        const injector = inject(Injector);
        const notificationStore = inject(NotificationStore);
        const approvalStore = inject(ApprovalStore);
        const calendarStore = inject(CalendarStore);
        const stores = [inject(AgentStore), inject(PersonaStore), inject(ToolPolicyStore), inject(MemoryStore), inject(DocumentsStore), inject(ReminderStore), notificationStore, approvalStore, calendarStore];
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
