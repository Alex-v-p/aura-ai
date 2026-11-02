import { open } from 'node:fs/promises';
import { expect, test, type Page, type Route } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const OWNER = 'owner-document-fixture';
const CSRF = 'document-fixture-csrf';
const DOCUMENT_ID = '11111111-1111-4111-8111-111111111111';
const FAILED_DOCUMENT_ID = '22222222-2222-4222-8222-222222222222';
const UPLOAD_ID = '33333333-3333-4333-8333-333333333333';
const now = '2026-10-13T10:00:00.000Z';

type DocumentStatus = 'uploading' | 'queued' | 'processing' | 'ready' | 'failed' | 'deleting';
type FixtureDocument = {
  id: string;
  filename: string;
  mediaType: string;
  byteCount: number;
  status: DocumentStatus;
  failure: { code: string; retryable: boolean } | null;
  version: number;
  extractedTextLength: number;
  chunkCount: number;
  createdAt: string;
  updatedAt: string;
};

const readyDocument = (overrides: Partial<FixtureDocument> = {}): FixtureDocument => ({
  id: DOCUMENT_ID,
  filename: 'owner-handbook.md',
  mediaType: 'text/markdown',
  byteCount: 2_048,
  status: 'ready',
  failure: null,
  version: 2,
  extractedTextLength: 1_024,
  chunkCount: 2,
  createdAt: now,
  updatedAt: now,
  ...overrides,
});

type DocumentFixtureOptions = {
  documents?: FixtureDocument[];
  uploadReceipt?: { document: FixtureDocument; duplicate: boolean };
  contentResponse?: 'accepted' | 'duplicate' | 'unavailable';
  pollStatus?: DocumentStatus;
  sessionStatus?: number;
  deleteResponse?: 'accepted' | 'unavailable';
  previewResponse?: 'ready' | 'unavailable';
  downloadResponse?: 'ready' | 'unavailable';
};

type DocumentFixture = {
  requests: Array<{
    method: string;
    pathname: string;
    url: string;
    headers: Record<string, string>;
    body: string | null;
    bodyByteLength: number | null;
  }>;
  state: { documents: FixtureDocument[]; pollReads: number; detailReads: number; deleted: boolean };
  route: (route: Route) => Promise<void>;
};

function assertNoPrivateResponseInBrowserState(page: Page, privateToken: string): Promise<void> {
  return expect
    .poll(async () =>
      page.evaluate(async (token) => {
        const values = [
          window.location.href,
          ...Object.keys(localStorage),
          ...Object.keys(sessionStorage),
          ...Object.values(localStorage),
          ...Object.values(sessionStorage),
        ];
        const cacheNames = 'caches' in window ? await caches.keys() : [];
        const cachedUrls = await Promise.all(
          cacheNames.map(async (name) => (await caches.open(name)).keys()),
        ).then((entries) => entries.flat().map((request) => request.url));
        return {
          containsToken: values.some((value) => value.includes(token)),
          apiCached: cachedUrls.some((url) => new URL(url).pathname.startsWith('/api/')),
        };
      }, privateToken),
    )
    .toEqual({ containsToken: false, apiCached: false });
}

