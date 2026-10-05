import { ChangeDetectionStrategy, Component, OnDestroy, effect, inject, signal } from '@angular/core';
import { AuraShellComponent } from './shell/aura-shell.component';
import { ThemePreference } from '@aura/shared/ui';
import { ConversationPanelComponent, ConversationStore } from '@aura/aura/interaction/conversations';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [AuraShellComponent, ConversationPanelComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<aura-shell [conversations]="conversationSummaries()" [theme]="theme()" [collapsed]="navCollapsed()" [mobileOpen]="mobileNavigationOpen()" (newConversation)="store.create()" (conversationSelected)="store.select($event)" (collapseChange)="navCollapsed.set($event)" (mobileOpenChange)="mobileNavigationOpen.set($event)" (themeChange)="setTheme($event)"><aura-conversation-panel /></aura-shell>`,
})
export class AppComponent implements OnDestroy {
  readonly store = inject(ConversationStore);
  readonly theme = signal<ThemePreference>(this.readTheme());
  readonly navCollapsed = signal(false);
  readonly mobileNavigationOpen = signal(false);
  readonly conversationSummaries = () => this.store.conversations().map((conversation) => ({ id: conversation.id, title: conversation.title, active: conversation.id === this.store.selectedId(), status: this.store.runStates()[conversation.id] === 'working' ? 'Working' : this.store.runStates()[conversation.id] === 'error' ? 'Needs attention' : undefined }));
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
  ngOnDestroy(): void { this.mediaQuery?.removeEventListener('change', this.mediaListener); this.themeEffect.destroy(); }
  private readTheme(): ThemePreference { if (typeof localStorage === 'undefined') return 'system'; const value = localStorage.getItem('aura-theme'); return value === 'light' || value === 'dark' ? value : 'system'; }
  private applyTheme(preference: ThemePreference): void { if (typeof document === 'undefined') return; const mode = preference === 'system' ? (this.mediaQuery?.matches ? 'dark' : 'light') : preference; document.documentElement.dataset['theme'] = mode; document.documentElement.style.colorScheme = mode; }
}
