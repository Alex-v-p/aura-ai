const OUTCOME_COPY: Readonly<Record<string, { readonly label: string; readonly announcement: string }>> = {
  completed: { label: 'Completed', announcement: 'Research completed.' },
  partial: { label: 'Partial results', announcement: 'Research completed with partial results.' },
  budget_exhausted: { label: 'Budget exhausted', announcement: 'Research stopped because its budget was exhausted.' },
  canceled: { label: 'Canceled', announcement: 'Research was canceled.' },
  failed: { label: 'Failed', announcement: 'Research failed.' },
  interrupted: { label: 'Interrupted', announcement: 'Research was interrupted.' },
};

export function researchLifecycleLabel(value: string): string {
  if (value === 'admitted') return 'Admitted';
  if (value === 'detached') return 'Continuing in the background';
  if (value === 'recovery unavailable') return 'Recovery unavailable';
  if (value.startsWith('terminal')) return OUTCOME_COPY[value.replace('terminal · ', '')]?.label ?? 'Complete';
  if (value === 'queued') return 'Queued';
  if (value === 'running') return 'Running';
  if (value === 'waiting') return 'Waiting';
  return value.replaceAll('_', ' ').replace(/^./, character => character.toUpperCase());
}

export function researchLifecycleAnnouncement(value: string): string {
  if (value === 'admitted') return 'Research admitted.';
  if (value === 'detached') return 'Research is continuing in the background.';
  if (value === 'recovery unavailable') return 'Research status is temporarily unavailable.';
  if (value.startsWith('terminal')) return OUTCOME_COPY[value.replace('terminal · ', '')]?.announcement ?? 'Research completed.';
  if (value === 'queued') return 'Research is queued.';
  if (value === 'running') return 'Research is running.';
  if (value === 'waiting') return 'Research is waiting for its next step.';
  return `Research status: ${researchLifecycleLabel(value).toLowerCase()}.`;
}
