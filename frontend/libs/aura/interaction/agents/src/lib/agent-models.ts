import { InjectionToken } from '@angular/core';

export type AgentStatus = 'active' | 'disabled';

export interface AgentReference {
  readonly profileId: string;
  readonly revisionId: string;
  readonly revision: number;
  readonly displayName: string;
  readonly status: AgentStatus;
  readonly newerRevisionAvailable?: boolean;
}

export interface AgentRevision extends AgentReference {
  readonly purpose: string;
  readonly behavioralInstructions: string;
  readonly personaRevisionId: string;
  readonly promptBundleRevisionId: string;
  readonly modelPolicyRevisionId: string;
  readonly createdAt: string;
}

export interface AgentProfile {
  readonly id: string;
  readonly displayName: string;
  readonly status: AgentStatus;
  readonly version: number;
  readonly currentRevisionId: string;
  readonly revisions: ReadonlyArray<AgentRevision>;
}

export interface AgentDraft {
  readonly displayName: string;
  readonly purpose: string;
  readonly behavioralInstructions: string;
  readonly personaRevisionId: string;
}

export interface AgentApi {
  listAgents(): Promise<ReadonlyArray<AgentProfile>>;
  getAgent(profileId: string): Promise<AgentProfile>;
  createAgent(draft: AgentDraft): Promise<AgentProfile>;
  createRevision(profileId: string, draft: AgentDraft, expectedVersion: number): Promise<AgentProfile>;
  setStatus(profileId: string, status: AgentStatus, expectedVersion: number): Promise<AgentProfile>;
}

export const AGENT_API = new InjectionToken<AgentApi>('AURA_AGENT_API');

const now = (): string => new Date().toISOString();

/**
 * A small transport seam keeps feature components independent of HTTP. The
 * generated transport is wired by the application when the API is available;
 * the deterministic in-memory implementation keeps the management surfaces
 * usable in Storybook and unauthenticated shell previews.
 */
export class InMemoryAgentApi implements AgentApi {
  private agents: AgentProfile[] = [this.seedAura()];

  listAgents(): Promise<ReadonlyArray<AgentProfile>> { return Promise.resolve(this.snapshot()); }
  getAgent(profileId: string): Promise<AgentProfile> { const profile = this.agents.find((item) => item.id === profileId); return profile ? Promise.resolve({ ...profile, revisions: profile.revisions.map((revision) => ({ ...revision })) }) : Promise.reject(new Error('Agent not found.')); }

  createAgent(draft: AgentDraft): Promise<AgentProfile> {
    const id = cryptoRandomUuid();
    const profile = this.profile(id, draft, 1);
    this.agents = [...this.agents, profile];
    return Promise.resolve(profile);
  }

  createRevision(profileId: string, draft: AgentDraft, expectedVersion: number): Promise<AgentProfile> {
    const current = this.require(profileId);
    if (current.version !== expectedVersion) return Promise.reject(new Error('This agent changed elsewhere. Refresh and try again.'));
    const revision = this.revision(current, draft, current.revisions.length + 1);
    const updated = { ...current, version: current.version + 1, currentRevisionId: revision.revisionId, revisions: [...current.revisions, revision] };
    this.replace(updated);
    return Promise.resolve(updated);
  }

  setStatus(profileId: string, status: AgentStatus, expectedVersion: number): Promise<AgentProfile> {
    const current = this.require(profileId);
    if (current.version !== expectedVersion) return Promise.reject(new Error('This agent changed elsewhere. Refresh and try again.'));
    const updated = { ...current, status, version: current.version + 1 };
    this.replace(updated);
    return Promise.resolve(updated);
  }

  private seedAura(): AgentProfile {
    return this.profile('7e4d9f2d-9b6f-4d0b-a2ad-0a8c8e6b3f10', { displayName: 'Aura', purpose: 'A steady, practical thinking partner.', behavioralInstructions: 'Be clear, kind, and action-oriented.', personaRevisionId: 'f2f4e37e-3e7b-48c3-b3fb-2d92dfb6a146' }, 1);
  }

  private profile(id: string, draft: AgentDraft, version: number): AgentProfile {
    const revision = this.revision({ id, displayName: draft.displayName, status: 'active' }, draft, 1);
    return { id, displayName: draft.displayName, status: 'active', version, currentRevisionId: revision.revisionId, revisions: [revision] };
  }

  private revision(profile: Pick<AgentProfile, 'id' | 'displayName' | 'status'>, draft: AgentDraft, revision: number): AgentRevision {
    return { profileId: profile.id, revisionId: cryptoRandomUuid(), revision, displayName: draft.displayName, status: profile.status, purpose: draft.purpose, behavioralInstructions: draft.behavioralInstructions, personaRevisionId: draft.personaRevisionId, promptBundleRevisionId: cryptoRandomUuid(), modelPolicyRevisionId: 'model-policy-default', createdAt: now() };
  }

  private require(id: string): AgentProfile { const profile = this.agents.find((item) => item.id === id); if (!profile) throw new Error('Agent not found.'); return profile; }
  private replace(profile: AgentProfile): void { this.agents = this.agents.map((item) => item.id === profile.id ? profile : item); }
  private snapshot(): ReadonlyArray<AgentProfile> { return this.agents.map((profile) => ({ ...profile, revisions: profile.revisions.map((revision) => ({ ...revision })) })); }
}

function cryptoRandomUuid(): string { if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID(); return '00000000-0000-4000-8000-' + Math.random().toString(16).slice(2, 14).padEnd(12, '0'); }
