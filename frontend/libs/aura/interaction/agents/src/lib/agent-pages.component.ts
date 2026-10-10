import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { AgentDraft, AgentProfile } from './agent-models';
import { AgentStore } from './agent-store';
import { PersonaStore } from '@aura/aura/interaction/personas';

@Component({ selector: 'aura-agent-list-page', standalone: true, imports: [RouterLink], changeDetection: ChangeDetectionStrategy.OnPush, template: `
  <section class="feature-page"><header class="feature-heading"><div><p class="eyebrow">Configuration</p><h1>Agents</h1><p class="lede">Create focused, versioned configurations for your conversations.</p></div><a class="primary-button" routerLink="/agents/new">Create agent</a></header>
  @if (store.notice()) { <p class="notice" role="alert">{{ store.notice() }}</p> }
  @if (store.loading()) { <p class="empty-state" role="status">Loading agents…</p> } @else { <div class="card-grid">@for (agent of store.agents(); track agent.id) { <a class="feature-card" [routerLink]="['/agents', agent.id]"><div class="card-top"><span class="status" [class.disabled]="agent.status === 'disabled'">{{ agent.status }}</span><span class="revision">r{{ agent.revisions.length }}</span></div><h2>{{ agent.displayName }}</h2><p>{{ agent.revisions.at(-1)?.purpose }}</p><span class="card-link">View revisions →</span></a> } @empty { <p class="empty-state">No agents yet. Create one to get started.</p> }</div> }
  </section>` , styleUrl: './agent-pages.component.css' })
export class AgentListPageComponent { readonly store = inject(AgentStore); }

@Component({ selector: 'aura-agent-detail-page', standalone: true, imports: [DatePipe, FormsModule, RouterLink], changeDetection: ChangeDetectionStrategy.OnPush, template: `
  @if (agent(); as current) { <section class="feature-page detail-page"><a class="back-link" routerLink="/agents">← Agents</a><header class="feature-heading"><div><p class="eyebrow">Agent configuration</p><h1>{{ current.displayName }}</h1><p class="lede">{{ current.status === 'active' ? 'Available for new runs.' : 'Disabled. Existing history remains readable.' }}</p></div><div class="form-actions"><a class="secondary-button" [routerLink]="['/agents', current.id, 'memory']">Memory policy</a><button class="secondary-button" type="button" (click)="store.setStatus(current.id, current.status === 'active' ? 'disabled' : 'active')">{{ current.status === 'active' ? 'Disable agent' : 'Enable agent' }}</button></div></header>
  @if (store.notice()) { <p class="notice" role="alert">{{ store.notice() }}</p> }<section class="detail-card"><div class="section-heading"><div><h2>Revision history</h2><p>Revisions are immutable; new work starts only after an explicit upgrade.</p></div><button class="primary-button" type="button" (click)="showRevision.set(!showRevision())">{{ showRevision() ? 'Cancel' : 'Create revision' }}</button></div>
  @if (showRevision()) { <form class="editor" (ngSubmit)="saveRevision(current)"><label>Display name<input name="displayName" [(ngModel)]="draft.displayName" required /></label><label>Purpose<textarea name="purpose" [(ngModel)]="draft.purpose" required></textarea></label><label>Behavioral instructions<textarea name="instructions" [(ngModel)]="draft.behavioralInstructions" required></textarea></label><label>Persona revision<select name="personaRevisionId" [(ngModel)]="draft.personaRevisionId" required [disabled]="personaStore.loading() || personaRevisions().length === 0"><option value="" disabled>Select a reusable persona revision</option>@for (persona of personaRevisions(); track persona.id) { <option [value]="persona.id">{{ persona.displayName }} r{{ persona.revision }}</option> }</select></label>@if (personaStore.notice()) { <p class="form-help" role="alert">{{ personaStore.notice() }}</p> } @else if (personaRevisions().length === 0 && !personaStore.loading()) { <p class="form-help" role="alert">No active persona revisions are available. Enable or create a persona before saving this revision.</p> }<p class="form-help">Persona and model policy references stay inside the governed configuration boundary.</p><button class="primary-button" type="submit" [disabled]="!canSaveRevision()">Save immutable revision</button></form> }
  <ol class="revision-list">@for (revision of current.revisions; track revision.revisionId) { <li><div><strong>{{ revision.displayName }} r{{ revision.revision }}</strong><span>{{ revision.createdAt | date:'mediumDate' }}</span></div><p>{{ revision.purpose }}</p><small>Persona revision {{ revision.personaRevisionId }}</small></li> }</ol></section></section> } @else { <p class="empty-state">Loading agent…</p> }`, styleUrl: './agent-pages.component.css' })
