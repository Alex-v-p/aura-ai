import type { RunInspection, TraceDetail } from '@aura/observatory-api-client';

export type PhaseKey = 'request' | 'context' | 'prompt' | 'routing' | 'generation' | 'thinking' | 'tools' | 'persistence' | 'delivery' | 'other';
export type PhaseItem = { readonly id: string; readonly label: string; readonly operation: string; readonly componentId: string; readonly component: string; readonly outcome: string; readonly startedAt: string; readonly durationMs: number | null; readonly measurementValue: number | null; readonly measurementUnit: string | null; readonly partial: boolean; readonly kind: 'span' | 'measurement' };
export type LifecycleObservation = { readonly label: string; readonly startedAt: string; readonly outcome: string };
export type ContinuationSummary = { readonly count: number; readonly startedAt: string | null; readonly endedAt: string | null; readonly outcomes: ReadonlyArray<string>; readonly partial: boolean };

export function lineageLabel(parentStepId: string | null, parentSpanId: string | null): string {
  return parentStepId ? `parent step ${parentStepId}` : parentSpanId ? `parent span ${parentSpanId}` : 'root branch';
}

export function spanLineageLabel(parentSpanId: string | null): string { return parentSpanId ? `parent ${parentSpanId}` : 'root span'; }

export function hasParallelBranch(parentSpanId: string | null, siblingParentIds: ReadonlyArray<string | null>): boolean {
  return parentSpanId !== null && siblingParentIds.filter((candidate) => candidate === parentSpanId).length > 1;
}

function siblingKey(parentStepId: string | null, parentSpanId: string | null, depth: number): string {
  if (parentStepId) return `step:${parentStepId}`;
  if (parentSpanId) return `span:${parentSpanId}`;
  return `root:${depth}`;
}

/** ARIA tree positions are relative to siblings, not the flattened sequence. */
export function inspectionStepPosition(step: RunInspection['steps'][number], steps: ReadonlyArray<RunInspection['steps'][number]>): number {
  const key = siblingKey(step.parentStepId, step.parentSpanId, step.depth);
  return steps.filter((candidate) => siblingKey(candidate.parentStepId, candidate.parentSpanId, candidate.depth) === key).findIndex((candidate) => candidate.stepId === step.stepId) + 1;
}

export function inspectionStepSetSize(step: RunInspection['steps'][number], steps: ReadonlyArray<RunInspection['steps'][number]>): number {
  const key = siblingKey(step.parentStepId, step.parentSpanId, step.depth);
  return steps.filter((candidate) => siblingKey(candidate.parentStepId, candidate.parentSpanId, candidate.depth) === key).length;
}

export function traceSpanPosition(span: TraceDetail['spans'][number], spans: ReadonlyArray<TraceDetail['spans'][number]>): number {
  const key = siblingKey(null, span.parentSpanId, span.depth);
  return spans.filter((candidate) => siblingKey(null, candidate.parentSpanId, candidate.depth) === key).findIndex((candidate) => candidate.spanId === span.spanId) + 1;
}

export function traceSpanSetSize(span: TraceDetail['spans'][number], spans: ReadonlyArray<TraceDetail['spans'][number]>): number {
  const key = siblingKey(null, span.parentSpanId, span.depth);
  return spans.filter((candidate) => siblingKey(null, candidate.parentSpanId, candidate.depth) === key).length;
}

/** Aggregate repeat deliveries by stable step identity before applying the UI bound. */
export function aggregateInspectionSteps(steps: RunInspection['steps'], limit: number): RunInspection['steps'] {
  const aggregate = new Map<string, RunInspection['steps'][number]>();
  const rank = (state: string): number => ({ failed: 6, rejected: 5, cancelled: 4, succeeded: 3, running: 2, pending: 1, unavailable: 0 }[state] ?? 0);
  for (const step of [...steps].sort((left, right) => left.sequence - right.sequence || left.stepId.localeCompare(right.stepId))) {
    const prior = aggregate.get(step.stepId);
    if (!prior) { aggregate.set(step.stepId, step); continue; }
    aggregate.set(step.stepId, { ...prior, state: rank(step.state) >= rank(prior.state) ? step.state : prior.state, finishedAt: step.finishedAt ?? prior.finishedAt, durationMs: step.durationMs ?? prior.durationMs, measurements: step.measurements.length ? step.measurements : prior.measurements, evidence: [...new Map([...prior.evidence, ...step.evidence].map((item) => [item.evidenceId, item])).values()], partial: prior.partial || step.partial, partialReasons: [...new Set([...prior.partialReasons, ...step.partialReasons])] });
  }
  return [...aggregate.values()].sort((left, right) => left.sequence - right.sequence || left.stepId.localeCompare(right.stepId)).slice(0, limit);
}

const TERMINAL_OUTCOMES = new Set(['succeeded', 'failed', 'cancelled']);

/**
 * Return the authoritative request boundary from the lifecycle observation.
 * A run can continue emitting correlated worker metadata after this point;
 * that metadata is not part of the foreground request timeline.
 */
export function terminalLifecycleAt(observations: ReadonlyArray<LifecycleObservation>, fallback: string | null = null): string | null {
  const terminal = observations
    .filter((item) => item.label === 'run_status' && TERMINAL_OUTCOMES.has(item.outcome) && Number.isFinite(Date.parse(item.startedAt)))
    .sort((left, right) => Date.parse(left.startedAt) - Date.parse(right.startedAt));
  return terminal.at(-1)?.startedAt ?? fallback;
}

