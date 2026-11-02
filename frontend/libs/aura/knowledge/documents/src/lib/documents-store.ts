import { Inject, Injectable, Optional, inject } from '@angular/core';
import { AuraDocumentsApi } from './documents-api';
import { DOCUMENT_API, type DocumentApi } from './documents-models';
import { DocumentsStoreState } from './documents-store-state';

export { DocumentsStoreState } from './documents-store-state';

@Injectable({ providedIn: 'root' })
export class DocumentsStore extends DocumentsStoreState {
  constructor(@Optional() @Inject(DOCUMENT_API) api?: DocumentApi) { super(api ?? inject(AuraDocumentsApi)); }
}
