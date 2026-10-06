import { InjectionToken } from '@angular/core';

export type PersonaStatus = 'active' | 'disabled';
export interface PersonaRevision { readonly id: string; readonly profileId: string; readonly revision: number; readonly displayName: string; readonly description: string; readonly behavioralInstructions: string; readonly status: PersonaStatus; readonly createdAt: string; }
export interface PersonaProfile { readonly id: string; readonly displayName: string; readonly status: PersonaStatus; readonly version: number; readonly currentRevisionId: string; readonly revisions: ReadonlyArray<PersonaRevision>; }
export interface PersonaDraft { readonly displayName: string; readonly description: string; readonly behavioralInstructions: string; }
export interface PersonaApi { listPersonas(): Promise<ReadonlyArray<PersonaProfile>>; getPersona(id: string): Promise<PersonaProfile>; createPersona(draft: PersonaDraft): Promise<PersonaProfile>; createRevision(id: string, draft: PersonaDraft, expectedVersion: number): Promise<PersonaProfile>; setStatus(id: string, status: PersonaStatus, expectedVersion: number): Promise<PersonaProfile>; }
export const PERSONA_API = new InjectionToken<PersonaApi>('AURA_PERSONA_API');

export class InMemoryPersonaApi implements PersonaApi {
  private personas: PersonaProfile[] = [this.profile('6d4b7f6c-7b25-4866-a1f0-3be2d0b4c7ab', { displayName: 'Neutral', description: 'A clear, balanced conversational style.', behavioralInstructions: 'Use direct language and a calm, respectful tone.' })];
  listPersonas(): Promise<ReadonlyArray<PersonaProfile>> { return Promise.resolve(this.snapshot()); }
  getPersona(id: string): Promise<PersonaProfile> { const profile = this.personas.find((item) => item.id === id); return profile ? Promise.resolve({ ...profile, revisions: profile.revisions.map((revision) => ({ ...revision })) }) : Promise.reject(new Error('Persona not found.')); }
  createPersona(draft: PersonaDraft): Promise<PersonaProfile> { const profile = this.profile(randomUuid(), draft); this.personas = [...this.personas, profile]; return Promise.resolve(profile); }
  createRevision(id: string, draft: PersonaDraft, expectedVersion: number): Promise<PersonaProfile> { const current = this.require(id); if (current.version !== expectedVersion) return Promise.reject(new Error('This persona changed elsewhere. Refresh and try again.')); const revision = this.revision(id, draft, current.revisions.length + 1); const updated = { ...current, version: current.version + 1, currentRevisionId: revision.id, revisions: [...current.revisions, revision] }; this.replace(updated); return Promise.resolve(updated); }
  setStatus(id: string, status: PersonaStatus, expectedVersion: number): Promise<PersonaProfile> { const current = this.require(id); if (current.version !== expectedVersion) return Promise.reject(new Error('This persona changed elsewhere. Refresh and try again.')); const updated = { ...current, status, version: current.version + 1 }; this.replace(updated); return Promise.resolve(updated); }
  private profile(id: string, draft: PersonaDraft): PersonaProfile { const revision = this.revision(id, draft, 1); return { id, displayName: draft.displayName, status: 'active', version: 1, currentRevisionId: revision.id, revisions: [revision] }; }
  private revision(profileId: string, draft: PersonaDraft, revision: number): PersonaRevision { return { id: randomUuid(), profileId, revision, displayName: draft.displayName, description: draft.description, behavioralInstructions: draft.behavioralInstructions, status: 'active', createdAt: new Date().toISOString() }; }
  private require(id: string): PersonaProfile { const profile = this.personas.find((item) => item.id === id); if (!profile) throw new Error('Persona not found.'); return profile; }
  private replace(updated: PersonaProfile): void { this.personas = this.personas.map((item) => item.id === updated.id ? updated : item); }
  private snapshot(): ReadonlyArray<PersonaProfile> { return this.personas.map((item) => ({ ...item, revisions: item.revisions.map((revision) => ({ ...revision })) })); }
}
function randomUuid(): string { if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID(); return '00000000-0000-4000-8000-' + Math.random().toString(16).slice(2, 14).padEnd(12, '0'); }
