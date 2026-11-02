#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
runner=$ROOT/deploy/compose/run-observatory-full-stack-e2e.sh

# Keep this regression at the public definition boundary. Evaluators and
# suites use the current metadata-only projection schema, which derives their
# default capture authorization; datasets and cases carry the explicit
# authorization because they are the content-bearing definition projections.
python3 - "$runner" <<'PY'
import ast
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
function = text.split("seed_evaluation_definitions() {", 1)[1]
source = function.split("    /app/.venv/bin/python - <<'PY'\n", 1)[1]
source = source.split("\nPY\n  admit_public_capture_authorization", 1)[0]
module = ast.parse(source, filename=str(path))
definitions = {}
for statement in module.body:
    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
        continue
    target = statement.targets[0]
    if isinstance(target, ast.Name) and target.id in {"EVALUATOR", "DATASET", "CASE", "SUITE"}:
        definitions[target.id] = statement.value

if set(definitions) != {"EVALUATOR", "DATASET", "CASE", "SUITE"}:
    raise SystemExit("definition fixture assignments are incomplete")

def keys(value: ast.AST) -> set[str]:
    if not isinstance(value, ast.Dict):
        raise SystemExit("definition fixture is not a literal mapping")
    return {item.value for item in value.keys if isinstance(item, ast.Constant) and isinstance(item.value, str)}

evaluator_keys = keys(definitions["EVALUATOR"])
dataset_keys = keys(definitions["DATASET"])
case_keys = keys(definitions["CASE"])
suite_keys = keys(definitions["SUITE"])
if "captureAuthorization" in evaluator_keys or "captureAuthorization" in suite_keys:
    raise SystemExit("metadata-only evaluator/suite uses stale captureAuthorization field")
if "captureAuthorization" not in dataset_keys or "captureAuthorization" not in case_keys:
    raise SystemExit("dataset/case fixture lost required captureAuthorization")

print("full-stack definition schema regression passed")
PY

printf '%s\n' 'full-stack definition fixture regression passed'
