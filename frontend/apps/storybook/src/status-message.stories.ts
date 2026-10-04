import type { Meta, StoryObj } from '@storybook/angular';
import { StatusMessageComponent } from '@aura/shared/ui';

const meta: Meta<StatusMessageComponent> = { title: 'Shared/Status message', component: StatusMessageComponent, tags: ['autodocs'] };
export default meta;
type Story = StoryObj<StatusMessageComponent>;
export const Information: Story = { args: { tone: 'info', icon: 'i' }, render: (args) => ({ props: args, template: '<aura-status-message [tone]="tone" [icon]="icon">A local preview keeps your work in this browser.</aura-status-message>' }) };
export const RecoverableError: Story = { args: { tone: 'danger', icon: '×' }, render: (args) => ({ props: args, template: '<aura-status-message [tone]="tone" [icon]="icon">We could not complete that preview. Your draft is still here.</aura-status-message>' }) };
