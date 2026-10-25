"""Test path setup for the independently packaged Observatory service."""

from __future__ import annotations

import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(SERVICE_ROOT / "src"))
