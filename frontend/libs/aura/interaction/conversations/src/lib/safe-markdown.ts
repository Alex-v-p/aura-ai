import DOMPurify from 'dompurify';
import { marked, type Tokens } from 'marked';

const ALLOWED_TAGS = [
  'a', 'blockquote', 'br', 'code', 'del', 'em', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'hr', 'li', 'ol', 'p', 'pre', 's', 'strong', 'table', 'tbody', 'td', 'tfoot', 'th', 'thead',
  'tr', 'ul', 'span',
];
const ALLOWED_ATTR = ['align', 'class', 'colspan', 'href', 'id', 'rel', 'rowspan', 'target', 'title'];
const ALLOWED_URI_REGEXP = /^(?:(?:https?|mailto):)/i;

export function renderSafeAssistantMarkdown(source: string): string {
  return renderAssistantMarkdown(source, false).html;
}

export interface SafeMarkdownRenderResult {
  readonly html: string;
  readonly codeBlocks: ReadonlyArray<{ readonly code: string; readonly language: string; readonly index: number }>;
  readonly codeBlockMarker: string;
}

/** Render with inert placeholders so Angular can own every fenced-code UI node. */
export function renderAssistantMarkdownWithCodePlaceholders(source: string): SafeMarkdownRenderResult {
  return renderAssistantMarkdown(source, true);
}

function renderAssistantMarkdown(source: string, useCodePlaceholders: boolean): SafeMarkdownRenderResult {
  const codeBlocks: Array<{ readonly code: string; readonly language: string; readonly index: number }> = [];
  const codeBlockMarker = `aura-code-${typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : Math.random().toString(36).slice(2)}`;
  const renderer = new marked.Renderer();
  renderer.code = ({ text, lang }: Tokens.Code): string => {
    const language = normalizeCodeLanguage(lang);
    const index = codeBlocks.push({ code: text, language, index: codeBlocks.length }) - 1;
    return useCodePlaceholders
      ? `<span id="${codeBlockMarker}:${index}"></span>`
      : `<pre><code class="language-${language}">${escapeHtml(text)}</code></pre>`;
  };

  let rawHtml = '';
  try {
    rawHtml = marked.parse(source, { renderer, gfm: true, async: false, silent: true }) as string;
  } catch {
    // Malformed partial Markdown is represented as no rich content. The
    // transcript remains available and the next streaming fragment can render.
  }
  const sanitized = DOMPurify.sanitize(rawHtml, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    ALLOW_DATA_ATTR: false,
    ALLOWED_URI_REGEXP,
    FORBID_TAGS: ['audio', 'base', 'button', 'canvas', 'embed', 'form', 'iframe', 'img', 'input', 'link', 'math', 'meta', 'object', 'script', 'select', 'style', 'svg', 'template', 'textarea', 'video'],
    FORBID_ATTR: ['style'],
  });
  return { html: normalizeLinks(sanitized), codeBlocks, codeBlockMarker };
}

export function normalizeCodeLanguage(language: string | undefined): string {
  const candidate = (language ?? '').trim().split(/\s+/u, 1)[0].toLowerCase();
  if (!candidate || !/^[a-z0-9+#._-]+$/u.test(candidate)) return 'text';
  return candidate.replaceAll('+', 'p').slice(0, 32) || 'text';
}

/** Copy without placing model output in any status or accessible label. */
export async function copyTextSafely(
  text: string,
  clipboard: Pick<Clipboard, 'writeText'> | undefined = typeof navigator === 'undefined' ? undefined : navigator.clipboard,
  documentRef: Document | undefined = typeof document === 'undefined' ? undefined : document,
): Promise<boolean> {
  try {
    if (clipboard) {
      await clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fall through to the selection-based browser fallback.
  }
  if (!documentRef) return false;
  let textarea: HTMLTextAreaElement | null = null;
  try {
    textarea = documentRef.createElement('textarea');
    textarea.value = text;
    textarea.setAttribute('readonly', '');
    textarea.setAttribute('aria-hidden', 'true');
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    documentRef.body.appendChild(textarea);
    textarea.select();
    const copied = documentRef.execCommand('copy');
    return copied;
  } catch {
    return false;
  } finally {
    textarea?.remove();
  }
}

function normalizeLinks(html: string): string {
  if (typeof DOMParser === 'undefined') return html;
  const document = new DOMParser().parseFromString(html, 'text/html');
  for (const anchor of Array.from(document.body.querySelectorAll('a'))) {
    const href = anchor.getAttribute('href') ?? '';
    if (!ALLOWED_URI_REGEXP.test(href)) {
      anchor.removeAttribute('href');
      anchor.removeAttribute('target');
      anchor.removeAttribute('rel');
    } else if (/^https?:/i.test(href)) {
      anchor.setAttribute('target', '_blank');
      anchor.setAttribute('rel', 'noopener noreferrer');
    }
  }
  return document.body.innerHTML;
}

function escapeHtml(value: string): string {
  return value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}
