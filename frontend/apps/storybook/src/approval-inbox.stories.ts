import { applicationConfig } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { computed, signal } from '@angular/core';
import { of } from 'rxjs';
import { ApprovalsPageComponent, ApprovalStore, type ApprovalDetail, type ApprovalSummary, type ApprovalView } from '@aura/aura/governance/approvals';

const now = '2026-10-10T09:00:00.000Z';
const pending: ApprovalSummary = { id: 'approval-pending', proposalId: 'proposal-pending', runId: 'run-pending', conversationId: 'conversation-pending', toolCallId: 'call-pending', toolId: 'reminder.create', toolVersion: 1, status: 'pending', executionStatus: 'not_started', version: 1, createdAt: now, updatedAt: now, expiresAt: '2026-10-10T10:00:00.000Z', decidedAt: null };
const approved: ApprovalSummary = { ...pending, id: 'approval-approved', proposalId: 'proposal-approved', runId: 'run-approved', conversationId: 'conversation-approved', status: 'approved', executionStatus: 'completed', version: 2, decidedAt: '2026-10-10T09:05:00.000Z' };
const rejected: ApprovalSummary = { ...pending, id: 'approval-rejected', proposalId: 'proposal-rejected', runId: 'run-rejected', conversationId: 'conversation-rejected', status: 'rejected', executionStatus: 'denied', version: 2, decidedAt: '2026-10-10T09:06:00.000Z' };
const detail: ApprovalDetail = { ...pending, proposal: { id: 'proposal-pending', content: 'Create a reminder to bring the library books back.', dueAt: '2026-10-11T16:30:00.000Z' } };
const approvedDetail: ApprovalDetail = { ...approved, proposal: { id: 'proposal-approved', content: 'Create the dentist reminder.', dueAt: '2026-10-12T09:00:00.000Z' } };

type FixtureOptions = {
  readonly items?: ReadonlyArray<ApprovalSummary>;
  readonly pendingItems?: ReadonlyArray<ApprovalSummary>;
  readonly selected?: ApprovalDetail | null;
  readonly loading?: boolean;
  readonly detailLoading?: boolean;
  readonly detailError?: string | null;
  readonly notice?: string | null;
  readonly errorStatus?: number | null;
  readonly decisionConfirmationRequired?: boolean;
  readonly connectionState?: 'offline' | 'connecting' | 'reconnecting' | 'live' | 'unavailable';
  readonly view?: ApprovalView;
  readonly loadedView?: ApprovalView | null;
};

function detailRoute(id: string): ActivatedRoute {
  return { paramMap: of(convertToParamMap({ id })) } as unknown as ActivatedRoute;
}

function fixtureStore(options: FixtureOptions = {}): ApprovalStore {
  const viewValue = options.view ?? 'pending';
  const items = options.items ?? (viewValue === 'history' ? [approved, rejected] : [pending]);
  const pendingItems = options.pendingItems ?? [pending];
  const collection = signal({ items, nextCursor: null, pendingCount: items.filter((item) => item.status === 'pending').length });
  const pendingCollection = signal({ items: pendingItems, nextCursor: null, pendingCount: pendingItems.length });
  const selected = signal<ApprovalDetail | null>(options.selected ?? null);
  const view = signal<ApprovalView>(viewValue);
  const loadedView = signal<ApprovalView | null>(options.loadedView === undefined ? viewValue : options.loadedView);
  const errorStatus = signal<number | null>(options.errorStatus ?? null);
  const decisionConfirmationRequired = signal(options.decisionConfirmationRequired ?? false);
  const notice = signal<string | null>(options.notice ?? null);
  return {
    collection,
    pendingCollection,
    selected,
    loading: signal(options.loading ?? false),
    loadingMore: signal(false),
    detailLoading: signal(options.detailLoading ?? false),
    detailError: signal(options.detailError ?? null),
    saving: signal(false),
    decisionConfirmationRequired,
    notice,
    errorStatus,
    retryAvailable: computed(() => notice() !== null && errorStatus() !== 401 && errorStatus() !== 403),
    connectionState: signal(options.connectionState ?? 'live'),
    view,
    loadedView,
    filter: signal<'all' | 'pending' | 'approved' | 'rejected' | 'expired' | 'canceled'>(viewValue === 'pending' ? 'pending' : 'all'),
    pendingCount: computed(() => pendingCollection().items.length),
    pendingCountReconciled: signal(true),
    pendingCountExact: computed(() => pendingCollection().nextCursor === null),
    resetEpoch: signal(0),
    start: async () => undefined,
    stop: () => undefined,
    retryConnection: () => undefined,
    retry: async () => { decisionConfirmationRequired.set(false); notice.set(null); errorStatus.set(null); return true; },
    refresh: async () => undefined,
    loadMore: async () => undefined,
    inspect: async (id: string) => { if (id === detail.id) selected.set(detail); return selected(); },
    approve: async () => true,
    reject: async () => true,
    setView: async (next: ApprovalView) => { view.set(next); loadedView.set(next); },
    setFilter: async () => undefined,
    clearNotice: () => { notice.set(null); errorStatus.set(null); },
    registerSessionResetHandler: () => undefined,
    resumeAfterAcceptedSession: () => undefined,
    forConversation: (conversationId: string) => pendingCollection().items.filter((item) => item.conversationId === conversationId),
    statusLabel: (status: string) => status[0].toUpperCase() + status.slice(1),
    executionLabel: (status: string) => status === 'not_started' ? 'Waiting to run' : status === 'queued' ? 'Queued' : status === 'executing' ? 'Executing' : status === 'completed' ? 'Completed' : status === 'denied' ? 'Denied' : 'Failed',
    canDecide: (approval: ApprovalSummary) => !decisionConfirmationRequired() && approval.status === 'pending' && selected()?.id === approval.id,
    resetSessionState: () => undefined,
  } as unknown as ApprovalStore;
}

