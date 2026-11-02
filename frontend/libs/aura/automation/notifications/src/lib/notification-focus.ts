export function focusNotificationReturnTarget(returnFocusId: string | null | undefined): void {
  if (typeof document === 'undefined') return;
  const opener = returnFocusId ? document.getElementById(returnFocusId) : null;
  const target = opener ?? (document.getElementById('notifications-heading') as HTMLElement | null);
  target?.focus();
}
