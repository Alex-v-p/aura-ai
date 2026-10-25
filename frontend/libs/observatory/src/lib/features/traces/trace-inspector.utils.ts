export type PhaseKey = 'request' | 'context' | 'prompt' | 'routing' | 'generation' | 'thinking' | 'tools' | 'persistence' | 'delivery' | 'other';
export type PhaseItem = { readonly id: string; readonly label: string; readonly operation: string; readonly componentId: string; readonly component: string; readonly outcome: string; readonly startedAt: string; readonly durationMs: number | null; readonly measurementValue: number | null; readonly measurementUnit: string | null; readonly partial: boolean; readonly kind: 'span' | 'measurement' };

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

/** Keep the lifecycle's canonical phase order while moving observed phases ahead of compact gaps. */
export function orderPhases<T extends { readonly key: PhaseKey }>(phases: ReadonlyArray<T>, observed: ReadonlySet<PhaseKey>): ReadonlyArray<T> {
  return phases.map((phase, index) => ({ phase, index })).sort((left, right) => Number(!observed.has(left.phase.key)) - Number(!observed.has(right.phase.key)) || left.index - right.index).map(({ phase }) => phase);
}
