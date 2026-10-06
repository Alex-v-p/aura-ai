import { Routes } from '@angular/router';
import { ConversationPanelComponent } from '@aura/aura/interaction/conversations';
import { AgentCreatePageComponent, AgentDetailPageComponent, AgentListPageComponent } from '@aura/aura/interaction/agents';
import { PersonaCreatePageComponent, PersonaDetailPageComponent, PersonaListPageComponent } from '@aura/aura/interaction/personas';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'conversation' },
  { path: 'conversation', component: ConversationPanelComponent },
  { path: 'agents', component: AgentListPageComponent },
  { path: 'agents/new', component: AgentCreatePageComponent },
  { path: 'agents/:id', component: AgentDetailPageComponent },
  { path: 'personas', component: PersonaListPageComponent },
  { path: 'personas/new', component: PersonaCreatePageComponent },
  { path: 'personas/:id', component: PersonaDetailPageComponent },
  { path: '**', redirectTo: 'conversation' },
];
