import { Injectable, inject, signal } from '@angular/core';
import type { AgentToolPolicy, ToolDescriptorSummary, ToolGrant } from '@aura/aura-api-client';
import { AuraToolPolicyApi } from './tool-policy-api';
import { TOOL_POLICY_API, type ToolPolicyApi } from './tool-policy-models';

@Injectable({ providedIn: 'root' })
export class ToolPolicyStore {
  private readonly api: ToolPolicyApi = inject(TOOL_POLICY_API, { optional: true }) ?? inject(AuraToolPolicyApi);
  readonly catalog = signal<ReadonlyArray<ToolDescriptorSummary>>([]);
  readonly policies = signal<ReadonlyArray<AgentToolPolicy>>([]);
  readonly attachedPolicyRevisionId = signal<string | null>(null);
  readonly agentVersion = signal(0);
  readonly loading = signal(false);
  readonly saving = signal(false);
  readonly notice = signal<string | null>(null);

  async load(agentProfileId: string): Promise<void> {
    this.loading.set(true);
    this.notice.set(null);
    try {
      const [catalog, policies] = await Promise.all([this.api.listCatalog(), this.api.listPolicies(agentProfileId)]);
      this.catalog.set(catalog.items);
      this.policies.set(policies.items);
      this.attachedPolicyRevisionId.set(policies.attachedPolicyRevisionId || null);
      this.agentVersion.set(policies.agentVersion);
    } catch (error: unknown) {
      this.notice.set(message(error, 'We could not load this agent tool policy.'));
    } finally { this.loading.set(false); }
  }

  async createAndAttach(agentProfileId: string, grants: ReadonlyArray<ToolGrant>): Promise<boolean> {
    this.saving.set(true);
    this.notice.set(null);
    try {
      const policy = await this.api.createPolicy(agentProfileId, grants, this.policies().at(-1)?.revision ?? 0);
      await this.api.attachPolicy(agentProfileId, policy.id, this.agentVersion());
      await this.load(agentProfileId);
      return true;
    } catch (error: unknown) {
      const notice = message(error, 'We could not attach that tool policy. Refresh and try again.');
      await this.load(agentProfileId);
      this.notice.set(notice);
      return false;
    } finally { this.saving.set(false); }
  }

  clearNotice(): void { this.notice.set(null); }
}

function message(error: unknown, fallback: string): string { return error instanceof Error ? error.message : fallback; }
