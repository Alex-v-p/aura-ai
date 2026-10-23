import { ChangeDetectionStrategy, Component, DestroyRef, ElementRef, HostListener, ViewChild, effect, inject, signal } from '@angular/core';
import {
  EvaluationWorkspaceComponent,
  EvaluationWorkspaceStore,
  ExperimentWorkspaceComponent,
  ExperimentWorkspaceStore,
  FetchObservatoryTransport,
  OBSERVATORY_GATEWAY,
  ObservatoryAnalyticsComponent,
  ObservatoryComparisonComponent,
  ObservatoryComponentsComponent,
  ObservatoryOperationsWorkspaceComponent,
  ObservatoryOverviewComponent,
  ObservatoryRetentionComponent,
  ObservatoryRunsComponent,
  ObservatoryStore,
  ObservatoryTraceComponent,
  OperationsWorkspaceStore,
  authCopyForState,
  consumeAuthRecoveryMarker,
  ownerLoginHref,
  ObservatoryStatusBadgeComponent,
  ObservatoryStatusTone,
} from '@aura/observatory';

type ObservatoryDestination = 'overview' | 'runs' | 'trace' | 'comparison' | 'components' | 'analytics' | 'operations' | 'retention' | 'evaluation' | 'experiments';

interface ObservatoryNavItem {
  readonly id: ObservatoryDestination;
  readonly label: string;
  readonly description: string;
  readonly glyph: string;
}

