import { describe, expect, it } from 'vitest';
import { RESEARCH_MODES, researchBudgetFieldError, researchModeLabel } from './research-models';

describe('research models', () => {
  it('exposes the contract research modes', () => { expect(RESEARCH_MODES).toContain('quick'); expect(RESEARCH_MODES).toContain('agent_led'); });
  it('labels agent-led mode for accessible UI', () => { expect(researchModeLabel('agent_led')).toBe('Agent-led'); });
  it('keeps valid extension fields while rejecting cumulative budget violations', () => {
    expect(researchBudgetFieldError('fetchedSources', 7, 15, 6)).toBeUndefined();
    expect(researchBudgetFieldError('browserPages', 0, 15)).toBeUndefined();
    expect(researchBudgetFieldError('wallTimeSeconds', 0, 15)).toContain('at least 1');
    expect(researchBudgetFieldError('fetchedSources', 0, 15)).toContain('at least 1');
    expect(researchBudgetFieldError('searchRequests', 0, 15)).toContain('at least 1');
    expect(researchBudgetFieldError('modelCalls', 0, 15)).toContain('at least 1');
    expect(researchBudgetFieldError('fetchedSources', 5, 15, 6)).toContain('admitted');
    expect(researchBudgetFieldError('modelCalls', 16, 15, 3)).toContain('exceed');
  });
});