function makeDocumentFixture(options: DocumentFixtureOptions = {}): DocumentFixture {
  const fixture: DocumentFixture = {
    requests: [],
    state: {
      documents: options.documents ?? [readyDocument()],
      pollReads: 0,
      detailReads: 0,
      deleted: false,
    },
    route: async () => {
      throw new Error('Document fixture route was not initialized.');
    },
  };

  const summary = (document: FixtureDocument): FixtureDocument => ({ ...document });
  const detail = (document: FixtureDocument): FixtureDocument => ({ ...document });
  const recordRequest = (route: Route): void => {
    const request = route.request();
    const url = new URL(request.url());
    const headers = request.headers();
    fixture.requests.push({
      method: request.method(),
      pathname: url.pathname,
      url: request.url(),
      headers,
      // Never retain a binary upload body in the fixture. This keeps the
      // boundary test faithful to streaming semantics and avoids making a
      // test artifact out of private file bytes.
      body: url.pathname.endsWith('/content') ? null : request.postData(),
      bodyByteLength: url.pathname.endsWith('/content')
        ? (request.postDataBuffer()?.byteLength ?? null)
        : null,
    });
  };
  const responseHeaders = { 'cache-control': 'no-store' };
  const documentPage = (): { items: FixtureDocument[]; nextCursor: null } => ({
    items: fixture.state.deleted ? [] : fixture.state.documents.map(summary),
    nextCursor: null,
  });
  const documentForId = (id: string): FixtureDocument | undefined =>
    fixture.state.documents.find((document) => document.id === id);

  // The shell requests these owner-scoped resources before rendering a routed feature.
  fixture.route = async (route: Route): Promise<void> => {
    recordRequest(route);
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/v1/auth/session') {
      if (options.sessionStatus)
        return route.fulfill({
          status: options.sessionStatus,
          headers: responseHeaders,
          json: { detail: 'session unavailable' },
        });
      return route.fulfill({
        headers: responseHeaders,
        json: {
          principal: {
            issuer: 'https://authentik.test',
            subject: OWNER,
            displayName: 'Document Owner',
          },
          csrfToken: CSRF,
          idleExpiresAt: '2099-10-13T10:00:00Z',
          absoluteExpiresAt: '2099-10-14T10:00:00Z',
        },
      });
    }
    if (url.pathname === '/api/v1/models')
      return route.fulfill({
        headers: responseHeaders,
        json: { models: [], defaultModelId: null, observedAt: now },
      });
    if (url.pathname === '/api/v1/agents')
      return route.fulfill({ headers: responseHeaders, json: { items: [] } });
    if (url.pathname === '/api/v1/conversations')
      return route.fulfill({ headers: responseHeaders, json: { items: [], nextCursor: null } });
    if (url.pathname === '/api/v1/documents' && request.method() === 'GET') {
      if (options.pollStatus && fixture.state.pollReads > 0) {
        fixture.state.documents = fixture.state.documents.map((document) => ({
          ...document,
          status: options.pollStatus as DocumentStatus,
          updatedAt: now,
        }));
      }
      fixture.state.pollReads += 1;
      return route.fulfill({ headers: responseHeaders, json: documentPage() });
    }
    if (url.pathname === '/api/v1/documents/search' && request.method() === 'POST') {
      const ready = fixture.state.documents.filter((document) => document.status === 'ready');
      return route.fulfill({
        headers: responseHeaders,
        json: {
          items: ready.length
            ? [
                {
                  evidenceId: '44444444-4444-4444-8444-444444444444',
                  documentId: ready[0].id,
                  displayName: ready[0].filename,
                  excerpt: 'A bounded owner-private excerpt.',
                  locator: { kind: 'section', sectionOrdinal: 1, heading: 'Overview' },
                  score: 0.94,
                },
              ]
            : [],
          nextCursor: null,
        },
      });
    }
    if (url.pathname === '/api/v1/documents/uploads' && request.method() === 'POST') {
      return route.fulfill({
        status: 201,
        headers: responseHeaders,
        json: {
          id: UPLOAD_ID,
          documentId: FAILED_DOCUMENT_ID,
          filename: 'new-note.md',
          mediaType: 'text/markdown',
          byteCount: 11,
          status: 'uploading',
          version: 1,
          createdAt: now,
          expiresAt: '2099-10-13T10:05:00Z',
        },
      });
    }
    if (
      url.pathname === `/api/v1/documents/uploads/${UPLOAD_ID}/content` &&
      request.method() === 'PUT'
    ) {
      if (options.contentResponse === 'unavailable')
        return route.fulfill({
          status: 503,
          headers: responseHeaders,
          json: { detail: 'Document storage is temporarily unavailable.', retryable: true },
        });
      const receipt = options.uploadReceipt ?? {
        document:
          options.contentResponse === 'duplicate'
            ? readyDocument()
            : readyDocument({
                id: FAILED_DOCUMENT_ID,
                filename: 'new-note.md',
                version: 1,
                byteCount: 11,
              }),
        duplicate: options.contentResponse === 'duplicate',
      };
      return route.fulfill({ status: 202, headers: responseHeaders, json: receipt });
    }
    const retryMatch = url.pathname.match(/^\/api\/v1\/documents\/([^/]+)\/retry$/);
    if (retryMatch && request.method() === 'POST') {
      const document =
        documentForId(retryMatch[1]) ?? readyDocument({ id: retryMatch[1], status: 'queued' });
      const queued = {
        ...document,
        status: 'queued' as const,
        failure: null,
        version: document.version + 1,
      };
      fixture.state.documents = fixture.state.documents.map((item) =>
        item.id === retryMatch[1] ? queued : item,
      );
      return route.fulfill({ status: 202, headers: responseHeaders, json: detail(queued) });
    }
    const idMatch = url.pathname.match(/^\/api\/v1\/documents\/([^/]+)$/);
    if (idMatch && request.method() === 'GET') {
      fixture.state.detailReads += 1;
      if (options.pollStatus && fixture.state.detailReads > 1) {
        fixture.state.documents = fixture.state.documents.map((document) => ({
          ...document,
          status: options.pollStatus as DocumentStatus,
          updatedAt: now,
        }));
      }
      const document = documentForId(idMatch[1]);
      if (!document)
        return route.fulfill({
          status: 404,
          headers: responseHeaders,
          json: { detail: 'Document not found.' },
        });
      return route.fulfill({ headers: responseHeaders, json: detail(document) });
    }
    if (idMatch && request.method() === 'DELETE') {
      if (options.deleteResponse === 'unavailable')
        return route.fulfill({
          status: 503,
          headers: responseHeaders,
          json: { detail: 'Document cleanup is temporarily unavailable.', retryable: true },
        });
      fixture.state.deleted = true;
      fixture.state.documents = fixture.state.documents.map((document) =>
        document.id === idMatch[1]
          ? { ...document, status: 'deleting', version: document.version + 1 }
          : document,
      );
      return route.fulfill({
        status: 202,
        headers: responseHeaders,
        json: { documentId: idMatch[1], status: 'deleting', version: 3, acceptedAt: now },
      });
    }
    const previewMatch = url.pathname.match(/^\/api\/v1\/documents\/([^/]+)\/preview$/);
    if (previewMatch && request.method() === 'GET') {
      if (options.previewResponse === 'unavailable')
        return route.fulfill({
          status: 503,
          headers: responseHeaders,
          json: { detail: 'Preview temporarily unavailable.', retryable: true },
        });
      return route.fulfill({
        headers: { ...responseHeaders, 'content-type': 'application/json' },
        json: {
          documentId: previewMatch[1],
          text: 'Bounded preview text from the ready document.',
          truncated: false,
          totalCharacters: 44,
        },
      });
    }
    const contentMatch = url.pathname.match(/^\/api\/v1\/documents\/([^/]+)\/content$/);
    if (contentMatch && request.method() === 'GET') {
      if (options.downloadResponse === 'unavailable')
        return route.fulfill({
          status: 503,
          headers: responseHeaders,
          json: { detail: 'Original download temporarily unavailable.', retryable: true },
        });
      return route.fulfill({
        status: 200,
        headers: {
          ...responseHeaders,
          'content-type': 'text/markdown',
          'content-disposition': 'attachment; filename="owner-handbook.md"',
          'content-length': '2048',
        },
        body: 'A bounded fixture document.',
      });
    }
    return route.fulfill({
      status: 404,
      headers: responseHeaders,
      json: { detail: 'Fixture endpoint not implemented.' },
    });
  };
  return fixture;
}

