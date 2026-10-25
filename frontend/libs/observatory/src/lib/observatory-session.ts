/** Reads only the server-issued CSRF value; callers must keep it in memory. */
export function extractSessionCsrf(payload: unknown): string | null {
  if (!payload || typeof payload !== 'object') return null;
  const value = (payload as { csrfToken?: unknown }).csrfToken;
  return typeof value === 'string' && value.length >= 32 && value.length <= 256 ? value : null;
}
