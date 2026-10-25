#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
runner=$ROOT/deploy/compose/run-observatory-full-stack-e2e.sh

# Artifact storage requires the declared byte count to be the exact stream
# length, not merely an upper bound. Keep both the public multipart fixture and
# the storage-boundary fixture derived from their actual bytes.
grep -Fqx 'PAYLOAD_BYTES = len(PAYLOAD)' "$runner"
grep -Fqx '            max_bytes=PAYLOAD_BYTES,' "$runner"
grep -Fq 'def metadata(artifact_id: str, purpose: str, digest: str, byte_count: int) -> dict:' "$runner"
grep -Fq '"byteCount":byte_count' "$runner"
grep -Fq 'metadata(artifact_id, purpose, digest, len(body))' "$runner"
test "$(grep -Fc 'max_bytes=1024' "$runner")" -eq 0
test "$(grep -Fc '"byteCount":26' "$runner")" -eq 0

printf '%s\n' 'full-stack artifact byte-integrity fixture regression passed'
