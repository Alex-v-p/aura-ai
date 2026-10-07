import { A11yModule } from '@angular/cdk/a11y';
import { CdkOverlayOrigin, OverlayModule, type ConnectedPosition } from '@angular/cdk/overlay';
import { ChangeDetectionStrategy, Component, ElementRef, EventEmitter, HostListener, Input, OnDestroy, Output, ViewChild, signal } from '@angular/core';
import { AdaptiveLayoutComponent } from '@aura/shared/layout';
import { ThemePreference, ThemeSelectComponent } from '@aura/shared/ui';
import { RouterLink, RouterLinkActive } from '@angular/router';
import { normalizeActivityBoundary } from './shell-conversation-filters';

export interface ShellConversationSummary { readonly id: string; readonly title: string; readonly active: boolean; readonly status?: string; readonly archived?: boolean; readonly agentProfileId?: string; readonly modelId?: string; readonly latestRunStatus?: string; }
export interface ShellConversationFilters { readonly q?: string; readonly agentProfileId?: string; readonly modelId?: string; readonly runStatus?: string; readonly archiveState: 'active' | 'archived' | 'all'; readonly activityFrom?: string; readonly activityTo?: string; }
export type ShellConversationAction = { readonly id: string; readonly action: 'rename' | 'archive' | 'restore' | 'inspect'; readonly title?: string; readonly focusId?: string };

@Component({
  selector: 'aura-shell',
  standalone: true,
  imports: [A11yModule, OverlayModule, AdaptiveLayoutComponent, ThemeSelectComponent, RouterLink, RouterLinkActive],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './aura-shell.component.html',
  styleUrl: './aura-shell.component.css',
})
export class AuraShellComponent implements OnDestroy {
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
    this.collapsedValue = value;
    if (value && this.filterPanelOpen()) this.closeFilterPanel(false);
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
  readonly isMobileViewport = signal(typeof window !== 'undefined' && window.matchMedia('(max-width: 720px)').matches);
  readonly isWideDesktopViewport = signal(typeof window !== 'undefined' && window.matchMedia('(min-width: 1280px)').matches);
  readonly prefersReducedMotion = signal(typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  readonly desktopContentReady = signal(true);
  readonly filterPanelOpen = signal(false);
  readonly filterOverlayOrigin = signal<CdkOverlayOrigin>({} as CdkOverlayOrigin);
  readonly filterOverlayPositions: ConnectedPosition[] = [
    { originX: 'end', originY: 'bottom', overlayX: 'start', overlayY: 'top', offsetX: 8, offsetY: 8 },
    { originX: 'end', originY: 'top', overlayX: 'start', overlayY: 'bottom', offsetX: 8, offsetY: -8 },
    { originX: 'start', originY: 'bottom', overlayX: 'start', overlayY: 'top', offsetY: 8 },
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
  private readonly viewportListener = (): void => {
    const isMobile = this.viewportQuery?.matches ?? false;
    const isWideDesktop = this.wideDesktopQuery?.matches ?? false;
    const prefersReducedMotion = this.reducedMotionQuery?.matches ?? false;
    this.isMobileViewport.set(isMobile);
    this.isWideDesktopViewport.set(isWideDesktop);
    this.prefersReducedMotion.set(prefersReducedMotion);
    this.desktopContentReady.set(!isWideDesktop || !this.collapsed || prefersReducedMotion);
    if (!isMobile && this.mobileOpen) this.mobileOpenChange.emit(false);
  };

  constructor() {
    this.viewportQuery?.addEventListener('change', this.viewportListener);
    this.wideDesktopQuery?.addEventListener('change', this.viewportListener);
    this.reducedMotionQuery?.addEventListener('change', this.viewportListener);
  }

  ngOnDestroy(): void {
    if (this.searchTimer) clearTimeout(this.searchTimer);
    this.viewportQuery?.removeEventListener('change', this.viewportListener);
    this.wideDesktopQuery?.removeEventListener('change', this.viewportListener);
    this.reducedMotionQuery?.removeEventListener('change', this.viewportListener);
  }

  closeMobileNavigation(restoreFocus = true): void {
    if (this.filterPanelOpen()) this.closeFilterPanel(false);
    this.mobileOpenChange.emit(false);
    if (restoreFocus && this.isMobileViewport()) queueMicrotask(() => this.mobileTrigger?.nativeElement.focus());
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

  toggleRowMenu(id: string, event?: Event): void { event?.stopPropagation(); this.rowMenuId.set(this.rowMenuId() === id ? null : id); }
  beginRename(conversation: ShellConversationSummary, event?: Event): void { event?.stopPropagation(); this.renamingId.set(conversation.id); this.renameValue.set(conversation.title); this.rowMenuId.set(null); queueMicrotask(() => document.getElementById(`rename-${conversation.id}`)?.focus()); }
  cancelRename(): void { this.renamingId.set(null); this.renameValue.set(''); }
  commitRename(id: string, event?: Event): void { event?.preventDefault(); const title = this.renameValue().trim(); if (title) this.conversationAction.emit({ id, action: 'rename', title }); this.cancelRename(); }
  updateRename(value: string): void { this.renameValue.set(value); }
  rowAction(id: string, action: 'archive' | 'restore' | 'inspect', event?: Event): void { event?.stopPropagation(); this.rowMenuId.set(null); this.conversationAction.emit({ id, action, ...(action === 'inspect' ? { focusId: `conversation-actions-${id}` } : {}) }); }

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
    if (!this.mobileOpen) return;
    event.preventDefault();
    this.closeMobileNavigation();
  }

  private focusFilterPanel(): void {
    this.filterPanel?.nativeElement.querySelector<HTMLElement>('select, input, button')?.focus();
  }
}
