import { expect, test, type Page, type Route } from '@playwright/test';

const now = '2026-10-07T12:00:00.000Z';
const later = '2026-10-07T12:01:00.000Z';

type FixtureMode = 'complete' | 'streaming';

function run(status: 'running' | 'completed' = 'completed'): Record<string, unknown> {
  return {
    id: 'run-safe-markdown',
    conversationId: 'conversation-safe-markdown',
    userMessageId: 'message-user',
    assistantMessageId: status === 'completed' ? 'message-assistant' : null,
    status,
    agentRevisionId: 'agent-researcher-r1',
    modelPolicyRevisionId: 'policy-1',
    provider: 'ollama',
    modelId: 'chat',
    retryOfRunId: null,
    createdAt: now,
    startedAt: now,
    finishedAt: status === 'completed' ? later : null,
    error: null,
  };
}

function summary(currentRun: Record<string, unknown> | null): Record<string, unknown> {
  return {
    id: 'conversation-safe-markdown',
    title: 'Safe rich response',
    agentProfileId: 'agent-researcher',
    agentRevisionId: 'agent-researcher-r1',
    modelId: 'chat',
    version: 1,
    createdAt: now,
    updatedAt: later,
    archivedAt: null,
    currentRun,
  };
}

const assistantMarkdown = [
  '# Safe answer',
  '',
  'A **bold** and *emphasized* paragraph with `inline code`.',
  '',
  '- first item',
  '  - nested item',
  '',
  '> A quoted answer',
  '',
  '| Name | Value |',
  '| --- | --- |',
  '| safe | content |',
  '',
  '---',
  '',
  '[safe link](https://example.test/docs) and [mail](mailto:help@example.test)',
  '[bad](javascript:alert(1)) [data](data:text/html,<script>alert(2)</script>) [file](file:///private)',
  '',
  '<script>alert("script")</script><img src="https://example.test/x.png" onerror="alert(3)">',
  '<style>body{display:none}</style><form><button>evil</button></form><iframe src="https://example.test"></iframe>',
  '',
  '```TypeScript\nconst dangerous = "<img src=x onerror=alert(4)>";\n```',
  '',
  '- nested list fence\n\n  ```python\n  print("nested list")\n  ```',
  '',
  '> nested quote fence\n> ```json\n> {"nested":true}\n> ```',
  '',
  '<pre><code>raw HTML code</code></pre>',
].join('\n');

const userPlainText = '<em>literal user text</em> <script>alert("user")</script>';

async function installFixture(page: Page, mode: FixtureMode = 'complete'): Promise<void> {
  const currentRun = mode === 'streaming' ? run('running') : null;
  await page.route('**/api/**', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/v1/auth/session') {
      await route.fulfill({ json: { principal: { issuer: 'https://issuer', subject: 'owner', displayName: 'Owner' }, csrfToken: 'csrf', idleExpiresAt: later, absoluteExpiresAt: later } });
      return;
    }
    if (url.pathname === '/api/v1/models') {
      await route.fulfill({ json: { models: [{ id: 'chat', displayName: 'Chat', provider: 'ollama', capabilities: ['chat'], availability: 'available', selectable: true, disabledReason: null }], defaultModelId: 'chat', observedAt: now } });
      return;
    }
    if (url.pathname === '/api/v1/agents' && request.method() === 'GET') {
      await route.fulfill({ json: { items: [{ id: 'agent-researcher', status: 'active', version: 1, currentRevision: { id: 'agent-researcher-r1', profileId: 'agent-researcher', revision: 1, displayName: 'Researcher', purpose: 'Research', instructions: 'Research', personaRevisionId: null, promptBundleRevisionId: 'bundle-1', modelPolicyRevisionId: 'policy-1', createdAt: now }, revisions: [] }] } });
      return;
    }
    if (url.pathname === '/api/v1/agents/agent-researcher' && request.method() === 'GET') {
      const revision = { id: 'agent-researcher-r1', profileId: 'agent-researcher', revision: 1, displayName: 'Researcher', purpose: 'Research', instructions: 'Research', personaRevisionId: null, promptBundleRevisionId: 'bundle-1', modelPolicyRevisionId: 'policy-1', createdAt: now };
      await route.fulfill({ json: { id: 'agent-researcher', status: 'active', version: 1, displayName: 'Researcher', createdAt: now, updatedAt: now, currentRevision: revision, revisions: [revision] } });
      return;
    }
    if (url.pathname === '/api/v1/personas' && request.method() === 'GET') {
      await route.fulfill({ json: { items: [] } });
      return;
    }
    if (url.pathname === '/api/v1/conversations' && request.method() === 'GET') {
      await route.fulfill({ json: { items: [summary(currentRun)], nextCursor: null } });
      return;
    }
    if (url.pathname === '/api/v1/conversations/conversation-safe-markdown' && request.method() === 'GET') {
      const current = mode === 'streaming' ? run('running') : null;
      await route.fulfill({ json: {
        ...summary(current),
        messages: [{ id: 'message-user', conversationId: 'conversation-safe-markdown', role: 'user', content: userPlainText, state: 'complete', runId: 'run-safe-markdown', createdAt: now, updatedAt: now },
          ...(mode === 'complete' ? [{ id: 'message-assistant', conversationId: 'conversation-safe-markdown', role: 'assistant', content: assistantMarkdown, state: 'complete', runId: 'run-safe-markdown', createdAt: later, updatedAt: later }] : [])],
        recentRuns: current ? [current] : [],
        agentAssignments: [],
      } });
      return;
    }
    if (url.pathname === '/api/v1/runs/run-safe-markdown/events') {
      const events = mode === 'streaming'
        ? [
          { id: 'event-1', text: 'Partial **answer** with <scr' },
          { id: 'event-2', text: 'ipt>alert(1)</script> and `code`' },
          { id: 'event-3', text: '\n\n```python\nprint("safe")\n```' },
        ]
        : [];
      let offset = 0;
      const body = events.map((event, index) => {
        const eventBody = `id: ${event.id}\nevent: assistant.delta\ndata: ${JSON.stringify({ schemaVersion: 1, eventId: event.id, sequence: index + 1, eventType: 'assistant.delta', runId: 'run-safe-markdown', conversationId: 'conversation-safe-markdown', occurredAt: now, data: { messageId: 'message-assistant', offset, text: event.text } })}\n\n`;
        offset += event.text.length;
        return eventBody;
      }).join('');
      await route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body });
      return;
    }
    await route.continue();
  });
}

