import { ChangeDetectionStrategy, Component, ElementRef, ViewChild, inject } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { LoadingStateComponent, StatusMessageComponent } from '@aura/shared/ui';
import { ConversationStore } from './conversation-store';

@Component({
  selector: 'aura-conversation-panel',
  standalone: true,
  imports: [FormsModule, LoadingStateComponent, StatusMessageComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './conversation-panel.component.html',
  styleUrl: './conversation-panel.component.css',
})
export class ConversationPanelComponent {
  readonly store = inject(ConversationStore);
  @ViewChild('composer') private readonly composer?: ElementRef<HTMLTextAreaElement>;

  submit(event?: Event): void { event?.preventDefault(); if (this.store.send()) queueMicrotask(() => this.composer?.nativeElement.focus()); }
  handleKeydown(event: KeyboardEvent): void { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); this.submit(event); } }
}
