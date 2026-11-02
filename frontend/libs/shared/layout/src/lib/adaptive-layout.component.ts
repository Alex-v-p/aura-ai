import { ChangeDetectionStrategy, Component } from '@angular/core';

/**
 * Product-neutral layout primitive for composing a full-height adaptive view.
 * Product shells own navigation, content, and interaction semantics.
 */
@Component({
  selector: 'aura-adaptive-layout',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: '<div class="adaptive-layout"><ng-content /></div>',
  styles: [':host { display: block; min-height: 100%; } .adaptive-layout { display: flex; min-height: 100%; width: 100%; }'],
})
export class AdaptiveLayoutComponent {}