const meta: Meta<ApprovalsPageComponent> = {
  title: 'Aura/Governance/Approval inbox',
  component: ApprovalsPageComponent,
  parameters: { layout: 'fullscreen' },
  decorators: [applicationConfig({ providers: [provideRouter([]), { provide: ApprovalStore, useValue: fixtureStore() }] })],
};
export default meta;
type Story = StoryObj<ApprovalsPageComponent>;

export const Pending: Story = { name: 'Pending decision inbox' };
export const History: Story = { name: 'Terminal history', decorators: [applicationConfig({ providers: [{ provide: ApprovalStore, useValue: fixtureStore({ view: 'history', items: [approved, rejected], pendingItems: [pending] }) }] })] };
export const Detail: Story = { name: 'Pending detail review', decorators: [applicationConfig({ providers: [{ provide: ActivatedRoute, useValue: detailRoute(detail.id) }, { provide: ApprovalStore, useValue: fixtureStore({ selected: detail }) }] })] };
export const TerminalDetail: Story = { name: 'Terminal detail', decorators: [applicationConfig({ providers: [{ provide: ActivatedRoute, useValue: detailRoute(approvedDetail.id) }, { provide: ApprovalStore, useValue: fixtureStore({ selected: approvedDetail, view: 'history', items: [approved, rejected], pendingItems: [pending] }) }] })] };
export const Loading: Story = { name: 'Loading approvals', decorators: [applicationConfig({ providers: [{ provide: ApprovalStore, useValue: fixtureStore({ items: [], pendingItems: [], loading: true, loadedView: null }) }] })] };
export const Empty: Story = { name: 'Empty pending inbox', decorators: [applicationConfig({ providers: [{ provide: ApprovalStore, useValue: fixtureStore({ items: [], pendingItems: [] }) }] })] };
export const AuthenticationError: Story = { name: 'Authentication recovery', decorators: [applicationConfig({ providers: [{ provide: ApprovalStore, useValue: fixtureStore({ items: [], pendingItems: [], notice: 'Your session has expired. Sign in again to view approvals.', errorStatus: 401, loadedView: null }) }] })] };
export const PermissionError: Story = { name: 'Permission recovery', decorators: [applicationConfig({ providers: [{ provide: ApprovalStore, useValue: fixtureStore({ items: [], pendingItems: [], notice: 'You do not have permission to view these approvals.', errorStatus: 403, loadedView: null }) }] })] };
export const Conflict: Story = { name: 'Decision conflict', decorators: [applicationConfig({ providers: [{ provide: ActivatedRoute, useValue: detailRoute(detail.id) }, { provide: ApprovalStore, useValue: fixtureStore({ selected: detail, notice: 'This approval changed elsewhere. Refresh before deciding again.', errorStatus: 409 }) }] })] };
export const UnknownOutcome: Story = { name: 'Unknown decision outcome', decorators: [applicationConfig({ providers: [{ provide: ActivatedRoute, useValue: detailRoute(detail.id) }, { provide: ApprovalStore, useValue: fixtureStore({ selected: detail, decisionConfirmationRequired: true, notice: 'The decision outcome is unknown. Refresh to confirm before deciding again.' }) }] })] };
export const UnavailableStream: Story = { name: 'Unavailable live updates', decorators: [applicationConfig({ providers: [{ provide: ApprovalStore, useValue: fixtureStore({ items: [pending], pendingItems: [pending], connectionState: 'unavailable', notice: 'Live approvals stopped after several attempts. Retry when you are ready.' }) }] })] };
export const Recovery: Story = { name: 'Recoverable collection error', decorators: [applicationConfig({ providers: [{ provide: ApprovalStore, useValue: fixtureStore({ items: [], pendingItems: [], notice: 'Approvals are temporarily unavailable.', errorStatus: 503, loadedView: null }) }] })] };
export const Mobile: Story = { name: 'Responsive mobile inbox', parameters: { viewport: { defaultViewport: 'mobile1' } } };
