import type { Meta, StoryObj } from '@storybook/angular';
import { LoadingStateComponent } from '@aura/shared/ui';

const meta: Meta<LoadingStateComponent> = {
  title: 'Shared/Loading state',
  component: LoadingStateComponent,
  tags: ['autodocs'],
};
export default meta;
type Story = StoryObj<LoadingStateComponent>;

export const Conversations: Story = { args: { label: 'Loading your conversation' } };
