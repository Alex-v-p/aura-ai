export function publicMemoryError(status: number): string {
  switch (status) {
    case 401: return 'Your session has expired. Sign in again to manage memory.';
    case 403: return 'You do not have permission to manage this memory.';
    case 404: return 'That memory is no longer available.';
    case 409: return 'This memory changed elsewhere. Refresh and try again.';
    case 422: return 'The memory request needs correction before it can be saved.';
    case 429: return 'Memory service is busy. Try again shortly.';
    case 503: return 'Memory service is temporarily unavailable. Your draft is still here.';
    default: return 'We could not complete that memory request. Your draft is still here.';
  }
}

/** Keeps one retry-safe idempotency key for the same retained manual draft. */
export class CreateRequestIdempotency {
  private fingerprint: string | null = null;
  private requestKey: string | null = null;

  next(body: unknown): string {
    const fingerprint = JSON.stringify(body);
    if (fingerprint !== this.fingerprint || !this.requestKey) {
      this.fingerprint = fingerprint;
      this.requestKey = typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `aura-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    }
    return this.requestKey;
  }

  clear(): void { this.fingerprint = null; this.requestKey = null; }
}
