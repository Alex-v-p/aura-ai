export function branchNavigationAvailable(branches: ReadonlyArray<Readonly<{ readonly id: string }>>, activeId: string | null, direction: -1 | 1): boolean {
  const index = branches.findIndex((branch) => branch.id === activeId);
  return index >= 0 && (direction < 0 ? index > 0 : index < branches.length - 1);
}
