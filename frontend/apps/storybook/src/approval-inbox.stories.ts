import { applicationConfig } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { computed, signal } from '@angular/core';
import { ApprovalsPageComponent, ApprovalStore, type ApprovalSummary } from '@aura/aura/governance/approvals';

const now = '2026-10-10T09:00:00.000Z';
const pending: ApprovalSummary = { id: 'approval-pending', proposalId: 'proposal-1', runId: 'run-1', conversationId: 'conversation-1', toolCallId: 'call-1', toolId: 'reminder.create', toolVersion: 1, status: 'pending', executionStatus: 'not_started', version: 1, createdAt: now, updatedAt: now, expiresAt: '2026-10-10T10:00:00.000Z', decidedAt: null };
const approved: ApprovalSummary = { ...pending, id: 'approval-approved', status: 'approved', executionStatus: 'completed', version: 2, decidedAt: '2026-10-10T09:05:00.000Z' };

function fixtureStore(items: ReadonlyArray<ApprovalSummary> = [pending, approved]): ApprovalStore {
  const collection = signal({ items, nextCursor: null, pendingCount: items.filter((item) => item.status === 'pending').length });
  return { collection, selected: signal(null), loading: signal(false), saving: signal(false), notice: signal(null), errorStatus: signal(null), connectionState: signal<'offline' | 'connecting' | 'live'>('live'), filter: signal<'all' | 'pending' | 'approved' | 'rejected' | 'expired' | 'canceled'>('all'), pendingCount: computed(() => collection().pendingCount), start: async () => undefined, stop: () => undefined, refresh: async () => undefined, loadMore: async () => undefined, inspect: async () => null, approve: async () => true, reject: async () => true, setFilter: async () => undefined, clearNotice: () => undefined, forConversation: () => [], statusLabel: (status: string) => status[0].toUpperCase() + status.slice(1), } as unknown as ApprovalStore;
}

const meta: Meta<ApprovalsPageComponent> = { title: 'Aura/Governance/Approval inbox', component: ApprovalsPageComponent, parameters: { layout: 'fullscreen' }, decorators: [applicationConfig({ providers: [{ provide: ApprovalStore, useValue: fixtureStore() }] })] };
export default meta;
type Story = StoryObj<ApprovalsPageComponent>;
export const PendingAndCompleted: Story = { name: 'Pending and completed approvals' };
export const Empty: Story = { name: 'Empty inbox', decorators: [applicationConfig({ providers: [{ provide: ApprovalStore, useValue: fixtureStore([]) }] })] };
export const Mobile: Story = { name: 'Responsive mobile inbox', parameters: { viewport: { defaultViewport: 'mobile1' } } };
