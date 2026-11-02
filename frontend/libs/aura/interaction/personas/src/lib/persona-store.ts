import { Injectable, computed, inject, signal } from '@angular/core';
import { PERSONA_API, previewContextFingerprint, type PersonaApi, type PersonaDraft, type PersonaModelOption, type PersonaPreviewRequest, type PersonaPreviewResponse, type PersonaProfile, type PersonaStatus } from './persona-models';

type SessionAuthorizationStatus = 401 | 403;

@Injectable({ providedIn: 'root' })
export class PersonaStore {
  private readonly api: PersonaApi = inject(PERSONA_API);
  readonly personas = signal<ReadonlyArray<PersonaProfile>>([]);
  readonly loading = signal(true);
  readonly notice = signal<string | null>(null);
  readonly loadFailed = signal(false);
  readonly detailLoading = signal(false);
  readonly detailError = signal<string | null>(null);
  readonly resetEpoch = signal(0);
  readonly activePersonas = computed(() => this.personas().filter((persona) => persona.status === 'active'));
  readonly builderDraft = signal<PersonaDraft>({ displayName: '', description: '', behavioralInstructions: '' });
  readonly previewLoading = signal(false);
  readonly previewResult = signal<PersonaPreviewResponse | null>(null);
  readonly previewContext = signal<string | null>(null);
  readonly statusUpdating = signal<string | null>(null);
  readonly models = signal<ReadonlyArray<PersonaModelOption>>([]);
  readonly modelsLoading = signal(true);
  readonly modelsError = signal<string | null>(null);
  readonly selectableModels = computed(() => this.models());
  private requestGeneration = 0;
  private listRequestGeneration = 0;
  private modelsRequestGeneration = 0;
  private detailRequestGeneration = 0;
  private previewRequestGeneration = 0;
  private sessionResetHandler: ((status?: SessionAuthorizationStatus) => void) | null = null;
  constructor() { void this.load(); void this.loadModels(); }
  async load(): Promise<void> { const sessionGeneration = this.requestGeneration; const generation = ++this.listRequestGeneration; this.loading.set(true); try { const profiles = await this.api.listPersonas(); const personas = await Promise.all(profiles.map((profile) => this.api.getPersona(profile.id))); if (sessionGeneration !== this.requestGeneration || generation !== this.listRequestGeneration) return; this.personas.set(personas); this.notice.set(null); this.loadFailed.set(false); } catch (error: unknown) { if (sessionGeneration !== this.requestGeneration || generation !== this.listRequestGeneration) return; this.loadFailed.set(true); this.notice.set(this.message(error)); this.handleAuthFailure(error); } finally { if (sessionGeneration === this.requestGeneration && generation === this.listRequestGeneration) this.loading.set(false); } }
  async loadModels(): Promise<void> { const sessionGeneration = this.requestGeneration; const generation = ++this.modelsRequestGeneration; this.modelsLoading.set(true); this.modelsError.set(null); try { const models = await this.api.listModels(); if (sessionGeneration !== this.requestGeneration || generation !== this.modelsRequestGeneration) return; this.models.set(models); } catch (error: unknown) { if (sessionGeneration !== this.requestGeneration || generation !== this.modelsRequestGeneration) return; this.modelsError.set('Selectable models are unavailable right now.'); this.handleAuthFailure(error); } finally { if (sessionGeneration === this.requestGeneration && generation === this.modelsRequestGeneration) this.modelsLoading.set(false); } }
  async loadDetail(id: string): Promise<void> { const sessionGeneration = this.requestGeneration; const generation = ++this.detailRequestGeneration; this.detailLoading.set(true); this.detailError.set(null); try { const profile = await this.api.getPersona(id); if (sessionGeneration !== this.requestGeneration || generation !== this.detailRequestGeneration) return; this.replace(profile); } catch (error: unknown) { if (sessionGeneration !== this.requestGeneration || generation !== this.detailRequestGeneration) return; const message = this.message(error); this.detailError.set(message); this.notice.set(message); this.handleAuthFailure(error); } finally { if (sessionGeneration === this.requestGeneration && generation === this.detailRequestGeneration) this.detailLoading.set(false); } }
  async retryDetail(id: string): Promise<void> { await this.loadDetail(id); }
  async create(draft: PersonaDraft): Promise<boolean> { const generation = this.requestGeneration; this.notice.set(null); try { const created = await this.api.createPersona(draft); if (generation !== this.requestGeneration) return false; this.personas.update((items) => [...items, created]); this.notice.set(null); return true; } catch (error: unknown) { if (generation !== this.requestGeneration) return false; this.notice.set(this.message(error)); this.handleAuthFailure(error); return false; } }
  setBuilderDraft(draft: PersonaDraft): void { this.builderDraft.set({ ...draft }); }
  resetBuilderDraft(): void { this.builderDraft.set({ displayName: '', description: '', behavioralInstructions: '' }); }
  registerSessionResetHandler(handler: ((status?: SessionAuthorizationStatus) => void) | null): void { this.sessionResetHandler = handler; }
  resetSessionState(): void { ++this.requestGeneration; this.api.clearSession?.(); this.personas.set([]); this.models.set([]); this.loading.set(false); this.loadFailed.set(false); this.detailLoading.set(false); this.detailError.set(null); this.modelsLoading.set(false); this.modelsError.set(null); this.notice.set(null); this.builderDraft.set({ displayName: '', description: '', behavioralInstructions: '' }); this.invalidatePreview(); this.statusUpdating.set(null); this.resetEpoch.update((value) => value + 1); }
  async preview(request: PersonaPreviewRequest, contextKey = previewContextFingerprint(request)): Promise<void> { if (this.previewLoading()) return; const sessionGeneration = this.requestGeneration; const generation = ++this.previewRequestGeneration; if (!request.modelId || !this.selectableModels().some((model) => model.id === request.modelId)) { this.notice.set('Choose an available model before previewing.'); return; } this.previewLoading.set(true); this.previewResult.set(null); this.previewContext.set(contextKey); try { const result = await this.api.preview(request); if (sessionGeneration !== this.requestGeneration || generation !== this.previewRequestGeneration) return; this.previewResult.set(result); this.notice.set(null); } catch (error: unknown) { if (sessionGeneration !== this.requestGeneration || generation !== this.previewRequestGeneration) return; this.notice.set(error instanceof Error ? 'Preview is unavailable right now. Check the model and try again.' : 'Preview is unavailable right now.'); this.handleAuthFailure(error); } finally { if (sessionGeneration === this.requestGeneration && generation === this.previewRequestGeneration) this.previewLoading.set(false); } }
  invalidatePreview(): void { ++this.previewRequestGeneration; this.previewLoading.set(false); this.previewResult.set(null); this.previewContext.set(null); }
  previewResultFor(contextKey: string): PersonaPreviewResponse | null { return this.previewContext() === contextKey ? this.previewResult() : null; }
  async revise(id: string, draft: PersonaDraft): Promise<boolean> { const generation = this.requestGeneration; this.notice.set(null); const current = this.find(id); if (!current) { this.notice.set('We could not find this persona. Your changes are still here.'); return false; } try { const updated = await this.api.createRevision(id, draft, current.version); if (generation !== this.requestGeneration) return false; this.replace(updated); this.notice.set(null); return true; } catch (error: unknown) { if (generation !== this.requestGeneration) return false; this.notice.set(this.message(error)); this.handleAuthFailure(error); return false; } }
  async setStatus(id: string, status: PersonaStatus): Promise<boolean> { if (this.statusUpdating()) return false; const generation = this.requestGeneration; const current = this.find(id); if (!current) return false; this.statusUpdating.set(id); try { const updated = await this.api.setStatus(id, status, current.version); if (generation !== this.requestGeneration) return false; this.replace(updated); return true; } catch (error: unknown) { if (generation !== this.requestGeneration) return false; this.notice.set(this.message(error)); this.handleAuthFailure(error); return false; } finally { if (generation === this.requestGeneration) this.statusUpdating.set(null); } }
  find(id: string): PersonaProfile | undefined { return this.personas().find((persona) => persona.id === id); }
  private replace(updated: PersonaProfile): void { this.personas.update((items) => items.map((item) => item.id === updated.id ? updated : item)); }
  private handleAuthFailure(error: unknown): void {
    const status = (error as { status?: unknown })?.status;
    if (status !== 401 && status !== 403) return;
    this.resetSessionState();
    this.sessionResetHandler?.(status);
    const recovery = status === 401 ? 'Your session has expired. Sign in again to view your personas.' : 'You do not have permission to view these personas.';
    this.loadFailed.set(true);
    this.notice.set(recovery);
    this.detailError.set(recovery);
    this.modelsError.set(recovery);
  }
  private message(error: unknown): string { return error instanceof Error ? error.message : 'We could not save this persona. Your changes are still here.'; }
}
