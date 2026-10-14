import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { focusBranchAction } from './branch-action-focus';

const template = readFileSync(resolve(process.cwd(), 'libs/aura/interaction/conversations/src/lib/conversation-panel.component.html'), 'utf8');
const componentSource = readFileSync(resolve(process.cwd(), 'libs/aura/interaction/conversations/src/lib/conversation-panel.component.ts'), 'utf8');

describe('conversation configuration select bindings', () => {
  it('keeps branch focus on the action when connected and falls back after replacement', () => {
    const fallback = document.createElement('button');
    fallback.id = 'branch-toolbar-heading';
    document.body.append(fallback);
    const trigger = document.createElement('button');
    document.body.append(trigger);

    focusBranchAction(trigger);
    expect(document.activeElement).toBe(trigger);

    trigger.disabled = true;
    focusBranchAction(trigger);
    expect(document.activeElement).toBe(fallback);

    trigger.remove();
    focusBranchAction(trigger);
    expect(document.activeElement).toBe(fallback);
    fallback.remove();
  });

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

  it('renders live model thinking in a separate inert accessible disclosure', () => {
    expect(template).toContain('<details class="model-thinking"');
    expect(template).toContain('<summary>Model thinking</summary>');
    expect(template).toContain('<pre>{{ thinking.text }}</pre>');
    expect(template).not.toContain('aura-safe-markdown [content]="thinking.text"');
    expect(template).not.toContain('[innerHTML]="thinking.text"');
  });

  it('falls back to the visible shell navigation trigger when the saved opener is hidden', () => {
    expect(componentSource).toContain('export function visibleFocusTarget');
    expect(componentSource).toContain("document.querySelector<HTMLButtonElement>('[aria-label=\"Open navigation\"]')");
    expect(componentSource).toContain('visibleFocusTarget(opener, navigationTrigger)?.focus()');
    expect(componentSource).toContain('if (returnFocus) setTimeout(() => {');
  });

  it('routes conversation approvals to governed review without inline decisions', () => {
    expect(template).toContain(`[routerLink]="['/approvals', approval.id]"`);
    expect(template).toContain('>Review and decide</a>');
    expect(template).not.toContain('(click)="approvalStore.approve');
    expect(template).not.toContain('(click)="approvalStore.reject');
  });

  it('renders validated document citations as internal accessible source links', () => {
    expect(template).toContain('document-citations');
    expect(template).toContain(`[routerLink]="['/documents', citation.documentId]"`);
    expect(template).toContain('[attr.aria-label]="citationAccessibleLabel(citation)"');
    expect(template).toContain('{{ citationLocator(citation) }}');
    expect(template).not.toContain('citation.url');
    expect(template).toContain('(click)="focusCitationDestination()"');
    expect(componentSource).toContain("document.getElementById('detail-heading') ?? document.getElementById('main-content')");
    expect(componentSource).toContain('target.focus()');
  });

  it('exposes an accessible bootstrap-ready seam before draft input is accepted', () => {
    expect(template).toContain('[attr.data-conversation-ready]="conversationReady() ? \'true\' : \'false\'"');
    expect(template).toContain('[attr.aria-busy]="conversationReady() ? \'false\' : \'true\'"');
    expect(componentSource).toContain('this.store.models().some((model) => model.selectable)');
    expect(componentSource).toContain('!this.agentStore.loading() && this.defaultAgentReady()');
  });

  it('renders a branch retry only when the store exposes a retryable action', () => {
    expect(template).toContain('@if (store.selectedBranch().errorAction)');
    expect(template).not.toContain('{{ branchError }} <button type="button" class="inline-action" (click)="retryBranchAction()">');
  });
});
