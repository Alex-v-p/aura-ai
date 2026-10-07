import { Injectable, computed, inject, signal } from '@angular/core';
import { PERSONA_API, PersonaApi, PersonaDraft, PersonaProfile, PersonaStatus } from './persona-models';
import { AuraPersonaApi } from './persona-api';

@Injectable({ providedIn: 'root' })
export class PersonaStore {
  private readonly api: PersonaApi = inject(PERSONA_API, { optional: true }) ?? new AuraPersonaApi();
  readonly personas = signal<ReadonlyArray<PersonaProfile>>([]);
  readonly loading = signal(true);
  readonly notice = signal<string | null>(null);
  readonly activePersonas = computed(() => this.personas().filter((persona) => persona.status === 'active'));
  constructor() { void this.load(); }
  async load(): Promise<void> { this.loading.set(true); try { const profiles = await this.api.listPersonas(); this.personas.set(await Promise.all(profiles.map((profile) => this.api.getPersona(profile.id)))); this.notice.set(null); } catch (error: unknown) { this.notice.set(this.message(error)); } finally { this.loading.set(false); } }
  async loadDetail(id: string): Promise<void> { try { this.replace(await this.api.getPersona(id)); } catch (error: unknown) { this.notice.set(this.message(error)); } }
  async create(draft: PersonaDraft): Promise<void> { try { const created = await this.api.createPersona(draft); this.personas.update((items) => [...items, created]); } catch (error: unknown) { this.notice.set(this.message(error)); } }
  async revise(id: string, draft: PersonaDraft): Promise<void> { const current = this.find(id); if (!current) return; try { this.replace(await this.api.createRevision(id, draft, current.version)); } catch (error: unknown) { this.notice.set(this.message(error)); } }
  async setStatus(id: string, status: PersonaStatus): Promise<void> { const current = this.find(id); if (!current) return; try { this.replace(await this.api.setStatus(id, status, current.version)); } catch (error: unknown) { this.notice.set(this.message(error)); } }
  find(id: string): PersonaProfile | undefined { return this.personas().find((persona) => persona.id === id); }
  private replace(updated: PersonaProfile): void { this.personas.update((items) => items.map((item) => item.id === updated.id ? updated : item)); }
  private message(error: unknown): string { return error instanceof Error ? error.message : 'We could not save this persona. Your changes are still here.'; }
}
