import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

/** Product-neutral responsive list/detail composition. */
@Component({
  selector: 'aura-list-detail-layout',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="list-detail" [class.has-selection]="hasSelection">
      <section class="list-pane" [attr.aria-label]="listLabel"><ng-content select="[list]" /></section>
      @if (hasSelection) { <section class="detail-pane" [attr.aria-label]="detailLabel"><ng-content select="[detail]" /></section> }
    </div>
  `,
  styles: [`
    :host { display: block; min-height: 0; }
    .list-detail { display: grid; gap: 1rem; grid-template-columns: minmax(15rem, 22rem) minmax(0, 1fr); min-height: 0; }
    .list-pane, .detail-pane { min-width: 0; }
    .detail-pane { border-left: 1px solid var(--border); padding-left: 1rem; }
    @media (max-width: 960px) { .list-detail { grid-template-columns: minmax(13rem, 18rem) minmax(0, 1fr); } }
    @media (max-width: 720px) { .list-detail { display: block; } .detail-pane { border-left: 0; border-top: 1px solid var(--border); margin-top: 1rem; padding-left: 0; padding-top: 1rem; } }
  `],
})
export class ListDetailLayoutComponent {
  @Input() hasSelection = true;
  @Input() listLabel = 'Items';
  @Input() detailLabel = 'Details';
}
