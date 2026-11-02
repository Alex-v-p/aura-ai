import { ChangeDetectionStrategy, Component, effect, HostListener, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { WorkflowStore } from './workflow-store';
import { workflowApiLoginHref } from './workflow-api';
import { WORKFLOW_STEP_KINDS, workflowStepKindLabel, workflowStatusLabel, type WorkflowSelector, type WorkflowStep, type WorkflowStepKind } from './workflow-models';

@Component({
  selector: 'aura-workflow-studio',
  standalone: true,
  imports: [DatePipe, FormsModule, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './workflow-studio.component.html',
  styleUrl: './workflow-studio.component.css',
})
export class WorkflowStudioComponent {
  readonly store = inject(WorkflowStore);
  readonly stepKinds = WORKFLOW_STEP_KINDS;
  readonly stepKindLabel = workflowStepKindLabel;
  readonly statusLabel = workflowStatusLabel;
  readonly authoringOpen = signal(false);
  readonly manualInputValues = signal<Record<string, string>>({});
  readonly manualInputErrors = signal<Readonly<Record<string, string>>>({});
  readonly activeStepIndex = signal(0);
  readonly editorAnnouncement = signal('');
  readonly publishReviewOpen = signal(false);
  readonly loginHref = workflowApiLoginHref('/workflows');
  readonly deletionScopes = ['draft', 'retired_definition', 'published_revision', 'terminal_instance_state', 'terminal_instance_history', 'terminal_instance_result', 'terminal_instance_checkpoints'] as const;
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private publishReviewReturnFocus: HTMLElement | null = null;
  private readonly publishReviewBackground = new Map<HTMLElement, { readonly inert: boolean; readonly ariaHidden: string | null }>();

  constructor() {
    effect(() => { if (this.store.resetEpoch() > 0) { this.store.stopLive(); this.manualInputValues.set({}); this.manualInputErrors.set({}); } });
    void this.store.load();
    void this.store.loadAgents();
    this.route.paramMap.subscribe((params) => {
      const workflowId = params.get('id');
      const instanceId = params.get('instanceId');
      const isNew = this.route.snapshot.url.some((segment) => segment.path === 'new');
      if (isNew) { this.authoringOpen.set(true); this.store.beginDraft(); return; }
      this.authoringOpen.set(false);
      if (workflowId) void this.store.inspect(workflowId).then(() => { if (instanceId) void this.store.inspectInstance(workflowId, instanceId); });
    });
    this.store.startLive();
  }
  newWorkflow(): void { this.authoringOpen.set(true); this.store.beginDraft(); void this.router.navigate(['/workflows/new']); }
  addStep(kind: WorkflowStepKind): void { const index = this.store.authoring().steps.length; this.store.addStep(kind); this.activeStepIndex.set(index); this.editorAnnouncement.set(`Step ${index + 1} added.`); this.focusStep(index); }
  moveStep(index: number, direction: -1 | 1): void { this.store.moveStep(index, direction); const target = index + direction; this.activeStepIndex.set(target); this.editorAnnouncement.set(`Step moved to position ${target + 1}.`); this.focusStep(target); }
  cloneStep(index: number): void { this.store.cloneStep(index); const target = index + 1; this.activeStepIndex.set(target); this.editorAnnouncement.set(`Step cloned to position ${target + 1}.`); this.focusStep(target); }
  removeStep(index: number): void { if (!this.confirm('Remove workflow step', `Remove step ${index + 1}? Its mapping will be discarded.`)) return; this.store.removeStep(index); const remaining = this.store.authoring().steps.length; const target = remaining ? Math.min(index, remaining - 1) : 0; this.activeStepIndex.set(target); this.editorAnnouncement.set(remaining ? `Step removed. Focus returned to step ${target + 1}.` : 'Step removed. Add a step to continue.'); this.focusStep(remaining ? target : -1); }
  isMobileEditor(): boolean { return typeof window !== 'undefined' && window.matchMedia('(max-width: 480px)').matches; }
  isStepVisible(index: number): boolean { const active = this.activeStepIndex(); return index === active || index === active - 1; }
  nextStep(): void { const active = this.activeStepIndex(); if (active >= this.store.authoring().steps.length - 1) { this.editorAnnouncement.set(`Step ${active + 1} is the final step.`); this.focusStep(active); return; } this.showStep(active + 1); }
  showStep(index: number): void { this.activeStepIndex.set(Math.max(0, Math.min(index, this.store.authoring().steps.length - 1))); this.focusStep(this.activeStepIndex()); }
  updateDisplayName(value: string): void { this.store.setAuthoring({ displayName: value }); }
  updateAgentRevision(index: number, value: string): void { const step = this.store.authoring().steps[index]; if (!step || step.kind !== 'agent_run') return; const steps = [...this.store.authoring().steps]; steps[index] = { ...step, agentRevisionId: value }; this.store.setSteps(steps); }
  updateInstructions(index: number, value: string): void { const step = this.store.authoring().steps[index]; if (!step || step.kind !== 'agent_run') return; const steps = [...this.store.authoring().steps]; steps[index] = { ...step, instructions: value }; this.store.setSteps(steps); }
  updateInputField(index: number, value: string): void { const step = this.store.authoring().steps[index]; if (!step || step.kind !== 'agent_run' || step.input.source !== 'instance_input') return; const steps = [...this.store.authoring().steps]; steps[index] = { ...step, input: { source: 'instance_input', field: value } }; this.store.setSteps(steps); }
  updateNotificationField(index: number, value: string): void { const step = this.store.authoring().steps[index]; if (!step || step.kind !== 'owner_notification' || step.content.source !== 'instance_input') return; const steps = [...this.store.authoring().steps]; steps[index] = { ...step, content: { source: 'instance_input', field: value } }; this.store.setSteps(steps); }
  updateWaitField(index: number, value: string): void { const step = this.store.authoring().steps[index]; if (!step || step.kind !== 'wait_until' || step.dueAt.source !== 'instance_input') return; const steps = [...this.store.authoring().steps]; steps[index] = { ...step, dueAt: { source: 'instance_input', field: value } }; this.store.setSteps(steps); }
  updateMapping(index: number, value: string, target: 'input' | 'content' | 'dueAt'): void { const step = this.store.authoring().steps[index]; if (!step || !value) return; const mapping = !value.startsWith('step:') ? { source: 'instance_input' as const, field: value.replace(/^input:/, '') } : (() => { const [, ordinal, field] = value.split(':'); return { source: 'step_output' as const, stepOrdinal: Number(ordinal), field: field as 'output' | 'decision' | 'decided_at' | 'notification_id' | 'status' | 'resumed_at' }; })(); const steps = [...this.store.authoring().steps]; if (target === 'input' && step.kind === 'agent_run') steps[index] = { ...step, input: mapping }; else if (target === 'content' && step.kind === 'owner_notification') steps[index] = { ...step, content: mapping }; else if (target === 'dueAt' && step.kind === 'wait_until') steps[index] = { ...step, dueAt: mapping }; this.store.setSteps(steps); }
  inputNames(): ReadonlyArray<string> { return this.store.authoring().inputFields.map((field) => field.name); }
  mappingOptions(index: number, target: 'wait' | 'bounded' | 'value' = 'value'): ReadonlyArray<string> {
    const options = this.store.authoring().inputFields
      .filter((field) => target === 'wait' ? field.valueType === 'instant' : target === 'bounded' ? field.valueType === 'bounded_text' : true)
      .map((field) => field.name);
    for (let ordinal = 1; ordinal <= index; ordinal += 1) {
      const kind = this.store.authoring().steps[ordinal - 1]?.kind;
      if (target === 'wait') {
        if (kind === 'wait_until') options.push(`step:${ordinal}:resumed_at`);
      } else if (target === 'bounded' || target === 'value') {
        if (kind === 'agent_run') options.push(`step:${ordinal}:output`);
        else if (kind === 'owner_approval') options.push(`step:${ordinal}:decision`, `step:${ordinal}:status`);
        else if (kind === 'owner_notification') options.push(`step:${ordinal}:notification_id`, `step:${ordinal}:status`);
      }
    }
    return options;
  }
  mappingLabel(option: string): string { if (!option.startsWith('step:')) return option; const [, ordinal, field] = option.split(':'); return `Step ${ordinal} · ${field}`; }
  agentRevisions(): ReadonlyArray<{ readonly id: string; readonly displayName: string }> { return this.store.agents().filter((agent) => agent.status === 'active').flatMap((agent) => agent.currentRevision ? [{ id: agent.currentRevision.id, displayName: `${agent.currentRevision.displayName} · ${agent.currentRevision.revision}` }] : []); }
  addInput(): void { this.store.setInputFields([...this.store.authoring().inputFields, { name: `input${this.store.authoring().inputFields.length + 1}`, valueType: 'bounded_text' }]); }
  updateInputType(index: number, valueType: 'bounded_text' | 'instant'): void { const fields = [...this.store.authoring().inputFields]; const current = fields[index]; if (!current) return; fields[index] = { ...current, valueType }; this.store.setInputFields(fields); }
  removeInput(index: number): void { this.store.setInputFields(this.store.authoring().inputFields.filter((_, itemIndex) => itemIndex !== index)); }
  updateInputName(index: number, name: string): void { const fields = [...this.store.authoring().inputFields]; const current = fields[index]; if (!current) return; fields[index] = { ...current, name }; this.store.setInputFields(fields); }
  async save(): Promise<void> { if (this.store.draft()) await this.store.saveDraft(); else await this.store.createDraft(); }
  async publish(): Promise<void> { await this.openPublishReview(); }
  async openPublishReview(): Promise<void> { this.publishReviewReturnFocus = typeof document !== 'undefined' && document.activeElement instanceof HTMLElement ? document.activeElement : null; await this.store.validate(); if (Object.keys(this.store.fieldErrors()).length) { this.publishReviewReturnFocus = null; return; } this.publishReviewOpen.set(true); if (typeof document !== 'undefined') setTimeout(() => { this.setPublishReviewBackgroundInert(true); document.getElementById('publish-review-dialog')?.focus(); }, 0); }
  cancelPublishReview(): void { this.setPublishReviewBackgroundInert(false); this.publishReviewOpen.set(false); this.restorePublishFocus(); }
  async confirmPublish(): Promise<void> { this.setPublishReviewBackgroundInert(false); this.publishReviewOpen.set(false); this.restorePublishFocus(); await this.store.publish(); }
  @HostListener('document:keydown', ['$event']) handlePublishReviewKeydown(event: KeyboardEvent): void { if (!this.publishReviewOpen() || event.key !== 'Tab' || typeof document === 'undefined') return; const dialog = document.getElementById('publish-review-dialog'); if (!dialog) return; const focusable = this.publishReviewFocusable(dialog); if (!focusable.length) { event.preventDefault(); dialog.focus(); return; } const first = focusable[0]; const last = focusable[focusable.length - 1]; const current = document.activeElement; if (current === dialog || !dialog.contains(current)) { event.preventDefault(); (event.shiftKey ? last : first).focus(); } else if (event.shiftKey && current === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && current === last) { event.preventDefault(); first.focus(); } }
  async publishRevision(): Promise<void> { if (!this.store.draft()) await this.store.cloneDraft(); if (this.store.draft()) await this.openPublishReview(); }
  publishReviewMapping(step: WorkflowStep): string { if (step.kind === 'owner_approval') return 'Owner confirmation; no value mapping.'; const selector: WorkflowSelector = step.kind === 'agent_run' ? step.input : step.kind === 'owner_notification' ? step.content : step.dueAt; return selector.source === 'instance_input' ? `Instance input “${selector.field}” (${this.inputTypeLabel(selector.field)}).` : `Step ${selector.stepOrdinal} ${selector.field} output.`; }
  publishReviewAgent(step: WorkflowStep): string { if (step.kind !== 'agent_run') return ''; return this.agentRevisions().find((agent) => agent.id === step.agentRevisionId)?.displayName ?? `Authorized revision ${step.agentRevisionId}`; }
  inputTypeLabel(fieldName: string): string { return this.store.authoring().inputFields.find((field) => field.name === fieldName)?.valueType === 'instant' ? 'instant' : 'bounded text'; }
  publishReviewLimits(): ReadonlyArray<string> { const limits = this.store.revision()?.limits; return [`Maximum steps: ${limits?.maxSteps ?? 8}`, `Agent runs: ${limits?.maxAgentRuns ?? 1}`, `Agent deadline: ${limits?.agentRunDeadlineSeconds ?? 300} seconds`, `Approval lifetime: ${limits?.approvalLifetimeSeconds ?? 604800} seconds`, `Wait horizon: ${limits?.waitHorizonSeconds ?? 2592000} seconds`, `Instance lifetime: ${limits?.instanceLifetimeSeconds ?? 2592000} seconds`, `Tools: ${limits?.toolsEnabled ? 'enabled' : 'disabled'}`, `Memory: ${limits?.memoryEnabled ? 'enabled' : 'disabled'}`, `Delegation: ${limits?.delegationEnabled ? 'enabled' : 'disabled'}`]; }
  resultDetails(result: unknown): ReadonlyArray<{ readonly key: string; readonly value: string }> { if (result && typeof result === 'object' && !Array.isArray(result)) return Object.entries(result).map(([key, value]) => ({ key, value: this.resultValue(value) })); return [{ key: 'value', value: this.resultValue(result) }]; }
  private resultValue(value: unknown): string { if (typeof value === 'string') return value; if (value === null) return 'null'; try { return JSON.stringify(value) ?? String(value); } catch { return String(value); } }
  async startInstance(): Promise<void> { await this.store.start(this.manualInputValues()); }
  async startManualRun(): Promise<void> { if (this.validateManualInputs()) { const instance = await this.store.start(this.manualInputValues()); const workflow = this.store.selectedWorkflow(); if (instance && workflow) void this.router.navigate(['/workflows', workflow.id, 'instances', instance.id]); } }
  updateManualInput(name: string, value: string): void { this.manualInputValues.update((current) => ({ ...current, [name]: value })); }
  async cancelRun(): Promise<void> { const instance = this.store.selectedInstance(); if (!instance) return; if (this.confirm('Cancel workflow run', `Cancel action for run ${instance.id} at authoritative version ${instance.version}?`)) await this.store.cancel(); }
  async recoverStep(action: 'request_replay' | 'settle_failed' | 'reconcile_completed' | 'reconcile_failed', stepExecutionId: string): Promise<void> { const instance = this.store.selectedInstance(); if (!instance || !stepExecutionId) return; if (this.confirm(action.replaceAll('_', ' '), `Apply ${action} to step execution ${stepExecutionId} in run ${instance.id} at authoritative version ${instance.version}?`)) await this.store.recover(action, stepExecutionId); }
  async retireDefinition(): Promise<void> { const workflow = this.store.selectedWorkflow(); if (!workflow) return; if (this.confirm('Retire workflow definition', `Retire definition ${workflow.id} at authoritative version ${workflow.version}? Published revisions remain immutable.`)) await this.store.retireDefinition(); }
  async retireRevision(): Promise<void> { const revision = this.store.revision(); if (!revision || revision.status !== 'published') return; if (this.confirm('Retire published revision', `Retire revision ${revision.id} at authoritative version ${revision.version}?`)) await this.store.retireRevision(); }
  async deleteScope(scope: typeof this.deletionScopes[number]): Promise<void> { const revision = this.store.revision(); if (scope === 'published_revision' && (!revision || revision.status !== 'retired')) return; const label = this.deletionLabel(scope); const confirmed = this.confirm(`Delete ${label}`, this.deletionConfirmation(scope)); if (confirmed) await this.store.delete(scope); }
  deletionConfirmation(scope: typeof this.deletionScopes[number]): string { const workflow = this.store.selectedWorkflow(); const draft = this.store.draft(); const revision = this.store.revision(); const instance = this.store.selectedInstance(); const targetId = instance?.status === 'terminal' ? instance.id : this.store.terminalDeletionTarget()?.instanceId; const targetVersion = instance?.status === 'terminal' ? instance.version : this.store.terminalDeletionTarget()?.version; const target = scope.startsWith('terminal_instance_') ? `${targetId ?? 'no selected terminal instance'} v${targetVersion ?? '?'}` : scope === 'published_revision' ? `${revision?.id ?? 'no selected revision'} v${revision?.version ?? '?'}` : scope === 'draft' ? `${draft?.id ?? 'no selected draft'} v${draft?.version ?? '?'}` : `${workflow?.id ?? 'workflow'} v${workflow?.version ?? '?'}`; const survivors = scope.startsWith('terminal_instance_') ? 'workflow definition and published revision survive' : scope === 'retired_definition' ? 'revision history survives' : 'workflow tombstone survives'; return `Delete exact scope ${scope} for ${target}. The tombstone is retained; ${survivors}.` }
  private confirm(title: string, message: string): boolean { return typeof window === 'undefined' || window.confirm(`${title}: ${message}`); }
  private publishReviewFocusable(dialog: HTMLElement | null): ReadonlyArray<HTMLElement> { if (!dialog) return []; const selector = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'; return Array.from(dialog.querySelectorAll<HTMLElement>(selector)).filter((element) => !element.hasAttribute('hidden') && element.getAttribute('aria-hidden') !== 'true'); }
  private setPublishReviewBackgroundInert(inert: boolean): void { if (typeof document === 'undefined') return; if (!inert) { for (const [element, previous] of this.publishReviewBackground) { element.inert = previous.inert; if (previous.ariaHidden === null) element.removeAttribute('aria-hidden'); else element.setAttribute('aria-hidden', previous.ariaHidden); } this.publishReviewBackground.clear(); return; } const root = document.querySelector<HTMLElement>('.workflow-studio'); const backdrop = root?.querySelector<HTMLElement>('.publish-review-backdrop'); if (!root || !backdrop) return; for (const child of Array.from(root.children)) { if (child === backdrop || this.publishReviewBackground.has(child as HTMLElement)) continue; const element = child as HTMLElement; this.publishReviewBackground.set(element, { inert: element.inert, ariaHidden: element.getAttribute('aria-hidden') }); element.inert = true; } }
  private restorePublishFocus(): void { const target = this.publishReviewReturnFocus; this.publishReviewReturnFocus = null; if (typeof document === 'undefined') return; const focus = (): void => { const fallback = Array.from(document.querySelectorAll<HTMLElement>('.workflow-studio button')).find((button) => button.textContent?.includes('Validate and publish')); const focusTarget = target?.isConnected ? target : fallback; if (!focusTarget) return; let ancestor: HTMLElement | null = focusTarget; while (ancestor?.closest('.workflow-studio')) { ancestor.inert = false; ancestor = ancestor.parentElement; } focusTarget.focus(); }; if (target?.isConnected) focus(); else setTimeout(focus, 0); }
  deletionLabel(scope: typeof this.deletionScopes[number]): string { return scope === 'draft' ? 'draft' : scope === 'retired_definition' ? 'retired definition' : scope === 'published_revision' ? 'published revision' : scope.replace('terminal_instance_', 'terminal instance '); }
  stepStatusLabel(status: string, kind: string): string { return kind === 'owner_approval' && status === 'pending' ? 'Waiting For Approval' : this.statusLabel(status); }
  runStatusLabel(status: string): string { const instance = this.store.selectedInstance(); return this.statusLabel(instance?.terminalOutcome ?? status); }
  recoveryAction(capability: string, effectOutcome: string): 'request_replay' | 'settle_failed' | 'reconcile_completed' | 'reconcile_failed' { if (capability === 'reconcile') return effectOutcome === 'completed' ? 'reconcile_completed' : 'reconcile_failed'; return capability as 'request_replay' | 'settle_failed'; }
  async inspectWorkflow(id: string): Promise<void> { void this.router.navigate(['/workflows', id]); await this.store.inspect(id); }
  async inspectInstance(id: string): Promise<void> { const workflow = this.store.selectedWorkflow(); if (workflow) { void this.router.navigate(['/workflows', workflow.id, 'instances', id]); await this.store.inspectInstance(workflow.id, id); } }
  reconnectLive(): void { this.store.stopLive(); void this.store.reconcile(); this.store.startLive(); }
  validateManualInputs(): boolean { const revision = this.store.revision(); if (!revision) return false; const errors: Record<string, string> = {}; for (const field of revision.inputFields) { const value = this.manualInputValues()[field.name]?.trim() ?? ''; if (!value) errors[field.name] = 'Enter a value before starting this run.'; else if (field.valueType === 'instant' && !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?(?:Z|[+-]\d{2}:?\d{2})$/.test(value)) errors[field.name] = 'Use an RFC 3339 instant, for example 2026-10-28T09:00:00Z.'; } this.manualInputErrors.set(errors); return !Object.keys(errors).length; }
  private focusStep(index: number): void { if (typeof document === 'undefined') return; setTimeout(() => { const card = index >= 0 ? document.querySelector<HTMLElement>(`[data-step-index="${index}"]`) : null; if (card?.classList.contains('mobile-hidden')) return; const heading = card?.querySelector<HTMLElement>('h4'); if (heading) heading.tabIndex = -1; const target = card?.querySelector<HTMLElement>('input:not([disabled]), textarea:not([disabled]), select:not([disabled])') ?? heading ?? card?.querySelector<HTMLElement>('button:not([disabled])') ?? document.querySelector<HTMLElement>('[data-step-picker] button:not([disabled])'); target?.focus(); }, 0); }
}

export { WorkflowStudioComponent as WorkflowStudioPageComponent };
