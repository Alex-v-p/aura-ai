import { describe, expect, it } from 'vitest';
import { safeRunErrorDisplay, safeTraceReference } from './run-detail-safety';

describe('conversation run detail sanitization', () => {
  it.each([
    'stack: /srv/aura/secrets.env token=private',
    'provider payload {"credential":"private"}',
    'worker attempt lease=private',
  ])('does not expose adversarial server text: %s', (message) => {
    const display = safeRunErrorDisplay({ code: 'PROVIDER_ERROR', message });
    expect(display).toBe('PROVIDER_ERROR: The model provider could not complete this run.');
    expect(display).not.toContain(message);
    expect(display).not.toMatch(/srv|credential|private|lease|worker|attempt/i);
  });

  it('uses a generic fallback for unbounded error codes', () => {
    expect(safeRunErrorDisplay({ code: 'provider\nsecret', message: 'private payload' })).toBe('The run could not be completed.');
  });

  it('only exposes strict 32-hex trace references', () => {
    expect(safeTraceReference('0123456789abcdef0123456789abcdef')).toBe('0123456789abcdef0123456789abcdef');
    expect(safeTraceReference('trace-failed')).toBeNull();
    expect(safeTraceReference('0123456789abcdef0123456789abcde/')).toBeNull();
    expect(safeTraceReference('0123456789abcdef0123456789abcdef<script>')).toBeNull();
  });
});
