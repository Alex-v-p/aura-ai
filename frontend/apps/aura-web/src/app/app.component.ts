import { ChangeDetectionStrategy, Component, OnDestroy, effect, inject, signal, untracked } from '@angular/core';
import { NavigationEnd, Router, RouterOutlet } from '@angular/router';
import { AuraShellComponent } from './shell/aura-shell.component';
import { ThemePreference } from '@aura/shared/ui';
import { ConversationStore, type ConversationListFilters } from '@aura/aura/interaction/conversations';
import { AgentStore } from '@aura/aura/interaction/agents';
import type { ShellConversationAction, ShellConversationFilters, ShellConversationSummary } from './shell/aura-shell.component';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [AuraShellComponent, RouterOutlet],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<aura-shell [conversations]="conversationSummaries()" [agentOptions]="agentOptions()" [modelOptions]="modelOptions()" [hasMoreConversations]="store.hasMoreConversations()" [loadingMoreConversations]="store.libraryLoadingMore()" [theme]="theme()" [collapsed]="navCollapsed()" [mobileOpen]="mobileNavigationOpen()" (newConversation)="createConversation()" (collapseChange)="navCollapsed.set($event)" (mobileOpenChange)="mobileNavigationOpen.set($event)" (themeChange)="setTheme($event)" (filtersChange)="applyLibraryFilters($event)" (loadMore)="store.loadMoreConversations()" (conversationAction)="handleConversationAction($event)"><router-outlet /></aura-shell>`,
})
export class AppComponent implements OnDestroy {
  readonly store = inject(ConversationStore);
  readonly agentStore = inject(AgentStore);
  private readonly router = inject(Router);
  readonly theme = signal<ThemePreference>(this.readTheme());
  readonly navCollapsed = signal(false);
  readonly mobileNavigationOpen = signal(false);
  readonly conversationSummaries = (): ReadonlyArray<ShellConversationSummary> => this.store.conversations().filter((conversation) => !conversation.id.startsWith('draft-')).map((conversation) => ({
    id: conversation.id,
    title: conversation.title,
    active: conversation.id === this.store.selectedId(),
    archived: Boolean(conversation.archivedAt),
    agentProfileId: conversation.agent?.profileId,
    modelId: conversation.modelId,
    latestRunStatus: conversation.currentRun?.status ?? conversation.retryableRun?.status,
    status: this.store.runStates()[conversation.id] === 'working' ? 'Working' : this.store.runStates()[conversation.id] === 'error' ? 'Needs attention' : undefined,
  }));
  readonly agentOptions = () => this.agentStore.activeAgents().map((agent) => ({ id: agent.id, label: agent.displayName }));
  readonly modelOptions = () => this.store.models().map((model) => ({ id: model.id, label: model.displayName }));
  private readonly routedConversationId = signal<string | null | undefined>(this.conversationIdFromUrl(this.router.url));
  private readonly routeSyncReady = signal(false);
  private routeSyncSequence = 0;
  private readonly routeSubscription = this.router.events.subscribe((event) => { if (event instanceof NavigationEnd) { this.routeSyncSequence += 1; this.routeSyncReady.set(false); this.routedConversationId.set(this.conversationIdFromUrl(event.urlAfterRedirects)); } });
  private readonly routeEffect = effect(() => { const routeId = this.routedConversationId(); const sequence = this.routeSyncSequence; if (routeId === undefined || this.store.loading()) return; untracked(() => { void this.syncConversationRoute(routeId, sequence); }); });
  private readonly persistedRouteEffect = effect(() => { const selectedId = this.store.selectedId(); const routeId = this.routedConversationId(); const persistedDraftId = this.store.lastPersistedDraftId(); const bareConversationRoute = routeId === null && this.router.url.split('?')[0] === '/conversation'; const matchingDraftRoute = routeId !== undefined && routeId === persistedDraftId; if (!this.routeSyncReady() || selectedId.startsWith('draft-') || (!bareConversationRoute && !matchingDraftRoute)) return; void this.router.navigate(['/conversation', selectedId], { replaceUrl: true }); });
  private readonly mediaQuery = typeof window === 'undefined' ? null : window.matchMedia('(prefers-color-scheme: dark)');
  private readonly mediaListener = (): void => { if (this.theme() === 'system') this.applyTheme('system'); };
  private readonly themeEffect = effect(() => this.applyTheme(this.theme()));

  constructor() {
    this.mediaQuery?.addEventListener('change', this.mediaListener);
    if (typeof window !== 'undefined' && new URLSearchParams(window.location.search).get('fixture') === 'error') {
      this.store.failNextLocalReply();
    }
  }
  setTheme(preference: ThemePreference): void { this.theme.set(preference); if (preference === 'system') localStorage.removeItem('aura-theme'); else localStorage.setItem('aura-theme', preference); }
  createConversation(): void { this.store.create(); void this.router.navigateByUrl('/conversation'); }
  applyLibraryFilters(filters: ShellConversationFilters): void { this.store.setLibraryFilters(filters as ConversationListFilters); }
  handleConversationAction(action: ShellConversationAction): void {
    if (action.action === 'rename' && action.title) void this.store.renameConversation(action.id, action.title);
    else if (action.action === 'archive') void this.store.archiveConversation(action.id);
    else if (action.action === 'restore') void this.store.restoreConversation(action.id);
    else if (action.action === 'inspect') {
      void this.router.navigate(['/conversation', action.id]).then(() => this.store.openRunInspector(action.id, action.focusId ?? null));
    }
  }
  ngOnDestroy(): void { this.mediaQuery?.removeEventListener('change', this.mediaListener); this.routeSubscription.unsubscribe(); this.themeEffect.destroy(); this.routeEffect.destroy(); this.persistedRouteEffect.destroy(); }
  private readTheme(): ThemePreference { if (typeof localStorage === 'undefined') return 'system'; const value = localStorage.getItem('aura-theme'); return value === 'light' || value === 'dark' ? value : 'system'; }
  private applyTheme(preference: ThemePreference): void { if (typeof document === 'undefined') return; const mode = preference === 'system' ? (this.mediaQuery?.matches ? 'dark' : 'light') : preference; document.documentElement.dataset['theme'] = mode; document.documentElement.style.colorScheme = mode; }
  private async syncConversationRoute(routeId: string | null, sequence: number): Promise<void> {
    const isCurrent = (): boolean => sequence === this.routeSyncSequence && this.routedConversationId() === routeId;
    if (!isCurrent()) return;
    try {
      if (routeId === null) { this.store.selectDraftForRoute(); return; }
      const selection = await this.store.selectFromRoute(routeId, isCurrent);
      if (!isCurrent() || selection.status === 'stale' || selection.status === 'selected') return;
      if (selection.status !== 'not_found') { this.store.showRouteNotice(selection.error.message); return; }
      this.store.selectDraftForRoute();
      this.store.showRouteNotice('That conversation is no longer available. Your current draft is still here.');
      await this.router.navigateByUrl('/conversation', { replaceUrl: true });
    } finally { if (isCurrent()) this.routeSyncReady.set(true); }
  }
  private conversationIdFromUrl(url: string): string | null | undefined {
    const segments = this.router.parseUrl(url).root.children['primary']?.segments ?? [];
    if (segments[0]?.path !== 'conversation') return undefined;
    return segments[1]?.path ?? null;
  }
}
