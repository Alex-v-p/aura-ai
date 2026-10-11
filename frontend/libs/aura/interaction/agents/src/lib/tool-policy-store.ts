import { Injectable, inject, signal } from '@angular/core';
import type { AgentToolPolicy, ToolDescriptorSummary, ToolGrant } from '@aura/aura-api-client';
import { AuraToolPolicyApi } from './tool-policy-api';
import { TOOL_POLICY_API, toolPolicyAuthorizationStatus, toolPolicyRecoveryNotice, type ToolPolicyApi } from './tool-policy-models';

type SessionAuthorizationStatus = 401 | 403;

@Injectable({ providedIn: 'root' })
export class ToolPolicyStore {
  private readonly api: ToolPolicyApi = inject(TOOL_POLICY_API, { optional: true }) ?? inject(AuraToolPolicyApi);
  readonly catalog = signal<ReadonlyArray<ToolDescriptorSummary>>([]);
  readonly policies = signal<ReadonlyArray<AgentToolPolicy>>([]);
  readonly attachedPolicyRevisionId = signal<string | null>(null);
  readonly agentVersion = signal(0);
  readonly loading = signal(false);
  readonly catalogLoading = signal(false);
  readonly catalogError = signal<string | null>(null);
  readonly saving = signal(false);
  readonly notice = signal<string | null>(null);
  readonly resetEpoch = signal(0);
  private requestGeneration = 0;
  private loadRequestGeneration = 0;
  private sessionResetHandler: ((status?: SessionAuthorizationStatus) => void) | null = null;

  async load(agentProfileId: string): Promise<void> {
    const sessionGeneration = this.requestGeneration;
    const generation = ++this.loadRequestGeneration;
    this.loading.set(true);
    this.notice.set(null);
    try {
      const [catalog, policies] = await Promise.all([this.api.listCatalog(), this.api.listPolicies(agentProfileId)]);
      if (sessionGeneration !== this.requestGeneration || generation !== this.loadRequestGeneration) return;
      this.catalog.set(catalog.items);
      this.policies.set(policies.items);
      this.attachedPolicyRevisionId.set(policies.attachedPolicyRevisionId || null);
      this.agentVersion.set(policies.agentVersion);
    } catch (error: unknown) {
      if (sessionGeneration === this.requestGeneration && generation === this.loadRequestGeneration) { this.notice.set(message(error, 'We could not load this agent tool policy.')); this.handleAuthFailure(error); }
    } finally { if (sessionGeneration === this.requestGeneration && generation === this.loadRequestGeneration) this.loading.set(false); }
  }

  /** Load the bounded registry without inventing a profile policy request. */
  async loadCatalog(): Promise<void> {
    const sessionGeneration = this.requestGeneration;
    this.catalogError.set(null);
    this.catalogLoading.set(true);
    try {
      const catalog = await this.api.listCatalog();
      if (sessionGeneration !== this.requestGeneration) return;
      this.catalog.set(catalog.items);
    } catch (error: unknown) {
      if (sessionGeneration === this.requestGeneration) {
        const notice = message(error, 'We could not load available capabilities.');
        this.catalogError.set(notice);
        this.notice.set(notice);
        this.handleAuthFailure(error);
      }
    } finally {
      if (sessionGeneration === this.requestGeneration) this.catalogLoading.set(false);
    }
  }

  async createAndAttach(agentProfileId: string, grants: ReadonlyArray<ToolGrant>): Promise<boolean> {
    const sessionGeneration = this.requestGeneration;
    this.saving.set(true);
    this.notice.set(null);
    try {
      const policy = await this.api.createPolicy(agentProfileId, grants, this.policies().at(-1)?.revision ?? 0);
      if (sessionGeneration !== this.requestGeneration) return false;
      await this.api.attachPolicy(agentProfileId, policy.id, this.agentVersion());
      if (sessionGeneration !== this.requestGeneration) return false;
      await this.load(agentProfileId);
      return true;
    } catch (error: unknown) {
      const notice = message(error, 'We could not attach that tool policy. Refresh and try again.');
      if (sessionGeneration === this.requestGeneration) { await this.load(agentProfileId); this.notice.set(notice); this.handleAuthFailure(error); }
      return false;
    } finally { this.saving.set(false); }
  }

  clearNotice(): void { this.notice.set(null); }
  registerSessionResetHandler(handler: ((status?: SessionAuthorizationStatus) => void) | null): void { this.sessionResetHandler = handler; }
  resetSessionState(): void { ++this.requestGeneration; this.api.clearSession?.(); this.catalog.set([]); this.catalogError.set(null); this.policies.set([]); this.attachedPolicyRevisionId.set(null); this.agentVersion.set(0); this.loading.set(false); this.catalogLoading.set(false); this.saving.set(false); this.notice.set(null); this.resetEpoch.update((value) => value + 1); }
  private handleAuthFailure(error: unknown): void {
    const status = toolPolicyAuthorizationStatus(error);
    if (status === null) return;
    this.resetSessionState();
    this.sessionResetHandler?.(status);
    this.notice.set(toolPolicyRecoveryNotice(status));
  }
}

function message(error: unknown, fallback: string): string { return error instanceof Error ? error.message : fallback; }