interface ObservatoryNavGroup {
  readonly id: string;
  readonly label: string;
  readonly items: readonly ObservatoryNavItem[];
}

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [
    ObservatoryOverviewComponent,
    ObservatoryRunsComponent,
    ObservatoryTraceComponent,
    ObservatoryComponentsComponent,
    ObservatoryRetentionComponent,
    ObservatoryAnalyticsComponent,
    ObservatoryComparisonComponent,
    EvaluationWorkspaceComponent,
    ExperimentWorkspaceComponent,
    ObservatoryOperationsWorkspaceComponent,
    ObservatoryStatusBadgeComponent,
  ],
  providers: [
    FetchObservatoryTransport,
    ObservatoryStore,
    EvaluationWorkspaceStore,
    ExperimentWorkspaceStore,
    OperationsWorkspaceStore,
    { provide: OBSERVATORY_GATEWAY, useExisting: FetchObservatoryTransport },
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrls: ['./app.component.css'],
  template: `
    <a class="skip-link" href="#observatory-main">Skip to Observatory content</a>

    <header class="shell-header">
      <div class="brand-lockup">
        @if (store.authState() === 'authenticated') {
          <button #navigationTrigger class="mobile-menu-toggle" type="button" [attr.aria-expanded]="navigationOpen()" aria-controls="observatory-sections" (click)="toggleNavigation()">
            <span class="menu-icon" aria-hidden="true">☰</span><span class="sr-only">{{ navigationOpen() ? 'Close' : 'Open' }} Observatory navigation</span>
          </button>
        }
        <div class="brand-mark" aria-hidden="true"><span>O</span></div>
        <div class="brand-copy"><p class="brand-eyebrow">Aura Observatory</p><h1>Operational metadata</h1></div>
      </div>
      <div class="header-context"><span class="context-title" aria-live="polite">{{ activeDestinationLabel() }}</span><span class="context-divider" aria-hidden="true"></span><span class="context-note">Private, metadata-only monitoring</span></div>
      @if (store.authState() === 'authenticated') {
        <div class="header-actions">
          <observatory-status-badge [label]="connectionLabel()" [tone]="connectionTone()" />
          <button class="sign-out-button" type="button" (click)="signOut()">Sign out</button>
        </div>
      }
    </header>

    @if (store.authState() === 'checking') {
      <main id="observatory-main" tabindex="-1" class="signed-out-state signed-out" aria-busy="true"><div class="state-kicker">Aura Observatory</div><h2>Checking your Observatory session</h2><p role="status">Your owner session is being checked.</p></main>
    } @else if (store.authState() === 'authenticated') {
      <div class="shell-layout">
        <div class="mobile-scrim" [class.visible]="isModalNavigation()" (click)="closeNavigation()" aria-hidden="true"></div>
        <aside #navigationRail class="navigation-rail" [class.mobile-open]="navigationOpen()" id="observatory-sections"
          [attr.role]="navigationRole()" [attr.aria-modal]="isModalNavigation() ? 'true' : null"
          aria-label="Observatory navigation" [attr.aria-hidden]="navigationUsable() ? null : 'true'"
          [attr.inert]="navigationUsable() ? null : ''" tabindex="-1">
          <div class="rail-heading"><div><p class="rail-kicker">Workspace</p><p class="rail-title">Observe clearly</p></div><button class="mobile-close" type="button" aria-label="Close Observatory navigation" (click)="closeNavigation()">×</button></div>
          <nav aria-label="Observatory sections" (keydown.escape)="closeNavigation()">
            @for (group of navigationGroups; track group.id) {
              <section class="nav-group" [attr.aria-labelledby]="'nav-group-' + group.id">
                <h2 [id]="'nav-group-' + group.id">{{ group.label }}</h2>
                <div class="nav-items">
                  @for (item of group.items; track item.id) {
                    <button type="button" class="nav-item" [class.active]="destination() === item.id" [attr.aria-current]="destination() === item.id ? 'page' : null" [attr.aria-label]="item.label" (click)="openDestination(item.id)">
                      <span class="nav-glyph" aria-hidden="true">{{ item.glyph }}</span><span class="nav-item-copy"><span class="nav-label">{{ item.label }}</span><span class="nav-description" aria-hidden="true">{{ item.description }}</span></span><span class="nav-current" aria-hidden="true">→</span>
                    </button>
                  }
                </div>
              </section>
            }
          </nav>
          <div class="rail-footer"><span class="rail-footer-dot" aria-hidden="true"></span><span>Owner session active</span></div>
        </aside>

        <main id="observatory-main" tabindex="-1" class="shell-main">
            <observatory-overview [hidden]="destination() !== 'overview'" [style.display]="destination() === 'overview' ? 'block' : 'none'" />
            <observatory-runs [hidden]="destination() !== 'runs'" [style.display]="destination() === 'runs' ? 'block' : 'none'" (runSelected)="openTraceFromRun()" />
            <observatory-trace [hidden]="destination() !== 'trace'" [style.display]="destination() === 'trace' ? 'block' : 'none'" (browseRuns)="openDestination('runs')" />
            <observatory-comparison [hidden]="destination() !== 'comparison'" [style.display]="destination() === 'comparison' ? 'block' : 'none'" />
            <observatory-components [hidden]="destination() !== 'components'" [style.display]="destination() === 'components' ? 'block' : 'none'" />
            <observatory-analytics [hidden]="destination() !== 'analytics'" [style.display]="destination() === 'analytics' ? 'block' : 'none'" />
            <observatory-operations-workspace [hidden]="destination() !== 'operations'" [style.display]="destination() === 'operations' ? 'block' : 'none'" />
            <observatory-retention [hidden]="destination() !== 'retention'" [style.display]="destination() === 'retention' ? 'block' : 'none'" />
            <observatory-evaluation-workspace [hidden]="destination() !== 'evaluation'" [style.display]="destination() === 'evaluation' ? 'block' : 'none'" />
            <observatory-experiment-workspace [hidden]="destination() !== 'experiments'" [style.display]="destination() === 'experiments' ? 'block' : 'none'" />
        </main>
      </div>
    } @else {
      <main id="observatory-main" tabindex="-1" class="signed-out-state signed-out" aria-labelledby="auth-recovery-title"><div class="sr-only" role="status" aria-live="polite">{{ authCopy().announcement }}</div><div class="state-kicker">Aura Observatory</div><h2 #authHeading id="auth-recovery-title" tabindex="-1">{{ authCopy().title }}</h2><p>{{ authCopy().message }}</p><div class="recovery-actions">@if (authCopy().retryAction === 'logout') { <button class="primary-action" type="button" (click)="retryLogout()">{{ authCopy().action }}</button><a class="secondary-action" [href]="ownerLoginHref('', returnPath())">Sign in as owner</a> } @else if (authCopy().retryAction === 'session') { <button class="primary-action" type="button" (click)="retryAuthentication()">{{ authCopy().action }}</button><a class="secondary-action" [href]="ownerLoginHref('', returnPath())">Sign in as owner</a> } @else { <a class="primary-action" [href]="ownerLoginHref('', returnPath())">{{ authCopy().action }}</a> }</div></main>
    }
  `,
})
export class AppComponent {
  readonly store = inject(ObservatoryStore);
  readonly operationsStore = inject(OperationsWorkspaceStore);
  readonly authCopy = () => authCopyForState(this.store.authState());
  readonly ownerLoginHref = ownerLoginHref;
  readonly destination = signal<ObservatoryDestination>('overview');
  readonly navigationOpen = signal(false);
  readonly compactViewport = signal(false);
  private readonly destroyRef = inject(DestroyRef);
  @ViewChild('navigationTrigger') private navigationTrigger?: ElementRef<HTMLButtonElement>;
  @ViewChild('navigationRail') private navigationRail?: ElementRef<HTMLElement>;
  @ViewChild('authHeading') private authHeading?: ElementRef<HTMLHeadingElement>;
  private lastFocusedAuthState = 'checking';
  private focusAuthenticatedLandmark = false;

