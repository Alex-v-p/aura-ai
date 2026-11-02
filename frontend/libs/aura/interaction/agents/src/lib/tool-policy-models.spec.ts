import { describe, expect, it } from 'vitest';
import { DOCUMENT_SEARCH_TOOL_ID, DOCUMENT_SEARCH_TOOL_VERSION, InMemoryToolPolicyApi, MEMORY_SEARCH_TOOL_ID, MEMORY_SEARCH_TOOL_VERSION, grantsDocumentSearch, grantsMemorySearch, toolPolicyAuthorizationStatus, toolPolicyRecoveryNotice } from './tool-policy-models';

describe('tool policy model', () => {
  it('recognizes only the versioned memory search grant', () => {
    expect(grantsMemorySearch({ id: 'p', agentProfileId: 'a', revision: 1, grants: [{ toolId: MEMORY_SEARCH_TOOL_ID, toolVersion: MEMORY_SEARCH_TOOL_VERSION }], maxCallsPerRun: 1, timeoutMs: 30_000, retryLimit: 0, createdAt: new Date().toISOString() })).toBe(true);
    expect(grantsMemorySearch({ id: 'p', agentProfileId: 'a', revision: 1, grants: [{ toolId: MEMORY_SEARCH_TOOL_ID, toolVersion: 2 }], maxCallsPerRun: 1, timeoutMs: 30_000, retryLimit: 0, createdAt: new Date().toISOString() })).toBe(false);
  });

  it('recognizes the additive document search grant without treating other versions as enabled', () => {
    expect(grantsDocumentSearch({ id: 'p', agentProfileId: 'a', revision: 2, grants: [{ toolId: DOCUMENT_SEARCH_TOOL_ID, toolVersion: DOCUMENT_SEARCH_TOOL_VERSION }], maxCallsPerRun: 1, timeoutMs: 30_000, retryLimit: 0, createdAt: new Date().toISOString() })).toBe(true);
    expect(grantsDocumentSearch({ id: 'p', agentProfileId: 'a', revision: 2, grants: [{ toolId: DOCUMENT_SEARCH_TOOL_ID, toolVersion: 2 }], maxCallsPerRun: 1, timeoutMs: 30_000, retryLimit: 0, createdAt: new Date().toISOString() })).toBe(false);
  });

  it('keeps the document search catalog descriptor aligned with Core authorization semantics', async () => {
    const catalog = await new InMemoryToolPolicyApi().listCatalog();
    const descriptor = catalog.items.find((item) => item.id === DOCUMENT_SEARCH_TOOL_ID && item.version === DOCUMENT_SEARCH_TOOL_VERSION);
    expect(descriptor).toMatchObject({ permissionScope: 'documents:read', sensitivity: 'restricted' });
  });

  it('seeds Aura and keeps custom agents deny-by-default until attached', async () => {
    const api = new InMemoryToolPolicyApi();
    const aura = await api.listPolicies('7e1d98ee-65b2-5bc7-9ca9-edd7ad85b540');
    expect(aura.items[0] && grantsMemorySearch(aura.items[0])).toBe(true);
    expect(aura.items[0] && grantsDocumentSearch(aura.items[0])).toBe(true);
    const custom = await api.listPolicies('custom-agent');
    expect(custom.items).toEqual([]);
    const policy = await api.createPolicy('custom-agent', [{ toolId: MEMORY_SEARCH_TOOL_ID, toolVersion: MEMORY_SEARCH_TOOL_VERSION }], 0);
    await api.attachPolicy('custom-agent', policy.id, custom.agentVersion);
    const attached = await api.listPolicies('custom-agent');
    expect(attached.attachedPolicyRevisionId).toBe(policy.id);
    expect(attached.items[0] && grantsMemorySearch(attached.items[0])).toBe(true);
  });

  it.each([401, 403] as const)('classifies catalog authorization failures for %s recovery', (status) => {
    expect(toolPolicyAuthorizationStatus({ status })).toBe(status);
    expect(toolPolicyRecoveryNotice(status)).toContain(status === 401 ? 'session has expired' : 'permission');
  });
});
