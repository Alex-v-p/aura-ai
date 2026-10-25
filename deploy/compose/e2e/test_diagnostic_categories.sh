#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
# shellcheck source=diagnostic-categories.sh
. "$ROOT/deploy/compose/e2e/diagnostic-categories.sh"
# Adversarial input must collapse to allowlisted names and counts only.
result=$(printf '%s\n' \
  'ERROR password=do-not-print request-body=private-content' \
  'Traceback: token=do-not-print' \
  'judge provider unavailable' | classify_diagnostic_stream)

case "$result" in
  'categories=dependency_unavailable:1,exception:1,failure:1,provider:1') ;;
  *)
    printf 'unexpected diagnostic classification: %s\n' "$result" >&2
    exit 1
    ;;
esac
if [[ "$result" == *password* || "$result" == *token* || "$result" == *private-content* ]]; then
  printf '%s\n' 'diagnostic classifier leaked source content' >&2
  exit 1
fi
printf '%s\n' 'diagnostic classifier passed metadata-only adversarial test'
