import { bootstrapApplication } from '@angular/platform-browser';
import { ChangeDetectionStrategy, Component, ElementRef, effect, inject, signal, ViewChild } from '@angular/core';
import {
  OBSERVATORY_GATEWAY,
  FetchObservatoryTransport,
  ObservatoryStore,
  ObservatoryOverviewComponent,
  ObservatoryRunsComponent,
  ObservatoryTraceComponent,
  ObservatoryComponentsComponent,
  ObservatoryRetentionComponent,
  ObservatoryAnalyticsComponent,
  authCopyForState,
  consumeAuthRecoveryMarker,
  ownerLoginHref,
} from '@aura/observatory';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [ObservatoryOverviewComponent, ObservatoryRunsComponent, ObservatoryTraceComponent, ObservatoryComponentsComponent, ObservatoryRetentionComponent, ObservatoryAnalyticsComponent],
  providers: [FetchObservatoryTransport, ObservatoryStore, { provide: OBSERVATORY_GATEWAY, useExisting: FetchObservatoryTransport }],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <a class="skip-link" href="#observatory-main">Skip to Observatory content</a>
    <header class="masthead">
      <div><p class="eyebrow">Aura Observatory</p><h1>Operational metadata</h1><p class="subtitle">Owner-only monitoring for runs, traces, component health, and ingest.</p></div>
      @if (store.authState() === 'authenticated') { <div class="header-actions"><div class="connection" role="status"><span class="pulse" [class]="'pulse-' + store.snapshot().state" aria-hidden="true"></span>{{ connectionLabel() }}</div><button type="button" (click)="signOut()">Sign out</button></div> }
    </header>
    @if (store.authState() === 'checking') {
      <main id="observatory-main" tabindex="-1" class="signed-out" aria-busy="true"><h2>Checking your Observatory session</h2><p role="status">Your owner session is being checked.</p></main>
    } @else if (store.authState() === 'authenticated') {
      <nav class="tabs" aria-label="Observatory sections"><button type="button" [class.active]="tab() === 'overview'" [attr.aria-current]="tab() === 'overview' ? 'page' : null" (click)="tab.set('overview')">Overview</button><button type="button" [class.active]="tab() === 'runs'" [attr.aria-current]="tab() === 'runs' ? 'page' : null" (click)="tab.set('runs')">Runs</button><button type="button" [class.active]="tab() === 'trace'" [attr.aria-current]="tab() === 'trace' ? 'page' : null" (click)="tab.set('trace')">Trace</button><button type="button" [class.active]="tab() === 'components'" [attr.aria-current]="tab() === 'components' ? 'page' : null" (click)="showComponents()">Components</button><button type="button" [class.active]="tab() === 'analytics'" [attr.aria-current]="tab() === 'analytics' ? 'page' : null" (click)="tab.set('analytics')">Analytics</button><button type="button" [class.active]="tab() === 'retention'" [attr.aria-current]="tab() === 'retention' ? 'page' : null" (click)="tab.set('retention')">Retention</button></nav>
      <main id="observatory-main" tabindex="-1"><observatory-overview [hidden]="tab() !== 'overview'" /><observatory-runs [hidden]="tab() !== 'runs'" (runSelected)="showTrace()" /><observatory-trace [hidden]="tab() !== 'trace'" /><observatory-components [hidden]="tab() !== 'components'" /><observatory-analytics [hidden]="tab() !== 'analytics'" /><observatory-retention [hidden]="tab() !== 'retention'" /></main>
    } @else {
      <main id="observatory-main" tabindex="-1" class="signed-out" aria-labelledby="auth-recovery-title"><div class="sr-only" role="status" aria-live="polite">{{ authCopy().announcement }}</div><h2 #authHeading id="auth-recovery-title" tabindex="-1">{{ authCopy().title }}</h2><p>{{ authCopy().message }}</p><div class="recovery-actions">@if (authCopy().retryAction === 'logout') { <button class="primary-action" type="button" (click)="retryLogout()">{{ authCopy().action }}</button><a class="secondary-action" [href]="ownerLoginHref('', returnPath())">Sign in as owner</a> } @else if (authCopy().retryAction === 'session') { <button class="primary-action" type="button" (click)="retryAuthentication()">{{ authCopy().action }}</button><a class="secondary-action" [href]="ownerLoginHref('', returnPath())">Sign in as owner</a> } @else { <a class="primary-action" [href]="ownerLoginHref('', returnPath())">{{ authCopy().action }}</a> }</div></main>
    }
  `,
  styles: [`
    :host { color:var(--text); display:block; min-height:100%; } .skip-link { background:var(--text); color:var(--canvas); left:.75rem; padding:.55rem .8rem; position:fixed; top:0; transform:translateY(-130%); z-index:5; } .skip-link:focus { transform:translateY(0); } .masthead { align-items:flex-start; display:flex; flex-wrap:wrap; gap:1rem; justify-content:space-between; margin:0 auto; max-width:82rem; padding:clamp(1.4rem,4vw,3rem) clamp(1rem,4vw,3rem) 1.3rem; } .eyebrow { color:var(--accent-hover); font-size:.75rem; font-weight:800; letter-spacing:.1em; margin:0 0 .45rem; } h1 { font-size:clamp(1.7rem,3vw,2.5rem); letter-spacing:-.04em; margin:0; } .subtitle { color:var(--text-muted); margin:.55rem 0 0; } .header-actions { align-items:center; display:flex; flex-wrap:wrap; gap:.65rem; } .header-actions button { background:transparent; border:1px solid var(--control-border); border-radius:.45rem; color:var(--text); cursor:pointer; padding:.45rem .7rem; } .header-actions button:focus-visible,.primary-action:focus-visible,.recovery-actions button:focus-visible,.secondary-action:focus-visible { outline:3px solid var(--focus); outline-offset:3px; } .connection { align-items:center; background:var(--surface); border:1px solid var(--border); border-radius:999px; display:flex; gap:.5rem; padding:.45rem .7rem; white-space:nowrap; } .pulse { border-radius:50%; height:.55rem; width:.55rem; } .pulse-ready,.pulse-empty { background:var(--success); } .pulse-loading,.pulse-stale { background:#c58b27; } .pulse-disconnected,.pulse-error { background:var(--danger); } .tabs { border-bottom:1px solid var(--border); display:flex; gap:.2rem; margin:0 auto; max-width:82rem; overflow:auto; padding:0 clamp(1rem,4vw,3rem); } .tabs button { background:transparent; border:0; border-bottom:3px solid transparent; color:var(--text-muted); cursor:pointer; padding:.75rem 1rem; white-space:nowrap; } .tabs button.active { border-bottom-color:var(--accent); color:var(--text); font-weight:750; } .tabs button:focus-visible { outline:3px solid var(--focus); outline-offset:-3px; } main { margin:0 auto; max-width:82rem; padding:1.4rem clamp(1rem,4vw,3rem) 3rem; } .signed-out { background:var(--surface); border:1px solid var(--border); border-radius:1rem; margin-top:1rem; max-width:44rem; } .signed-out h2 { margin-top:0; width:fit-content; max-width:100%; } .signed-out h2[tabindex="-1"]:focus { outline:3px solid var(--focus); outline-offset:4px; border-radius:.25rem; } .recovery-actions { align-items:center; display:flex; flex-wrap:wrap; gap:.75rem; margin-top:1.25rem; } .primary-action,.recovery-actions button { background:var(--accent); border:0; border-radius:.45rem; color:var(--on-accent); cursor:pointer; display:inline-block; padding:.65rem .85rem; text-decoration:none; } .secondary-action { background:transparent; border:1px solid var(--control-border); border-radius:.45rem; color:var(--text); cursor:pointer; display:inline-block; padding:.65rem .85rem; text-decoration:none; } .sr-only { height:1px; margin:-1px; overflow:hidden; padding:0; position:absolute; width:1px; clip:rect(0,0,0,0); white-space:nowrap; } @media (prefers-reduced-motion:reduce) { *,*::before,*::after { scroll-behavior:auto !important; transition-duration:.01ms !important; } } @media (max-width:640px) { .masthead { display:grid; } .header-actions { justify-self:start; } }
  `],
})
class AppComponent {
  readonly store = inject(ObservatoryStore);
  readonly authCopy = () => authCopyForState(this.store.authState());
  readonly ownerLoginHref = ownerLoginHref;
  readonly tab = signal<'overview' | 'runs' | 'trace' | 'components' | 'analytics' | 'retention'>('overview');
  @ViewChild('authHeading') private authHeading?: ElementRef<HTMLHeadingElement>;
  private lastFocusedAuthState = 'checking';
  private focusAuthenticatedLandmark = false;
  constructor() {
    effect(() => {
      const state = this.store.authState();
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
  showTrace(): void { this.tab.set('trace'); setTimeout(() => document.getElementById('trace-title')?.focus(), 0); }
  showComponents(): void { if (this.tab() !== 'components') this.store.refreshCatalog(); this.tab.set('components'); }
  signOut(): void { this.tab.set('overview'); void this.store.logout(); }
  retryAuthentication(): void { this.store.retryAuthentication(); }
  retryLogout(): void { void this.store.logout(); }
  returnPath(): string { return typeof window === 'undefined' ? '/' : `${window.location.pathname}${window.location.search}${window.location.hash}`; }
  connectionLabel(): string { const state = this.store.snapshot().state; return state === 'ready' || state === 'empty' ? 'Live updates on' : state === 'disconnected' ? 'Disconnected' : state === 'stale' ? 'Stale snapshot' : state === 'error' ? 'Observatory unavailable' : 'Connecting'; }
}

bootstrapApplication(AppComponent).catch((error: unknown) => console.error(error));
