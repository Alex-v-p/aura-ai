import { describe, expect, it } from 'vitest';
import { copyTextSafely, normalizeCodeLanguage, renderAssistantMarkdownWithCodePlaceholders, renderSafeAssistantMarkdown } from './safe-markdown';

function documentFor(markdown: string): Document {
  return new DOMParser().parseFromString(renderSafeAssistantMarkdown(markdown), 'text/html');
}

describe('safe assistant Markdown', () => {
  it('renders the supported Markdown vocabulary', () => {
    const document = documentFor([
      '# Heading',
      '',
      'A **bold** and *emphasized* paragraph with `inline code`.',
      '',
      '- one',
      '- two',
      '',
      '> a blockquote',
      '',
      '| Name | Value |',
      '| --- | --- |',
      '| A | B |',
      '',
      '---',
      '',
      '[Aura](https://example.test/docs) and [mail](mailto:help@example.test)',
      '',
      '```TypeScript\nconst answer = 42;\n```',
    ].join('\n'));

    expect(document.querySelector('h1')?.textContent).toBe('Heading');
    expect(document.querySelector('strong')?.textContent).toBe('bold');
    expect(document.querySelector('em')?.textContent).toBe('emphasized');
    expect(document.querySelectorAll('ul li')).toHaveLength(2);
    expect(document.querySelector('blockquote')?.textContent).toContain('a blockquote');
    expect(document.querySelectorAll('table th')).toHaveLength(2);
    expect(document.querySelector('hr')).not.toBeNull();
    expect(document.querySelector('code')?.textContent).toContain('inline code');
    expect(document.querySelector('pre code')?.textContent).toContain('const answer = 42;');
  });

  it('allows only safe link schemes and adds safe external-link attributes', () => {
    const document = documentFor([
      '[http](http://example.test)',
      '[https](https://example.test)',
      '[mail](mailto:help@example.test)',
      '[javascript](javascript:alert(1))',
      '[data](data:text/html,<script>alert(1)</script>)',
      '[file](file:///tmp/private.txt)',
    ].join('\n\n'));

    const links = [...document.querySelectorAll<HTMLAnchorElement>('a[href]')];
    expect(links.map((link) => link.textContent)).toEqual(['http', 'https', 'mail']);
    expect(links[0]?.target).toBe('_blank');
    expect(links[0]?.rel).toContain('noopener');
    expect(links[0]?.rel).toContain('noreferrer');
    expect(links[2]?.target).not.toBe('_blank');
    expect(document.body.innerHTML).not.toMatch(/javascript:|data:|file:/i);
  });

  it('removes dangerous elements, attributes, and malformed HTML while keeping text inert', () => {
    const document = documentFor([
      '<script>alert(1)</script><style>body{display:none}</style>',
      '<form><button formaction="javascript:alert(2)">submit</button></form>',
      '<img src="https://example.test/x.png" onerror="alert(3)">',
      '<iframe src="https://example.test"></iframe><video src="x"></video>',
      '<div onclick="alert(4)"><span>safe text',
    ].join('\n'));

    expect(document.querySelector('script, style, form, button, img, iframe, video, audio, object, embed')).toBeNull();
    expect(document.querySelector('[onerror], [onclick], [onload], [style]')).toBeNull();
    expect(document.body.textContent).toContain('safe text');
    expect(document.body.innerHTML).not.toMatch(/javascript:|data:|file:/i);
  });

  it('normalizes fenced-code language labels to bounded plain text', () => {
    const document = documentFor('```TypeScript\nconst answer = 42;\n```');
    expect(document.querySelector('pre code')?.className).toBe('language-typescript');
    expect(normalizeCodeLanguage(' c++ ')).toBe('cpp');
    expect(normalizeCodeLanguage('')).toBe('text');
    expect(normalizeCodeLanguage('javascript"><script>')).toBe('text');
    expect(normalizeCodeLanguage('x'.repeat(200))).toMatch(/^x{32}$/u);
  });

  it('sanitizes every streaming fragment independently', () => {
    const fragments = [
      'Partial **answer** with <scr',
      'ipt>alert(1)</script> and [safe](https://example.test)',
      '\n\n```python\nprint("done")\n```',
    ];

    for (const fragment of fragments) {
      const document = documentFor(fragment);
      expect(document.querySelector('script, iframe, img, form, button')).toBeNull();
      expect(document.body.innerHTML).not.toMatch(/on[a-z]+\s*=|javascript:|data:|file:/i);
    }
  });

  it('handles a large response without emitting active content', () => {
    const document = documentFor(Array.from({ length: 2_000 }, (_, index) => `- item ${index} with **content**`).join('\n'));

    expect(document.querySelectorAll('li')).toHaveLength(2_000);
    expect(document.querySelector('script, style, iframe, form, button, img')).toBeNull();
  });

  it('retains fenced-code provenance when fences are nested and rejects raw pre as a code control', () => {
    const nested = renderAssistantMarkdownWithCodePlaceholders([
      '- list item\n\n  ```python\n  print("nested list")\n  ```',
      '> quote\n> ```json\n> {"nested":true}\n> ```',
      '<pre><code>raw HTML code</code></pre>',
    ].join('\n\n'));
    const document = new DOMParser().parseFromString(nested.html, 'text/html');
    expect(document.querySelectorAll('[id^="aura-code-"]')).toHaveLength(2);
    expect(nested.codeBlocks.map((block) => block.code)).toEqual(['print("nested list")', '{"nested":true}']);
    expect(document.querySelector('pre code')?.textContent).toContain('raw HTML code');
  });

  it('copies without exposing code in the result and falls back when Clipboard API is unavailable', async () => {
    const clipboard = { writeText: async (text: string) => { void text; } };
    expect(await copyTextSafely('private code', clipboard)).toBe(true);

    const fallbackDocument = document.implementation.createHTMLDocument();
    Object.defineProperty(fallbackDocument, 'execCommand', { value: () => true });
    expect(await copyTextSafely('private code', undefined, fallbackDocument)).toBe(true);

    const unavailableDocument = document.implementation.createHTMLDocument();
    Object.defineProperty(unavailableDocument, 'execCommand', { value: () => false });
    expect(await copyTextSafely('private code', undefined, unavailableDocument)).toBe(false);

    const throwingDocument = document.implementation.createHTMLDocument();
    Object.defineProperty(throwingDocument, 'execCommand', { value: () => { throw new Error('copy denied'); } });
    expect(await copyTextSafely('private code', undefined, throwingDocument)).toBe(false);
    expect(throwingDocument.body.querySelector('textarea')).toBeNull();
    expect(throwingDocument.body.textContent).not.toContain('private code');
  });
});