/**
 * Bound detail observations after the lifecycle settles. Core may publish the
 * terminal status before its canonical duration measurement, so the boundary
 * includes both lifecycle records and the terminal run duration timestamp.
 */
export function terminalObservationBoundary(observations: ReadonlyArray<LifecycleObservation>, finishedAt: string | null = null): string | null {
  const candidates = [
    terminalLifecycleAt(observations),
    ...observations.filter((item) => item.label === 'run_duration_ms' && Number.isFinite(Date.parse(item.startedAt))).map((item) => item.startedAt),
    finishedAt,
  ].filter((value): value is string => value !== null && value !== '' && Number.isFinite(Date.parse(value)));
  return candidates.sort((left, right) => Date.parse(left) - Date.parse(right)).at(-1) ?? null;
}

export function isAtOrBeforeBoundary(startedAt: string, boundary: string | null): boolean {
  if (!boundary) return true;
  const timestamp = Date.parse(startedAt);
  const boundaryTimestamp = Date.parse(boundary);
  return Number.isFinite(timestamp) && Number.isFinite(boundaryTimestamp) && timestamp <= boundaryTimestamp;
}

/** Summarize late metadata without exposing an unbounded sequence in the UI. */
export function continuationSummary(observations: ReadonlyArray<LifecycleObservation>, boundary: string | null, partial = false): ContinuationSummary {
  const late = observations
    .filter((item) => !isAtOrBeforeBoundary(item.startedAt, boundary))
    .sort((left, right) => Date.parse(left.startedAt) - Date.parse(right.startedAt));
  return {
    count: late.length,
    startedAt: late[0]?.startedAt ?? null,
    endedAt: late.at(-1)?.startedAt ?? null,
    outcomes: [...new Set(late.map((item) => item.outcome))].sort(),
    partial,
  };
}

export function phaseFor(value: string): PhaseKey {
  const text = value.toLowerCase();
  // These compound operation names are explicit so broad component words do
  // not move persistence or routing records into a neighbouring phase.
  if (/memory[._-](?:persist|write|save)/.test(text)) return 'persistence';
  if (/memory[._-](?:retriev|search|rank|rerank)/.test(text) || /document[._-](?:retriev|search|rank|rerank)/.test(text)) return 'context';
  if (/model[._-](?:route|routing|select|selection)/.test(text)) return 'routing';
  if (/tool(?:[._\s-]|$)|policy|approval/.test(text)) return 'tools';
  if (/run_started|run_status|admission|request|(?:^|[._\s-])run[._-]execute(?:$|[._\s-])/.test(text)) return 'request';
  if (/retriev|memory|document|search|rerank|chunk|context/.test(text)) return 'context';
  if (/prompt|persona|synthesi|assembly/.test(text)) return 'prompt';
  if (/rout|model_selection|provider/.test(text)) return 'routing';
  if (/generation|inference|llm|model|token|assistant/.test(text)) return 'generation';
  if (/thinking|reasoning/.test(text)) return 'thinking';
  if (/persist|database|outbox|repository/.test(text)) return 'persistence';
  if (/deliver|stream|sse|notification/.test(text)) return 'delivery';
  return 'other';
}

function isSameTraceRecord(span: PhaseItem, measurement: PhaseItem): boolean {
  if (span.componentId !== measurement.componentId || span.operation !== measurement.operation || span.outcome !== measurement.outcome) return false;
  const sameTimestamp = span.startedAt === measurement.startedAt;
  const sameDuration = span.durationMs !== null && measurement.durationMs !== null && Math.abs(span.durationMs - measurement.durationMs) < 0.001;
  return sameTimestamp && sameDuration;
}

/** Prefer the trace lineage record when a run detail observation is its source. */
export function phaseItemsFor(key: PhaseKey, spans: ReadonlyArray<PhaseItem>, measurements: ReadonlyArray<PhaseItem>): ReadonlyArray<PhaseItem> {
  const phaseSpans = spans.filter((item) => phaseFor(`${item.componentId} ${item.operation}`) === key);
  const phaseMeasurements = measurements.filter((item) => phaseFor(`${item.componentId} ${item.operation} ${item.label}`) === key && !phaseSpans.some((span) => isSameTraceRecord(span, item)));
  return [...phaseSpans, ...phaseMeasurements].sort((left, right) => Date.parse(left.startedAt) - Date.parse(right.startedAt) || left.id.localeCompare(right.id));
}

/** Keep selected primary trace lineage foreground; apply the lifecycle fence only to detail measurements. */
export function foregroundPhaseItemsFor(key: PhaseKey, spans: ReadonlyArray<PhaseItem>, measurements: ReadonlyArray<PhaseItem>, boundary: string | null): ReadonlyArray<PhaseItem> {
  return phaseItemsFor(key, spans, measurements).filter((item) => item.kind === 'span' || isAtOrBeforeBoundary(item.startedAt, boundary));
}

/** Keep the lifecycle's canonical phase order while moving observed phases ahead of compact gaps. */
export function orderPhases<T extends { readonly key: PhaseKey }>(phases: ReadonlyArray<T>, observed: ReadonlySet<PhaseKey>): ReadonlyArray<T> {
  return phases.map((phase, index) => ({ phase, index })).sort((left, right) => Number(!observed.has(left.phase.key)) - Number(!observed.has(right.phase.key)) || left.index - right.index).map(({ phase }) => phase);
}