  readonly navigationGroups: readonly ObservatoryNavGroup[] = [
    { id: 'monitor', label: 'Monitor', items: [
      { id: 'overview', label: 'Overview', description: 'Current health', glyph: '◌' },
      { id: 'runs', label: 'Runs', description: 'Recent execution', glyph: '↗' },
      { id: 'trace', label: 'Trace', description: 'Inspect a run', glyph: '⌁' },
      { id: 'components', label: 'Components', description: 'Catalogue & topology', glyph: '◫' },
    ] },
    { id: 'analyze', label: 'Analyze', items: [
      { id: 'analytics', label: 'Analytics', description: 'Operational signals', glyph: '▥' },
      { id: 'comparison', label: 'Compare', description: 'Run evidence', glyph: '⇄' },
    ] },
    { id: 'evaluate', label: 'Evaluate', items: [
      { id: 'evaluation', label: 'Evaluations', description: 'Quality workspace', glyph: '✓' },
      { id: 'experiments', label: 'Experiments', description: 'Baselines & changes', glyph: '⌘' },
    ] },
    { id: 'operate', label: 'Operate', items: [
      { id: 'operations', label: 'Operations', description: 'Dashboards & alerts', glyph: '⚙' },
      { id: 'retention', label: 'Retention', description: 'Storage lifecycle', glyph: '⌫' },
    ] },
  ];

  constructor() {
    if (typeof window !== 'undefined') {
      const updateViewport = () => this.compactViewport.set(window.matchMedia('(max-width: 52rem)').matches);
      updateViewport();
      window.addEventListener('resize', updateViewport, { passive: true });
      this.destroyRef.onDestroy(() => window.removeEventListener('resize', updateViewport));
    }
    effect(() => {
      const state = this.store.authState();
      if (state !== 'authenticated') this.operationsStore.disconnect();
      if (state === 'checking') {
        this.focusAuthenticatedLandmark = this.lastFocusedAuthState !== 'checking';
        this.lastFocusedAuthState = 'checking';
        return;
      }
      if (state === 'authenticated') {
        const shouldFocus = this.focusAuthenticatedLandmark;
        this.focusAuthenticatedLandmark = false;
        this.lastFocusedAuthState = 'authenticated';
        if (shouldFocus) setTimeout(() => document.getElementById('observatory-main')?.focus(), 0);
        return;
      }
      if (state === this.lastFocusedAuthState) return;
      this.lastFocusedAuthState = state;
      setTimeout(() => this.authHeading?.nativeElement.focus(), 0);
    });
    void this.store.start(consumeAuthRecoveryMarker());
  }

