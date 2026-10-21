import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

import { isEvaluationAttempt, isEvaluationDataset, isEvaluationJob, parseEvaluationAttemptPage, parseEvaluationDatasetPage, parseEvaluationLiveEvent } from './evaluation-dto-adapter';

const provenance = {
  createdByComponent: 'aura.observatory.evaluation_api',
  createdByVersion: '1.0.0',
  sourceDigest: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
};

const dataset = {
  schemaVersion: 1,
  resourceType: 'dataset',
  datasetId: '11111111-1111-4111-8111-111111111111',
  datasetVersion: '1.0.0',
  sourceKind: 'synthetic',
  scopeIds: ['private'],
  captureClassification: 'metadata_only',
  caseCount: 2,
  retentionMode: 'manual_only',
  provenance,
  state: 'active',
  createdAt: '2026-10-21T00:00:00Z',
} as const;

const liveEvent = {
  schemaVersion: 1,
  eventId: '22222222-2222-4222-8222-222222222222',
  eventType: 'evaluation.changed',
  occurredAt: '2026-10-21T00:00:00Z',
  resourceType: 'evaluation_dataset',
  resourceId: dataset.datasetId,
  change: 'created',
  state: 'active',
  capturePolicy: 'metadata_only',
} as const;
const executionProvenance = {
  dataset: { resourceId: dataset.datasetId, resourceVersion: dataset.datasetVersion },
  suite: { resourceId: '22222222-2222-4222-8222-222222222222', resourceVersion: '1.0.0' },
  evaluators: [{ evaluatorId: '33333333-3333-4333-8333-333333333333', evaluatorVersion: '1.0.0', kind: 'deterministic' }],
  components: [{ componentId: 'aura.core.api', componentVersion: '1.0.0', codeRevision: 'rev-1', imageDigest: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', configurationRevision: 'config-1' }],
  model: { modelId: 'local-model', modelVersion: '1.0.0', modelDigest: 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc' },
  deploymentRevision: 'deploy-1',
  reproducibility: { seed: 7, clockMode: 'fixed', timeZone: 'UTC', locale: 'en-US', fixtureRevision: 'fixture-1', promptRevision: 'prompt-1', policyRevision: 'policy-1' },
} as const;
const job = {
  schemaVersion: 1, resourceType: 'job', jobId: '44444444-4444-4444-8444-444444444444', suite: executionProvenance.suite, status: 'queued', captureClassification: 'metadata_only', attemptCount: 1, resultCount: 0, artifactCount: 0, deadlineAt: '2026-10-21T01:00:00Z', failureCode: null, provenance: executionProvenance, createdAt: '2026-10-21T00:00:00Z', updatedAt: '2026-10-21T00:00:00Z',
} as const;
const attempt = { attemptId: '55555555-5555-4555-8555-555555555555', jobId: job.jobId, attemptNumber: 1, status: 'queued', startedAt: null, finishedAt: null, nextAttemptAt: null, failureCode: null } as const;

describe('evaluation transport adapters', () => {
  it('accepts a bounded generated dataset page and rejects malformed fields', () => {
    const page = {
      items: [dataset],
      nextCursor: null,
      snapshotAt: '2026-10-21T00:00:00Z',
      partial: false,
    };
    expect(isEvaluationDataset(dataset)).toBe(true);
    expect(parseEvaluationDatasetPage(page)).toEqual(page);
    expect(parseEvaluationDatasetPage({ ...page, items: [{ ...dataset, caseCount: -1 }] })).toBeNull();
    expect(parseEvaluationDatasetPage({ ...page, nextCursor: 'not a bounded cursor' })).toBeNull();
  });

  it('rejects private or arbitrary DTO fields before template access', () => {
    for (const field of ['content', 'prompt', 'rubric', 'comment', 'endpoint', 'credential', 'object', 'bucket', 'filename', 'owner', 'attributes']) {
      expect(isEvaluationDataset({ ...dataset, [field]: 'PRIVATE_CONTENT' }), field).toBe(false);
      expect(isEvaluationDataset({ ...dataset, provenance: { ...provenance, [field]: 'PRIVATE_CONTENT' } }), `provenance.${field}`).toBe(false);
      expect(parseEvaluationDatasetPage({
        items: [dataset],
        nextCursor: null,
        snapshotAt: '2026-10-21T00:00:00Z',
        partial: false,
        [field]: 'PRIVATE_CONTENT',
      }), field).toBeNull();
    }
    expect(parseEvaluationDatasetPage({
      items: [{ ...dataset, provenance: { ...provenance, nested: { comment: 'PRIVATE_CONTENT' } } }],
      nextCursor: null,
      snapshotAt: '2026-10-21T00:00:00Z',
      partial: false,
    })).toBeNull();
  });

  it('accepts only the closed metadata-only live-event envelope', () => {
    expect(parseEvaluationLiveEvent(liveEvent)).toEqual(liveEvent);
    expect(parseEvaluationLiveEvent(JSON.stringify(liveEvent))).toEqual(liveEvent);
    expect(parseEvaluationLiveEvent({ ...liveEvent, prompt: 'PRIVATE_CONTENT' })).toBeNull();
    expect(parseEvaluationLiveEvent({ ...liveEvent, capturePolicy: 'full_content' })).toBeNull();
    expect(parseEvaluationLiveEvent('{malformed-json')).toBeNull();
  });

  it('matches generated job provenance and queued/claimed attempt variants', () => {
    expect(isEvaluationJob(job)).toBe(true);
    expect(isEvaluationJob({ ...job, provenance: provenance })).toBe(false);
    expect(isEvaluationAttempt(attempt)).toBe(true);
    expect(isEvaluationAttempt({ ...attempt, workerComponentId: null })).toBe(true);
    expect(isEvaluationAttempt({ ...attempt, status: 'running', workerComponentId: 'aura.core.worker' })).toBe(true);
    expect(isEvaluationAttempt({ ...attempt, status: 'running' })).toBe(false);
    expect(isEvaluationAttempt({ ...attempt, workerComponentId: 'aura.core.worker' })).toBe(false);
    expect(parseEvaluationAttemptPage({ items: [attempt], nextCursor: null, snapshotAt: '2026-10-21T00:00:00Z', partial: false })).not.toBeNull();
    expect(parseEvaluationAttemptPage({ items: [{ ...attempt, content: 'PRIVATE_CONTENT' }], nextCursor: null, snapshotAt: '2026-10-21T00:00:00Z', partial: false })).toBeNull();
  });

  it('keeps authenticated evaluation state memory-only at the transport and feature boundaries', () => {
    const gateway = readFileSync('libs/observatory/src/lib/observatory-gateway.ts', 'utf8');
    const store = readFileSync('libs/observatory/src/lib/features/evaluation/evaluation-workspace.store.ts', 'utf8');
    const component = readFileSync('libs/observatory/src/lib/features/evaluation/evaluation-workspace.component.ts', 'utf8');
    expect(gateway).toContain("cache: 'no-store'");
    expect(gateway).not.toMatch(/localStorage|sessionStorage|indexedDB|caches\./i);
    expect(store).not.toMatch(/localStorage|sessionStorage|indexedDB|caches\./i);
    expect(store).toContain('clear(): void');
    expect(component).toContain('this.store.clear()');
    expect(component).toContain("authState !== 'authenticated'");
    expect(component).toContain('revision > this.lastEvaluationRevision && authState === \'authenticated\'');
  });
});
