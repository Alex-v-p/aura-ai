import type { Meta, StoryObj } from '@storybook/angular';
import { componentWrapperDecorator, moduleMetadata } from '@storybook/angular';
import { CommonModule } from '@angular/common';
import { ObservatoryIconComponent, type ObservatoryIconName } from '@aura/observatory';

const names: readonly ObservatoryIconName[] = [
  'overview', 'runs', 'trace', 'components', 'analytics', 'compare',
  'evaluation', 'experiments', 'operations', 'retention', 'success', 'warning', 'danger',
];

const meta: Meta<ObservatoryIconComponent> = {
  title: 'Observatory/Semantic icons',
  component: ObservatoryIconComponent,
  parameters: { a11y: { test: 'error' } },
  decorators: [moduleMetadata({ imports: [CommonModule] }), componentWrapperDecorator((story) => `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));gap:16px;padding:24px;max-width:720px">${story}</div>`)],
};

export default meta;
type Story = StoryObj<ObservatoryIconComponent>;

export const NavigationAndState: Story = {
  render: () => ({
    props: { names },
    template: '<div *ngFor="let name of names" style="align-items:center;display:flex;gap:8px"><observatory-icon [name]="name" [label]="name"></observatory-icon><span>{{ name }}</span></div>',
  }),
};
