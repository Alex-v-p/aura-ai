import { describe, expect, it } from 'vitest';
import { emptyWorkflowDraft, newWorkflowStep, validateWorkflowDraft, workflowLoginHref } from './workflow-models';

describe('workflow authoring model', () => {
  it('limits authoring to the four sequential step kinds', () => {
    const draft = { ...emptyWorkflowDraft(), displayName: 'Owner handoff', inputFields: [{ name: 'when', valueType: 'instant' as const }], steps: [newWorkflowStep('wait_until', [{ name: 'when', valueType: 'instant' }]), newWorkflowStep('owner_approval')] };
    expect(validateWorkflowDraft(draft)).toEqual({});
    expect(newWorkflowStep('agent_run')).toMatchObject({ kind: 'agent_run', input: { source: 'instance_input' } });
  });

  it('rejects duplicate inputs and mappings to future or missing values', () => {
    const draft = { ...emptyWorkflowDraft(), displayName: 'Invalid', inputFields: [{ name: 'value', valueType: 'bounded_text' as const }, { name: 'value', valueType: 'bounded_text' as const }], steps: [{ kind: 'owner_notification' as const, content: { source: 'step_output' as const, stepOrdinal: 2, field: 'output' as const } }] };
    const errors = validateWorkflowDraft(draft);
    expect(errors['inputFields.1.name']).toContain('unique');
    expect(errors['steps.0.mapping']).toContain('earlier');
  });

  it('requires wait mappings to use instant inputs', () => {
    const draft = { ...emptyWorkflowDraft(), displayName: 'Wait', inputFields: [{ name: 'message', valueType: 'bounded_text' as const }], steps: [{ kind: 'wait_until' as const, dueAt: { source: 'instance_input' as const, field: 'message' } }] };
    expect(validateWorkflowDraft(draft)['steps.0.mapping']).toContain('instant');
  });

  it('builds a same-origin login recovery link with a bounded return path', () => {
    expect(workflowLoginHref()).toBe('/api/v1/auth/login?return_to=%2Fworkflows');
    expect(workflowLoginHref('/workflows/abc?tab=instances')).toBe('/api/v1/auth/login?return_to=%2Fworkflows%2Fabc%3Ftab%3Dinstances');
    expect(workflowLoginHref('https://evil.example/redirect')).toBe('/api/v1/auth/login?return_to=%2Fworkflows');
  });
});