async function installDocumentFixture(
  page: Page,
  options: DocumentFixtureOptions = {},
): Promise<DocumentFixture> {
  const fixture = makeDocumentFixture(options);
  await page.route('**/api/v1/**', fixture.route);
  return fixture;
}

async function openDocuments(
  page: Page,
  options: DocumentFixtureOptions = {},
): Promise<DocumentFixture> {
  const fixture = await installDocumentFixture(page, options);
  await page.goto('/documents');
  await expect(page.getByRole('heading', { name: /documents/i }).first()).toBeVisible();
  return fixture;
}

async function createSparseFixtureFile(
  testInfo: { outputPath: (name: string) => string },
  name: string,
  byteLength: number,
): Promise<string> {
  const filePath = testInfo.outputPath(name);
  const handle = await open(filePath, 'w');
  try {
    await handle.truncate(byteLength);
  } finally {
    await handle.close();
  }
  return filePath;
}

function documentRow(page: Page, filename: string, state?: RegExp): ReturnType<Page['locator']> {
  const rows = page.locator('.document-row').filter({ hasText: filename });
  return (state ? rows.filter({ hasText: state }) : rows).first();
}

test.describe('Aura Web document library', () => {
  test('exposes Documents under Knowledge and keeps the collection owner-private', async ({
    page,
  }, testInfo) => {
    const fixture = await openDocuments(page);
    if (testInfo.project.name === 'mobile') {
      await page.getByRole('button', { name: 'Open navigation' }).click();
      await expect(page.locator('#primary-navigation')).toHaveClass(/is-mobile-open/);
    }
    const navigation = page.locator('#primary-navigation');
    await expect(navigation.getByRole('link', { name: 'Documents', exact: true })).toHaveAttribute(
      'aria-current',
      'page',
    );
    await expect(page).toHaveURL(/\/documents$/);
    await expect(page.getByText('owner-handbook.md', { exact: true })).toBeVisible();
    expect(
      fixture.requests.filter(
        (request) => request.pathname === '/api/v1/documents' && request.method === 'GET',
      ),
    ).toHaveLength(1);
    await expect(page.getByRole('searchbox', { name: /search documents/i })).toBeVisible();
    const a11y = await new AxeBuilder({ page }).include('.documents-page').analyze();
    expect(a11y.violations).toEqual([]);
  });

  test('supports keyboard selection, drag and drop, exact format guidance, and exact 100 MB validation', async ({
    page,
  }, testInfo) => {
    const fixture = await openDocuments(page, { documents: [] });
    const input = page.locator('input[type="file"]');
    await expect(input).toHaveAttribute('accept', /pdf|markdown|plain|wordprocessingml/);
    await expect(page.getByText(/PDF|DOCX|Markdown|plain text/i).first()).toBeVisible();

    await input.focus();
    const expectedBeforeBoundary = 2;
    await input.setInputFiles({
      name: 'keyboard-note.md',
      mimeType: 'text/markdown',
      buffer: Buffer.from('# Keyboard upload'),
    });
    await expect(page.getByText(/uploading|queued|ready|keyboard-note\.md/i).first()).toBeVisible();
    expect(
      fixture.requests.some(
        (request) => request.pathname === '/api/v1/documents/uploads' && request.method === 'POST',
      ),
    ).toBe(true);

    const dropTarget = page.locator('.upload-card').first();
    await dropTarget.evaluate((target) => {
      const dataTransfer = new DataTransfer();
      dataTransfer.items.add(new File(['drop'], 'drop-note.txt', { type: 'text/plain' }));
      target.dispatchEvent(
        new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer }),
      );
      target.dispatchEvent(
        new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer }),
      );
    });
    await expect(page.getByText(/drop-note\.txt|uploading|queued|ready/i).first()).toBeVisible();

    await input.setInputFiles({
      name: 'spoofed.pdf',
      mimeType: 'text/plain',
      buffer: Buffer.from('not a PDF'),
    });
    await expect(
      page.getByRole('alert').filter({ hasText: /supported|PDF|file type|signature/i }),
    ).toBeVisible();
    expect(
      fixture.requests.filter(
        (request) => request.pathname === '/api/v1/documents/uploads' && request.method === 'POST',
      ),
    ).toHaveLength(expectedBeforeBoundary);

    if (testInfo.project.name === 'desktop') {
      const exactLimitPath = await createSparseFixtureFile(testInfo, 'exact-limit.md', 100_000_000);
      await input.setInputFiles(exactLimitPath);
      await expect(page.getByText(/exact-limit\.md|uploading|queued|ready/i).first()).toBeVisible();
      expect(
        fixture.requests.filter(
          (request) =>
            request.pathname === '/api/v1/documents/uploads' && request.method === 'POST',
        ),
      ).toHaveLength(3);
    }

    const acceptedUploads = testInfo.project.name === 'desktop' ? 3 : 2;

    const oversizedPath = await createSparseFixtureFile(testInfo, 'too-large.md', 100_000_001);
    await input.setInputFiles(oversizedPath);
    await expect(
      page.getByRole('alert').filter({ hasText: /100\s*MB|100,000,000|too large/i }),
    ).toBeVisible();
    expect(
      fixture.requests.filter(
        (request) => request.pathname === '/api/v1/documents/uploads' && request.method === 'POST',
      ),
    ).toHaveLength(acceptedUploads);
  });

  test('streams content with CSRF, idempotency, expected version, length and media type, and can be canceled', async ({
    page,
  }) => {
    const fixture = await openDocuments(page, { documents: [] });
    let contentStarted = false;
    await page.unroute('**/api/v1/**');
    await page.route('**/api/v1/**', async (route) => {
      if (route.request().url().includes(`/uploads/${UPLOAD_ID}/content`)) {
        const request = route.request();
        const url = new URL(request.url());
        fixture.requests.push({
          method: request.method(),
          pathname: url.pathname,
          url: request.url(),
          headers: request.headers(),
          body: null,
          bodyByteLength: request.postDataBuffer()?.byteLength ?? null,
        });
        contentStarted = true;
        // Leave the stream in flight long enough for the UI's cancel action to
        // abort it. This proves cancellation is not reported as success.
        await new Promise((resolve) => setTimeout(resolve, 2_000));
        return route.abort();
      }
      await fixture.route(route);
    });
    await page
      .getByRole('button', { name: /upload/i })
      .first()
      .click();
    const input = page.locator('input[type="file"]');
    await input.setInputFiles({
      name: 'cancel-me.md',
      mimeType: 'text/markdown',
      buffer: Buffer.from('cancel me'),
    });
    await expect.poll(() => contentStarted).toBe(true);
    const contentRequest = fixture.requests.find((request) =>
      request.pathname.endsWith(`/uploads/${UPLOAD_ID}/content`),
    );
    expect(contentRequest?.method).toBe('PUT');
    expect(contentRequest?.headers['x-csrf-token']).toBe(CSRF);
    expect(contentRequest?.headers['idempotency-key']).toMatch(/\S+/);
    expect(
      contentRequest?.headers['if-match'] ?? contentRequest?.headers['x-expected-version'],
    ).toMatch(/1/);
    expect(contentRequest?.bodyByteLength).toBe(9);
    expect(contentRequest?.headers['content-type']).toMatch(/text\/markdown/);
    await page.getByRole('button', { name: /cancel upload/i }).click();
    await expect(page.getByRole('status').filter({ hasText: /cancel/i })).toBeVisible();
    await expect(page.getByText(/cancel-me\.md/i)).toBeVisible();
  });

  test('reports duplicate content and polls processing until a terminal state', async ({
    page,
  }) => {
    const fixture = await openDocuments(page, {
      documents: [readyDocument({ status: 'queued' })],
      pollStatus: 'ready',
      contentResponse: 'duplicate',
    });
    await expect.poll(() => fixture.state.detailReads).toBeGreaterThan(0);
    await expect(page.getByText(/ready/i).first()).toBeVisible();
    const input = page.locator('input[type="file"]');
    await input.setInputFiles({
      name: 'same-content.md',
      mimeType: 'text/markdown',
      buffer: Buffer.from('same'),
    });
    await expect(
      page.getByRole('status').filter({ hasText: /already have|duplicate|existing/i }),
    ).toBeVisible();
    await expect(page.getByText('owner-handbook.md', { exact: true })).toHaveCount(1);
  });

  test('shows failed processing with retry, detail metadata, bounded plain-text preview, and ephemeral download', async ({
    page,
  }) => {
    const failed = readyDocument({
      id: FAILED_DOCUMENT_ID,
      filename: 'failed.docx',
      mediaType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      status: 'failed',
      failure: { code: 'parser_timeout', retryable: true },
      version: 3,
    });
    const fixture = await openDocuments(page, { documents: [failed, readyDocument()] });
    const row = documentRow(page, failed.filename);
    await expect(row).toBeVisible();
    await expect(row.locator('.state-text.failed')).toHaveText(/failed/i);
    await row.click();
    await expect(page).toHaveURL(new RegExp(`/documents/${FAILED_DOCUMENT_ID}$`));
    const failureAlert = page.getByRole('alert').filter({ hasText: 'Processing needs attention' });
    await expect(failureAlert).toBeVisible();
    await expect(failureAlert.locator('strong')).toHaveText('Processing needs attention');
    const retryProcessing = page.getByRole('button', { name: 'Retry processing', exact: true });
    await expect(retryProcessing).toBeVisible();
    await retryProcessing.click();
    await expect(page.getByText(/Queued for processing|queued/i).first()).toBeVisible();
    expect(
      fixture.requests.some(
        (request) =>
          request.pathname.endsWith(`/documents/${FAILED_DOCUMENT_ID}/retry`) &&
          request.method === 'POST',
      ),
    ).toBe(true);

    const ready = readyDocument();
    await page.goto(`/documents/${ready.id}`);
    await expect(page.getByText(ready.filename, { exact: true })).toBeVisible();
    await expect(page.getByText(/bounded preview text/i)).toBeVisible();
    await expect(page.getByRole('button', { name: /download/i })).toBeVisible();
    await page.getByRole('button', { name: /download/i }).click();
    const contentRequest = fixture.requests.find((request) =>
      request.pathname.endsWith(`/documents/${ready.id}/content`),
    );
    expect(contentRequest?.method).toBe('GET');
    expect(contentRequest?.url).not.toContain('owner-handbook');
  });

  test('does not disclose another owner’s document through an identifier-only detail route', async ({
    page,
  }) => {
    const fixture = await openDocuments(page);
    const foreignId = '99999999-9999-4999-8999-999999999999';
    await page.goto(`/documents/${foreignId}`);
    await expect(
      page.getByRole('alert').filter({ hasText: /unavailable|not available|no longer/i }),
    ).toBeVisible();
    await expect(page.getByText('owner-handbook.md', { exact: true })).toHaveCount(0);
    const detailRequest = fixture.requests.find((request) => request.pathname.endsWith(foreignId));
    expect(detailRequest?.method).toBe('GET');
  });

  test('uses a POST-body search, excludes deleting documents, and requires explicit permanent-delete confirmation', async ({
    page,
  }) => {
    const fixture = await openDocuments(page);
    const privateQuery = 'private-sentinel-42';
    const search = page.getByRole('searchbox', { name: /search documents/i });
    await search.fill(privateQuery);
    await search.press('Enter');
    await expect(page.getByText(/bounded owner-private excerpt/i)).toBeVisible();
    const searchRequest = fixture.requests.find(
      (request) => request.pathname === '/api/v1/documents/search',
    );
    expect(searchRequest?.method).toBe('POST');
    expect(searchRequest?.body).toContain(privateQuery);
    expect(searchRequest?.url).not.toContain(privateQuery);
    expect(new URL(searchRequest?.url ?? 'http://fixture').search).toBe('');
    expect(searchRequest?.headers['x-csrf-token']).toBe(CSRF);

    await page
      .getByRole('button', { name: /owner-handbook\.md/i })
      .first()
      .click();
    await page.getByRole('button', { name: /delete/i }).click();
    const confirmation = page.getByRole('dialog', { name: /delete|permanently/i });
    await expect(confirmation).toBeVisible();
    await expect(confirmation.getByRole('button', { name: /cancel/i })).toBeVisible();
    await confirmation.getByRole('button', { name: 'Delete permanently', exact: true }).click();
    await expect(
      page.getByRole('status').filter({ hasText: /deleting|removed|cleanup/i }),
    ).toBeVisible();
    expect(
      fixture.requests.some(
        (request) =>
          request.pathname === `/api/v1/documents/${DOCUMENT_ID}` && request.method === 'DELETE',
      ),
    ).toBe(true);
    await page.goto('/documents');
    await expect(page.getByText('owner-handbook.md', { exact: true })).toHaveCount(0);
    await expect(page.getByText(/empty|no documents/i).first()).toBeVisible();
  });

  test('clears private document state across a session reset and never writes document traffic to browser storage or caches', async ({
    page,
  }) => {
    const privateQuery = 'private-sentinel-42';
    const consoleMessages: string[] = [];
    page.on('console', (message) => consoleMessages.push(message.text()));
    const fixture = await openDocuments(page);
    const serviceWorker = await page.request.get('/sw.js');
    expect(serviceWorker.ok()).toBe(true);
    const serviceWorkerSource = await serviceWorker.text();
    expect(serviceWorkerSource).toContain("url.pathname.startsWith('/api/')");
    expect(serviceWorkerSource).toContain("event.request.method !== 'GET'");
    const search = page.getByRole('searchbox', { name: /search documents/i });
    await search.fill(privateQuery);
    await search.press('Enter');
    await expect(page.getByText(/bounded owner-private excerpt/i)).toBeVisible();
    await assertNoPrivateResponseInBrowserState(page, privateQuery);
    expect(fixture.requests.every((request) => !request.url.includes(privateQuery))).toBe(true);
    expect(consoleMessages.some((message) => message.includes(privateQuery))).toBe(false);

    // A principal switch must invalidate private state before the replacement
    // session is accepted. The fixture changes only the session response.
    let switched = false;
    await page.unroute('**/api/v1/**');
    await page.route('**/api/v1/**', async (route) => {
      if (route.request().url().endsWith('/auth/session') && switched)
        return route.fulfill({
          headers: { 'cache-control': 'no-store' },
          json: {
            principal: {
              issuer: 'https://authentik.test',
              subject: 'replacement-owner',
              displayName: 'Replacement Owner',
            },
            csrfToken: 'replacement-csrf',
            idleExpiresAt: '2099-10-13T10:00:00Z',
            absoluteExpiresAt: '2099-10-14T10:00:00Z',
          },
        });
      return fixture.route(route);
    });
    switched = true;
    await page.reload();
    await expect(page.getByRole('searchbox', { name: /search documents/i })).toHaveValue('');
    await assertNoPrivateResponseInBrowserState(page, privateQuery);
  });

  test('keeps collection and detail usable at 200 percent zoom and mobile widths with no horizontal overflow', async ({
    page,
  }, testInfo) => {
    test.skip(
      testInfo.project.name !== 'desktop' && testInfo.project.name !== 'mobile',
      'Representative wide and narrow projects cover this invariant.',
    );
    await openDocuments(page);
    await page.evaluate(() => {
      document.documentElement.style.zoom = '2';
    });
    const overflow = await page.evaluate(() => {
      const root = document.documentElement;
      const zoom = Number.parseFloat(getComputedStyle(root).zoom || '1');
      return root.scrollWidth / zoom <= root.clientWidth + 1;
    });
    expect(overflow).toBe(true);
    const a11y = await new AxeBuilder({ page }).include('.documents-page').analyze();
    expect(a11y.violations).toEqual([]);
    await page.evaluate(() => {
      document.documentElement.style.zoom = '';
    });
  });

  test('shows calm unavailable and canceled states with a safe next action', async ({ page }) => {
    await openDocuments(page, { sessionStatus: 503 });
    await expect(
      page.getByRole('alert').filter({ hasText: /unavailable|try again|sign in/i }),
    ).toBeVisible();
    await expect(page.getByRole('button', { name: /retry|sign in|try again/i })).toHaveCount(1);
  });
});
