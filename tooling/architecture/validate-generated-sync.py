#!/usr/bin/env python3
from __future__ import annotations

from common import ROOT, emit, git_changed_paths, path_matches

GENERATED_ROOTS = ["packages/python/aura-client", "packages/python/aura-observatory-client", "frontend/libs/platform/aura-api-client", "frontend/libs/platform/observatory-api-client"]

if __name__ == "__main__":
    if not (ROOT / "contracts").is_dir() or not any((ROOT / root).exists() for root in GENERATED_ROOTS):
        raise SystemExit(emit("skipped", "generated-sync", ["Contracts and generated-client outputs are not both present yet."]))
    paths = git_changed_paths()
    contracts_changed = any(path_matches(path, "contracts/**") for path in paths)
    generated_changed = any(path_matches(path, root + "/**") for root in GENERATED_ROOTS for path in paths)
    errors = []
    if contracts_changed and not generated_changed:
        errors.append("Contract sources changed without any generated client output.")
    if generated_changed and not contracts_changed:
        errors.append("Generated client output changed without a contract source change.")
    raise SystemExit(emit("failed" if errors else "passed", "generated-sync", errors or ["Contract and generated changes are synchronized."]))
