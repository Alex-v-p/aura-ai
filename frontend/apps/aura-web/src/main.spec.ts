import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const source = readFileSync('apps/aura-web/src/main.ts', 'utf8');

describe('Aura Web session composition', () => {
  it('registers the workflow store with the session reset boundary', () => {
    expect(source).toContain("import('@aura/aura/automation/workflows')");
    expect(source).toContain('workflowStore = injector.get(WorkflowStore);');
    expect(source).toContain('resetWorkflowStore();');
  });
});
