import { NgTemplateOutlet } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';
import { copyTextSafely, renderAssistantMarkdownWithCodePlaceholders } from './safe-markdown';

type MarkdownTag = 'a' | 'blockquote' | 'br' | 'code' | 'del' | 'em' | 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6' | 'hr' | 'li' | 'ol' | 'p' | 'pre' | 's' | 'strong' | 'table' | 'tbody' | 'td' | 'tfoot' | 'th' | 'thead' | 'tr' | 'ul' | 'span';
type MarkdownNode = MarkdownTextNode | MarkdownElementNode | MarkdownCodeNode;
interface MarkdownTextNode { readonly kind: 'text'; readonly text: string; readonly key: number; }
interface MarkdownCodeNode { readonly kind: 'code-block'; readonly code: string; readonly language: string; readonly index: number; readonly key: number; }
interface MarkdownElementNode {
  readonly kind: 'element';
  readonly tag: MarkdownTag;
  readonly children: ReadonlyArray<MarkdownNode>;
  readonly key: number;
  readonly href?: string | null;
  readonly target?: string | null;
  readonly rel?: string | null;
  readonly align?: string | null;
  readonly colspan?: string | null;
  readonly rowspan?: string | null;
}

/**
 * Renders model-authored Markdown in a deliberately small HTML subset.
 * DOMPurify is the first boundary; every text and attribute value is then
 * inserted through Angular bindings. Fenced-code controls are always Angular
 * template nodes, including fences nested in lists and blockquotes.
 */
