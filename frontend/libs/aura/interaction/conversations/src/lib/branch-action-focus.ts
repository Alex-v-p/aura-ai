export function focusBranchAction(trigger: HTMLButtonElement | null, fallbackId = 'branch-toolbar-heading'): void {
  if (trigger?.isConnected && !trigger.disabled) {
    trigger.focus();
    return;
  }
  if (typeof document !== 'undefined') document.getElementById(fallbackId)?.focus();
}