export class AgentDetailPageComponent {
  private readonly route = inject(ActivatedRoute);
  readonly store = inject(AgentStore);
  readonly personaStore = inject(PersonaStore);
  readonly personaRevisions = computed(() => this.personaStore.activePersonas().flatMap((persona) => persona.revisions));
  readonly agent = computed(() => this.store.find(this.route.snapshot.paramMap.get('id') ?? ''));
  readonly showRevision = signal(false);
  draft: AgentDraft = { displayName: '', purpose: '', behavioralInstructions: '', personaRevisionId: '' };
  constructor() { void this.store.loadDetail(this.route.snapshot.paramMap.get('id') ?? ''); }
  canSaveRevision(): boolean { return Boolean(this.draft.displayName.trim() && this.draft.purpose.trim() && this.draft.behavioralInstructions.trim() && this.draft.personaRevisionId && this.personaRevisions().some((persona) => persona.id === this.draft.personaRevisionId)); }
  saveRevision(current: AgentProfile): void { void this.store.revise(current.id, this.draft).then(() => { this.showRevision.set(false); }); }
}

@Component({ selector: 'aura-agent-create-page', standalone: true, imports: [FormsModule, RouterLink], changeDetection: ChangeDetectionStrategy.OnPush, template: `
  <section class="feature-page detail-page"><a class="back-link" routerLink="/agents">← Agents</a><header class="feature-heading"><div><p class="eyebrow">New configuration</p><h1>Create an agent</h1><p class="lede">Start with the essentials. You can add immutable revisions later.</p></div></header><form class="detail-card editor" (ngSubmit)="save()"><label>Display name<input name="displayName" [(ngModel)]="draft.displayName" required /></label><label>Purpose<textarea name="purpose" [(ngModel)]="draft.purpose" required></textarea></label><label>Behavioral instructions<textarea name="instructions" [(ngModel)]="draft.behavioralInstructions" required></textarea></label><label>Persona revision<select name="personaRevisionId" [(ngModel)]="draft.personaRevisionId" required [disabled]="personaStore.loading() || personaRevisions().length === 0"><option value="" disabled>Select a reusable persona revision</option>@for (persona of personaRevisions(); track persona.id) { <option [value]="persona.id">{{ persona.displayName }} r{{ persona.revision }}</option> }</select></label>@if (personaStore.notice()) { <p class="form-help" role="alert">{{ personaStore.notice() }}</p> } @else if (personaRevisions().length === 0 && !personaStore.loading()) { <p class="form-help" role="alert">No active persona revisions are available. Enable or create a persona before saving this agent.</p> }<p class="form-help">Choose a reusable persona revision. It remains immutable once this agent revision is created.</p><div class="form-actions"><a class="secondary-button" routerLink="/agents">Cancel</a><button class="primary-button" type="submit" [disabled]="!canSave()">Create agent</button></div></form></section>`, styleUrl: './agent-pages.component.css' })
export class AgentCreatePageComponent {
  readonly store = inject(AgentStore);
  readonly personaStore = inject(PersonaStore);
  readonly personaRevisions = computed(() => this.personaStore.activePersonas().flatMap((persona) => persona.revisions));
  draft: AgentDraft = { displayName: '', purpose: '', behavioralInstructions: '', personaRevisionId: '' };
  canSave(): boolean { return Boolean(this.draft.displayName.trim() && this.draft.purpose.trim() && this.draft.behavioralInstructions.trim() && this.draft.personaRevisionId && this.personaRevisions().some((persona) => persona.id === this.draft.personaRevisionId)); }
  save(): void { void this.store.create(this.draft); }
}
