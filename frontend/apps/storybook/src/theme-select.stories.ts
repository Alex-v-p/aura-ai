import type { Meta, StoryObj } from '@storybook/angular';
import { ThemeSelectComponent } from '@aura/shared/ui';

const meta: Meta<ThemeSelectComponent> = { title: 'Shared/Theme selection', component: ThemeSelectComponent, tags: ['autodocs'] };
export default meta;
type Story = StoryObj<ThemeSelectComponent>;
export const System: Story = { args: { value: 'system' } };
export const Dark: Story = { args: { value: 'dark' } };