async function openFixture(page: Page, mode: FixtureMode = 'complete'): Promise<void> {
  await installFixture(page, mode);
  await page.goto('/conversation/conversation-safe-markdown');
  await expect(page.getByRole('status', { name: /loading your conversation/i })).toBeHidden();
  await expect(page.getByRole('heading', { name: 'Safe rich response' })).toBeVisible();
}

test.describe('safe rich assistant responses', () => {
  test('renders supported Markdown and preserves user messages as escaped plain text', async ({ page }) => {
    await openFixture(page);
    const assistant = page.getByRole('article', { name: 'Aura response' });
    const user = page.getByRole('article', { name: 'Your message' });

    await expect(assistant.locator('aura-safe-markdown')).toBeVisible();
    await expect(assistant.locator('h1')).toHaveText('Safe answer');
    await expect(assistant.locator('strong')).toHaveText('bold');
    await expect(assistant.locator('em')).toHaveText('emphasized');
    await expect(assistant.locator('ul li')).toHaveCount(3);
    await expect(assistant.locator('blockquote').filter({ hasText: 'A quoted answer' })).toContainText('A quoted answer');
    await expect(assistant.locator('table')).toBeVisible();
    await expect(assistant.locator('hr')).toHaveCount(1);
    await expect(assistant.locator('pre code').filter({ hasText: 'const dangerous' })).toContainText('const dangerous');
    await expect(user.locator('em, script')).toHaveCount(0);
    await expect(user).toContainText(userPlainText);
  });

  test('removes active content and rejects unsafe URL schemes', async ({ page }) => {
    await openFixture(page);
    const assistant = page.getByRole('article', { name: 'Aura response' });
    await expect(assistant.locator('script, style, img, form, iframe, video, audio, object, embed')).toHaveCount(0);
    await expect(assistant.locator('[onerror], [onclick], [onload], [style]')).toHaveCount(0);
    const links = assistant.locator('a[href]');
    await expect(links).toHaveCount(2);
    await expect(links.nth(0)).toHaveAttribute('href', 'https://example.test/docs');
    await expect(links.nth(0)).toHaveAttribute('target', '_blank');
    await expect(links.nth(0)).toHaveAttribute('rel', /noopener/);
    await expect(links.nth(1)).toHaveAttribute('href', 'mailto:help@example.test');
    await expect(assistant.locator('a[href^="javascript:"], a[href^="data:"], a[href^="file:"]')).toHaveCount(0);
    const buttons = await assistant.locator('button').evaluateAll((items) => items.every((button) => button.classList.contains('code-copy')));
    expect(buttons).toBe(true);
  });

  test('provides normalized code labels and accessible copy feedback', async ({ page }) => {
    await openFixture(page);
    const assistant = page.getByRole('article', { name: 'Aura response' });
    await expect(assistant.locator('.code-language').first()).toHaveText('typescript');
    const copy = assistant.getByRole('button', { name: /copy code/i }).first();
    await expect(copy).toBeVisible();
    await copy.click();
    await expect(assistant.locator('[aria-live="polite"]')).toContainText(/copied/i);
  });

  test('keeps nested fences Angular-owned while leaving raw pre markup without controls', async ({ page }) => {
    await openFixture(page);
    const assistant = page.getByRole('article', { name: 'Aura response' }).locator('aura-safe-markdown');
    await expect(assistant.locator('.code-block')).toHaveCount(3);
    await expect(assistant.locator('.code-language')).toHaveText(['typescript', 'python', 'json']);
    const rawPre = assistant.locator('pre').filter({ hasText: 'raw HTML code' });
    await expect(rawPre).toHaveCount(1);
    await expect(rawPre.locator('xpath=ancestor::section[contains(@class, "code-block")]')).toHaveCount(0);
  });

  test('keeps each streaming partial Markdown update inert', async ({ page }) => {
    await openFixture(page, 'streaming');
    const assistant = page.getByRole('article', { name: 'Aura response' }).locator('aura-safe-markdown');
    await expect.poll(async () => assistant.locator('pre code').count()).toBe(1);
    await expect(assistant.locator('script, img, iframe, form, button:not(.code-copy)')).toHaveCount(0);
    await expect(assistant.locator('[onerror], [onclick], [onload]')).toHaveCount(0);
    await expect(assistant).toContainText('Partial');
    await expect(assistant).toContainText('print("safe")');
  });
});
