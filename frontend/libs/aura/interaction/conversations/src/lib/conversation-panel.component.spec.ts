import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const template = readFileSync(resolve(process.cwd(), 'libs/aura/interaction/conversations/src/lib/conversation-panel.component.html'), 'utf8');
const componentSource = readFileSync(resolve(process.cwd(), 'libs/aura/interaction/conversations/src/lib/conversation-panel.component.ts'), 'utf8');

describe('conversation configuration select bindings', () => {
  it('binds Agent and Persona controls through ngModel so async options retain persisted revisions', () => {
    expect(template).toContain('[ngModel]="configurationRevisionValue(stagedAgent())"');
    expect(template).toContain('(ngModelChange)="chooseAgent($event)"');
    expect(template).toContain('[ngModel]="configurationRevisionValue(stagedPersona(), stagedUseAgentDefaultPersona())"');
    expect(template).toContain('(ngModelChange)="choosePersona($event)"');
    expect(template).not.toContain('[value]="stagedPersona()?.revisionId');
  });

  it('keeps the agent-default sentinel explicit for the Persona control', () => {
    expect(template).toContain('configurationRevisionValue(stagedPersona(), stagedUseAgentDefaultPersona())');
  });

  it('falls back to the visible shell navigation trigger when the saved opener is hidden', () => {
    expect(componentSource).toContain('export function visibleFocusTarget');
    expect(componentSource).toContain("document.querySelector<HTMLButtonElement>('[aria-label=\"Open navigation\"]')");
    expect(componentSource).toContain('visibleFocusTarget(opener, navigationTrigger)?.focus()');
    expect(componentSource).toContain('if (returnFocus) setTimeout(() => {');
  });
});
