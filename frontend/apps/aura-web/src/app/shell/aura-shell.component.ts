import { A11yModule } from '@angular/cdk/a11y';
import { CdkOverlayOrigin, OverlayModule, type ConnectedPosition } from '@angular/cdk/overlay';
import { NgTemplateOutlet } from '@angular/common';
import { ChangeDetectionStrategy, Component, ElementRef, EventEmitter, HostListener, Input, OnDestroy, Output, ViewChild, inject, signal } from '@angular/core';
import { AdaptiveLayoutComponent } from '@aura/shared/layout';
import { ThemePreference, ThemeSelectComponent } from '@aura/shared/ui';
import { NavigationEnd, Router, RouterLink, RouterLinkActive } from '@angular/router';
import { ApprovalStore } from '@aura/aura/governance/approvals';
import { NotificationStore } from '@aura/aura/automation/notifications';
import { normalizeActivityBoundary } from './shell-conversation-filters';

export interface ShellConversationSummary { readonly id: string; readonly title: string; readonly active: boolean; readonly status?: string; readonly archived?: boolean; readonly agentProfileId?: string; readonly modelId?: string; readonly latestRunStatus?: string; }
export interface ShellConversationFilters { readonly q?: string; readonly agentProfileId?: string; readonly modelId?: string; readonly runStatus?: string; readonly archiveState: 'active' | 'archived' | 'all'; readonly activityFrom?: string; readonly activityTo?: string; }
export type ShellConversationAction = { readonly id: string; readonly action: 'rename' | 'archive' | 'restore' | 'inspect'; readonly title?: string; readonly focusId?: string };

