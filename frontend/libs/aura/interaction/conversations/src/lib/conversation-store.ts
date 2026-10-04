import { Injectable, computed, signal } from '@angular/core';

export type TurnRole = 'user' | 'assistant';
export type RunState = 'idle' | 'working' | 'interrupted' | 'error';

export interface ConversationTurn { readonly id: string; readonly role: TurnRole; readonly text: string; readonly state?: 'partial' | 'interrupted'; }
export interface Conversation { readonly id: string; readonly title: string; readonly turns: ReadonlyArray<ConversationTurn>; readonly updatedAt: number; }

const initialConversation: Conversation = { id: 'welcome', title: 'A thoughtful beginning', turns: [], updatedAt: Date.now() };

@Injectable({ providedIn: 'root' })
export class ConversationStore {
  readonly loading = signal(true);
  readonly conversations = signal<ReadonlyArray<Conversation>>([initialConversation]);
  readonly selectedId = signal(initialConversation.id);
  readonly drafts = signal<Readonly<Record<string, string>>>({ [initialConversation.id]: '' });
  readonly runState = signal<RunState>('idle');
  readonly notice = signal<string | null>(null);
  readonly selected = computed(() => this.conversations().find((conversation) => conversation.id === this.selectedId()) ?? initialConversation);
  readonly draft = computed(() => this.drafts()[this.selectedId()] ?? '');
  private responseTimer: ReturnType<typeof setTimeout> | undefined;
  private failNextResponse = false;
  private lastPrompt: { readonly conversationId: string; readonly text: string } | undefined;

  constructor() {
    queueMicrotask(() => this.loading.set(false));
  }

  select(id: string): void {
    if (this.conversations().some((conversation) => conversation.id === id)) {
      this.stop();
      this.selectedId.set(id);
      this.notice.set(null);
      this.runState.set('idle');
    }
  }

  create(): void {
    this.stop();
    const conversation: Conversation = { id: `conversation-${Date.now()}`, title: 'New conversation', turns: [], updatedAt: Date.now() };
    this.conversations.update((items) => [conversation, ...items]);
    this.drafts.update((drafts) => ({ ...drafts, [conversation.id]: '' }));
    this.selectedId.set(conversation.id);
    this.notice.set(null);
    this.runState.set('idle');
  }

  updateDraft(value: string): void {
    this.drafts.update((drafts) => ({ ...drafts, [this.selectedId()]: value }));
    if (value.trim() && this.runState() === 'idle') this.notice.set(null);
  }

  send(): boolean {
    const text = this.draft().trim();
    if (!text || this.runState() === 'working') { if (!text) this.notice.set('Write a message before sending.'); return false; }
    const id = this.selectedId();
    const userTurn: ConversationTurn = { id: `user-${Date.now()}`, role: 'user', text };
    this.drafts.update((drafts) => ({ ...drafts, [id]: '' }));
    this.conversations.update((items) => items.map((item) => item.id === id ? { ...item, title: item.turns.length === 0 ? this.makeTitle(text) : item.title, turns: [...item.turns, userTurn], updatedAt: Date.now() } : item));
    this.notice.set(null); this.runState.set('working');
    this.lastPrompt = { conversationId: id, text };
    this.scheduleResponse(id, text);
    return true;
  }

  stop(): void {
    if (!this.responseTimer) return;
    clearTimeout(this.responseTimer);
    this.responseTimer = undefined;
    const conversationId = this.lastPrompt?.conversationId ?? this.selectedId();
    this.conversations.update((items) => items.map((item) => item.id === conversationId ? {
      ...item,
      turns: item.turns.map((turn, index) => index === item.turns.length - 1 && turn.role === 'user' ? { ...turn, state: 'interrupted' } : turn),
      updatedAt: Date.now(),
    } : item));
    this.runState.set('interrupted');
    this.notice.set('Generation stopped. Your message is still in the conversation.');
  }
  retry(): void {
    if (this.runState() === 'error' && this.lastPrompt?.conversationId === this.selectedId()) {
      this.notice.set(null);
      this.runState.set('working');
      this.scheduleResponse(this.lastPrompt.conversationId, this.lastPrompt.text);
      return;
    }
    this.notice.set(null);
    this.runState.set('idle');
  }

  /** Test and Storybook fixture hook for the recoverable local error state. */
  failNextLocalReply(): void { this.failNextResponse = true; }

  private scheduleResponse(conversationId: string, prompt: string): void {
    this.responseTimer = setTimeout(() => {
      if (this.failNextResponse) {
        this.failNextResponse = false;
        this.runState.set('error');
        this.notice.set('We could not complete that local preview. Your message is still here. Try again when you are ready.');
        this.responseTimer = undefined;
        return;
      }
      const assistantTurn: ConversationTurn = { id: `assistant-${Date.now()}`, role: 'assistant', text: this.localReply(prompt) };
      this.conversations.update((items) => items.map((item) => item.id === conversationId ? { ...item, turns: [...item.turns, assistantTurn], updatedAt: Date.now() } : item));
      this.runState.set('idle');
      this.responseTimer = undefined;
    }, 850);
  }

  private makeTitle(text: string): string { return text.length > 30 ? `${text.slice(0, 30).trimEnd()}…` : text; }
  private localReply(prompt: string): string { return `I’m here with you. You shared “${prompt}”. This local preview keeps the conversation in memory while the Aura service boundary is being built.`; }
}
