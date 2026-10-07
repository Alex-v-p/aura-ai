const SAFE_RUN_ERROR_MESSAGES: Readonly<Record<string, string>> = {
  DELIVERY_ERROR: 'The run update could not be delivered.',
  MODEL_TIMEOUT: 'The model provider exceeded the configured run timeout.',
  PERSISTENCE_ERROR: 'The run could not be persisted.',
  PROVIDER_ERROR: 'The model provider could not complete this run.',
  RUN_TIMEOUT: 'The model provider exceeded the configured run timeout.',
};

export function safeRunErrorDisplay(error: { readonly code: string; readonly message: string } | null): string | null {
  if (!error) return null;
  const code = /^[A-Z][A-Z0-9_]{0,63}$/.test(error.code) ? error.code : null;
  const message = code ? SAFE_RUN_ERROR_MESSAGES[code] : undefined;
  return message ? `${code}: ${message}` : 'The run could not be completed.';
}

export function safeTraceReference(traceId: string | null | undefined): string | null {
  return typeof traceId === 'string' && /^[0-9a-f]{32}$/i.test(traceId) ? traceId : null;
}
