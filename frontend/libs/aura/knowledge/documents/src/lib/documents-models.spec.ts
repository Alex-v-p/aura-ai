import { describe, expect, it } from 'vitest';
import { DOCUMENT_MAX_BYTES, DOCUMENT_SEARCH_LIMIT, EphemeralDocumentOperationKeys, documentSearchRequest, documentTypeLabel, formatDocumentSize, mediaTypeForFile, validateDocumentFile } from './documents-models';

function file(name: string, size: number, type = ''): File { return new File([new Uint8Array(Math.max(1, Math.min(size, 16)))], name, { type }); }

describe('document upload guidance', () => {
  it('accepts supported formats and maps their media types', () => {
    expect(mediaTypeForFile(file('guide.pdf', 20, 'application/pdf'))).toBe('application/pdf');
    expect(mediaTypeForFile(file('notes.md', 20, 'text/markdown'))).toBe('text/markdown');
    expect(validateDocumentFile(file('notes.txt', 20, 'text/plain'))).toBeNull();
  });
  it('enforces the exact 100 MB browser boundary', () => {
    const oversized = { name: 'large.txt', size: DOCUMENT_MAX_BYTES + 1, type: 'text/plain' } as File;
    expect(validateDocumentFile(oversized)).toContain('100 MB');
    expect(validateDocumentFile({ name: 'exact.txt', size: DOCUMENT_MAX_BYTES, type: 'text/plain' } as File)).toBeNull();
  });
  it('keeps labels and sizes readable', () => {
    expect(documentTypeLabel('application/vnd.openxmlformats-officedocument.wordprocessingml.document')).toBe('DOCX');
    expect(formatDocumentSize(1024 * 1024)).toBe('1.0 MB');
  });
  it('keeps mutation idempotency keys stable until the owner operation is reconciled', () => {
    const keys = new EphemeralDocumentOperationKeys();
    const first = keys.get('delete:document-1');
    expect(keys.get('delete:document-1')).toBe(first);
    keys.forget('delete:document-1');
    expect(keys.get('delete:document-1')).not.toBe(first);
    keys.clear();
    expect(keys.get('retry:document-1')).not.toBe(first);
  });
  it('builds an in-contract bounded search body', () => {
    const request = documentSearchRequest('  private notes  ', 'next-page');
    expect(request).toEqual({ query: 'private notes', limit: DOCUMENT_SEARCH_LIMIT, cursor: 'next-page' });
    expect(request.limit).toBeLessThanOrEqual(20);
  });
});
