export function normalizeActivityBoundary(name: 'activityFrom' | 'activityTo', value: string): string | undefined {
  if (!value) return undefined;
  const date = new Date(`${value}T00:00:00.000Z`);
  if (Number.isNaN(date.getTime())) return undefined;
  if (name === 'activityFrom') return date.toISOString();
  date.setUTCDate(date.getUTCDate() + 1);
  return date.toISOString();
}
