export type BranchEditFocusFallback = 'first-edit-or-toolbar';

export interface BranchEditFocusRequest {
  readonly triggerId: string;
  readonly fallback: BranchEditFocusFallback;
}

export type BranchRetryActionKind = 'list' | 'edit' | 'regenerate' | 'select';

/**
 * Completes one branch retry and requests focus only for a successful edit
 * branch. Keeping the await and the success gate together prevents a
 * conflict-refresh retry from focusing the pre-retry transcript.
 */
export async function retryBranchActionAndFocus(
  actionKind: BranchRetryActionKind | null | undefined,
  retry: () => Promise<boolean>,
  requestFocus: () => void,
): Promise<void> {
  if (!actionKind) return;
  const isEdit = actionKind === 'edit';
  const succeeded = await retry();
  if (isEdit && succeeded) requestFocus();
}

/**
 * Resolves the latest rendered branch action and verifies that focus landed on
 * the connected element. The original trigger can disappear when a branch
 * replaces the edited message, so resolution happens on every view check.
 */
export function focusBranchEditTarget(request: BranchEditFocusRequest): boolean {
  if (typeof document === 'undefined') return false;
  const trigger = document.getElementById(request.triggerId);
  const fallback = request.fallback === 'first-edit-or-toolbar'
    ? document.querySelector<HTMLElement>('[data-branch-edit-trigger]') ?? document.getElementById('branch-toolbar-heading')
    : null;
  for (const target of [trigger, fallback]) {
    if (!target || !target.isConnected || target.hasAttribute('disabled')) continue;
    target.focus();
    if (document.activeElement === target) return true;
  }
  return false;
}
