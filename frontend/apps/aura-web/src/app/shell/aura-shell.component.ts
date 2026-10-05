import { A11yModule } from '@angular/cdk/a11y';
import { ChangeDetectionStrategy, Component, ElementRef, EventEmitter, HostListener, Input, OnDestroy, Output, ViewChild, signal } from '@angular/core';
import { AdaptiveLayoutComponent } from '@aura/shared/layout';
import { ThemePreference, ThemeSelectComponent } from '@aura/shared/ui';

export interface ShellConversationSummary { readonly id: string; readonly title: string; readonly active: boolean; readonly status?: string; }

@Component({
  selector: 'aura-shell',
  standalone: true,
  imports: [A11yModule, AdaptiveLayoutComponent, ThemeSelectComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './aura-shell.component.html',
  styleUrl: './aura-shell.component.css',
})
export class AuraShellComponent implements OnDestroy {
  @Input() conversations: ReadonlyArray<ShellConversationSummary> = [];
  @Input() theme: ThemePreference = 'system';
  @Input()
  get collapsed(): boolean { return this.collapsedValue; }
  set collapsed(value: boolean) {
    const changed = this.collapsedValue !== value;
    this.collapsedValue = value;
    if (this.isWideDesktopViewport() && (value || changed)) this.desktopContentReady.set(!value && this.prefersReducedMotion());
  }
  @Input() mobileOpen = false;
  @Output() readonly newConversation = new EventEmitter<void>();
  @Output() readonly conversationSelected = new EventEmitter<string>();
  @Output() readonly collapseChange = new EventEmitter<boolean>();
  @Output() readonly mobileOpenChange = new EventEmitter<boolean>();
  @Output() readonly themeChange = new EventEmitter<ThemePreference>();
  @ViewChild('mobileTrigger') private readonly mobileTrigger?: ElementRef<HTMLButtonElement>;
  readonly isMobileViewport = signal(typeof window !== 'undefined' && window.matchMedia('(max-width: 720px)').matches);
  readonly isWideDesktopViewport = signal(typeof window !== 'undefined' && window.matchMedia('(min-width: 1280px)').matches);
  readonly prefersReducedMotion = signal(typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  readonly desktopContentReady = signal(true);
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
    this.viewportQuery?.removeEventListener('change', this.viewportListener);
    this.wideDesktopQuery?.removeEventListener('change', this.viewportListener);
    this.reducedMotionQuery?.removeEventListener('change', this.viewportListener);
  }

  closeMobileNavigation(restoreFocus = true): void {
    this.mobileOpenChange.emit(false);
    if (restoreFocus && this.isMobileViewport()) queueMicrotask(() => this.mobileTrigger?.nativeElement.focus());
  }

  startNewConversation(): void {
    this.newConversation.emit();
    this.closeMobileNavigation(this.isMobileViewport());
  }

  selectConversation(id: string): void {
    this.conversationSelected.emit(id);
    this.closeMobileNavigation(this.isMobileViewport());
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
    if (!this.mobileOpen) return;
    event.preventDefault();
    this.closeMobileNavigation();
  }
}
