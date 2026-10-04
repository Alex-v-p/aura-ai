import { A11yModule } from '@angular/cdk/a11y';
import { ChangeDetectionStrategy, Component, ElementRef, EventEmitter, HostListener, Input, OnDestroy, Output, ViewChild, signal } from '@angular/core';
import { AdaptiveLayoutComponent } from '@aura/shared/layout';
import { ThemePreference, ThemeSelectComponent } from '@aura/shared/ui';

export interface ShellConversationSummary { readonly id: string; readonly title: string; readonly active: boolean; }

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
  @Input() collapsed = false;
  @Input() mobileOpen = false;
  @Output() readonly newConversation = new EventEmitter<void>();
  @Output() readonly conversationSelected = new EventEmitter<string>();
  @Output() readonly collapseChange = new EventEmitter<boolean>();
  @Output() readonly mobileOpenChange = new EventEmitter<boolean>();
  @Output() readonly themeChange = new EventEmitter<ThemePreference>();
  @ViewChild('mobileTrigger') private readonly mobileTrigger?: ElementRef<HTMLButtonElement>;
  readonly isMobileViewport = signal(typeof window !== 'undefined' && window.matchMedia('(max-width: 720px)').matches);
  private readonly viewportQuery = typeof window === 'undefined' ? null : window.matchMedia('(max-width: 720px)');
  private readonly viewportListener = (): void => {
    const isMobile = this.viewportQuery?.matches ?? false;
    this.isMobileViewport.set(isMobile);
    if (!isMobile && this.mobileOpen) this.mobileOpenChange.emit(false);
  };

  constructor() {
    this.viewportQuery?.addEventListener('change', this.viewportListener);
  }

  ngOnDestroy(): void {
    this.viewportQuery?.removeEventListener('change', this.viewportListener);
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

  @HostListener('document:keydown.escape', ['$event'])
  handleEscape(event: Event): void {
    if (!this.mobileOpen) return;
    event.preventDefault();
    this.closeMobileNavigation();
  }
}
