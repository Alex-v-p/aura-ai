import { A11yModule } from '@angular/cdk/a11y';
import { DatePipe, DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, ElementRef, OnDestroy, ViewChild, effect, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import type { DocumentDetail, DocumentSearchResult, DocumentStatus } from '@aura/aura-api-client';
import { documentTypeLabel, formatDocumentSize } from './documents-models';
import { DocumentsStore } from './documents-store';

@Component({ selector: 'aura-documents-page', standalone: true, imports: [A11yModule, DatePipe, DecimalPipe, FormsModule, RouterLink], changeDetection: ChangeDetectionStrategy.OnPush, templateUrl: './documents-pages.component.html', styleUrl: './documents-pages.component.css' })
export class DocumentsPageComponent implements OnDestroy {
  readonly store = inject(DocumentsStore);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  readonly selectedId = signal<string | null>(null);
  readonly deleteOpen = signal(false);
  readonly deleteOpenerId = signal<string | null>(null);
  readonly searchDraft = signal('');
  readonly formatSize = formatDocumentSize;
  readonly typeLabel = documentTypeLabel;
  @ViewChild('fileInput') private readonly fileInput?: ElementRef<HTMLInputElement>;
  @ViewChild('deleteCancel') private readonly deleteCancel?: ElementRef<HTMLButtonElement>;
  @ViewChild('deletionStatus') private readonly deletionStatus?: ElementRef<HTMLElement>;
  private readonly routeSubscription = this.route.paramMap.subscribe((params) => { const id = params.get('id'); this.selectedId.set(id); if (id) void this.store.loadDetail(id); else this.store.clearSelection(); });
  private readonly resetEffect = effect(() => { if (this.store.resetEpoch() > 0) { this.selectedId.set(null); this.deleteOpen.set(false); this.searchDraft.set(''); } });
  private loaded = false;

  constructor() { void this.store.load().finally(() => { this.loaded = true; }); }
  get searchItems(): ReadonlyArray<DocumentSearchResult> { return this.store.searchResults()?.items ?? []; }
  get collectionItems() { return this.store.documents().items; }
  get currentDetail(): DocumentDetail | null { return this.store.selected(); }
  onSearch(value: string): void { this.searchDraft.set(value); void this.store.load(value); }
  chooseFiles(files: FileList | File[]): void { for (const file of Array.from(files)) void this.store.upload(file); if (this.fileInput) this.fileInput.nativeElement.value = ''; }
  onFileInput(event: Event): void { const input = event.target as HTMLInputElement; if (input.files) this.chooseFiles(input.files); }
  onDrop(event: DragEvent): void { event.preventDefault(); if (event.dataTransfer?.files) this.chooseFiles(event.dataTransfer.files); }
  openUpload(): void { this.fileInput?.nativeElement.click(); }
  selectDocument(id: string): void { void this.router.navigate(['/documents', id]); }
  backToCollection(): void { void this.router.navigate(['/documents']); }
  cancelUpload(key: string): void { this.store.cancelUpload(key); }
  retryUpload(item: { readonly key: string }): void { this.store.retryUpload(item.key); }
  removeUpload(item: { readonly key: string }): void { this.store.removeUpload(item.key); }
  retryDetail(): void { const item = this.currentDetail; if (item) void this.store.loadDetail(item.id); }
  retryProcessing(): void { const item = this.currentDetail; if (item) void this.store.retry(item.id, item.version); }
  openDelete(event: Event): void { this.deleteOpenerId.set((event.currentTarget as HTMLElement | null)?.id ?? null); this.deleteOpen.set(true); queueMicrotask(() => this.deleteCancel?.nativeElement.focus()); }
  closeDelete(restoreFocus = true): void { this.deleteOpen.set(false); if (restoreFocus) queueMicrotask(() => { const opener = this.deleteOpenerId(); if (opener) document.getElementById(opener)?.focus(); }); }
  async confirmDelete(): Promise<void> { const item = this.currentDetail; if (!item) return; const accepted = await this.store.remove(item.id, item.version); if (!accepted) return; const deleting = this.store.selected()?.status === 'deleting'; this.closeDelete(!deleting); if (deleting) setTimeout(() => this.deletionStatus?.nativeElement.focus(), 0); else this.backToCollection(); }
  async download(): Promise<void> {
    const item = this.currentDetail; if (!item || item.status !== 'ready') return;
    await this.store.download(item.id, item.filename);
  }
  ngOnDestroy(): void { this.routeSubscription.unsubscribe(); this.resetEffect.destroy(); this.store.stopPolling(); }
  statusLabel(status: DocumentStatus): string { return status === 'queued' ? 'Queued for processing' : status === 'processing' ? 'Processing' : status === 'uploading' ? 'Uploading' : status === 'deleting' ? 'Deleting' : status[0].toUpperCase() + status.slice(1); }
  failureMessage(code: string, retryable: boolean): string { const normalized = code.replace(/[_-]+/g, ' '); return retryable ? `Aura could not process this document (${normalized}) yet. You can retry processing.` : `Aura could not process this document (${normalized}).`; }
  isLoading(): boolean { return this.store.loading() && !this.loaded; }
}
