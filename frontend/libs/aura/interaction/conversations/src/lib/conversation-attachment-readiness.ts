export function selectedDocumentIsPending(documentId: string, documents: ReadonlyArray<{ readonly id: string; readonly status: string }>): boolean {
  const document = documents.find((item) => item.id === documentId);
  return !document || document.status !== 'ready';
}
