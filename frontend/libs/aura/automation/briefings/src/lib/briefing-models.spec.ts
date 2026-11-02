import { describe, expect, it } from 'vitest';
import { emptyBriefingDraft, isIanaTimeZone, validateBriefingDraft } from './briefing-models';

const capability = { executionPolicyRevisionId: 'policy', executionPolicyRevision: 'briefing-execution-v1', deadlineSeconds: 300, maxOutputTokens: 16384, maxOutputChars: 32768, cadenceKind: 'daily', memoryEnabled: false, toolsEnabled: false } as const;
describe('briefing models', () => {
  it('rejects incomplete drafts while preserving the in-memory shape', () => { const errors = validateBriefingDraft(emptyBriefingDraft(), capability); expect(errors['displayName']).toBeTruthy(); expect(errors['calendarIds']).toBeTruthy(); expect(errors['documentIds']).toBeTruthy(); });
  it('requires a daily, memory-free, tool-free capability', () => { expect(validateBriefingDraft({ ...emptyBriefingDraft(), displayName: 'Morning', timeZone: 'Europe/Brussels', agentRevisionId: 'revision', calendarIds: ['calendar'], documentIds: ['document'] }, capability)).toEqual({}); expect(validateBriefingDraft({ ...emptyBriefingDraft(), displayName: 'Morning', timeZone: 'Europe/Brussels', agentRevisionId: 'revision', calendarIds: ['calendar'], documentIds: ['document'] }, { ...capability, memoryEnabled: true })).toHaveProperty('capability'); });
  it('validates IANA timezone values', () => { expect(isIanaTimeZone('Europe/Brussels')).toBe(true); expect(isIanaTimeZone('not-a-zone')).toBe(false); });
});
