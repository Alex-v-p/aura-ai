from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "tooling" / "architecture" / "common.py"
SPEC = importlib.util.spec_from_file_location("aura_architecture_common", SOURCE)
if SPEC is None or SPEC.loader is None:  # pragma: no cover
    raise RuntimeError(f"Cannot load shared governance helpers from {SOURCE}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

for NAME in dir(MODULE):
    if not NAME.startswith("_"):
        globals()[NAME] = getattr(MODULE, NAME)
