import { ChangeDetectionStrategy, Component, EventEmitter, Input, OnDestroy, Output, effect, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import type { RunDetail, RunEvidenceManifest, RunInspection, TraceDetail } from '@aura/observatory-api-client';
import { ObservatoryStore } from '../../observatory-store';
import { durationAriaLabel, formatDurationMs, isCurrentCapturedRunEvidenceManifest } from '../../observatory-models';
import { ObservatoryIconComponent, type ObservatoryIconName } from '../../presentation/observatory-icon.component';
import { aggregateInspectionSteps, closeOpenEvidenceDetails, continuationSummary as summarizeContinuation, foregroundPhaseItemsFor, isAtOrBeforeBoundary, lineageLabel, orderPhases, phaseFor, revealBatch, spanLineageLabel as spanLineageText, stageOutcomeCounts, stageSummaryLabel as summarizeStage, terminalObservationBoundary, type ContinuationSummary, type PhaseItem, type PhaseKey } from './trace-inspector.utils';

type PhaseDefinition = { readonly key: PhaseKey; readonly icon: ObservatoryIconName; readonly label: string; readonly description: string };
type Observation = RunDetail['observations'][number];
type Span = TraceDetail['spans'][number];
type StageSummary = {
  readonly key: PhaseKey;
  readonly label: string;
  readonly icon: ObservatoryIconName;
  readonly count: number;
  readonly durationMs: number | null;
  readonly state: 'succeeded' | 'failed' | 'running' | 'partial' | 'cancelled' | 'unavailable';
  readonly errorCount: number;
  readonly failedCount: number;
  readonly runningCount: number;
  readonly partialCount: number;
  readonly cancelledCount: number;
  readonly unavailableCount: number;
  readonly partial: boolean;
  readonly parallel: boolean;
  readonly branched: boolean;
  readonly partialReasons: ReadonlyArray<string>;
  readonly firstStartedAt: string | null;
};
const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'cancelled']);
const MAX_CONTINUATION_PREVIEW = 20;
// These match the generated contract page bounds. The UI still starts with a
// compact twenty-item batch, then lets the operator reveal the rest of the
// authoritative page without silently stranding records.
const MAX_INSPECTION_STEPS = 1000;
const MAX_TRACE_SPANS = 10_000;
const MAX_MEASUREMENTS = 128;
const MAX_EVIDENCE_TEXT = 120_000;
const DETAIL_BATCH_SIZE = 20;

const PHASES: ReadonlyArray<PhaseDefinition> = [
  { key: 'request', icon: 'runs', label: 'Request admission', description: 'Run admission and lifecycle boundaries' },
  { key: 'context', icon: 'document', label: 'Context & retrieval', description: 'Memory, documents, search, and ranking timings' },
  { key: 'prompt', icon: 'document', label: 'Prompt construction', description: 'Prompt, persona, and context assembly metadata' },
  { key: 'routing', icon: 'analytics', label: 'Model routing', description: 'Provider and model selection metadata' },
  { key: 'generation', icon: 'runs', label: 'Generation', description: 'Inference and response generation timings' },
  { key: 'thinking', icon: 'overview', label: 'Displayed thinking', description: 'Displayed thinking lifecycle metadata' },
  { key: 'tools', icon: 'operations', label: 'Tools & policy', description: 'Tool policy, approval, and execution metadata' },
  { key: 'persistence', icon: 'retention', label: 'Persistence', description: 'Database, memory, and outbox timings' },
  { key: 'delivery', icon: 'next', label: 'Streaming & delivery', description: 'Streaming, SSE, and notification metadata' },
  { key: 'other', icon: 'overview', label: 'Other metadata', description: 'Related observations without a phase classifier' },
];