@Component({
  selector: 'aura-safe-markdown',
  standalone: true,
  imports: [NgTemplateOutlet],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="markdown-html">
      <ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: nodes() }" />
    </div>
    <ng-template #renderNodes let-nodes>
      @for (node of nodes; track node.key) {
        @if (node.kind === 'text') {
          {{ node.text }}
        } @else if (node.kind === 'code-block') {
          <section class="code-block" aria-label="Code block">
            <div class="code-block-toolbar">
              <span class="code-language">{{ node.language }}</span>
              <button type="button" class="code-copy" [attr.aria-label]="copiedIndex() === node.index ? 'Code copied' : 'Copy code'" (click)="copyCode(node.code, node.index)">
                {{ copiedIndex() === node.index ? 'Copied' : 'Copy' }}
              </button>
            </div>
            <pre><code>{{ node.code }}</code></pre>
          </section>
        } @else {
          @switch (node.tag) {
            @case ('a') { <a [attr.href]="$any(node).href" [attr.target]="$any(node).target" [attr.rel]="$any(node).rel" [attr.title]="$any(node).title"><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: $any(node).children }" /></a> }
            @case ('blockquote') { <blockquote><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></blockquote> }
            @case ('br') { <br /> }
            @case ('code') { <code><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></code> }
            @case ('del') { <del><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></del> }
            @case ('em') { <em><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></em> }
            @case ('h1') { <h1><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></h1> }
            @case ('h2') { <h2><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></h2> }
            @case ('h3') { <h3><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></h3> }
            @case ('h4') { <h4><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></h4> }
            @case ('h5') { <h5><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></h5> }
            @case ('h6') { <h6><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></h6> }
            @case ('hr') { <hr /> }
            @case ('li') { <li><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></li> }
            @case ('ol') { <ol><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></ol> }
            @case ('p') { <p><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></p> }
            @case ('pre') { <pre><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></pre> }
            @case ('s') { <s><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></s> }
            @case ('strong') { <strong><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></strong> }
            @case ('table') { <table><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></table> }
            @case ('tbody') { <tbody><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></tbody> }
            @case ('td') { <td [attr.align]="$any(node).align" [attr.colspan]="$any(node).colspan" [attr.rowspan]="$any(node).rowspan"><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></td> }
            @case ('tfoot') { <tfoot><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></tfoot> }
            @case ('th') { <th [attr.align]="$any(node).align" [attr.colspan]="$any(node).colspan" [attr.rowspan]="$any(node).rowspan"><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></th> }
            @case ('thead') { <thead><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></thead> }
            @case ('tr') { <tr><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></tr> }
            @case ('ul') { <ul><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></ul> }
            @case ('span') { <span><ng-container [ngTemplateOutlet]="renderNodes" [ngTemplateOutletContext]="{ $implicit: node.children }" /></span> }
          }
        }
      }
    </ng-template>
    <span class="sr-only" aria-live="polite">{{ copyFeedback() === 'copied' ? 'Code copied to clipboard' : copyFeedback() === 'unavailable' ? 'Copy unavailable. Select the code manually.' : '' }}</span>
  `,
  styles: [`
    :host { display: block; }
    .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
    .markdown-html :first-child { margin-top: 0; }
    .markdown-html :last-child { margin-bottom: 0; }
    .markdown-html h1, .markdown-html h2, .markdown-html h3, .markdown-html h4, .markdown-html h5, .markdown-html h6 { margin: 1rem 0 .45rem; line-height: 1.25; }
    .markdown-html p, .markdown-html ul, .markdown-html ol, .markdown-html blockquote, .markdown-html table { margin: .65rem 0; }
    .markdown-html ul, .markdown-html ol { padding-inline-start: 1.5rem; }
    .markdown-html blockquote { border-inline-start: 3px solid var(--aura-border, #c9d1d9); margin-inline: 0; padding-inline-start: 1rem; color: var(--aura-muted, #52606d); }
    .markdown-html table { border-collapse: collapse; width: 100%; }
    .markdown-html th, .markdown-html td { border: 1px solid var(--aura-border, #c9d1d9); padding: .35rem .5rem; text-align: start; }
    .markdown-html a { color: var(--aura-link, #155eef); text-decoration: underline; }
    .markdown-html code { border-radius: .25rem; background: color-mix(in srgb, currentColor 10%, transparent); padding: .1rem .25rem; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .9em; }
    .code-block { margin: .8rem 0; overflow: hidden; border: 1px solid var(--aura-border, #c9d1d9); border-radius: .5rem; background: #111827; color: #f9fafb; }
    .code-block-toolbar { display: flex; align-items: center; justify-content: space-between; gap: .75rem; border-bottom: 1px solid #374151; padding: .4rem .65rem; }
    .code-language { font: 600 .75rem/1 ui-monospace, SFMono-Regular, Menlo, monospace; text-transform: lowercase; }
    .code-copy { border: 1px solid #6b7280; border-radius: .25rem; background: transparent; color: inherit; cursor: pointer; padding: .25rem .5rem; }
    .code-copy:focus-visible { outline: 2px solid #93c5fd; outline-offset: 2px; }
    .code-block pre { overflow-x: auto; margin: 0; padding: .75rem; }
    .code-block code { padding: 0; background: transparent; font: .875rem/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre; }
  `],
})
export class SafeMarkdownComponent {
  readonly content = input('');
  readonly copiedIndex = signal<number | null>(null);
  readonly copyFeedback = signal<'copied' | 'unavailable' | null>(null);
  readonly nodes = computed(() => parseMarkdownNodes(this.content()));

  async copyCode(code: string, index: number): Promise<void> {
    try {
      const copied = await copyTextSafely(code);
      this.copyFeedback.set(copied ? 'copied' : 'unavailable');
      if (copied) {
        this.copiedIndex.set(index);
        window.setTimeout(() => {
          if (this.copiedIndex() === index) this.copiedIndex.set(null);
        }, 1800);
      }
    } catch {
      this.copyFeedback.set('unavailable');
    }
  }
}

const ELEMENT_TAGS = new Set<MarkdownTag>(['a', 'blockquote', 'br', 'code', 'del', 'em', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'li', 'ol', 'p', 'pre', 's', 'strong', 'table', 'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'ul', 'span']);

function parseMarkdownNodes(source: string): ReadonlyArray<MarkdownNode> {
  const rendered = renderAssistantMarkdownWithCodePlaceholders(source);
  if (typeof DOMParser === 'undefined') return [];
  const document = new DOMParser().parseFromString(rendered.html, 'text/html');
  let key = 0;
  const parseNode = (node: Node): ReadonlyArray<MarkdownNode> => {
    if (node.nodeType === Node.TEXT_NODE) {
      const text = node.textContent ?? '';
      return text ? [{ kind: 'text', text, key: key++ }] : [];
    }
    if (!(node instanceof HTMLElement)) return [];
    const placeholder = node.getAttribute('id');
    const markerPrefix = `${rendered.codeBlockMarker}:`;
    if (node.tagName === 'SPAN' && placeholder?.startsWith(markerPrefix)) {
      const index = Number(placeholder.slice(markerPrefix.length));
      const code = Number.isInteger(index) && index >= 0 ? rendered.codeBlocks[index] : undefined;
      return code ? [{ kind: 'code-block', ...code, key: key++ }] : [];
    }
    const tag = node.tagName.toLowerCase() as MarkdownTag;
    if (!ELEMENT_TAGS.has(tag)) return Array.from(node.childNodes).flatMap(parseNode);
    const children = Array.from(node.childNodes).flatMap(parseNode);
    return [{
      kind: 'element', tag, children, key: key++,
      ...(tag === 'a' ? {
        href: node.getAttribute('href'), target: node.getAttribute('target'), rel: node.getAttribute('rel'),
      } : {}),
      ...(['th', 'td'].includes(tag) ? {
        align: node.getAttribute('align'), colspan: node.getAttribute('colspan'), rowspan: node.getAttribute('rowspan'),
      } : {}),
    }];
  };
  return Array.from(document.body.childNodes).flatMap(parseNode);
}
