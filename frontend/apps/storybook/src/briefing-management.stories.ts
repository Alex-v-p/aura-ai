import { applicationConfig } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { signal } from '@angular/core';
import { provideRouter, withDisabledInitialNavigation } from '@angular/router';
import { AgentStore } from '@aura/aura/interaction/agents';
import { BriefingStore, BriefingsPageComponent } from '@aura/aura/automation/briefings';

const now = '2026-10-27T07:00:00.000Z';
const capability = { executionPolicyRevisionId: 'policy-story', executionPolicyRevision: 'briefing-execution-v1', deadlineSeconds: 300, maxOutputTokens: 16_384, maxOutputChars: 32_768, cadenceKind: 'daily', memoryEnabled: false, toolsEnabled: false };
const schedule = { id: 'schedule-story', version: 3, displayName: 'Morning owner briefing', status: 'active', cadence: { kind: 'daily', localTime: '08:00', timeZone: 'Europe/Brussels' }, sources: { includeReminders: true, calendarIds: ['calendar-story'], documentIds: ['document-story'] }, nextDueAt: now, createdAt: now, updatedAt: now };

function briefingFixture(): Record<string, unknown> {
  return {
    capabilities: signal(capability), schedules: signal([schedule]), occurrences: signal([]), selectedSchedule: signal(null), selectedOccurrence: signal(null), selectedResult: signal(null), draft: signal({ displayName: '', localTime: '08:00', timeZone: '', agentRevisionId: '', calendarIds: [], documentIds: [] }), errors: signal({}), loading: signal(false), detailLoading: signal(false), saving: signal(false), notice: signal(null), errorStatus: signal(null), eligibleAgentRevisions: signal([]), selectableCalendars: signal([{ id: 'calendar-story', displayName: 'Personal calendar', timeZone: 'Europe/Brussels', selected: true }]), readyDocuments: signal([{ id: 'document-story', filename: 'Owner notes.md', mediaType: 'text/markdown', status: 'ready' }]), sourcesComplete: signal(true), calendarsHaveMore: signal(false), calendarsLoading: signal(false), documentsHaveMore: signal(false), documentsLoading: signal(false), sourceNotice: signal(''), load: async () => undefined, clearDetail: () => undefined, clearDraft: () => undefined, setDraft: () => undefined, toggleCalendar: () => undefined, toggleDocument: () => undefined, loadMoreCalendars: async () => undefined, loadMoreDocuments: async () => undefined, retrySources: async () => undefined, reloadCapabilities: async () => undefined, create: async () => null, inspect: async () => null, inspectOccurrence: async () => null, pauseOrResume: async () => true, deleteSchedule: async () => true, deleteHistory: async () => true, deleteResult: async () => true,
  };
}

const meta: Meta<BriefingsPageComponent> = { title: 'Aura/Automate/Briefings', component: BriefingsPageComponent, parameters: { layout: 'fullscreen' }, decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), { provide: BriefingStore, useValue: briefingFixture() }, { provide: AgentStore, useValue: { activeAgents: () => [] } }] })] };
export default meta;
type Story = StoryObj<BriefingsPageComponent>;
export const Management: Story = { name: 'Private schedule management' };