@Component({
  selector: 'observatory-trace', standalone: true, imports: [DatePipe, ObservatoryIconComponent], changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <h2 id="trace-title" tabindex="-1">Run inspector</h2>
    @if (store.selectedRun().detail; as run) {
      <section class="run-card" aria-labelledby="selected-run-title">
        <div class="run-heading"><p class="eyebrow">Selected run</p><h3 id="selected-run-title">{{ run.runId }}</h3><p class="muted">Started {{ run.startedAt | date:'medium' }}</p></div>
        <div class="run-summary"><span class="status" [class]="'status-' + run.status"><observatory-icon [name]="statusIcon(run.status)" />{{ run.status }}</span><span class="summary-value" [attr.aria-label]="durationAriaLabel(run.durationMs)"><strong>{{ formatDuration(run.durationMs) }}</strong><small>authoritative run duration</small></span><span class="capture-badge"><observatory-icon name="document" /> Governed evidence</span></div>
      </section>
      <p class="sr-only" role="status" aria-live="polite">Selected run {{ run.runId }} is {{ run.status }}. {{ run.durationMs === null ? 'Duration unavailable.' : 'Duration ' + durationAriaLabel(run.durationMs) + '.' }}</p>
      <p class="capture-note" role="note"><strong>Governed capture.</strong> Timings and lineage are available immediately. Exact owner evidence, when captured, stays behind explicit disclosure and is rendered inertly; excluded, partial, unavailable, and deleted states are shown as reported.</p>
      @if (store.selectedRun().inspection; as inspection) {
        <section class="inspection-card" aria-labelledby="inspection-title">
          <div class="section-heading"><div><p class="eyebrow">Governed run inspection</p><h3 id="inspection-title">Execution timeline</h3><p class="muted">One bounded, authoritative snapshot. Repeated stream events are summarized; exact evidence stays closed until you disclose it.</p></div><span class="completeness" [class]="'completeness-' + inspection.completeness.state">{{ completenessLabel(inspection) }}</span></div>
          @if (store.selectedRunInspectionFreshness() === 'stale') { <p class="notice" role="status">The last inspection snapshot is preserved but stale because its refresh failed. Metadata remains visible; retry to verify the current run.</p> }
          @if (inspection.completeness.state === 'deleted') { <p class="notice" role="status">Retained evidence was deleted by the owner. Metadata and step timing remain visible.</p> } @else if (inspection.completeness.state === 'unavailable') { <p class="notice" role="status">Evidence capture is unavailable for this inspection. Metadata remains visible; no evidence bytes can be disclosed.</p> } @else if (inspection.partial || inspection.completeness.reasons.length) { <p class="notice" role="status">This inspection is partial: {{ partialReasonLabel(inspection) }}</p> }
          @if (store.selectedRunEvidenceState() === 'deleting') { <p class="notice" role="status">Evidence deletion is in progress. Metadata remains available.</p> } @else if (store.selectedRunEvidenceState() === 'deleted') { <p class="notice" role="status">Evidence was deleted. Metadata remains available; no bytes will be fetched.</p> }
          <section class="operator-summary" aria-labelledby="operator-summary-title">
            <div class="section-heading"><div><p class="eyebrow">Operator summary</p><h3 id="operator-summary-title">{{ runVerdict(run) }}</h3><p class="muted">Answer first: what happened, what was slow, what failed, and what evidence is available.</p></div><span class="completeness" [class]="'completeness-' + inspection.completeness.state"><observatory-icon [name]="inspection.completeness.state === 'complete' ? 'success' : 'warning'" />{{ completenessLabel(inspection) }}</span></div>
            <div class="operator-kpis" aria-label="Run outcome summary"><div><strong>{{ formatDuration(run.durationMs) }}</strong><span>Total duration</span></div><div><strong>{{ longestOperationLabel() }}</strong><span>Longest recorded operation</span></div><div><strong>{{ errorCount() }}</strong><span>Recorded errors</span></div><div><strong>{{ outcomeCount('failed') }}</strong><span>Failed records</span></div><div><strong>{{ outcomeCount('running') }}</strong><span>Running records</span></div><div><strong>{{ outcomeCount('partial') }}</strong><span>Partial records</span></div><div><strong>{{ outcomeCount('cancelled') }}</strong><span>Cancelled records</span></div><div><strong>{{ outcomeCount('unavailable') }}</strong><span>Unavailable records</span></div><div><strong>{{ evidenceSummaryLabel(inspection) }}</strong><span>Evidence coverage</span></div></div>
            <p class="stage-highlights"><span><observatory-icon name="document" /><strong>Retrieval</strong> {{ stageSummaryLabel('context') }}</span><span><observatory-icon name="runs" /><strong>Generation</strong> {{ stageSummaryLabel('generation') }}</span><span><observatory-icon name="operations" /><strong>Tools</strong> {{ stageSummaryLabel('tools') }}</span></p>
            @if (stageRows().length) { <ol class="stage-list" aria-label="Aggregate execution stages">@for (stage of stageRows(); track stage.key) { <li><details class="stage-accordion" [open]="isStageOpen(stage.key)" (toggle)="toggleStage(stage.key, $event)"><summary class="stage-row" [class.stage-failed]="stage.state === 'failed'" [class.stage-running]="stage.state === 'running'" [class.stage-cancelled]="stage.state === 'cancelled'" [class.stage-unavailable]="stage.state === 'unavailable'" [class.stage-parallel]="stage.parallel"><span class="stage-icon" aria-hidden="true"><observatory-icon [name]="stage.icon" /></span><div><h4>{{ stage.label }}</h4><p>{{ stage.count }} record{{ stage.count === 1 ? '' : 's' }} · {{ stage.state }}@if (stage.partial) { · partial: {{ reasonLabels(stage.partialReasons) }} }@if (stage.parallel) { · overlapping intervals } @else if (stage.branched) { · branched records }</p></div><span class="stage-duration" [attr.aria-label]="stage.durationMs === null ? 'Longest operation unavailable' : 'Longest operation ' + durationAriaLabel(stage.durationMs)">{{ stage.durationMs === null ? 'Longest operation unavailable' : formatDuration(stage.durationMs) }}</span><span class="stage-facets" aria-label="Stage outcome counts">{{ stage.failedCount }} failed · {{ stage.runningCount }} running · {{ stage.partialCount }} partial · {{ stage.cancelledCount }} cancelled · {{ stage.unavailableCount }} unavailable</span></summary><div class="stage-preview">@for (item of stagePreview(stage.key); track item.id) { <div><strong>{{ item.label }}</strong><span>{{ item.outcome }} · {{ recordValue(item) }}</span></div> } @if (stage.count > 20) { <p class="disclosure-hint">Showing 20 records. Open Advanced below for the full bounded timeline.</p> }</div></details></li> }</ol> } @else { <p class="empty" role="status">No aggregate stages were reported for this run.</p> }
            <div class="coverage-inline" aria-label="Evidence coverage by stage">@for (item of coverageItems(); track item.key) { <span><observatory-icon [name]="item.icon" /><strong>{{ item.label }}</strong><small>{{ coverageStateLabel(item.state) }}</small></span> }</div>
          </section>
          <details class="technical-disclosure" (toggle)="toggleTechnicalDisclosure($event)"><summary><observatory-icon name="trace" /> Inspect exact execution steps and evidence <span class="disclosure-hint">Bounded, paged records; evidence bytes stay lazy</span></summary>
          <ol class="inspection-timeline" aria-label="Run execution steps">
            @for (step of revealBatch(timelineSteps(inspection), technicalStepLimit); track step.stepId) {
              <li class="inspection-step" [attr.aria-label]="stepAriaLabel(step, run.startedAt)" [style.--depth]="step.depth">
                <div class="step-rail" aria-hidden="true"><observatory-icon class="step-icon" [name]="stepIcon(step.category)" /></div>
                <div class="step-body">
                  <div class="step-heading"><div><h4>{{ step.label }}</h4><p>{{ categoryLabel(step.category) }} · sequence {{ step.sequence }}</p><p class="step-lineage"><observatory-icon name="trace" /><span>{{ stepLineageLabel(step) }}</span><span>start {{ phaseOffset(step.startedAt, run.startedAt) }}</span></p></div><div class="step-meta"><span class="state-chip state-{{ step.state }}">{{ step.state }}</span><span [attr.aria-label]="durationAriaLabel(step.durationMs)">{{ formatDuration(step.durationMs) }}</span></div></div>
                  @if (step.measurements.length) { <div class="measurement-row" aria-label="Step measurements"><p class="measurement-heading">Measurements · {{ step.measurements.length }}</p>@for (measurement of measurementsForStep(step); track measurement.name) { <span><strong>{{ measurement.value }}</strong> {{ measurement.unit }}<small>{{ measurement.name }}</small></span> } @if (step.measurements.length > measurementsForStep(step).length) { <button class="load-more" type="button" (click)="showMoreMeasurements(step.stepId, step.measurements.length)">Show next {{ boundedBatchSize(step.measurements.length, measurementsForStep(step).length) }} measurements</button> }</div> }
                  @if (step.partial || step.partialReasons.length) { <p class="step-partial">Partial: {{ reasonLabels(step.partialReasons) }}</p> }
                  @if (step.evidence.length) { <div class="evidence-list"><p class="evidence-heading">Evidence · {{ step.evidence.length }} manifests</p>@for (evidence of evidenceForStep(step); track evidence.evidenceId) { <details class="evidence-item" (toggle)="toggleEvidence($event, evidence)"><summary><observatory-icon class="evidence-icon" [name]="mediaIcon(evidence.mediaClassification)" /><span>{{ evidenceLabel(evidence.kind) }}</span><span class="evidence-state state-{{ evidence.captureState }}">{{ evidence.captureState }}</span><span class="evidence-media">{{ evidence.mediaType }}</span></summary>@if (isEvidenceOpen(evidence)) { @if (evidence.captureState === 'captured' && store.selectedRunEvidenceState() === 'loading') { <p class="evidence-loading" role="status">Loading owner evidence…</p> } @else if (store.selectedRunEvidenceError(); as error) { <p class="evidence-error" role="alert">{{ error }}</p> } @else if (loadedEvidenceMatches(evidence)) { @if (evidence.mediaClassification === 'text' || evidence.mediaClassification === 'structured') { <pre class="evidence-preview">{{ evidenceText() }}</pre> } @else if (evidence.mediaClassification === 'image' && evidenceImageUrl()) { <img class="evidence-image" [src]="evidenceImageUrl()" alt="Approved run evidence preview" /> } @else { <p class="evidence-loading">Evidence is retained as {{ evidence.mediaType }} ({{ evidence.byteCount }} bytes); preview is not available.</p> } } @else if (evidence.captureState !== 'captured') { <p class="evidence-loading">{{ captureStateDescription(evidence) }}</p> } }</details> } @if (step.evidence.length > evidenceForStep(step).length) { <button class="load-more" type="button" (click)="showMoreEvidence(step.stepId, step.evidence.length)">Show next {{ boundedBatchSize(step.evidence.length, evidenceForStep(step).length) }} evidence manifests</button> }</div> }
                </div>
              </li>
            }
          </ol>
          @if (timelineSteps(inspection).length > technicalStepLimit) { <button class="load-more" type="button" (click)="showMoreSteps(timelineSteps(inspection).length)">Show next {{ boundedBatchSize(timelineSteps(inspection).length, technicalStepLimit) }} steps</button> } @if (inspection.nextCursor) { <p class="evidence-limit">Server continuation available: this snapshot page ends at a cursor. Client reveal controls do not imply that later pages are loaded.</p> }
          <div class="inspection-actions"><button class="secondary" type="button" (click)="clearEvidence()" [disabled]="!store.selectedRunEvidence()">Close disclosed evidence</button><button class="danger" type="button" (click)="deleteEvidence()" [disabled]="inspection.completeness.state === 'deleted' || store.selectedRunEvidenceState() === 'deleting' || store.selectedRunEvidenceState() === 'loading'">{{ store.selectedRunEvidenceState() === 'deleting' ? 'Deletion in progress…' : 'Delete retained evidence' }}</button></div>
          </details>
        </section>
      } @else if (store.selectedRunState() === 'ready') { <section class="inspection-card inspection-unavailable" aria-labelledby="inspection-unavailable-title"><h3 id="inspection-unavailable-title">Run inspection unavailable</h3><p>The metadata run loaded, but its governed inspection snapshot is not available. Retry to check again.</p><button type="button" (click)="retry()">Retry inspection</button></section> }
      @if (store.selectedRunState() === 'loading') { <p class="notice" role="status">Refreshing this run; the last known projection remains visible.</p> } @else if (store.selectedRunState() === 'error') { <p class="notice" role="status">The latest run refresh is unavailable; showing the last known projection. <button type="button" (click)="retry()">Retry</button></p> }

      <details class="technical-disclosure phase-disclosure"><summary><observatory-icon name="analytics" /> Inspect phase records <span class="disclosure-hint">{{ itemCount() }} metadata records · {{ phaseLimitLabel() }}</span></summary><section class="phases-card" aria-labelledby="phases-title"><div class="section-heading"><div><p class="eyebrow">Phase coverage</p><h3 id="phases-title">Captured phases</h3><p class="muted">The foreground timeline ends at the authoritative terminal lifecycle event. Each phase stays closed and reveals at most twenty records.</p></div><span class="muted">{{ itemCount() }} metadata records</span></div><ol class="phase-list" aria-label="Run phase coverage">@for (phase of orderedPhases(); track phase.key) { <li class="phase-row" [class.phase-empty]="phaseItems(phase.key).length === 0"><div class="phase-marker"><observatory-icon [name]="phase.icon" /></div><details class="phase-record-disclosure"><summary><span class="phase-title"><h4>{{ phase.label }}</h4><span class="phase-count">{{ phaseItemCount(phase.key) ? phaseItemCount(phase.key) + ' record' + (phaseItemCount(phase.key) === 1 ? '' : 's') : 'Not observed' }}</span></span></summary><div class="phase-content"><p class="phase-description">{{ phase.description }}</p>@if (phaseItems(phase.key).length) { <ul class="phase-items">@for (item of phaseItems(phase.key); track item.id) { <li><span class="item-kind" aria-hidden="true"><observatory-icon [name]="item.kind === 'span' ? 'trace' : 'analytics'" /></span><span class="item-label"><strong>{{ item.label }}</strong><small>{{ item.component }}</small><time [attr.datetime]="item.startedAt">{{ item.startedAt | date:'HH:mm:ss.SSS' }} · {{ phaseOffset(item.startedAt, run.startedAt) }}</time></span><span class="item-time" [attr.aria-label]="recordValueAriaLabel(item)">{{ recordValue(item) }}</span><span class="outcome" [class]="'outcome-' + item.outcome">{{ item.outcome }}</span>@if (item.partial) { <span class="partial">partial</span> }</li> }</ul>@if (phaseItemCount(phase.key) > phaseItems(phase.key).length) { <button class="load-more" type="button" (click)="showMorePhase(phase.key)">Show next {{ boundedBatchSize(phaseItemCount(phase.key), phaseItems(phase.key).length) }} phase records</button> } } @else { <p class="phase-unavailable">Not observed in retained metadata.</p> }</div></details></li> }</ol></section></details>

      @if (continuationSummary(); as continuation) { @if (continuation.count > 0 || continuation.partial) { <details class="technical-disclosure continuation-disclosure"><summary><observatory-icon name="next" /> Inspect background continuation <span class="disclosure-hint">{{ continuation.count > 0 ? continuationCountLabel(continuation) : 'Page incomplete' }}</span></summary><section class="continuation-card" aria-labelledby="continuation-title"><div class="section-heading"><div><p class="eyebrow">Post-run activity</p><h3 id="continuation-title">Background continuation</h3><p class="muted">Correlated metadata after the request completed. It is not part of the request duration.</p></div><span class="phase-count"><observatory-icon name="next" /> {{ continuation.count > 0 ? continuationCountLabel(continuation) : 'Page incomplete' }}</span></div>@if (continuation.count > 0) { <p class="continuation-range"><strong>{{ formatContinuationRange(continuation) }}</strong>@if (continuation.outcomes.length) { <span> · outcomes: {{ continuation.outcomes.join(', ') }}</span> }</p><details class="continuation-details" [open]="continuationOpen" (toggle)="continuationOpen = $any($event.target).open"><summary><observatory-icon name="next" /> Inspect a page-bounded preview ({{ continuationPreview().length }} of {{ continuationCountLabel(continuation) }})</summary><ul class="phase-items continuation-items" aria-label="Page-bounded post-run metadata preview">@for (item of continuationPreview(); track item.id) { <li><span class="item-kind" aria-hidden="true"><observatory-icon name="analytics" /></span><span class="item-label"><strong>{{ item.label }}</strong><small>{{ item.component }}</small><time [attr.datetime]="item.startedAt">{{ item.startedAt | date:'HH:mm:ss.SSS' }}</time></span><span class="item-time" [attr.aria-label]="recordValueAriaLabel(item)">{{ recordValue(item) }}</span><span class="outcome" [class]="'outcome-' + item.outcome">{{ item.outcome }}</span></li> }</ul>@if (continuation.count > MAX_CONTINUATION_PREVIEW || continuation.partial) { <p class="continuation-limit">Showing at most the first {{ MAX_CONTINUATION_PREVIEW }} records from this loaded metadata page. @if (continuation.partial) { More correlated metadata may exist beyond this page. } @else { Remaining loaded records stay collapsed and are not rendered as request steps. }</p> }</details> } @else { <p class="continuation-limit" role="status">Further correlated metadata was not loaded; no post-run count or range is known from this page.</p> }</section></details> } }

      @if (store.selectedRun().trace; as trace) { <details class="technical-disclosure trace-disclosure"><summary><observatory-icon name="trace" /> Inspect raw span lineage <span class="disclosure-hint">{{ trace.spans.length }} spans retained · first {{ boundedSpanCount(trace.spans.length) }} shown on open@if (trace.nextCursor) { · server continuation available }</span></summary><section class="trace-card" aria-labelledby="trace-id"><div class="trace-heading"><div><p class="eyebrow">Trace lineage</p><h3 id="trace-id">{{ trace.traceId }}</h3><p class="muted">Primary execution spans · {{ trace.spans.length }} retained</p></div><span class="status" [class.status-warning]="trace.partial"><observatory-icon [name]="trace.partial ? 'warning' : 'success'" />{{ trace.partial ? 'Partial trace' : 'Complete trace' }}</span></div><div class="waterfall-legend" aria-label="Trace waterfall scale"><span><i aria-hidden="true"></i>Relative duration</span><span>Longest span: {{ formatDuration(maxDuration()) }}</span></div><ul class="trace-tree" aria-label="Trace span lineage">@for (span of revealBatch(trace.spans, technicalSpanLimit); track span.spanId) { <li class="span-row" [attr.aria-label]="spanAriaLabel(span, run.startedAt)" [class.parallel-branch]="isParallelSpan(span, revealBatch(trace.spans, technicalSpanLimit))" [style.--depth]="span.depth"><div class="span-name"><span class="sr-only">Level {{ span.depth + 1 }}. </span><span class="branch" aria-hidden="true"><observatory-icon [name]="span.depth ? 'next' : 'trace'" /></span><strong>{{ span.operation }}</strong><small>{{ span.componentId }} · {{ spanLineageLabel(span) }}</small><small>Start {{ phaseOffset(span.startedAt, run.startedAt) }}</small>@if (span.partial) { <span class="partial">partial / missing parent</span> }</div><div class="waterfall" role="img" [attr.aria-label]="'Relative duration bar: ' + durationAriaLabel(span.durationMs)"><span aria-hidden="true" [style.width.%]="barWidth(span.durationMs)"></span></div><time [attr.aria-label]="durationAriaLabel(span.durationMs)">{{ formatDuration(span.durationMs) }}</time><span class="outcome" [class]="'outcome-' + span.outcome">{{ span.outcome }}</span></li> }</ul>@if (trace.spans.length > technicalSpanLimit && technicalSpanLimit < MAX_TRACE_SPANS) { <button class="load-more" type="button" (click)="showMoreSpans(trace.spans.length)">Show next {{ boundedBatchSize(trace.spans.length, technicalSpanLimit) }} spans</button> } @if (trace.nextCursor) { <p class="evidence-limit">Server continuation available: this trace page ends at a cursor; client reveal controls cover only the loaded page.</p> }</section></details> } @else { <section class="trace-card trace-unavailable" aria-labelledby="trace-unavailable-title"><h3 id="trace-unavailable-title">Trace lineage unavailable</h3><p>The run metadata is retained, but its primary execution trace has not arrived or is outside the current retention window.</p><button type="button" (click)="retry()">Retry trace</button></section> }
    } @else if (store.selectedRunState() === 'loading') { @if (store.liveDisconnected()) { <p class="notice" role="status">Live updates are disconnected; retrying the selected run projection.</p> } <div class="route-skeleton trace-skeleton" role="status" aria-label="Loading the selected run inspector"><span></span><span></span><span></span></div> } @else if (store.selectedRunState() === 'error') { <section class="trace-card trace-unavailable" aria-labelledby="trace-load-error-title" role="alert"><h3 id="trace-load-error-title">Run inspector needs a retry</h3><p>{{ store.selectedRunError() || 'The selected run could not be loaded within the bounded recovery window.' }}</p><button type="button" (click)="retry()">Retry selected run</button></section> } @else { <section class="empty empty-state" role="status" aria-live="polite"><p>Select a run to inspect its lifecycle, timings, and retained metadata.</p><button type="button" (click)="browseRuns.emit()">Browse runs</button></section> }
  `,
  styles: [`
    :host { display:block; } .run-card,.coverage-card,.phases-card,.trace-card,.continuation-card { background:var(--surface); border:1px solid var(--border); border-radius:1rem; margin-bottom:1rem; padding:1.25rem; } .run-card,.trace-heading,.section-heading { align-items:flex-start; display:flex; justify-content:space-between; gap:1rem; } h2 { font-size:1.1rem; margin:0 0 1rem; overflow-wrap:anywhere; } h3 { font-size:1.1rem; margin:0; overflow-wrap:anywhere; } h4 { font-size:.95rem; margin:0; } h2:focus-visible { outline:3px solid var(--focus); outline-offset:3px; } .eyebrow { color:var(--text-muted); font-size:.75rem; font-weight:750; letter-spacing:.08em; margin:0 0 .4rem; text-transform:uppercase; } .muted,small { color:var(--text-muted); } .muted { margin:.45rem 0 0; } .run-heading h3 { font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:1rem; } .run-summary { align-items:flex-end; display:flex; flex-wrap:wrap; gap:.6rem; justify-content:flex-end; } .status,.outcome,.partial,.phase-count,.capture-badge { align-items:center; border-radius:999px; display:inline-flex; font-size:.75rem; font-weight:700; gap:.25rem; padding:.25rem .5rem; text-transform:capitalize; } .status { background:var(--info-surface); color:var(--info-text); } .status-succeeded,.outcome-succeeded { background:var(--success-surface); color:var(--success-text); } .status-failed,.outcome-failed { background:var(--danger-surface); color:var(--danger-text); } .status-running,.status-queued { background:var(--warning-surface); color:var(--warning-text); } .status-cancelled,.status-unknown,.outcome-cancelled,.outcome-unknown { background:var(--surface-subtle); color:var(--text-muted); } .status-warning,.partial { background:var(--warning-surface); color:var(--warning-text); } .capture-badge { background:var(--surface-subtle); color:var(--text-muted); } .summary-value { display:grid; gap:.1rem; text-align:right; } .summary-value strong { font-size:1.15rem; } .summary-value small { font-size:.68rem; } .capture-note { background:var(--surface-subtle); border-left:3px solid var(--accent); color:var(--text-muted); margin:0 0 1rem; padding:.8rem 1rem; } .section-heading { align-items:center; border-bottom:1px solid var(--border); padding-bottom:.7rem; } .section-heading h3 { margin:0; } .coverage-grid { display:grid; gap:.65rem; grid-template-columns:repeat(3,minmax(0,1fr)); margin-top:1rem; } .coverage-item { align-items:flex-start; border:1px solid var(--border); border-radius:.6rem; display:flex; gap:.55rem; min-height:4.3rem; padding:.7rem; } .coverage-icon { color:var(--accent); font-size:1.15rem; line-height:1.1; } .coverage-item strong { display:block; font-size:.78rem; } .coverage-item p { color:var(--text-muted); font-size:.7rem; margin:.35rem 0 0; } .operator-summary { background:var(--surface-subtle); border:1px solid var(--border); border-radius:.8rem; padding:1rem; } .operator-kpis { display:grid; gap:.6rem; grid-template-columns:repeat(4,minmax(0,1fr)); margin:1rem 0; } .operator-kpis div { background:var(--surface); border:1px solid var(--border); border-radius:.55rem; display:grid; gap:.15rem; min-width:0; padding:.65rem; } .operator-kpis strong { font-size:.9rem; overflow-wrap:anywhere; } .operator-kpis span,.stage-row p,.disclosure-hint { color:var(--text-muted); font-size:.72rem; } .stage-highlights { display:flex; flex-wrap:wrap; gap:.4rem 1rem; margin:.5rem 0 .8rem; } .stage-highlights span { align-items:center; color:var(--text-muted); display:inline-flex; font-size:.72rem; gap:.25rem; } .stage-highlights observatory-icon { color:var(--accent); } .stage-highlights strong { color:var(--text); } .stage-list { display:grid; gap:.4rem; list-style:none; margin:0; padding:0; } .stage-accordion { background:var(--surface); border:1px solid var(--border); border-radius:.55rem; } .stage-row { align-items:center; cursor:pointer; display:grid; gap:.65rem; grid-template-columns:auto minmax(0,1fr) auto auto; list-style:none; padding:.65rem; } .stage-row::-webkit-details-marker,.technical-disclosure > summary::-webkit-details-marker { display:none; } .stage-row:focus-visible,.technical-disclosure > summary:focus-visible,.stage-accordion > summary:focus-visible { outline:3px solid var(--focus); outline-offset:2px; } .stage-icon { align-items:center; background:var(--accent-soft); border-radius:50%; color:var(--accent-strong); display:inline-flex; height:1.65rem; justify-content:center; width:1.65rem; } .stage-row h4 { font-size:.83rem; } .stage-row p { margin:.15rem 0 0; } .stage-duration { color:var(--text-muted); font-size:.78rem; font-variant-numeric:tabular-nums; white-space:nowrap; } .stage-errors { align-items:center; color:var(--danger-text); display:inline-flex; font-size:.72rem; gap:.2rem; white-space:nowrap; } .stage-failed { border-left:3px solid var(--danger-text); } .stage-running { border-left:3px solid var(--warning-text); } .stage-parallel { background:color-mix(in srgb,var(--accent-soft) 30%,var(--surface)); } .stage-preview { border-top:1px solid var(--border); padding:.45rem .7rem .7rem; } .stage-preview > div { align-items:center; border-bottom:1px solid var(--border); display:flex; gap:.5rem; justify-content:space-between; padding:.45rem 0; } .stage-preview > div:last-of-type { border-bottom:0; } .stage-preview span { color:var(--text-muted); font-size:.72rem; } .coverage-inline { display:grid; gap:.35rem; grid-template-columns:repeat(6,minmax(0,1fr)); margin-top:.7rem; } .coverage-inline span { align-items:center; display:flex; flex-wrap:wrap; gap:.25rem .35rem; min-width:0; } .coverage-inline observatory-icon { color:var(--accent); } .coverage-inline strong { font-size:.68rem; } .coverage-inline small { flex-basis:100%; font-size:.65rem; padding-left:1.25rem; } .technical-disclosure { border:1px solid var(--border); border-radius:.65rem; margin-top:1rem; padding:.7rem; } .technical-disclosure > summary { align-items:center; color:var(--accent-hover); cursor:pointer; display:flex; flex-wrap:wrap; font-size:.8rem; font-weight:750; gap:.4rem; list-style:none; } .technical-disclosure > summary .disclosure-hint { margin-left:auto; } .technical-disclosure[open] > summary { border-bottom:1px solid var(--border); margin-bottom:.8rem; padding-bottom:.65rem; } .phase-disclosure,.continuation-disclosure,.trace-disclosure { background:var(--surface); } .phase-disclosure .phases-card,.trace-disclosure .trace-card,.continuation-disclosure .continuation-card { border:0; margin:0; padding:0; }
  `, `
    .run-card,.coverage-card,.phases-card,.trace-card,.continuation-card { background:transparent; border:0; border-top:2px solid var(--control-border); border-radius:0; padding:1.1rem 0; } .coverage-card,.phases-card,.trace-card,.continuation-card { margin-top:1.5rem; } .trace-heading { align-items:flex-start; } .phase-list { padding-top:1.25rem; } .waterfall { background:repeating-linear-gradient(to right,var(--surface-subtle) 0,var(--surface-subtle) calc(25% - 1px),var(--border) 25%); border-radius:.2rem; } .waterfall span { background:var(--accent); border-radius:.1rem; } .continuation-details summary:focus-visible { outline:3px solid var(--focus); outline-offset:3px; } @media (max-width:700px) { .run-card,.trace-heading,.section-heading { align-items:flex-start; flex-direction:column; } .run-summary { align-items:flex-start; justify-content:flex-start; } .summary-value { text-align:left; } .coverage-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .phase-items li { grid-template-columns:auto minmax(0,1fr) auto; } .phase-items .outcome,.phase-items .partial { grid-column:2; justify-self:start; } .span-row { align-items:start; grid-template-columns:1fr auto; padding-left:clamp(.25rem,calc(.25rem + (var(--depth) * .55rem)),2.5rem); } .span-name { grid-column:1 / -1; } .waterfall { grid-column:1; } .span-row time,.span-row .outcome { align-self:center; justify-self:end; } }
    @media (max-width:420px) { .coverage-grid { grid-template-columns:1fr; } }
    @media (prefers-reduced-motion:reduce) { *,*::before,*::after { scroll-behavior:auto !important; transition-duration:.01ms !important; } }
    @media (forced-colors:active) { .phase-marker,.waterfall span { forced-color-adjust:none; } .phase-marker { background:Highlight; color:HighlightText; } .waterfall span { background:Highlight; } }
  `, `
    .inspection-card { background:var(--surface); border:1px solid var(--border); border-radius:1rem; margin-bottom:1rem; padding:1.25rem; } .completeness { border-radius:999px; font-size:.75rem; font-weight:750; padding:.3rem .55rem; text-transform:capitalize; } .completeness-complete,.state-succeeded,.state-captured { background:var(--success-surface); color:var(--success-text); } .completeness-partial,.state-partial,.state-excluded,.state-dropped { background:var(--warning-surface); color:var(--warning-text); } .completeness-unavailable,.completeness-deleted,.state-unavailable,.state-deleted { background:var(--surface-subtle); color:var(--text-muted); } .inspection-timeline { list-style:none; margin:1rem 0 0; padding:0; } .inspection-step { display:grid; gap:.75rem; grid-template-columns:2rem minmax(0,1fr); margin-left:calc(var(--depth, 0) * 1.25rem); max-width:calc(100% - (var(--depth, 0) * 1.25rem)); padding-bottom:.75rem; position:relative; } .inspection-step:not(:last-child)::before { background:var(--border); content:''; inset-block:1.75rem 0; left:.92rem; position:absolute; width:2px; } .step-rail { position:relative; z-index:1; } .step-icon,.evidence-icon { align-items:center; background:var(--accent-soft); border:1px solid var(--accent); border-radius:50%; color:var(--accent-strong); display:inline-flex; height:1.8rem; justify-content:center; width:1.8rem; } .step-body { border:1px solid var(--border); border-radius:.65rem; min-width:0; padding:.8rem; } .step-heading { align-items:flex-start; display:flex; gap:.75rem; justify-content:space-between; } .step-heading h4 { overflow-wrap:anywhere; } .step-heading p { color:var(--text-muted); font-size:.72rem; margin:.25rem 0 0; } .step-lineage { align-items:center; display:flex; flex-wrap:wrap; gap:.35rem; } .step-lineage observatory-icon { color:var(--accent); } .step-meta { align-items:flex-end; display:flex; flex-wrap:wrap; gap:.35rem; justify-content:flex-end; text-align:right; } .state-chip,.evidence-state { border-radius:999px; font-size:.7rem; font-weight:750; padding:.2rem .45rem; text-transform:capitalize; } .state-running,.state-pending { background:var(--warning-surface); color:var(--warning-text); } .state-failed,.state-rejected { background:var(--danger-surface); color:var(--danger-text); } .measurement-row { display:flex; flex-wrap:wrap; gap:.5rem; margin-top:.75rem; } .measurement-row span { background:var(--surface-subtle); border-radius:.4rem; color:var(--text-muted); font-size:.72rem; padding:.35rem .5rem; } .measurement-row strong { color:var(--text); } .measurement-row small { display:block; font-size:.62rem; margin-top:.15rem; } .step-partial,.evidence-limit { color:var(--warning-text); font-size:.72rem; margin:.65rem 0 0; } .evidence-list { border-top:1px solid var(--border); margin-top:.75rem; padding-top:.65rem; } .evidence-heading { color:var(--text-muted); font-size:.7rem; font-weight:750; margin:0 0 .4rem; text-transform:uppercase; } .evidence-item { border-top:1px solid var(--border); padding:.35rem 0; } .evidence-item summary { align-items:center; cursor:pointer; display:flex; flex-wrap:wrap; gap:.45rem; list-style:none; min-height:2.2rem; } .evidence-item summary::-webkit-details-marker { display:none; } .evidence-item summary:focus-visible { outline:3px solid var(--focus); outline-offset:2px; } .evidence-item summary > span:nth-child(2) { flex:1 1 10rem; font-size:.78rem; font-weight:700; overflow-wrap:anywhere; } .evidence-media { color:var(--text-muted); font-size:.67rem; } .evidence-preview { background:var(--surface-subtle); border:1px solid var(--border); border-radius:.4rem; font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:.75rem; max-height:20rem; overflow:auto; padding:.75rem; white-space:pre-wrap; overflow-wrap:anywhere; } .evidence-image { background:var(--surface-subtle); border:1px solid var(--border); border-radius:.4rem; display:block; max-height:20rem; max-width:100%; object-fit:contain; } .evidence-loading,.evidence-error { color:var(--text-muted); font-size:.75rem; margin:.45rem 0; } .evidence-error { color:var(--danger-text); } .inspection-actions { border-top:1px solid var(--border); display:flex; flex-wrap:wrap; gap:.6rem; margin-top:1rem; padding-top:.9rem; } .inspection-actions button { border:1px solid var(--control-border); border-radius:.4rem; cursor:pointer; min-height:2.3rem; padding:.4rem .7rem; } .inspection-actions .secondary { background:transparent; color:var(--text); } .inspection-actions .danger { background:var(--danger-surface); color:var(--danger-text); } .inspection-unavailable { border-style:dashed; } .inspection-unavailable p { color:var(--text-muted); } .inspection-unavailable button { background:var(--accent); border:0; border-radius:.4rem; color:var(--on-accent); padding:.45rem .7rem; }
    @media (max-width:700px) { .inspection-card { padding:.9rem; } .inspection-step { margin-left:calc(var(--depth, 0) * .45rem); max-width:calc(100% - (var(--depth, 0) * .45rem)); } .step-heading { flex-direction:column; } .step-meta { align-items:flex-start; justify-content:flex-start; text-align:left; } .evidence-media { flex-basis:100%; margin-left:2.25rem; } .inspection-actions button { flex:1 1 100%; } }
    .load-more { background:var(--surface); border:1px solid var(--control-border); border-radius:.4rem; color:var(--accent-hover); cursor:pointer; font-size:.76rem; font-weight:750; margin:.6rem 0; min-height:2.2rem; padding:.45rem .7rem; }
    .load-more:focus-visible { outline:3px solid var(--focus); outline-offset:2px; }
    @media (max-width:700px) { .operator-kpis { grid-template-columns:repeat(2,minmax(0,1fr)); } .coverage-inline { grid-template-columns:repeat(3,minmax(0,1fr)); } .stage-row { grid-template-columns:auto minmax(0,1fr) auto; } .stage-errors { grid-column:2 / -1; } .technical-disclosure > summary .disclosure-hint { flex-basis:100%; margin-left:1.4rem; } }
    @media (max-width:420px) { .operator-kpis,.coverage-inline { grid-template-columns:1fr 1fr; } .stage-row { align-items:start; } .stage-duration { font-size:.7rem; } }
    @media (forced-colors:active) { .step-icon,.evidence-icon,.stage-icon { forced-color-adjust:none; } .step-icon,.stage-icon { background:Highlight; color:HighlightText; } }
  `, `
    .waterfall span { display:block; height:100%; min-width:2px; }
    .stage-facets { color:var(--text-muted); font-size:.68rem; white-space:nowrap; }
    .stage-cancelled,.stage-unavailable { border-left:3px solid var(--control-border); }
    @media (max-width:700px) { .operator-kpis { grid-template-columns:repeat(2,minmax(0,1fr)); } .stage-facets { grid-column:2 / -1; white-space:normal; } }
    .span-row { align-items:center; display:grid; gap:.6rem; grid-template-columns:minmax(0,1fr) minmax(6rem,1fr) auto auto; }
    .span-row .waterfall { align-self:center; display:block; min-height:.65rem; min-width:6rem; width:100%; }
    @media (max-width:700px) { .span-row { grid-template-columns:minmax(0,1fr) minmax(4rem,.65fr) auto auto; } .span-row .waterfall { grid-column:2; min-width:4rem; } }
    @media (max-width:420px) { .capture-note { font-size:.7rem; line-height:1.25; margin-bottom:.55rem; padding:.45rem .6rem; } .inspection-card { padding:.65rem; } .inspection-card > .section-heading { margin-bottom:.35rem; padding-bottom:.35rem; } .inspection-card > .section-heading .muted,.operator-summary > .section-heading .muted { display:none; } .operator-summary { padding:.55rem; } .operator-summary > .section-heading { margin-bottom:.35rem; padding-bottom:.35rem; } .operator-kpis { gap:.3rem; grid-template-columns:repeat(4,minmax(0,1fr)); margin:.55rem 0; } .operator-kpis div { padding:.35rem .25rem; } .operator-kpis strong { font-size:.68rem; } .operator-kpis span { font-size:.56rem; line-height:1.1; } .stage-list { gap:.2rem; } .stage-row { gap:.35rem; grid-template-columns:auto minmax(0,1fr) auto; padding:.38rem; } .stage-icon { height:1.25rem; width:1.25rem; } .stage-row h4 { font-size:.72rem; } .stage-row p { font-size:.59rem; line-height:1.1; max-height:1.2em; overflow:hidden; } .stage-duration { font-size:.6rem; } .stage-facets { display:none; } .stage-highlights { font-size:.62rem; gap:.25rem .5rem; margin:.3rem 0 .5rem; } .coverage-inline { gap:.2rem; grid-template-columns:repeat(3,minmax(0,1fr)); margin-top:.45rem; } .coverage-inline strong { font-size:.58rem; } .coverage-inline small { font-size:.55rem; padding-left:.9rem; } }
    .technical-disclosure > summary,.phase-record-disclosure > summary { list-style:revert; }
    .technical-disclosure > summary::-webkit-details-marker,.phase-record-disclosure > summary::-webkit-details-marker { display:inline; }
    .phase-record-disclosure { min-width:0; width:100%; }
    .phase-record-disclosure > summary { cursor:pointer; padding:.35rem 0; }
    .phase-record-disclosure > summary:focus-visible { outline:3px solid var(--focus); outline-offset:2px; }
  `],
})
export class ObservatoryTraceComponent implements OnDestroy {
  readonly store = inject(ObservatoryStore);
  readonly phases = PHASES;
  readonly evidenceItems = [
    { key: 'request', icon: 'runs' as ObservatoryIconName, label: 'Request & lifecycle' },
    { key: 'context', icon: 'document' as ObservatoryIconName, label: 'Chunks & context' },
    { key: 'prompt', icon: 'document' as ObservatoryIconName, label: 'Prompt text' },
    { key: 'generation', icon: 'runs' as ObservatoryIconName, label: 'Generated content' },
    { key: 'thinking', icon: 'overview' as ObservatoryIconName, label: 'Displayed thinking' },
    { key: 'tools', icon: 'operations' as ObservatoryIconName, label: 'Tool payloads' },
  ] as const;
  readonly MAX_CONTINUATION_PREVIEW = MAX_CONTINUATION_PREVIEW;
  readonly MAX_INSPECTION_STEPS = MAX_INSPECTION_STEPS;
  readonly MAX_TRACE_SPANS = MAX_TRACE_SPANS;
  technicalStepLimit = DETAIL_BATCH_SIZE;
  technicalSpanLimit = DETAIL_BATCH_SIZE;
  continuationOpen = false;
  readonly evidenceText = signal('');
  readonly evidenceImageUrl = signal<string | null>(null);
  private openEvidenceId: string | null = null;
  private openStageKeys = new Set<PhaseKey>();
  private lastSelectedRunId: string | null = null;
  private lastInspectionFingerprint: string | null = null;
  private evidenceDisclosureRequest = 0;
  constructor() {
    effect(() => {
      const runId = this.store.selectedRunId();
      if (runId === this.lastSelectedRunId) return;
      this.lastSelectedRunId = runId;
      this.technicalStepLimit = DETAIL_BATCH_SIZE;
      this.technicalSpanLimit = DETAIL_BATCH_SIZE;
      this.openStageKeys = new Set<PhaseKey>();
      for (const key of this.phases.map((phase) => phase.key)) delete this.phaseLimits[key];
      for (const key of Object.keys(this.evidenceLimits)) delete this.evidenceLimits[key];
      for (const key of Object.keys(this.measurementLimits)) delete this.measurementLimits[key];
      this.resetDisclosedEvidence();
    });
    effect(() => {
      const inspection = this.store.selectedRun().inspection;
      const fingerprint = this.inspectionFingerprint(inspection);
      if (fingerprint === this.lastInspectionFingerprint) return;
      this.lastInspectionFingerprint = fingerprint;
      // A live refresh can revoke a manifest without changing the selected run.
      // Clear both local disclosure state and the store's in-memory blob.
      this.resetDisclosedEvidence();
    });
  }
  ngOnDestroy(): void { this.resetDisclosedEvidence(); }
  @Output() readonly browseRuns = new EventEmitter<void>();
  @Input() set runId(value: string | null) { if (value) void this.store.openRun(value); }
  revealBatch<T>(items: ReadonlyArray<T>, visibleCount: number): ReadonlyArray<T> { return revealBatch(items, visibleCount); }
  boundedSpanCount(count: number): number { return Math.min(count, DETAIL_BATCH_SIZE); }
  boundedBatchSize(total: number, shown: number): number { return Math.min(DETAIL_BATCH_SIZE, Math.max(total - shown, 0)); }
  showMoreSteps(total: number): void { this.technicalStepLimit = Math.min(MAX_INSPECTION_STEPS, this.technicalStepLimit + DETAIL_BATCH_SIZE, total); }
  showMoreSpans(total: number): void { this.technicalSpanLimit = Math.min(MAX_TRACE_SPANS, this.technicalSpanLimit + DETAIL_BATCH_SIZE, total); }
  evidenceForStep(step: RunInspection['steps'][number]): ReadonlyArray<RunEvidenceManifest> { return revealBatch(step.evidence, this.evidenceLimits[step.stepId] ?? DETAIL_BATCH_SIZE); }
  showMoreEvidence(stepId: string, total: number): void { this.evidenceLimits[stepId] = Math.min(total, (this.evidenceLimits[stepId] ?? DETAIL_BATCH_SIZE) + DETAIL_BATCH_SIZE); }
  measurementsForStep(step: RunInspection['steps'][number]): RunInspection['steps'][number]['measurements'] { return revealBatch(step.measurements, this.measurementLimits[step.stepId] ?? DETAIL_BATCH_SIZE); }
  showMoreMeasurements(stepId: string, total: number): void { this.measurementLimits[stepId] = Math.min(MAX_MEASUREMENTS, total, (this.measurementLimits[stepId] ?? DETAIL_BATCH_SIZE) + DETAIL_BATCH_SIZE); }
  isStageOpen(key: PhaseKey): boolean { return this.openStageKeys.has(key); }
  toggleStage(key: PhaseKey, event: Event): void {
    const details = event.currentTarget as HTMLDetailsElement | null;
    const next = new Set(this.openStageKeys);
    if (details?.open) next.add(key); else next.delete(key);
    this.openStageKeys = next;
  }
  toggleTechnicalDisclosure(event: Event): void {
    const disclosure = event.currentTarget as HTMLDetailsElement | null;
    if (disclosure?.open) return;
    // A hidden parent can retain an open descendant <details> element. Close
    // those descendants before clearing the in-memory evidence payload so a
    // later reopen cannot present stale text or an object URL.
    closeOpenEvidenceDetails(disclosure);
    this.resetDisclosedEvidence();
  }
  formatDuration(durationMs: number | null): string { return formatDurationMs(durationMs); }
  durationAriaLabel(durationMs: number | null): string { return durationAriaLabel(durationMs); }
  statusIcon(status: string): ObservatoryIconName { return status === 'succeeded' ? 'success' : status === 'failed' || status === 'cancelled' ? 'danger' : status === 'running' || status === 'queued' ? 'warning' : 'overview'; }
  stepIcon(category: string): ObservatoryIconName { const icons: Record<string, ObservatoryIconName> = { admission: 'runs', context: 'document', retrieval: 'document', prompt: 'document', routing: 'analytics', generation: 'runs', thinking: 'overview', tool: 'operations', policy: 'operations', synthesis: 'document', persistence: 'retention', delivery: 'next', worker: 'operations', automation: 'operations', schedule: 'overview', evaluation: 'evaluation' }; return icons[category] ?? 'overview'; }
  mediaIcon(media: string): ObservatoryIconName { return media === 'image' ? 'image' : media === 'structured' ? 'json' : 'document'; }
  timelineSteps(inspection: RunInspection): RunInspection['steps'] { return aggregateInspectionSteps(inspection.steps, MAX_INSPECTION_STEPS); }
  stepLineageLabel(step: RunInspection['steps'][number]): string { return lineageLabel(step.parentStepId, step.parentSpanId); }
  stepAriaLabel(step: RunInspection['steps'][number], runStartedAt: string): string { return `${step.label}; level ${step.depth + 1}; ${this.stepLineageLabel(step)}; starts ${this.phaseOffset(step.startedAt, runStartedAt)}; state ${step.state}`; }
  completenessLabel(inspection: RunInspection): string { const state = inspection.completeness.state; return state === 'complete' ? 'Complete capture' : state === 'partial' ? 'Partial capture' : state === 'deleted' ? 'Evidence deleted' : 'Capture unavailable'; }
  categoryLabel(category: string): string { return category.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()); }
  partialReasonLabel(inspection: RunInspection): string { return this.reasonLabels([...inspection.partialReasons, ...inspection.completeness.reasons]); }
  runVerdict(run: RunDetail): string {
    if (run.status === 'failed') return 'Run failed';
    if (run.status === 'cancelled') return 'Run was cancelled';
    if (run.status === 'running' || run.status === 'queued') return 'Run is still in progress';
    if (run.partial) return 'Run completed with partial metadata';
    return 'Run completed successfully';
  }
  stageRows(): ReadonlyArray<StageSummary> {
    const inspection = this.store.selectedRun().inspection;
    const steps = inspection ? aggregateInspectionSteps(inspection.steps, Number.MAX_SAFE_INTEGER) : [];
    return this.phases.map((phase): StageSummary | null => {
      const items = this.phaseItemsUnbounded(phase.key);
      const phaseSteps = steps.filter((step) => phaseFor(`${step.category} ${step.label}`) === phase.key);
      // Inspection steps are the authoritative aggregate source when present;
      // otherwise use the reconciled foreground span/measurement projection.
      const useSteps = phaseSteps.length > 0;
      const count = useSteps ? phaseSteps.length : items.length;
      if (!count) return null;
      const durations = (useSteps ? phaseSteps.map((step) => step.durationMs) : items.map((item) => item.durationMs)).filter((value): value is number => value !== null && Number.isFinite(value));
      const states = useSteps ? phaseSteps.map((step) => step.state) : items.map((item) => item.outcome);
      const facets = stageOutcomeCounts(states, useSteps ? phaseSteps.map((step) => step.partial) : items.map((item) => item.partial));
      const failedCount = facets.failed;
      const runningCount = facets.running;
      const cancelledCount = facets.cancelled;
      const partialCount = facets.partial;
      const unavailableCount = facets.unavailable;
      const errorCount = failedCount;
      const state: StageSummary['state'] = failedCount ? 'failed' : runningCount ? 'running' : unavailableCount ? 'unavailable' : cancelledCount ? 'cancelled' : partialCount ? 'partial' : 'succeeded';
      const starts = (useSteps ? phaseSteps.map((step) => step.startedAt) : items.map((item) => item.startedAt)).filter((value) => Number.isFinite(Date.parse(value))).sort();
      const parentKeys = new Set(phaseSteps.map((step) => step.parentStepId ?? step.parentSpanId).filter((value): value is string => Boolean(value)));
      const partialReasons = useSteps ? [...new Set(phaseSteps.flatMap((step) => step.partialReasons))] : [];
      return { key: phase.key, label: phase.label, icon: phase.icon, count, durationMs: durations.length ? Math.max(...durations) : null, state, errorCount, failedCount, runningCount, partialCount, cancelledCount, unavailableCount, partial: partialCount > 0, parallel: this.hasOverlappingIntervals(phaseSteps), branched: parentKeys.size > 0 && phaseSteps.length > parentKeys.size, partialReasons, firstStartedAt: starts[0] ?? null };
    }).filter((stage): stage is StageSummary => stage !== null).sort((left, right) => (left.firstStartedAt ?? '').localeCompare(right.firstStartedAt ?? '') || left.key.localeCompare(right.key));
  }
  longestOperationLabel(): string { const stage = this.stageRows().filter((item) => item.durationMs !== null).sort((left, right) => (right.durationMs ?? 0) - (left.durationMs ?? 0))[0]; return stage ? `${stage.label} · ${this.formatDuration(stage.durationMs)}` : 'Unavailable'; }
  errorCount(): number { return this.stageRows().reduce((total, stage) => total + stage.errorCount, 0); }
  outcomeCount(kind: 'failed' | 'running' | 'partial' | 'cancelled' | 'unavailable'): number { return this.stageRows().reduce((total, stage) => total + ({ failed: stage.failedCount, running: stage.runningCount, partial: stage.partialCount, cancelled: stage.cancelledCount, unavailable: stage.unavailableCount }[kind]), 0); }
  evidenceSummaryLabel(inspection: RunInspection): string { const items = this.coverageItems(); const captured = items.filter((item) => item.state === 'captured').length; const unavailable = items.filter((item) => ['unavailable', 'not-observed'].includes(item.state)).length; return `${captured}/${items.length} captured${unavailable ? ` · ${unavailable} unavailable` : ''}`; }
  stageSummaryLabel(key: PhaseKey): string { const stage = this.stageRows().find((item) => item.key === key); return stage ? summarizeStage(stage.state, stage.durationMs) : 'not observed'; }
  stagePreview(key: PhaseKey): ReadonlyArray<PhaseItem> { return this.phaseItemsUnbounded(key).slice(0, 20); }
  reasonLabels(reasons: ReadonlyArray<string>): string { return reasons.length ? [...new Set(reasons)].map((reason) => reason.replaceAll('_', ' ')).join(', ') : 'bounded capture'; }
  evidenceLabel(kind: string): string { return kind.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()); }
  captureStateDescription(evidence: RunEvidenceManifest): string { return evidence.reason ? `${this.evidenceLabel(evidence.captureState)}: ${this.reasonLabels([evidence.reason])}.` : `Evidence is ${evidence.captureState}; no bytes are available.`; }
  isEvidenceOpen(evidence: RunEvidenceManifest): boolean { return this.openEvidenceId === evidence.evidenceId; }
  loadedEvidenceMatches(evidence: RunEvidenceManifest): boolean { const loaded = this.store.selectedRunEvidence(); const current = this.currentEvidenceManifest(evidence.evidenceId); return Boolean(loaded && current && loaded.evidenceId === evidence.evidenceId && this.store.selectedRunEvidenceState() === 'ready' && isCurrentCapturedRunEvidenceManifest(current, loaded.manifest) && isCurrentCapturedRunEvidenceManifest(current, evidence)); }
  async toggleEvidence(event: Event, evidence: RunEvidenceManifest): Promise<void> { const details = event.target as HTMLDetailsElement; if (!details.open) { if (this.openEvidenceId === evidence.evidenceId) this.clearEvidence(); return; } const request = ++this.evidenceDisclosureRequest; this.openEvidenceId = evidence.evidenceId; this.evidenceText.set(''); this.revokeImageUrl(); if (evidence.captureState !== 'captured') return; await this.store.loadRunEvidence(evidence); if (!this.isCurrentEvidenceDisclosure(request, evidence)) return; const loaded = this.store.selectedRunEvidence(); if (!loaded || !isCurrentCapturedRunEvidenceManifest(this.currentEvidenceManifest(evidence.evidenceId), loaded.manifest)) return; if (evidence.mediaClassification === 'text' || evidence.mediaClassification === 'structured') { const text = (await loaded.blob.text()).slice(0, MAX_EVIDENCE_TEXT); if (!this.isCurrentEvidenceDisclosure(request, evidence) || !isCurrentCapturedRunEvidenceManifest(this.currentEvidenceManifest(evidence.evidenceId), loaded.manifest)) return; this.evidenceText.set(text); } else if (evidence.mediaClassification === 'image') { if (!this.isCurrentEvidenceDisclosure(request, evidence)) return; this.evidenceImageUrl.set(URL.createObjectURL(loaded.blob)); } }
  clearEvidence(): void { this.evidenceDisclosureRequest += 1; this.openEvidenceId = null; this.evidenceText.set(''); this.revokeImageUrl(); this.store.clearRunEvidence(); }
  deleteEvidence(): void { if (typeof window !== 'undefined' && !window.confirm('Delete all retained evidence for this run? Metadata will remain visible.')) return; void this.store.deleteRunEvidence(); }
  private revokeImageUrl(): void { const url = this.evidenceImageUrl(); if (url) URL.revokeObjectURL(url); this.evidenceImageUrl.set(null); }
  private resetDisclosedEvidence(): void { this.evidenceDisclosureRequest += 1; this.openEvidenceId = null; this.evidenceText.set(''); this.revokeImageUrl(); this.store.clearRunEvidence(); }
  private currentEvidenceManifest(evidenceId: string): RunEvidenceManifest | null { return this.store.selectedRun().inspection?.steps.flatMap((step) => step.evidence).find((manifest) => manifest.evidenceId === evidenceId) ?? null; }
  private isCurrentEvidenceDisclosure(request: number, evidence: RunEvidenceManifest): boolean { const loaded = this.store.selectedRunEvidence(); return request === this.evidenceDisclosureRequest && this.openEvidenceId === evidence.evidenceId && loaded?.runId === this.store.selectedRunId() && isCurrentCapturedRunEvidenceManifest(this.currentEvidenceManifest(evidence.evidenceId), evidence); }
  private inspectionFingerprint(inspection: RunInspection | null): string | null { if (!inspection) return null; return JSON.stringify({ completeness: inspection.completeness, partial: inspection.partial, steps: inspection.steps.map((step) => ({ stepId: step.stepId, state: step.state, sequence: step.sequence, evidence: step.evidence.map((manifest) => ({ evidenceId: manifest.evidenceId, captureState: manifest.captureState, mediaClassification: manifest.mediaClassification, mediaType: manifest.mediaType, digest: manifest.digest, byteCount: manifest.byteCount })) })) }); }
  coverageItems(): ReadonlyArray<(typeof this.evidenceItems)[number] & { readonly state: 'captured' | 'excluded' | 'partial' | 'dropped' | 'deleted' | 'unavailable' | 'not-observed' }> {
    const inspection = this.store.selectedRun().inspection;
    const kinds: Record<string, ReadonlyArray<string>> = { request: ['admitted_request', 'admitted_trigger'], context: ['conversation_context', 'memory_chunk', 'document_chunk', 'model_visible_attachment'], prompt: ['compiled_prompt'], generation: ['generation_output'], thinking: ['displayed_thinking'], tools: ['tool_arguments', 'tool_result'] };
    return this.evidenceItems.map((item) => {
      const manifests = inspection?.steps.flatMap((step) => step.evidence).filter((manifest) => kinds[item.key].includes(manifest.kind)) ?? [];
      if (!manifests.length) return { ...item, state: inspection ? 'not-observed' : 'unavailable' };
      const states = new Set(manifests.map((manifest) => manifest.captureState));
      if (states.size === 1) return { ...item, state: manifests[0].captureState };
      // A mixed manifest set is not wholly captured: retain the authoritative
      // per-manifest badges and summarize the aggregate as partial.
      return { ...item, state: 'partial' };
    });
  }
  coverageStateLabel(state: ReturnType<ObservatoryTraceComponent['coverageItems']>[number]['state']): string { return ({ captured: 'Captured', excluded: 'Excluded by policy', partial: 'Partially captured', dropped: 'Dropped before storage', deleted: 'Deleted by owner', unavailable: 'Unavailable', 'not-observed': 'Not observed in this run' } as Record<string, string>)[state]; }
  evidenceAvailableCount(): number { return this.coverageItems().filter((item) => item.state === 'captured').length; }
  itemCount(): number { return this.phases.reduce((total, phase) => total + this.phaseItemCount(phase.key), 0); }
  phaseItemCount(key: PhaseKey): number { return this.phaseItemsUnbounded(key).length; }
  phaseLimitLabel(): string { const total = this.itemCount(); const visible = this.phases.reduce((count, phase) => count + this.phaseItems(phase.key).length, 0); return total === visible ? 'all records shown' : `${visible} of ${total} shown`; }
  showMorePhase(key: PhaseKey): void { this.phaseLimits[key] = Math.min(this.phaseItemCount(key), (this.phaseLimits[key] ?? 20) + 20); }
  continuationSummary(): ContinuationSummary {
    const detail = this.store.selectedRun().detail;
    if (!detail) return { count: 0, startedAt: null, endedAt: null, outcomes: [], partial: false };
    return summarizeContinuation(detail.observations.map((item) => ({ label: item.measurement.name, startedAt: item.observedAt, outcome: item.outcome })), this.terminalBoundary(detail), detail.partial || detail.nextCursor !== null);
  }
  continuationPreview(): ReadonlyArray<PhaseItem> {
    const detail = this.store.selectedRun().detail;
    if (!detail) return [];
    const boundary = this.terminalBoundary(detail);
    return detail.observations
      .map((item) => this.observationItem(item))
      .filter((item) => !isAtOrBeforeBoundary(item.startedAt, boundary))
      .sort((left, right) => Date.parse(left.startedAt) - Date.parse(right.startedAt) || left.id.localeCompare(right.id))
      .slice(0, MAX_CONTINUATION_PREVIEW);
  }
  formatContinuationRange(summary: ContinuationSummary): string {
    if (!summary.startedAt || !summary.endedAt) return 'Time range unavailable';
    const start = new Date(summary.startedAt);
    const end = new Date(summary.endedAt);
    const prefix = summary.partial ? 'Loaded metadata window: ' : 'Observed ';
    return start.getTime() === end.getTime() ? `${prefix}${start.toISOString()}` : `${prefix}${start.toISOString()} – ${end.toISOString()}`;
  }
  continuationCountLabel(summary: ContinuationSummary): string {
    const count = summary.count.toLocaleString('en-US');
    return summary.partial ? `${count}+ loaded records` : `${count} record${summary.count === 1 ? '' : 's'}`;
  }
  orderedPhases(): ReadonlyArray<PhaseDefinition> {
    const observed = new Set(this.phases.filter((phase) => this.phaseItems(phase.key).length > 0).map((phase) => phase.key));
    return orderPhases(this.phases, observed);
  }
  recordValue(item: PhaseItem): string {
    if (item.kind === 'measurement' && item.measurementValue !== null && item.measurementUnit === 'milliseconds') return formatDurationMs(item.measurementValue);
    if (item.kind === 'measurement' && item.measurementValue !== null) return `${item.measurementValue}${item.measurementUnit ? ` ${item.measurementUnit}` : ''}`;
    return formatDurationMs(item.durationMs);
  }
  recordValueAriaLabel(item: PhaseItem): string {
    if (item.kind === 'measurement' && item.measurementValue !== null) return `${item.measurementValue}${item.measurementUnit ? ` ${item.measurementUnit}` : ''}`;
    return durationAriaLabel(item.durationMs);
  }
  phaseOffset(startedAt: string, runStartedAt: string): string {
    const offsetMs = Date.parse(startedAt) - Date.parse(runStartedAt);
    if (!Number.isFinite(offsetMs)) return 'offset unavailable';
    const sign = offsetMs < 0 ? '−' : '+';
    return `${sign}${formatDurationMs(Math.abs(offsetMs))}`;
  }
  spanLineageLabel(span: Span): string { return spanLineageText(span.parentSpanId); }
  spanAriaLabel(span: Span, runStartedAt: string): string { return `${span.operation}; level ${span.depth + 1}; ${this.spanLineageLabel(span)}; starts ${this.phaseOffset(span.startedAt, runStartedAt)}; outcome ${span.outcome}`; }
  isParallelSpan(span: Span, spans: ReadonlyArray<Span>): boolean {
    const start = Date.parse(span.startedAt); const end = start + span.durationMs;
    return Number.isFinite(start) && end > start && spans.some((candidate) => candidate.spanId !== span.spanId && candidate.parentSpanId === span.parentSpanId && Date.parse(candidate.startedAt) < end && start < Date.parse(candidate.startedAt) + candidate.durationMs);
  }
  private hasOverlappingIntervals(steps: ReadonlyArray<RunInspection['steps'][number]>): boolean {
    const intervals = steps.map((step) => {
      const start = Date.parse(step.startedAt);
      const end = step.finishedAt ? Date.parse(step.finishedAt) : step.durationMs !== null ? start + step.durationMs : Number.NaN;
      return { parent: step.parentStepId ?? step.parentSpanId, start, end };
    }).filter((interval) => Number.isFinite(interval.start) && Number.isFinite(interval.end) && interval.end > interval.start);
    return intervals.some((left, index) => intervals.slice(index + 1).some((right) => left.parent === right.parent && left.start < right.end && right.start < left.end));
  }
  phaseItems(key: PhaseKey): ReadonlyArray<PhaseItem> {
    return this.phaseItemsUnbounded(key).slice(0, this.phaseLimits[key] ?? 20);
  }
  private readonly phaseLimits: Partial<Record<PhaseKey, number>> = {};
  private readonly evidenceLimits: Record<string, number> = {};
  private readonly measurementLimits: Record<string, number> = {};
  private phaseItemsUnbounded(key: PhaseKey): ReadonlyArray<PhaseItem> {
    const detail = this.store.selectedRun().detail;
    const trace = this.store.selectedRun().trace;
    const observations = (detail?.observations ?? []).map((item) => this.observationItem(item));
    const spans = (trace?.spans ?? []).map((item) => this.spanItem(item));
    const boundary = detail ? this.terminalBoundary(detail) : null;
    return foregroundPhaseItemsFor(key, spans, observations, boundary);
  }
  private terminalBoundary(detail: RunDetail): string | null {
    const lifecycle = detail.observations.map((item) => ({ label: item.measurement.name, startedAt: item.observedAt, outcome: item.outcome }));
    return terminalObservationBoundary(lifecycle, TERMINAL_STATUSES.has(detail.status) ? detail.finishedAt : null);
  }
  private observationItem(item: Observation): PhaseItem { return { id: `measurement-${item.observationId}`, label: item.measurement.name, operation: item.operation, componentId: item.componentId, component: `${item.componentId} · ${item.serviceRole}`, outcome: item.outcome, startedAt: item.observedAt, durationMs: item.measurement.unit === 'milliseconds' ? item.measurement.value : null, measurementValue: item.measurement.value, measurementUnit: item.measurement.unit, partial: false, kind: 'measurement' }; }
  private spanItem(item: Span): PhaseItem { return { id: `span-${item.spanId}`, label: item.operation, operation: item.operation, componentId: item.componentId, component: item.componentId, outcome: item.outcome, startedAt: item.startedAt, durationMs: item.durationMs, measurementValue: null, measurementUnit: null, partial: item.partial, kind: 'span' }; }
  barWidth(duration: number): number { const spans = this.store.selectedRun().trace?.spans ?? []; const max = Math.max(...spans.map((span) => span.durationMs), 1); return Math.max(2, Math.round((duration / max) * 100)); }
  maxDuration(): number { return Math.max(...(this.store.selectedRun().trace?.spans ?? []).map((span) => span.durationMs), 0); }
  retry(): void { const runId = this.store.selectedRunId(); if (runId) void this.store.openRun(runId); }
}
