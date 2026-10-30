import { ChangeDetectionStrategy, Component, Input, OnChanges, OnDestroy, SimpleChanges, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import type { DurableTaskDetail, ResearchActivity } from '@aura/aura-api-client';
import { ConversationResearchActivityApi } from './conversation-research-activity-api';
import { researchLifecycleAnnouncement, researchLifecycleLabel } from './research-activity-lifecycle';

@Component({ selector: 'aura-research-activity', standalone: true, imports: [RouterLink], changeDetection: ChangeDetectionStrategy.OnPush, template: `<section class="research-activity" aria-label="Research activity"><span class="research-activity-icon" aria-hidden="true">⌕</span><div><strong>Research task {{ activity.taskRef.taskId }}</strong><span>{{ modeLabel(activity.mode) }} research · {{ lifecycleLabel() }}</span>@if (lifecycle() === 'detached') {<small>Continuing in the background.</small>}@if (task(); as snapshot) {<small>{{ snapshot.stage || snapshot.sourceStatus }} · {{ snapshot.sourceOutcome || snapshot.status }}</small>@if (snapshot.artifactRefs.length) {<span class="research-artifacts">@for (artifact of snapshot.artifactRefs; track artifact.artifactId) { <a [routerLink]="['/artifacts', artifact.artifactId]">Open result</a> }</span>} }</div><span class="research-activity-live" aria-live="polite" aria-atomic="true">{{ lifecycleAnnouncement() }}</span><a [routerLink]="['/tasks', activity.taskRef.taskId]">Open task</a></section>`, styles: [`.research-activity{align-items:center;background:var(--surface-subtle);border:1px solid var(--border);border-radius:.7rem;display:flex;gap:.7rem;margin-top:.8rem;padding:.75rem}.research-activity-icon{color:var(--accent);font-size:1.3rem}.research-activity div{display:grid;flex:1;gap:.2rem;min-width:0}.research-activity span,.research-activity small{color:var(--text-muted);font-size:.8rem}.research-activity a{color:var(--accent);font-size:.82rem;font-weight:650}.research-artifacts{display:flex;gap:.35rem;flex-wrap:wrap}.research-activity-live{height:1px;overflow:hidden;position:absolute;width:1px}`] })
export class ResearchActivityComponent implements OnChanges, OnDestroy {
  @Input({ required: true }) activity!: ResearchActivity;
  readonly task = signal<DurableTaskDetail | null>(null);
  readonly lifecycle = signal('admitted');
  private readonly api = inject(ConversationResearchActivityApi);
  private generation = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  ngOnChanges(changes: SimpleChanges): void { if (changes['activity']) { this.generation += 1; this.task.set(null); this.refresh(this.generation); } }
  ngOnDestroy(): void { if (this.timer) clearTimeout(this.timer); this.generation += 1; }
  modeLabel(mode: ResearchActivity['mode']): string { return mode === 'agent_led' ? 'Agent-led' : `${mode[0].toUpperCase()}${mode.slice(1)}`; }
  lifecycleLabel(): string { return researchLifecycleLabel(this.lifecycle()); }
  lifecycleAnnouncement(): string { return researchLifecycleAnnouncement(this.lifecycle()); }
  private refresh(generation: number): void { void this.api.getTask(this.activity.taskRef.taskId).then(snapshot => { if (generation !== this.generation || snapshot.id !== this.activity.taskRef.taskId) return; this.task.set(snapshot); this.lifecycle.set(lifecycleFor(snapshot)); if (snapshot.status === 'queued' || snapshot.status === 'running' || snapshot.status === 'waiting') this.timer = setTimeout(() => this.refresh(generation), 5000); }).catch(() => { if (generation === this.generation) this.lifecycle.set('recovery unavailable'); }); }
}
function lifecycleFor(snapshot: DurableTaskDetail): string { if (snapshot.status === 'completed' || snapshot.status === 'failed' || snapshot.status === 'canceled') return snapshot.sourceOutcome ? `terminal · ${snapshot.sourceOutcome}` : 'terminal'; if (snapshot.sourceStatus === 'detached' || snapshot.stage === 'draining') return 'detached'; return snapshot.status; }