  openDestination(destination: ObservatoryDestination): void {
    if (destination === 'components' && this.destination() !== destination) this.store.refreshCatalog();
    // Close the modal before changing the destination so focus never lands in
    // a workspace underneath an active navigation dialog.
    this.closeNavigation(false);
    this.destination.set(destination);
    // Each destination is a distinct workspace. Restore the page origin so
    // route switching never opens a new surface halfway down the prior one.
    if (typeof window !== 'undefined') window.scrollTo({ top: 0, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
    this.focusDestination(destination);
  }

  openTraceFromRun(): void {
    this.openDestination('trace');
  }

  activeGroupLabel(): string { return this.navigationGroups.find((group) => group.items.some((item) => item.id === this.destination()))?.label ?? 'Monitor'; }
  activeDestinationLabel(): string { return this.navigationGroups.flatMap((group) => group.items).find((item) => item.id === this.destination())?.label ?? 'Overview'; }
  toggleNavigation(): void {
    if (this.store.authState() !== 'authenticated') return;
    if (this.navigationOpen()) this.closeNavigation();
    else this.openNavigation();
  }
  openNavigation(): void {
    if (this.store.authState() !== 'authenticated') return;
    this.navigationOpen.set(true);
    setTimeout(() => this.focusNavigationEntry(), 0);
  }
  closeNavigation(restoreFocus = true): void {
    const wasOpen = this.navigationOpen();
    this.navigationOpen.set(false);
    if (restoreFocus && wasOpen && this.compactViewport()) setTimeout(() => this.navigationTrigger?.nativeElement.focus(), 0);
  }
  signOut(): void { this.destination.set('overview'); void this.store.logout(); }
  retryAuthentication(): void { this.store.retryAuthentication(); }
  retryLogout(): void { void this.store.logout(); }
  returnPath(): string { return typeof window === 'undefined' ? '/' : `${window.location.pathname}${window.location.search}${window.location.hash}`; }
  connectionLabel(): string { const state = this.store.snapshot().state; return state === 'ready' || state === 'empty' ? 'Live updates on' : state === 'disconnected' ? 'Disconnected' : state === 'stale' ? 'Stale snapshot' : state === 'error' ? 'Observatory unavailable' : 'Connecting'; }
  connectionTone(): ObservatoryStatusTone { const state = this.store.snapshot().state; return state === 'ready' || state === 'empty' ? 'success' : state === 'disconnected' || state === 'error' ? 'danger' : state === 'stale' ? 'warning' : 'info'; }
  navigationUsable(): boolean { return !this.compactViewport() || this.navigationOpen(); }
  isModalNavigation(): boolean { return this.compactViewport() && this.navigationOpen(); }
  navigationRole(): 'dialog' | 'complementary' { return this.isModalNavigation() ? 'dialog' : 'complementary'; }

  @HostListener('document:keydown', ['$event'])
  handleNavigationKeydown(event: KeyboardEvent): void {
    if (!this.isModalNavigation()) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      this.closeNavigation();
      return;
    }
    if (event.key !== 'Tab') return;
    const panel = this.navigationRail?.nativeElement;
    if (!panel) return;
    const focusable = Array.from(panel.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])')).filter((element) => !element.hasAttribute('inert') && element.getClientRects().length > 0);
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  private focusNavigationEntry(): void {
    if (!this.isModalNavigation()) return;
    const first = this.navigationRail?.nativeElement.querySelector<HTMLElement>('.mobile-close, .nav-item');
    first?.focus();
  }

  private focusDestination(destination: ObservatoryDestination): void {
    // Desktop keeps focus on the selected rail item. Compact navigation is a
    // modal interaction, so return focus to the newly visible page landmark.
    if (!this.compactViewport()) return;
    const selectors: Record<ObservatoryDestination, string> = {
      overview: 'observatory-overview h2',
      runs: 'observatory-runs h2',
      trace: '#trace-title',
      components: '#components-title',
      analytics: '#analytics-title',
      comparison: '#comparison-title',
      evaluation: '#evaluation-title',
      experiments: '#experiment-title',
      operations: '#operations-title',
      retention: 'observatory-retention h2',
    };
    setTimeout(() => {
      const heading = Array.from(document.querySelectorAll<HTMLElement>(selectors[destination]))
        .find((element) => element.getClientRects().length > 0);
      if (heading) {
        if (!heading.hasAttribute('tabindex')) heading.tabIndex = -1;
        heading.focus();
      } else {
        document.getElementById('observatory-main')?.focus();
      }
    }, 0);
  }
}