@Component({
  selector: 'aura-shell',
  standalone: true,
  imports: [A11yModule, OverlayModule, NgTemplateOutlet, AdaptiveLayoutComponent, ThemeSelectComponent, RouterLink, RouterLinkActive],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './aura-shell.component.html',
  styleUrl: './aura-shell.component.css',
})
export class AuraShellComponent implements OnDestroy {
  private readonly router = inject(Router);
  readonly notifications = inject(NotificationStore);
  readonly approvals = inject(ApprovalStore);
  @Input() conversations: ReadonlyArray<ShellConversationSummary> = [];
  @Input() agentOptions: ReadonlyArray<{ readonly id: string; readonly label: string }> = [];
  @Input() modelOptions: ReadonlyArray<{ readonly id: string; readonly label: string }> = [];
  @Input() hasMoreConversations = false;
  @Input() loadingMoreConversations = false;
  @Input() theme: ThemePreference = 'system';
  @Input()
  get collapsed(): boolean { return this.collapsedValue; }
  set collapsed(value: boolean) {
    const changed = this.collapsedValue !== value;
    if (!value && changed && this.isWideDesktopViewport() && this.libraryPanelOpen()) this.closeLibraryForLayoutTransition();
    this.collapsedValue = value;
    if (value && this.filterPanelOpen()) this.closeFilterPanel(false);
    if (value && this.preferencesOpen()) this.closePreferences(false);
    if (this.isWideDesktopViewport() && (value || changed)) this.desktopContentReady.set(!value && this.prefersReducedMotion());
  }
  @Input() mobileOpen = false;
  @Output() readonly newConversation = new EventEmitter<void>();
  @Output() readonly conversationSelected = new EventEmitter<string>();
  @Output() readonly collapseChange = new EventEmitter<boolean>();
  @Output() readonly mobileOpenChange = new EventEmitter<boolean>();
  @Output() readonly themeChange = new EventEmitter<ThemePreference>();
  @Output() readonly filtersChange = new EventEmitter<ShellConversationFilters>();
  @Output() readonly loadMore = new EventEmitter<void>();
  @Output() readonly conversationAction = new EventEmitter<ShellConversationAction>();
  @ViewChild('mobileTrigger') private readonly mobileTrigger?: ElementRef<HTMLButtonElement>;
  @ViewChild('filterTrigger') private readonly filterTrigger?: ElementRef<HTMLButtonElement>;
  @ViewChild('filterPanel') private readonly filterPanel?: ElementRef<HTMLElement>;
  @ViewChild('libraryTrigger') private readonly libraryTrigger?: ElementRef<HTMLButtonElement>;
  @ViewChild('libraryPanel') private readonly libraryPanel?: ElementRef<HTMLElement>;
  @ViewChild('preferencesTrigger') private readonly preferencesTrigger?: ElementRef<HTMLButtonElement>;
  @ViewChild('preferencesPanel') private readonly preferencesPanel?: ElementRef<HTMLElement>;
  readonly isMobileViewport = signal(typeof window !== 'undefined' && window.matchMedia('(max-width: 720px)').matches);
  readonly isWideDesktopViewport = signal(typeof window !== 'undefined' && window.matchMedia('(min-width: 1280px)').matches);
  readonly prefersReducedMotion = signal(typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  readonly conversationContext = signal(this.router.url.split('?')[0].startsWith('/conversation'));
  readonly desktopContentReady = signal(true);
  readonly filterPanelOpen = signal(false);
  readonly filterOverlayOrigin = signal<CdkOverlayOrigin>({} as CdkOverlayOrigin);
  readonly libraryPanelOpen = signal(false);
  readonly libraryOverlayOrigin = signal<CdkOverlayOrigin>({} as CdkOverlayOrigin);
  readonly preferencesOpen = signal(false);
  readonly preferencesOverlayOrigin = signal<CdkOverlayOrigin>({} as CdkOverlayOrigin);
  readonly filterOverlayPositions: ConnectedPosition[] = [
    { originX: 'end', originY: 'bottom', overlayX: 'start', overlayY: 'top', offsetX: 8, offsetY: 8 },
    { originX: 'end', originY: 'top', overlayX: 'start', overlayY: 'bottom', offsetX: 8, offsetY: -8 },
    { originX: 'start', originY: 'bottom', overlayX: 'start', overlayY: 'top', offsetY: 8 },
  ];
  readonly libraryOverlayPositions: ConnectedPosition[] = [
    { originX: 'end', originY: 'top', overlayX: 'start', overlayY: 'top', offsetX: 12 },
    { originX: 'end', originY: 'bottom', overlayX: 'start', overlayY: 'bottom', offsetX: 12 },
    { originX: 'start', originY: 'top', overlayX: 'end', overlayY: 'top', offsetX: -12 },
  ];
  readonly preferencesOverlayPositions: ConnectedPosition[] = [
    { originX: 'end', originY: 'bottom', overlayX: 'start', overlayY: 'bottom', offsetX: 12 },
    { originX: 'end', originY: 'top', overlayX: 'start', overlayY: 'top', offsetX: 12 },
    { originX: 'start', originY: 'top', overlayX: 'end', overlayY: 'top', offsetX: -12 },
  ];
  readonly rowMenuId = signal<string | null>(null);
  readonly renamingId = signal<string | null>(null);
  readonly renameValue = signal('');
  readonly libraryFilters = signal<ShellConversationFilters>({ archiveState: 'active' });
  readonly activityFromInput = signal('');
  readonly activityToInput = signal('');
  private searchTimer: ReturnType<typeof setTimeout> | null = null;
  private collapsedValue = false;
  private readonly viewportQuery = typeof window === 'undefined' ? null : window.matchMedia('(max-width: 720px)');
  private readonly wideDesktopQuery = typeof window === 'undefined' ? null : window.matchMedia('(min-width: 1280px)');
  private readonly reducedMotionQuery = typeof window === 'undefined' ? null : window.matchMedia('(prefers-reduced-motion: reduce)');
  private readonly routeSubscription = this.router.events.subscribe((event) => {
    if (!(event instanceof NavigationEnd)) return;
    this.conversationContext.set(event.urlAfterRedirects.split('?')[0].startsWith('/conversation'));
    // A contextual library is detached from the rail. Preserve a predictable
    // focus return when a row navigates to a conversation and closes it.
    this.closeLibraryPanel(this.libraryPanelOpen() && this.isCompactSidebar());
    this.closeFilterPanel(false);
  });
  private readonly viewportListener = (): void => {
    const isMobile = this.viewportQuery?.matches ?? false;
    const isWideDesktop = this.wideDesktopQuery?.matches ?? false;
    const prefersReducedMotion = this.reducedMotionQuery?.matches ?? false;
    if (isWideDesktop && !this.collapsed && this.libraryPanelOpen()) this.closeLibraryForLayoutTransition();
    this.isMobileViewport.set(isMobile);
    this.isWideDesktopViewport.set(isWideDesktop);
    this.prefersReducedMotion.set(prefersReducedMotion);
    this.desktopContentReady.set(!isWideDesktop || !this.collapsed || prefersReducedMotion);
    if (!isWideDesktop && this.preferencesOpen()) this.closePreferences(false);
    if (!isMobile && this.mobileOpen) this.mobileOpenChange.emit(false);
  };

  constructor() {
    void this.notifications.start();
    void this.approvals.start();
    this.viewportQuery?.addEventListener('change', this.viewportListener);
    this.wideDesktopQuery?.addEventListener('change', this.viewportListener);
    this.reducedMotionQuery?.addEventListener('change', this.viewportListener);
  }

  ngOnDestroy(): void {
    this.notifications.stop();
    this.approvals.stop();
    if (this.searchTimer) clearTimeout(this.searchTimer);
    this.viewportQuery?.removeEventListener('change', this.viewportListener);
    this.wideDesktopQuery?.removeEventListener('change', this.viewportListener);
    this.reducedMotionQuery?.removeEventListener('change', this.viewportListener);
    this.routeSubscription.unsubscribe();
  }

  closeMobileNavigation(restoreFocus = true): void {
    if (this.filterPanelOpen()) this.closeFilterPanel(false);
    if (this.libraryPanelOpen()) this.closeLibraryPanel(false);
    if (this.preferencesOpen() && this.isCompactSidebar()) this.closePreferences(false);
    this.mobileOpenChange.emit(false);
    if (restoreFocus && this.isMobileViewport()) {
      const restore = (): void => this.mobileTrigger?.nativeElement.focus();
      queueMicrotask(restore);
      setTimeout(restore, 0);
    }
  }

  isCompactSidebar(): boolean { return !this.isWideDesktopViewport() || this.collapsed; }

  toggleNavigationCollapse(): void {
    if (this.collapsed && this.isWideDesktopViewport()) this.closeLibraryForLayoutTransition();
    this.collapseChange.emit(!this.collapsed);
  }

  showInlineConversationLibrary(): boolean {
    return this.conversationContext() && ((this.isWideDesktopViewport() && !this.collapsed) || (this.isMobileViewport() && this.mobileOpen));
  }

  toggleLibraryPanel(origin: CdkOverlayOrigin): void {
    if (this.libraryPanelOpen()) {
      this.closeLibraryPanel();
      return;
    }
    this.libraryOverlayOrigin.set(origin);
    this.libraryPanelOpen.set(true);
    queueMicrotask(() => this.focusLibraryPanel());
  }

  handleLibraryPanelAttach(): void { setTimeout(() => this.focusLibraryPanel(), 0); }

  closeLibraryPanel(restoreFocus = true): void {
    if (!this.libraryPanelOpen()) return;
    this.libraryPanelOpen.set(false);
    if (restoreFocus) queueMicrotask(() => this.libraryTrigger?.nativeElement.focus());
  }

  handleLibraryOutsideClick(event: MouseEvent): void {
    const target = event.target as HTMLElement | null;
    // The compact trigger disappears when the rail expands; let the layout
    // transition own focus restoration instead of returning focus to a node
    // that is about to be removed.
    this.closeLibraryPanel(!target?.closest('.collapse-button'));
  }

  private closeLibraryForLayoutTransition(): void {
    this.closeLibraryPanel(false);
    const focusActiveDestination = (): void => document.querySelector<HTMLElement>('#primary-navigation .product-link[aria-current="page"]')?.focus();
    // CDK removes the overlay in a later render turn; repeat after that
    // teardown so focus never lands on the detached contextual origin.
    queueMicrotask(focusActiveDestination);
    setTimeout(focusActiveDestination, 0);
  }

  togglePreferences(origin: CdkOverlayOrigin): void {
    if (this.preferencesOpen()) {
      this.closePreferences();
      return;
    }
    this.preferencesOverlayOrigin.set(origin);
    this.preferencesOpen.set(true);
    queueMicrotask(() => this.focusPreferencesPanel());
  }

  handlePreferencesAttach(): void { setTimeout(() => this.focusPreferencesPanel(), 0); }

  closePreferences(restoreFocus = true): void {
    if (!this.preferencesOpen()) return;
    this.preferencesOpen.set(false);
    if (restoreFocus) queueMicrotask(() => this.preferencesTrigger?.nativeElement.focus());
  }

  startNewConversation(): void {
    this.newConversation.emit();
    this.closeMobileNavigation(this.isMobileViewport());
  }

  toggleFilterPanel(event?: Event, origin?: CdkOverlayOrigin): void {
    event?.stopPropagation();
    if (this.filterPanelOpen()) {
      this.closeFilterPanel();
      return;
    }
    this.filterOverlayOrigin.set(origin ?? this.filterOverlayOrigin());
    this.filterPanelOpen.set(true);
    queueMicrotask(() => this.focusFilterPanel());
  }

  handleFilterOverlayAttach(): void { setTimeout(() => this.focusFilterPanel(), 0); }

  closeFilterPanel(returnFocus = true): void {
    if (!this.filterPanelOpen()) return;
    this.filterPanelOpen.set(false);
    if (returnFocus) queueMicrotask(() => this.filterTrigger?.nativeElement.focus());
  }

  clearFilters(): void {
    if (this.searchTimer) clearTimeout(this.searchTimer);
    this.searchTimer = null;
    const next: ShellConversationFilters = { archiveState: 'active' };
    this.activityFromInput.set('');
    this.activityToInput.set('');
    this.libraryFilters.set(next);
    this.filtersChange.emit(next);
  }

  selectConversation(id: string): void {
    this.conversationSelected.emit(id);
    this.closeMobileNavigation(this.isMobileViewport());
  }

  setQuery(value: string): void {
    const next: ShellConversationFilters = { ...this.libraryFilters(), q: value.slice(0, 200) || undefined };
    this.libraryFilters.set(next);
    if (this.searchTimer) clearTimeout(this.searchTimer);
    this.searchTimer = setTimeout(() => this.filtersChange.emit(this.libraryFilters()), 250);
  }

  setFilter(name: 'agentProfileId' | 'modelId' | 'runStatus' | 'activityFrom' | 'activityTo', value: string): void {
    if (name === 'activityFrom') this.activityFromInput.set(value);
    if (name === 'activityTo') this.activityToInput.set(value);
    const normalizedValue = name === 'activityFrom' || name === 'activityTo' ? normalizeActivityBoundary(name, value) : value || undefined;
    const next = { ...this.libraryFilters(), [name]: normalizedValue } as ShellConversationFilters;
    this.libraryFilters.set(next); this.filtersChange.emit(next);
  }

  setArchiveState(value: ShellConversationFilters['archiveState']): void {
    const next: ShellConversationFilters = { ...this.libraryFilters(), archiveState: value };
    this.libraryFilters.set(next); this.filtersChange.emit(next);
  }

  toggleRowMenu(id: string, event?: Event): void {
    event?.stopPropagation();
    if (this.rowMenuId() === id) { this.closeRowMenu(); return; }
    this.rowMenuId.set(id);
    queueMicrotask(() => document.getElementById(`conversation-actions-${id}`)?.parentElement?.querySelector<HTMLElement>('.row-menu [role="menuitem"]')?.focus());
  }
  beginRename(conversation: ShellConversationSummary, event?: Event): void { event?.stopPropagation(); this.renamingId.set(conversation.id); this.renameValue.set(conversation.title); this.rowMenuId.set(null); queueMicrotask(() => document.getElementById(`rename-${conversation.id}`)?.focus()); }
  cancelRename(): void { this.renamingId.set(null); this.renameValue.set(''); }
  commitRename(id: string, event?: Event): void { event?.preventDefault(); const title = this.renameValue().trim(); if (title) this.conversationAction.emit({ id, action: 'rename', title }); this.cancelRename(); }
  updateRename(value: string): void { this.renameValue.set(value); }
  closeRowMenu(restoreFocus = true): void {
    const id = this.rowMenuId();
    this.rowMenuId.set(null);
    if (restoreFocus && id) queueMicrotask(() => document.getElementById(`conversation-actions-${id}`)?.focus());
  }
  handleRowMenuKeydown(event: KeyboardEvent): void {
    const menu = event.currentTarget as HTMLElement;
    const items = Array.from(menu.querySelectorAll<HTMLElement>('[role="menuitem"]'));
    if (event.key === 'Escape') { event.preventDefault(); this.closeRowMenu(); return; }
    if (!items.length || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const index = items.indexOf(document.activeElement as HTMLElement);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (index + (event.key === 'ArrowUp' ? -1 : 1) + items.length) % items.length;
    items[next]?.focus();
  }
  rowAction(id: string, action: 'archive' | 'restore' | 'inspect', event?: Event): void {
    event?.stopPropagation();
    this.closeRowMenu(action === 'inspect' ? false : true);
    const contextualFocusId = !this.isMobileViewport() && this.isCompactSidebar() ? 'conversation-library-trigger' : `conversation-actions-${id}`;
    this.conversationAction.emit({ id, action, ...(action === 'inspect' ? { focusId: contextualFocusId } : {}) });
  }

  handleSidebarTransitionEnd(event: Event): void {
    const transition = event as TransitionEvent;
    if (transition.propertyName !== 'flex-basis' || this.collapsed || !this.isWideDesktopViewport()) return;
    const sidebar = transition.currentTarget as HTMLElement | null;
    if ((sidebar?.getBoundingClientRect().width ?? 0) < 272) return;
    this.desktopContentReady.set(true);
  }

  @HostListener('document:keydown.escape', ['$event'])
  handleEscape(event: Event): void {
    if (this.filterPanelOpen()) {
      event.preventDefault();
      this.closeFilterPanel();
      return;
    }
    if (this.libraryPanelOpen()) {
      event.preventDefault();
      this.closeLibraryPanel();
      return;
    }
    if (this.preferencesOpen() && this.isCompactSidebar()) {
      event.preventDefault();
      this.closePreferences();
      return;
    }
    if (this.rowMenuId()) {
      event.preventDefault();
      this.closeRowMenu();
      return;
    }
    if (!this.mobileOpen) return;
    event.preventDefault();
    this.closeMobileNavigation();
  }

  private focusFilterPanel(): void {
    this.filterPanel?.nativeElement.querySelector<HTMLElement>('select, input, button')?.focus();
  }

  private focusLibraryPanel(): void {
    this.libraryPanel?.nativeElement.querySelector<HTMLElement>('button, input, a')?.focus();
  }

  private focusPreferencesPanel(): void {
    this.preferencesPanel?.nativeElement.querySelector<HTMLElement>('input, button, a')?.focus();
  }
}
