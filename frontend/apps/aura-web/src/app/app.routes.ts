import { Routes } from '@angular/router';
import { ConversationPanelComponent } from '@aura/aura/interaction/conversations';
import { AgentCreatePageComponent, AgentDetailPageComponent, AgentListPageComponent, AgentToolPolicyPageComponent } from '@aura/aura/interaction/agents';
import { PersonaCreatePageComponent, PersonaDetailPageComponent, PersonaListPageComponent } from '@aura/aura/interaction/personas';
import { AgentMemoryPolicyPageComponent, MemoryCandidatesPageComponent, MemoryPageComponent, MemorySettingsPageComponent } from '@aura/aura/knowledge/memory';
import { DocumentsPageComponent } from '@aura/aura/knowledge/documents';
import { RemindersPageComponent } from '@aura/aura/automation/reminders';
import { NotificationsPageComponent } from '@aura/aura/automation/notifications';
import { ApprovalsPageComponent } from '@aura/aura/governance/approvals';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'conversation' },
  { path: 'conversation/:conversationId', component: ConversationPanelComponent },
  { path: 'conversation', component: ConversationPanelComponent },
  { path: 'agents', component: AgentListPageComponent },
  { path: 'agents/new', component: AgentCreatePageComponent },
  { path: 'agents/:id/tools', component: AgentToolPolicyPageComponent },
  { path: 'agents/:id', component: AgentDetailPageComponent },
  { path: 'personas', component: PersonaListPageComponent },
  { path: 'personas/new', component: PersonaCreatePageComponent },
  { path: 'personas/:id', component: PersonaDetailPageComponent },
  { path: 'memory', component: MemoryPageComponent },
  { path: 'memory/candidates', component: MemoryCandidatesPageComponent },
  { path: 'memory/settings', component: MemorySettingsPageComponent },
  { path: 'memory/:id', component: MemoryPageComponent },
  { path: 'documents', component: DocumentsPageComponent },
  { path: 'documents/:id', component: DocumentsPageComponent },
  { path: 'reminders', component: RemindersPageComponent },
  { path: 'reminders/series/:seriesId', component: RemindersPageComponent },
  { path: 'reminders/:id', component: RemindersPageComponent },
  { path: 'notifications', component: NotificationsPageComponent },
  { path: 'notifications/:id', component: NotificationsPageComponent },
  { path: 'approvals', component: ApprovalsPageComponent },
  { path: 'approvals/:id', component: ApprovalsPageComponent },
  { path: 'agents/:id/memory', component: AgentMemoryPolicyPageComponent },
  { path: '**', redirectTo: 'conversation' },
];
