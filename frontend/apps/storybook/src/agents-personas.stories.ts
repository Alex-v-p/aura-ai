import { APP_INITIALIZER, inject } from '@angular/core';
import { applicationConfig, moduleMetadata } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { provideRouter, Router, RouterOutlet } from '@angular/router';
import { AGENT_API, AgentCreatePageComponent, AgentListPageComponent, AgentStore, InMemoryAgentApi } from '@aura/aura/interaction/agents';
import { PERSONA_API, PersonaCreatePageComponent, PersonaDetailPageComponent, PersonaListPageComponent, PersonaStore, InMemoryPersonaApi } from '@aura/aura/interaction/personas';

const meta: Meta = {
  title: 'Aura/Management surfaces/Agents and Personas',
  parameters: { layout: 'fullscreen', a11y: { manual: true } },
};
export default meta;

const managementProviders = [
  { provide: AGENT_API, useFactory: () => new InMemoryAgentApi() },
  { provide: AgentStore, useClass: AgentStore },
  { provide: PERSONA_API, useFactory: () => new InMemoryPersonaApi() },
  { provide: PersonaStore, useClass: PersonaStore },
];
const providers = applicationConfig({ providers: [...managementProviders, provideRouter([])] });
const detailProviders = applicationConfig({ providers: [
  ...managementProviders,
  provideRouter([{ path: 'personas/:id', component: PersonaDetailPageComponent }]),
  { provide: APP_INITIALIZER, multi: true, useFactory: () => { const router = inject(Router); return () => router.navigateByUrl('/personas/6d4b7f6c-7b25-4866-a1f0-3be2d0b4c7ab', { replaceUrl: true }); } },
] });

const story = (selector: string, component: unknown, storyProviders = providers): StoryObj => ({
  decorators: [storyProviders, moduleMetadata({ imports: [component] })],
  render: () => ({ template: `<div data-feature-root class="aura-management-story" style="background:var(--canvas);color:var(--text);min-height:100vh;padding:1.5rem"><${selector} /></div>` }),
});

export const AgentLibrary: StoryObj = story('aura-agent-list-page', AgentListPageComponent);
export const AgentBuilder: StoryObj = story('aura-agent-create-page', AgentCreatePageComponent);
export const PersonaLibrary: StoryObj = story('aura-persona-list-page', PersonaListPageComponent);
export const PersonaBuilder: StoryObj = story('aura-persona-create-page', PersonaCreatePageComponent);
export const PersonaDetail: StoryObj = story('router-outlet', RouterOutlet, detailProviders);
