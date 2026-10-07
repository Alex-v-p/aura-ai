import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const appSource = readFileSync('apps/aura-web/src/app/app.component.ts', 'utf8');

describe('AppComponent conversation library projection', () => {
  it('omits transient draft IDs from library summaries', () => {
    expect(appSource).toContain(".filter((conversation) => !conversation.id.startsWith('draft-'))");
    expect(appSource).toContain('readonly conversationSummaries');
  });
});
