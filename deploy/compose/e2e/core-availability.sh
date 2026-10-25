#!/usr/bin/env bash

# Shared, metadata-only Core availability probes for disposable acceptance
# runners.  The caller supplies a Compose command array named `compose`, a
# bounded `step_timeout`, and `bounded`, `wait_running`, and `wait_healthy`
# helpers.  Keeping this probe here prevents every outage scenario from
# inventing a different Core liveness check.

assert_core_available() {
  local container_id
  container_id=$("${compose[@]}" ps -q aura-core-api 2>/dev/null | head -n 1 || true)
  if [ -z "$container_id" ]; then
    printf '%s\n' 'AURA_E2E Core API container is unavailable during dependency outage' >&2
    return 1
  fi

  docker exec "$container_id" python - <<'PY'
import asyncio
import urllib.request

import redis.asyncio
from aura_core.domains.governance.identity.models import Principal
from aura_core.platform.auth import RedisSessionBackend, SessionService, Settings


for path in ("/health/live",):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=3) as response:
        if response.status != 200:
            raise SystemExit("Core liveness probe failed")


async def authenticated_session_probe() -> None:
    settings = Settings(role="api")
    redis_client = redis.asyncio.from_url(settings.valkey_url, decode_responses=True)
    sessions = SessionService(RedisSessionBackend(redis_client), settings)
    session_id, _ = await sessions.create(
        Principal(settings.oidc_issuer, settings.owner_subject, "Disposable owner")
    )
    try:
        request = urllib.request.Request(
            "http://127.0.0.1:8000/api/v1/auth/session",
            headers={
                "Cookie": f"{settings.session_cookie.name}={session_id}",
                "Origin": settings.public_origin,
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != 200:
                raise SystemExit("Core authenticated session probe failed")
    finally:
        await sessions.revoke(session_id)
        await redis_client.aclose()


asyncio.run(authenticated_session_probe())
PY
  local status=$?
  [ "$status" -eq 0 ] || return "$status"
  printf '%s\n' 'AURA_E2E Core liveness and authenticated session remained usable'
}

capture_core_telemetry_export_stats() {
  local service=${1:-aura-core-api} container_id
  case "$service" in
    aura-core-api|aura-core-worker) ;;
    *) return 1 ;;
  esac
  container_id=$("${compose[@]}" ps -q "$service" 2>/dev/null | head -n 1 || true)
  [ -n "$container_id" ] || return 1
  container_started_at=$(docker inspect -f '{{.State.StartedAt}}' "$container_id" 2>/dev/null || true)
  container_pid=$(docker inspect -f '{{.State.Pid}}' "$container_id" 2>/dev/null || true)
  [ -n "$container_started_at" ] && [ -n "$container_pid" ] || return 1
docker logs --since 180s "$container_id" 2>/dev/null | \
  CORE_TELEMETRY_SERVICE="$service" \
  CORE_TELEMETRY_CONTAINER_ID="${container_id:0:16}" \
  CORE_TELEMETRY_STARTED_AT="$container_started_at" \
  CORE_TELEMETRY_PID="$container_pid" \
  python3 -c '
import json
import math
import os
import sys

latest = None
for line in sys.stdin:
    try:
        record = json.loads(line)
    except (TypeError, ValueError):
        continue
    if record.get("event") != "aura.telemetry.health":
        continue
    names = (
        "telemetry_export_accepted_count",
        "telemetry_export_failure_count",
        "telemetry_export_retry_count",
        "telemetry_export_dropped_count",
        "retained",
        "dropped",
        "rejected",
        "abandoned_batches",
        "export_failures",
    )
    values = {
        name: record.get(name)
        for name in names
    }
    if all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and value >= 0
        for value in values.values()
    ):
        latest = values
if latest is None:
    raise SystemExit("Core telemetry exporter health sample unavailable")
print(json.dumps({
    "service": os.environ.get("CORE_TELEMETRY_SERVICE", "unknown")[:32],
    "containerId": os.environ.get("CORE_TELEMETRY_CONTAINER_ID", "unavailable")[:16],
    "startedAt": os.environ.get("CORE_TELEMETRY_STARTED_AT", "unavailable")[:40],
    "pid": int(os.environ.get("CORE_TELEMETRY_PID", "0")),
    **latest,
}, separators=(",", ":")))
'
}

capture_collector_queue_signal() {
  local api_id
  api_id=$("${compose[@]}" ps -q observatory-api 2>/dev/null | head -n 1 || true)
  [ -n "$api_id" ] || return 1
  docker exec -i "$api_id" uv run python - <<'PY'
import json
import math
import re
import urllib.error
import urllib.request

allowed = {
    "otelcol_exporter_sent_spans": "sentSpans",
    "otelcol_exporter_send_failed_spans": "sendFailedSpans",
    "otelcol_exporter_enqueue_failed_spans": "enqueueFailedSpans",
    "otelcol_exporter_retry_exhausted_spans": "retryExhaustedSpans",
    "otelcol_exporter_sent_metric_points": "sentMetricPoints",
    "otelcol_exporter_send_failed_metric_points": "sendFailedMetricPoints",
    "otelcol_exporter_enqueue_failed_metric_points": "enqueueFailedMetricPoints",
    "otelcol_exporter_queue_size": "queueSize",
    "otelcol_exporter_queue_capacity": "queueCapacity",
}
try:
    with urllib.request.urlopen("http://observatory-collector:8888/metrics", timeout=5) as response:
        exposition = response.read(1_000_000).decode("utf-8", "replace")
except (OSError, urllib.error.URLError):
    print(json.dumps({"available": False}, separators=(",", ":")))
    raise SystemExit(0)
totals = {field: 0.0 for field in allowed.values()}
found = set()
for line in exposition.splitlines():
    if not line or line.startswith("#") or " " not in line:
        continue
    name_and_labels, raw_value = line.rsplit(None, 1)
    name = name_and_labels.split("{", 1)[0]
    field = allowed.get(name)
    if field is None:
        continue
    labels = dict(re.findall(r'(\w+)="([^"]*)"', name_and_labels))
    if labels.get("exporter") != "otlphttp/observatory":
        continue
    try:
        value = float(raw_value)
    except ValueError:
        continue
    if not math.isfinite(value) or value < 0:
        continue
    totals[field] += value
    found.add(field)
if not found:
    print(json.dumps({"available": False}, separators=(",", ":")))
else:
    payload = {name: totals[name] for name in sorted(found)}
    capacity = payload.get("queueCapacity")
    queue_size = payload.get("queueSize")
    if capacity is not None and capacity > 0 and queue_size is not None:
        payload["queueUtilization"] = min(1.0, max(0.0, queue_size / capacity))
    print(json.dumps({"available": True, **payload}, separators=(",", ":")))
PY
}

assert_monotonic_core_telemetry_export_stats() {
  local before=$1 after=$2
  python3 - "$before" "$after" <<'PY'
import json
import math
import sys

before = json.loads(sys.argv[1])
after = json.loads(sys.argv[2])
names = (
    "telemetry_export_accepted_count",
    "telemetry_export_failure_count",
    "telemetry_export_retry_count",
    "telemetry_export_dropped_count",
    "retained",
    "dropped",
    "rejected",
    "abandoned_batches",
    "export_failures",
)
for name in names:
    previous = float(before[name])
    current = float(after[name])
    if not math.isfinite(previous) or not math.isfinite(current) or current < previous:
        raise SystemExit("Core telemetry exporter health counters were not monotonic")
PY
}

assert_core_telemetry_export_transition() {
  local phase=$1 before=$2 during=$3
  python3 - "$before" "$during" <<'PY'
import json
import sys

before = json.loads(sys.argv[1])
during = json.loads(sys.argv[2])
names = (
    "telemetry_export_accepted_count",
    "telemetry_export_failure_count",
    "telemetry_export_retry_count",
    "telemetry_export_dropped_count",
    "retained",
    "dropped",
    "rejected",
    "abandoned_batches",
    "export_failures",
)
for name in names:
    if float(during[name]) < float(before[name]):
        raise SystemExit("Core telemetry exporter health counters regressed")
retry_growth = float(during["telemetry_export_retry_count"]) > float(
    before["telemetry_export_retry_count"]
)
drop_growth = float(during["telemetry_export_dropped_count"]) > float(
    before["telemetry_export_dropped_count"]
)
if not (retry_growth or drop_growth):
    raise SystemExit("Collector outage did not increase Core exporter retry/drop counters")
print("Core telemetry exporter retry/drop transition observed")
PY
  local status=$?
  [ "$status" -eq 0 ] || return "$status"
  printf 'AURA_E2E phase=%s core_telemetry_export=retry-or-drop-transition\n' "$phase"
}

assert_core_telemetry_export_recovered() {
  local phase=$1 during=$2 recovered=$3
  python3 - "$during" "$recovered" <<'PY'
import json
import sys

during = json.loads(sys.argv[1])
recovered = json.loads(sys.argv[2])
for name in (
    "telemetry_export_accepted_count",
    "telemetry_export_failure_count",
    "telemetry_export_retry_count",
    "telemetry_export_dropped_count",
    "retained",
    "dropped",
    "abandoned_batches",
    "export_failures",
):
    if float(recovered[name]) < float(during[name]):
        raise SystemExit("Core telemetry exporter counters regressed after Collector recovery")
print("Core telemetry exporter counters remained valid after recovery")
PY
  local status=$?
  [ "$status" -eq 0 ] || return "$status"
  printf 'AURA_E2E phase=%s core_telemetry_export=recovered\n' "$phase"
}

assert_observatory_metric_samples() {
  local phase=$1
  shift
  local api_id
  api_id=$("${compose[@]}" ps -q observatory-api 2>/dev/null | head -n 1 || true)
  [ -n "$api_id" ] || return 2
  if ! docker exec "$api_id" python - "$@" <<'PY'
import math
import re
import sys
import urllib.request

required = tuple(sys.argv[1:])
with urllib.request.urlopen("http://127.0.0.1:8000/metrics", timeout=5) as response:
    exposition = response.read(1_000_000).decode("utf-8", "replace")
samples: dict[str, list[tuple[dict[str, str], float]]] = {name: [] for name in required}
for line in exposition.splitlines():
    if not line or line.startswith("#") or " " not in line:
        continue
    name_and_labels, raw_value = line.rsplit(None, 1)
    try:
        value = float(raw_value)
    except ValueError:
        continue
    if not math.isfinite(value):
        continue
    name = name_and_labels.split("{", 1)[0]
    if name not in samples:
        continue
    labels_match = re.search(r"\{([^}]*)\}", name_and_labels)
    labels = dict(re.findall(r'(\w+)="([^"]*)"', labels_match.group(1))) if labels_match else {}
    samples[name].append((labels, value))
for name, values in samples.items():
    if not values:
        raise SystemExit(2)
    if name in {
        "observatory_evaluation_artifact_operation_count",
    } and not any(value >= 1 and labels.get("operation") and labels.get("outcome") for labels, value in values):
        raise SystemExit(1)
    if name == "observatory_evaluation_scheduler_heartbeat_age_seconds" and not any(value >= 0 for _, value in values):
        raise SystemExit(1)
print("metric samples observed")
PY
  then
    # The API metrics endpoint is the authorized public evidence boundary. A
    # missing family is an explicit prerequisite, not a synthetic success.
    return 2
  fi
  printf 'AURA_E2E phase=%s metric_samples=verified count=%s\n' "$phase" "$#"
}

wait_for_observatory_metric_samples() {
  local phase=$1
  shift
  local deadline=$((SECONDS + step_timeout))
  while (( SECONDS < deadline )); do
    if assert_observatory_metric_samples "$phase" "$@" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  printf 'AURA_E2E phase=%s metric_samples=prerequisite\n' "$phase" >&2
  return 2
}

capture_observatory_metric_total() {
  local phase=$1 metric=$2 api_id
  api_id=$("${compose[@]}" ps -q observatory-api 2>/dev/null | head -n 1 || true)
  if [ -z "$api_id" ]; then
    printf 'AURA_E2E phase=%s metric=%s surface=unavailable\n' "$phase" "$metric" >&2
    return 1
  fi
  docker exec "$api_id" python - "$metric" <<'PY'
import json
import math
import re
import sys
import urllib.request

metric = sys.argv[1]
with urllib.request.urlopen("http://127.0.0.1:8000/metrics", timeout=5) as response:
    exposition = response.read(1_000_000).decode("utf-8", "replace")
total = 0.0
sample_count = 0
for line in exposition.splitlines():
    if not line or line.startswith("#") or " " not in line:
        continue
    name_and_labels, raw_value = line.rsplit(None, 1)
    if name_and_labels.split("{", 1)[0] != metric:
        continue
    try:
        value = float(raw_value)
    except ValueError:
        continue
    if not math.isfinite(value) or value < 0:
        continue
    labels_match = re.search(r"\{([^}]*)\}", name_and_labels)
    labels = dict(re.findall(r'(\w+)="([^"]*)"', labels_match.group(1))) if labels_match else {}
    if metric in {
        "observatory_evaluation_artifact_operation_count",
        "observatory_operations_recovery_count",
    } and (not labels.get("operation") or not labels.get("outcome")):
        raise SystemExit("required bounded metric labels are missing")
    total += value
    sample_count += 1
print(json.dumps({"value": total, "samples": sample_count}, separators=(",", ":")))
PY
  local status=$?
  [ "$status" -eq 0 ] || return "$status"
  printf 'AURA_E2E phase=%s metric=%s sample=bounded-total\n' "$phase" "$metric" >&2
}

assert_observatory_metric_increment() {
  local phase=$1 before=$2 after=$3 metric=$4
  python3 - "$before" "$after" "$metric" <<'PY'
import json
import math
import sys

before = json.loads(sys.argv[1])
after = json.loads(sys.argv[2])
metric = sys.argv[3]
before_value = float(before.get("value", -1))
after_value = float(after.get("value", -1))
if not all(math.isfinite(value) and value >= 0 for value in (before_value, after_value)):
    raise SystemExit(f"{metric} metric values were not finite and bounded")
if int(after.get("samples", 0)) < 1 or after_value <= before_value:
    raise SystemExit(f"{metric} metric did not increase after the executed workflow")
print(f"{metric} metric increment observed")
PY
  local status=$?
  [ "$status" -eq 0 ] || return "$status"
  printf 'AURA_E2E phase=%s metric=%s increment=verified\n' "$phase" "$metric"
}

assert_scheduler_heartbeat_transition() {
  local phase=$1 before=$2 stopped=$3 recovered=$4
  python3 - "$before" "$stopped" "$recovered" <<'PY'
import json
import math
import sys

values = [json.loads(value).get("value") for value in sys.argv[1:]]
sample_counts = [int(json.loads(value).get("samples", 0)) for value in sys.argv[1:]]
if any(count < 1 for count in sample_counts):
    raise SystemExit(2)
if not all(isinstance(value, (int, float)) and math.isfinite(float(value)) and value >= 0 for value in values):
    raise SystemExit("scheduler heartbeat age samples were not finite and bounded")
before, stopped, recovered = map(float, values)
if stopped <= before:
    raise SystemExit("scheduler heartbeat age did not advance while scheduler was stopped")
if recovered >= stopped:
    raise SystemExit("scheduler heartbeat age did not improve after scheduler recovery")
print("scheduler heartbeat stopped/restarted transition observed")
PY
  local status=$?
  [ "$status" -eq 0 ] || return "$status"
  printf 'AURA_E2E phase=%s scheduler_heartbeat=stopped-advanced-recovered\n' "$phase"
}

assert_core_evaluation_runner_nats_recovery_metric() {
  # observatory-v1 exposes no authorized metric-specific query for a Core
  # metric, and the Observatory Prometheus endpoint contains only
  # Observatory-owned series. Do not read /metrics as a cross-service
  # substitute; retain an explicit capability blocker until ingested Core
  # telemetry is queryable through an authorized component-metric resource.
  printf '%s\n' \
    'AURA_E2E Core evaluation_runner_nats_recovery_count has no authorized Observatory ingest query' >&2
  return 2
}

assert_core_ready() {
  local container_id
  container_id=$("${compose[@]}" ps -q aura-core-api 2>/dev/null | head -n 1 || true)
  if [ -z "$container_id" ]; then
    printf '%s\n' 'AURA_E2E Core API container is unavailable during readiness probe' >&2
    return 1
  fi

  docker exec "$container_id" python - <<'PY'
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8000/health/ready", timeout=5) as response:
    payload = json.loads(response.read(65536))
    if response.status != 200 or payload.get("status") != "ready":
        raise SystemExit("Core readiness probe did not recover")
PY
  local status=$?
  [ "$status" -eq 0 ] || return "$status"
}

assert_observatory_public_surfaces() {
  local phase=$1 api_id web_id
  api_id=$("${compose[@]}" ps -q observatory-api 2>/dev/null | head -n 1 || true)
  web_id=$("${compose[@]}" ps -q observatory-web 2>/dev/null | head -n 1 || true)
  if [ -z "$api_id" ] || [ -z "$web_id" ]; then
    printf 'AURA_E2E phase=%s observatory_public_surface=unavailable\n' "$phase" >&2
    return 1
  fi
  docker exec "$api_id" python - <<'PY'
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3) as response:
    payload = json.loads(response.read(4096))
    if response.status != 200 or payload.get("status") != "ok":
        raise SystemExit("Observatory API public health did not report ok")
PY
  local status=$?
  [ "$status" -eq 0 ] || return "$status"
  docker exec "$web_id" sh -c \
    'wget --quiet --output-document=- http://127.0.0.1:4300/healthz | grep -q "^ok$"'
  status=$?
  [ "$status" -eq 0 ] || return "$status"
  printf 'AURA_E2E phase=%s observatory_public_surface=ok\n' "$phase"
}

assert_observatory_component_signal() {
  local phase=$1
  local expected=${2:-recovered}
  local api_id
  api_id=$("${compose[@]}" ps -q observatory-api 2>/dev/null | head -n 1 || true)
  if [ -z "$api_id" ]; then
    printf 'AURA_E2E phase=%s component_signal=unavailable\n' "$phase" >&2
    return 1
  fi
  docker exec "$api_id" python - "$expected" <<'PY'
import asyncio
import json
import re
import sys
import urllib.error
import urllib.request

import redis.asyncio
from aura_observatory.auth import MemoryLoginStateBackend, ObservatoryAuth, Principal, RedisSessionBackend
from aura_observatory.settings import Settings


async def main() -> None:
    expected = sys.argv[1]
    settings = Settings(role="api")
    redis_client = redis.asyncio.from_url(
        settings.session_store_url,
        password=settings.session_store_password,
        decode_responses=True,
    )
    auth = ObservatoryAuth(
        settings,
        RedisSessionBackend(redis_client, namespace=settings.session_namespace),
        MemoryLoginStateBackend(),
    )
    session_id, _ = await auth.create_session(
        Principal(settings.oidc_issuer, settings.owner_subject, "E2E owner")
    )
    headers = {
        "Cookie": f"{settings.effective_session_cookie_name}={session_id}",
        "Origin": settings.public_origin,
    }
    component_id = "aura.observatory.query_api"
    health_url = (
        "http://127.0.0.1:8000/api/v1/components/"
        f"{component_id}/health?component_version=1.0.0"
    )
    definition_url = (
        "http://127.0.0.1:8000/api/v1/components/"
        f"{component_id}/definitions/1.0.0"
    )

    def get(url: str, request_headers: dict[str, str] | None = None) -> tuple[int, object]:
        request = urllib.request.Request(url, headers=request_headers or {})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read(65536)
                return response.status, json.loads(body)
        except urllib.error.HTTPError as error:
            return error.code, None

    status, payload = get(health_url, headers)
    if expected == "degraded":
        if status != 503:
            raise SystemExit("component health did not expose dependency degradation")
    elif status != 200 or not isinstance(payload, dict):
        raise SystemExit("component health did not recover")
    else:
        summary = payload.get("summary")
        if not isinstance(summary, dict) or not {"sampleCount", "outcomes"} <= set(summary):
            raise SystemExit("component health omitted bounded runtime summary")
        if not isinstance(summary.get("sampleCount"), int) or summary["sampleCount"] < 1:
            raise SystemExit("component health omitted a bounded runtime sample")
        outcomes = summary.get("outcomes")
        if not isinstance(outcomes, dict) or not any(
            isinstance(value, int) and value > 0 for value in outcomes.values()
        ):
            raise SystemExit("component health omitted a bounded runtime outcome")
        definition_status, definition = get(definition_url, headers)
        if definition_status != 200 or not isinstance(definition, dict):
            raise SystemExit("component definition unavailable")
        manifest_metrics = set(definition.get("metrics", []))
        if not {
            "observatory_query_duration_ms",
            "observatory_query_outcome",
        } <= manifest_metrics or not definition.get("evaluators"):
            raise SystemExit("component definition omitted metric/evaluator declarations")
        metrics_request = urllib.request.Request("http://127.0.0.1:8000/metrics")
        try:
            with urllib.request.urlopen(metrics_request, timeout=5) as response:
                metrics_status = response.status
                metrics = response.read(65536).decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            metrics_status = error.code
            metrics = ""
        if metrics_status != 200:
            raise SystemExit("component metrics surface unavailable")
        duration_samples: list[float] = []
        outcome_samples: list[tuple[dict[str, str], float]] = []
        for line in metrics.splitlines():
            if not line or line.startswith("#") or " " not in line:
                continue
            name_and_labels, raw_value = line.rsplit(None, 1)
            try:
                value = float(raw_value)
            except ValueError:
                continue
            if name_and_labels.startswith("observatory_query_duration_ms_count"):
                duration_samples.append(value)
            elif name_and_labels.startswith("observatory_query_outcome"):
                labels_match = re.search(r"\{([^}]*)\}", name_and_labels)
                labels = {}
                if labels_match:
                    labels = dict(re.findall(r'(\w+)="([^"]*)"', labels_match.group(1)))
                outcome_samples.append((labels, value))
        if not duration_samples or max(duration_samples) < 1:
            raise SystemExit("component runtime duration metric had no bounded sample")
        if not any(
            value >= 1 and labels.get("resource") and labels.get("outcome")
            for labels, value in outcome_samples
        ):
            raise SystemExit("component runtime metrics were not exposed")
    await auth.sessions.delete(session_id)
    await redis_client.aclose()


asyncio.run(main())
PY
  local status=$?
  [ "$status" -eq 0 ] || return "$status"
  printf 'AURA_E2E phase=%s component=aura.observatory.query_api signal=%s\n' "$phase" "$expected"
}

assert_observatory_postgres_degraded() {
  local phase=$1 api_id status
  api_id=$("${compose[@]}" ps -q observatory-api 2>/dev/null | head -n 1 || true)
  [ -n "$api_id" ] || return 1
  status=$(docker exec "$api_id" python - <<'PY'
import asyncio
import urllib.error
import urllib.request

import redis.asyncio
from aura_observatory.auth import MemoryLoginStateBackend, ObservatoryAuth, Principal, RedisSessionBackend
from aura_observatory.settings import Settings


async def main() -> None:
    settings = Settings(role="api")
    redis_client = redis.asyncio.from_url(
        settings.session_store_url,
        password=settings.session_store_password,
        decode_responses=True,
    )
    auth = ObservatoryAuth(
        settings,
        RedisSessionBackend(redis_client, namespace=settings.session_namespace),
        MemoryLoginStateBackend(),
    )
    session_id, _ = await auth.create_session(
        Principal(settings.oidc_issuer, settings.owner_subject, "E2E owner")
    )
    try:
        request = urllib.request.Request(
            "http://127.0.0.1:8000/api/v1/components/"
            "aura.observatory.query_api/definitions/1.0.0",
            headers={
                "Cookie": f"{settings.effective_session_cookie_name}={session_id}",
                "Origin": settings.public_origin,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                print(response.status)
        except urllib.error.HTTPError as error:
            print(error.code)
        except (OSError, TimeoutError):
            print(599)
    finally:
        await auth.sessions.delete(session_id)
        await redis_client.aclose()


asyncio.run(main())
PY
) || return $?
  case "$status" in
    5*)
      printf 'AURA_E2E phase=%s postgres_surface=authenticated-definition-degraded status_class=%s\n' "$phase" "${status:0:1}xx"
      ;;
    *)
      printf 'AURA_E2E phase=%s postgres_surface_status=%s\n' "$phase" "$status" >&2
      return 1
      ;;
  esac
}

create_observatory_owner_session() {
  local api_id=$1
  [ -n "$api_id" ] || return 1
  docker exec "$api_id" python - <<'PY'
import asyncio

import redis.asyncio
from aura_observatory.auth import MemoryLoginStateBackend, ObservatoryAuth, Principal, RedisSessionBackend
from aura_observatory.settings import Settings


async def main() -> None:
    settings = Settings(role="api")
    redis_client = redis.asyncio.from_url(
        settings.session_store_url,
        password=settings.session_store_password,
        decode_responses=True,
    )
    auth = ObservatoryAuth(
        settings,
        RedisSessionBackend(redis_client, namespace=settings.session_namespace),
        MemoryLoginStateBackend(),
    )
    session_id, _ = await auth.create_session(
        Principal(settings.oidc_issuer, settings.owner_subject, "E2E owner")
    )
    print(session_id)
    await redis_client.aclose()


asyncio.run(main())
PY
  local status=$?
  [ "$status" -eq 0 ] || return "$status"
}

assert_observatory_session_degraded() {
  local phase=$1 session_id=$2 api_id status
  api_id=$("${compose[@]}" ps -q observatory-api 2>/dev/null | head -n 1 || true)
  [ -n "$api_id" ] || return 1
  [ -n "$session_id" ] || return 1
  # The live session credential is sent on stdin.  It must not appear in the
  # docker exec argv, environment, logs, or any other process metadata.
  status=$(docker exec -i "$api_id" python -c '
import sys
import urllib.error
import urllib.request

from aura_observatory.settings import Settings

settings = Settings(role="api")
session_id = sys.stdin.read().strip()
if not session_id:
    raise SystemExit("session credential was not provided on stdin")
request = urllib.request.Request(
    "http://127.0.0.1:8000/api/v1/auth/me",
    headers={"Cookie": settings.effective_session_cookie_name + "=" + session_id},
)
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        print(response.status)
except urllib.error.HTTPError as error:
    print(error.code)
except (OSError, TimeoutError):
    print(599)
' <<<"$session_id") || return $?
  case "$status" in
    5*) ;;
    *)
      printf 'AURA_E2E phase=%s session_surface_status=%s\n' "$phase" "$status" >&2
      return 1
      ;;
  esac
  printf 'AURA_E2E phase=%s session_surface=authenticated-session-unavailable status_class=%s\n' "$phase" "${status:0:1}xx"
}

assert_observatory_session_recovered() {
  local phase=$1 session_id=$2 api_id status
  api_id=$("${compose[@]}" ps -q observatory-api 2>/dev/null | head -n 1 || true)
  [ -n "$api_id" ] || return 1
  [ -n "$session_id" ] || return 1
  # Keep the session credential on stdin rather than exposing it through
  # process arguments.  The owner session is revoked in the async finally
  # block even when the recovered authenticated request fails.
  status=$(docker exec -i "$api_id" python -c '
import asyncio
import json
import sys
import urllib.request

import redis.asyncio
from aura_observatory.auth import MemoryLoginStateBackend, ObservatoryAuth, RedisSessionBackend
from aura_observatory.settings import Settings


async def main() -> None:
    settings = Settings(role="api")
    session_id = sys.stdin.read().strip()
    if not session_id:
        raise SystemExit("session credential was not provided on stdin")
    redis_client = redis.asyncio.from_url(
        settings.session_store_url,
        password=settings.session_store_password,
        decode_responses=True,
    )
    auth = ObservatoryAuth(
        settings,
        RedisSessionBackend(redis_client, namespace=settings.session_namespace),
        MemoryLoginStateBackend(),
    )
    try:
        request = urllib.request.Request(
            "http://127.0.0.1:8000/api/v1/auth/me",
            headers={
                "Cookie": f"{settings.effective_session_cookie_name}={session_id}",
                "Origin": settings.public_origin,
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read(4096))
            if response.status != 200 or not isinstance(payload.get("csrfToken"), str):
                raise SystemExit("authenticated session did not recover")
    finally:
        await auth.sessions.delete(session_id)
        await redis_client.aclose()
    print("200")


asyncio.run(main())
' <<<"$session_id") || return $?
  [ "$status" = 200 ] || {
    printf 'AURA_E2E phase=%s session_surface_status=%s\n' "$phase" "$status" >&2
    return 1
  }
  printf 'AURA_E2E phase=%s session_surface=authenticated-session-recovered\n' "$phase"
}

capture_observatory_ingest_signal() {
  local api_id
  api_id=$("${compose[@]}" ps -q observatory-api 2>/dev/null | head -n 1 || true)
  [ -n "$api_id" ] || return 1
  docker exec -i "$api_id" uv run python - <<'PY'
import asyncio
import json
import urllib.request

import redis.asyncio
from aura_observatory.auth import MemoryLoginStateBackend, ObservatoryAuth, Principal, RedisSessionBackend
from aura_observatory.settings import Settings


async def main() -> None:
    settings = Settings(role="api")
    redis_client = redis.asyncio.from_url(
        settings.session_store_url,
        password=settings.session_store_password,
        decode_responses=True,
    )
    auth = ObservatoryAuth(
        settings,
        RedisSessionBackend(redis_client, namespace=settings.session_namespace),
        MemoryLoginStateBackend(),
    )
    session_id, _ = await auth.create_session(
        Principal(settings.oidc_issuer, settings.owner_subject, "E2E owner")
    )
    try:
        request = urllib.request.Request(
            "http://127.0.0.1:8000/api/v1/overview",
            headers={
                "Cookie": f"{settings.effective_session_cookie_name}={session_id}",
                "Origin": settings.public_origin,
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read(65536))
        health = payload.get("health")
        if not isinstance(health, dict):
            raise SystemExit("ingest health snapshot unavailable")
        ingest = payload.get("ingest")
        counts = ingest.get("counts") if isinstance(ingest, dict) else None
        if not isinstance(counts, dict):
            raise SystemExit("ingest counter snapshot unavailable")
        for name in ("accepted", "rejected", "duplicate", "malformed", "storageFailure"):
            value = counts.get(name)
            if type(value) is not int or value < 0:
                raise SystemExit("ingest counter snapshot was not bounded")
        required = {
            "state",
            "intakeLagMs",
            "visibilityWatermark",
            "visibilityLagCount",
            "workerHeartbeatAt",
            "workerHeartbeatAgeMs",
            "backlogCount",
            "oldestBacklogAgeMs",
        }
        if not required <= set(health):
            raise SystemExit("ingest health snapshot omitted bounded signals")
        metrics_request = urllib.request.Request("http://127.0.0.1:8000/metrics")
        with urllib.request.urlopen(metrics_request, timeout=5) as response:
            metrics = response.read(65536).decode("utf-8", "replace")
        for name in (
            "observatory_ingest_intake_lag_ms",
            "observatory_ingest_visibility_lag_count",
            "observatory_ingest_worker_heartbeat_age_ms",
            "observatory_ingest_backlog_count",
        ):
            if name not in metrics:
                raise SystemExit(f"ingest metric not exposed: {name}")
        heartbeat_samples = []
        heartbeat_name = "observatory_ingest_worker_heartbeat_age_ms"
        for line in metrics.splitlines():
            if line.startswith(heartbeat_name) and " " in line and not line.startswith("#"):
                try:
                    heartbeat_samples.append(float(line.rsplit(None, 1)[1]))
                except ValueError:
                    pass
        if not heartbeat_samples or max(heartbeat_samples) < 0:
            raise SystemExit("ingest worker heartbeat metric had no bounded sample")
        print(json.dumps({
            "accepted": counts["accepted"],
            "rejected": counts["rejected"],
            "duplicate": counts["duplicate"],
            "malformed": counts["malformed"],
            "storageFailure": counts["storageFailure"],
            "state": health.get("state"),
            "intakeLagMs": health.get("intakeLagMs"),
            "visibilityWatermark": health.get("visibilityWatermark"),
            "visibilityLagCount": health.get("visibilityLagCount"),
            "workerHeartbeatAgeMs": health.get("workerHeartbeatAgeMs"),
            "backlogCount": health.get("backlogCount"),
        }, separators=(",", ":")))
    finally:
        await auth.sessions.delete(session_id)
        await redis_client.aclose()


asyncio.run(main())
PY
  local status=$?
  [ "$status" -eq 0 ] || return "$status"
}

assert_observatory_ingest_signal() {
  local phase=$1 snapshot
  snapshot=$(capture_observatory_ingest_signal) || return 1
  [ -n "$snapshot" ] || return 1
  printf 'AURA_E2E phase=%s ingest_signal=health-watermark-heartbeat-backlog-observed\n' "$phase"
}

assert_collector_ingest_transition() {
  local phase=$1 before=$2 after=$3
  python3 - "$before" "$after" <<'PY'
import json
import sys

before = json.loads(sys.argv[1])
after = json.loads(sys.argv[2])

def number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None

before_lag = number(before.get("intakeLagMs"))
after_lag = number(after.get("intakeLagMs"))
before_heartbeat = number(before.get("workerHeartbeatAgeMs"))
after_heartbeat = number(after.get("workerHeartbeatAgeMs"))
before_visibility = number(before.get("visibilityWatermark"))
after_visibility = number(after.get("visibilityWatermark"))
before_pending = max(
    number(before.get("visibilityLagCount")) or 0.0,
    number(before.get("backlogCount")) or 0.0,
)
after_pending = max(
    number(after.get("visibilityLagCount")) or 0.0,
    number(after.get("backlogCount")) or 0.0,
)
state_transition = before.get("state") != after.get("state") and after.get("state") in {
    "delayed", "partial", "degraded", "unavailable"
}
stale_lag = (
    before_lag is not None and after_lag is not None and after_lag > before_lag
    and (after_lag - before_lag) >= 1000.0
)
stale_heartbeat = (
    before_heartbeat is not None and after_heartbeat is not None
    and after_heartbeat - before_heartbeat >= 1000.0
)
pending_growth = after_pending > before_pending
watermark_stalled = (
    before_visibility is not None and after_visibility is not None
    and after_visibility == before_visibility
)
if not state_transition and not (
    stale_lag and (watermark_stalled or pending_growth or stale_heartbeat)
):
    raise SystemExit("collector outage produced no stale/degraded ingest transition")
print("collector ingest outage transition observed")
PY
  local status=$?
  [ "$status" -eq 0 ] || return "$status"
  printf 'AURA_E2E phase=%s collector_signal=stale-or-degraded-transition\n' "$phase"
}

assert_collector_ingest_recovery() {
  local phase=$1 degraded=$2 recovered=$3
  python3 - "$degraded" "$recovered" <<'PY'
import json
import sys

degraded = json.loads(sys.argv[1])
recovered = json.loads(sys.argv[2])

def number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None

degraded_lag = number(degraded.get("intakeLagMs"))
recovered_lag = number(recovered.get("intakeLagMs"))
degraded_heartbeat = number(degraded.get("workerHeartbeatAgeMs"))
recovered_heartbeat = number(recovered.get("workerHeartbeatAgeMs"))
degraded_watermark = number(degraded.get("visibilityWatermark"))
recovered_watermark = number(recovered.get("visibilityWatermark"))
degraded_pending = max(
    number(degraded.get("visibilityLagCount")) or 0.0,
    number(degraded.get("backlogCount")) or 0.0,
)
recovered_pending = max(
    number(recovered.get("visibilityLagCount")) or 0.0,
    number(recovered.get("backlogCount")) or 0.0,
)
healthy_state = recovered.get("state") == "healthy"
lag_reduced = (
    degraded_lag is not None and recovered_lag is not None and recovered_lag < degraded_lag
)
heartbeat_reduced = (
    degraded_heartbeat is not None
    and recovered_heartbeat is not None
    and recovered_heartbeat < degraded_heartbeat
)
watermark_advanced = (
    degraded_watermark is not None
    and recovered_watermark is not None
    and recovered_watermark > degraded_watermark
)
pending_reduced = recovered_pending < degraded_pending
if not healthy_state or not (lag_reduced or heartbeat_reduced or watermark_advanced or pending_reduced):
    raise SystemExit("collector ingest signal did not recover")
print("collector ingest recovery observed")
PY
  local status=$?
  [ "$status" -eq 0 ] || return "$status"
  printf 'AURA_E2E phase=%s collector_signal=recovered\n' "$phase"
}

assert_observatory_api_degraded() {
  local phase=$1 web_id
  web_id=$("${compose[@]}" ps -q observatory-web 2>/dev/null | head -n 1 || true)
  [ -n "$web_id" ] || return 1
  if docker exec "$web_id" sh -c \
    'wget --quiet --output-document=- http://observatory-api:8000/health >/dev/null 2>&1'; then
    printf 'AURA_E2E phase=%s api_surface=unexpectedly_reachable\n' "$phase" >&2
    return 1
  fi
  printf 'AURA_E2E phase=%s api_surface=unavailable\n' "$phase"
}

assert_observatory_web_degraded() {
  local phase=$1 api_id
  api_id=$("${compose[@]}" ps -q observatory-api 2>/dev/null | head -n 1 || true)
  [ -n "$api_id" ] || return 1
if docker exec "$api_id" python - <<'PY' 2>/dev/null
import urllib.request

urllib.request.urlopen("http://observatory-web:4300/healthz", timeout=3)
PY
  then
    printf 'AURA_E2E phase=%s web_surface=unexpectedly_reachable\n' "$phase" >&2
    return 1
  fi
  printf 'AURA_E2E phase=%s web_surface=unavailable\n' "$phase"
}

assert_core_nats_unavailable() {
  local container_id
  container_id=$("${compose[@]}" ps -q aura-core-api 2>/dev/null | head -n 1 || true)
  if [ -z "$container_id" ]; then
    printf '%s\n' 'AURA_E2E Core API container is unavailable during NATS outage probe' >&2
    return 1
  fi

  docker exec "$container_id" python - <<'PY'
import json
import urllib.error
import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:8000/health/ready", timeout=5)
except urllib.error.HTTPError as error:
    if error.code != 503:
        raise SystemExit(f"Core readiness returned unexpected HTTP status {error.code}")
    payload = json.loads(error.read(65536))
    nats = payload.get("checks", {}).get("nats", {})
    if nats.get("status") == "ready":
        raise SystemExit("Core readiness omitted the shared NATS outage")
    print("Core truthfully reported shared NATS unavailable")
else:
    raise SystemExit("Core readiness unexpectedly remained ready while shared NATS was stopped")
PY
  local status=$?
  [ "$status" -eq 0 ] || return "$status"
}

wait_core_ready() {
  local deadline=$((SECONDS + step_timeout))
  while (( SECONDS < deadline )); do
    if assert_core_ready 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  printf '%s\n' 'AURA_E2E Core readiness did not recover before the bounded deadline' >&2
  return 1
}

run_core_normal_run_phase() {
  local phase=$1
  printf 'AURA_E2E phase=%s action=authenticated-core-normal-run\n' "$phase"
  bounded "$step_timeout" "${compose[@]}" run --rm --no-deps --no-TTY \
    -e AURA_OBSERVATORY_SERVICES_STOPPED=1 \
    -e AURA_OBSERVATORY_RUN_PROMPT="AURA0098_CORE_OUTAGE_${phase}" frontend-e2e \
    sh -c "exec pnpm --dir frontend exec playwright test \
      --config=apps/observatory-web-e2e/full-stack.config.ts \
      --grep 'keeps Core usable after the platform stops Observatory services'"
  local status=$?
  [ "$status" -eq 0 ] || return "$status"
  printf 'AURA_E2E phase=%s authenticated-core-normal-run=passed\n' "$phase"
}

run_core_provider_unavailable_probe() {
  local phase=$1
  local container_id
  container_id=$("${compose[@]}" ps -q aura-core-api 2>/dev/null | head -n 1 || true)
  [ -n "$container_id" ] || return 1
  docker exec "$container_id" python - <<'PY'
import asyncio
import json
import time
import urllib.error
import urllib.request

import redis.asyncio
from aura_core.domains.governance.identity.models import Principal
from aura_core.platform.auth import RedisSessionBackend, SessionService, Settings


async def main() -> None:
    settings = Settings(role="api")
    redis_client = redis.asyncio.from_url(settings.valkey_url, decode_responses=True)
    sessions = SessionService(RedisSessionBackend(redis_client), settings)
    session_id, session = await sessions.create(
        Principal(settings.oidc_issuer, settings.owner_subject, "Disposable owner")
    )
    headers = {
        "Content-Type": "application/json",
        "Cookie": f"{settings.session_cookie.name}={session_id}",
        "Origin": settings.public_origin,
        "X-CSRF-Token": session.csrf_token,
        "Idempotency-Key": "aura0098-provider-outage-probe",
    }
    payload = json.dumps({
        "message": "AURA0098_PROVIDER_OUTAGE_PROBE",
        "modelId": "fixture-chat-2",
    }).encode()
    try:
        request = urllib.request.Request(
            "http://127.0.0.1:8000/api/v1/conversations",
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                status = response.status
                accepted = json.loads(response.read(65536))
        except urllib.error.HTTPError as error:
            status = error.code
            try:
                problem = json.loads(error.read(8192))
            except (TypeError, ValueError, json.JSONDecodeError):
                problem = None
            accepted = None
        if status in {422, 503}:
            if not isinstance(problem, dict):
                raise SystemExit("provider outage problem envelope was unavailable")
            code = problem.get("code")
            category = problem.get("category") or problem.get("errorClass")
            problem_type = problem.get("type")
            category_text = str(category).casefold() if isinstance(category, str) else ""
            type_text = str(problem_type).casefold() if isinstance(problem_type, str) else ""
            provider_code = code in {
                "DEPENDENCY_UNAVAILABLE",
                "PROVIDER_UNAVAILABLE",
                "PROVIDER_ERROR",
            }
            provider_category = (
                category_text in {"provider", "provider_unavailable", "dependency"}
                or "provider" in type_text
                or "dependency" in type_text
            )
            if not provider_code or not provider_category or problem.get("retryable") is not True:
                raise SystemExit("provider outage problem was not explicitly provider-retryable")
            print("bounded provider-unavailable admission")
            return
        if status != 202 or not isinstance(accepted, dict):
            raise SystemExit("provider outage did not produce a bounded public outcome")
        conversation = accepted.get("conversation")
        run = accepted.get("run")
        conversation_id = conversation.get("id") if isinstance(conversation, dict) else None
        run_id = run.get("id") if isinstance(run, dict) else None
        if not isinstance(conversation_id, str) or not isinstance(run_id, str):
            raise SystemExit("provider outage admission omitted bounded run identifiers")
        detail_request = urllib.request.Request(
            f"http://127.0.0.1:8000/api/v1/conversations/{conversation_id}",
            headers={"Cookie": f"{settings.session_cookie.name}={session_id}"},
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(detail_request, timeout=5) as response:
                    detail = json.loads(response.read(65536))
            except (OSError, urllib.error.HTTPError):
                time.sleep(1)
                continue
            runs = detail.get("recentRuns", []) if isinstance(detail, dict) else []
            current = detail.get("currentRun") if isinstance(detail, dict) else None
            candidates = [item for item in runs if isinstance(item, dict)]
            if isinstance(current, dict):
                candidates.append(current)
            target = next((item for item in candidates if item.get("id") == run_id), None)
            if not isinstance(target, dict):
                time.sleep(1)
                continue
            if target.get("status") in {"failed", "canceled", "cancelled", "interrupted"}:
                error = target.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                retryable = error.get("retryable") if isinstance(error, dict) else None
                if code not in {"PROVIDER_ERROR", "RUN_TIMEOUT"} or retryable is not True:
                    raise SystemExit("provider outage returned an unbounded terminal code")
                print("bounded provider-unavailable run outcome")
                return
            if target.get("status") == "completed":
                raise SystemExit("provider outage unexpectedly completed a Core run")
            time.sleep(1)
        raise SystemExit("provider outage run did not reach a bounded terminal outcome")
    finally:
        await sessions.revoke(session_id)
        await redis_client.aclose()


asyncio.run(main())
PY
  local status=$?
  [ "$status" -eq 0 ] || return "$status"
  printf 'AURA_E2E phase=%s core_provider=bounded-unavailable\n' "$phase"
}

run_dependency_outage_phase() {
  local phase=$1
  local service=$2
  local session_id=""
  local collector_before="" collector_degraded="" collector_recovered=""
  local core_export_before="" core_export_during="" core_export_recovered=""
  local scheduler_before="" scheduler_stopped="" scheduler_recovered=""
  if [ "$service" = observatory-session ]; then
    local api_id
    api_id=$("${compose[@]}" ps -q observatory-api 2>/dev/null | head -n 1 || true)
    session_id=$(create_observatory_owner_session "$api_id") || return $?
  fi
  if [ "$service" = observatory-collector ]; then
    collector_before=$(capture_observatory_ingest_signal) || return $?
    core_export_before=$(capture_core_telemetry_export_stats) || return $?
  fi
  if [ "$service" = observatory-evaluation-scheduler ]; then
    scheduler_before=$(capture_observatory_metric_total \
      "$phase-before" observatory_evaluation_scheduler_heartbeat_age_seconds) || {
      local capture_status=$?
      [ "$capture_status" -eq 1 ] && return 1
      return 2
    }
  fi
  printf 'AURA_E2E phase=%s service=%s action=stop\n' "$phase" "$service"
  assert_observatory_public_surfaces "$phase-before" || return $?
  bounded "$step_timeout" "${compose[@]}" stop "$service" || return $?
  run_core_normal_run_phase "$phase" || return $?
  assert_core_available || return $?
  case "$service" in
    observatory-collector)
      # The Collector is a Core-to-Observatory boundary.  Use only the public
      # persisted ingest signals and fail closed when they do not transition;
      # do not infer a Collector identity from an unrelated component.
      collector_degraded=$(capture_observatory_ingest_signal) || return $?
      assert_collector_ingest_transition "$phase-degraded" "$collector_before" "$collector_degraded" || return $?
      core_export_during=$(capture_core_telemetry_export_stats) || return $?
      assert_core_telemetry_export_transition "$phase-degraded" "$core_export_before" "$core_export_during" || return $?
      ;;
    observatory-postgres)
      assert_observatory_postgres_degraded "$phase-degraded" || return $?
      ;;
    observatory-clickhouse)
      assert_observatory_component_signal "$phase-degraded" degraded || return $?
      ;;
    observatory-session)
      assert_observatory_session_degraded "$phase-degraded" "$session_id" || return $?
      ;;
    observatory-evaluation-scheduler)
      scheduler_stopped=$(capture_observatory_metric_total \
        "$phase-degraded" observatory_evaluation_scheduler_heartbeat_age_seconds) || {
        local capture_status=$?
        [ "$capture_status" -eq 1 ] && return 1
        return 2
      }
      ;;
  esac
  printf 'AURA_E2E phase=%s service=%s core=usable\n' "$phase" "$service"
  bounded "$step_timeout" "${compose[@]}" start "$service" || return $?
  wait_running "$service" || return $?
  local container_id health
  container_id=$("${compose[@]}" ps -q "$service" 2>/dev/null | head -n 1 || true)
  health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || true)
  if [ "$health" != none ] && ! wait_healthy "$service" 2>/dev/null; then
    printf 'AURA_E2E phase=%s service=%s recovery=unhealthy\n' "$phase" "$service" >&2
    return 1
  fi
  assert_core_available || return $?
  assert_observatory_public_surfaces "$phase-recovered" || return $?
  if [ "$service" = observatory-session ]; then
    assert_observatory_session_recovered "$phase-recovered" "$session_id" || return $?
  elif [ "$service" = observatory-collector ]; then
    collector_recovered=$(capture_observatory_ingest_signal) || return $?
    assert_collector_ingest_recovery "$phase-recovered" "$collector_degraded" "$collector_recovered" || return $?
    core_export_recovered=$(capture_core_telemetry_export_stats) || return $?
    assert_core_telemetry_export_recovered "$phase-recovered" "$core_export_during" "$core_export_recovered" || return $?
  elif [ "$service" = aura-garage ] || [ "$service" = observatory-evaluation-worker ]; then
    printf 'AURA_E2E phase=%s service=%s public_dependency_signal=prerequisite\n' \
      "$phase-recovered" "$service"
    return 2
  elif [ "$service" = observatory-evaluation-scheduler ]; then
    scheduler_recovered=$(capture_observatory_metric_total \
      "$phase-recovered" observatory_evaluation_scheduler_heartbeat_age_seconds) || {
      local capture_status=$?
      [ "$capture_status" -eq 1 ] && return 1
      printf 'AURA_E2E phase=%s service=%s public_dependency_signal=prerequisite\n' \
        "$phase-recovered" "$service" >&2
      return 2
    }
    assert_scheduler_heartbeat_transition "$phase-recovered" \
      "$scheduler_before" "$scheduler_stopped" "$scheduler_recovered" || {
      local transition_status=$?
      [ "$transition_status" -eq 2 ] && return 2
      return "$transition_status"
    }
  elif [ "$service" = observatory-postgres ] || [ "$service" = observatory-clickhouse ]; then
    assert_observatory_component_signal "$phase-recovered" || return $?
  else
    assert_observatory_component_signal "$phase-recovered" || return $?
  fi
  printf 'AURA_E2E phase=%s service=%s recovery=passed\n' "$phase" "$service"
}

run_shared_nats_outage_phase() {
  local phase=$1
  local service=$2
  printf 'AURA_E2E phase=%s service=%s action=stop shared-core-dependency\n' "$phase" "$service"
  bounded "$step_timeout" "${compose[@]}" stop "$service" || return $?
  assert_core_nats_unavailable || return $?
  bounded "$step_timeout" "${compose[@]}" start "$service" || return $?
  wait_running "$service" || return $?
  bounded "$step_timeout" "${compose[@]}" up --detach --wait \
    aura-core-api aura-core-worker aura-core-scheduler || return $?
  wait_core_ready || return $?
  assert_core_available || return $?
  if ! assert_core_evaluation_runner_nats_recovery_metric; then
    printf '%s\n' 'AURA_E2E evaluation runner NATS recovery metric remains an explicit public-ingest prerequisite' >&2
    return 2
  fi
  printf 'AURA_E2E phase=%s service=%s recovery=passed\n' "$phase" "$service"
}

run_fixture_ollama_outage_phase() {
  local phase=$1
  local provider_compose=(docker compose -p aura-e2e-provider -f "$ROOT/deploy/compose/e2e/provider.yaml")
  printf 'AURA_E2E phase=%s service=aura-e2e-ollama action=stop\n' "$phase"
  bounded "$step_timeout" "${provider_compose[@]}" stop aura-e2e-ollama || return $?
  assert_core_available || return $?
  run_core_provider_unavailable_probe "$phase" || return $?
  printf 'AURA_E2E phase=%s service=aura-e2e-ollama core=usable\n' "$phase"
  bounded "$step_timeout" "${provider_compose[@]}" start aura-e2e-ollama || return $?
  local deadline=$((SECONDS + step_timeout))
  local container_id
  while (( SECONDS < deadline )); do
    container_id=$("${provider_compose[@]}" ps -q aura-e2e-ollama 2>/dev/null | head -n 1 || true)
    if [ -n "$container_id" ] && [ "$(docker inspect -f '{{.State.Health.Status}}' "$container_id" 2>/dev/null || true)" = healthy ]; then
      assert_core_available || return $?
      run_core_normal_run_phase "$phase-recovered" || return $?
      printf 'AURA_E2E phase=%s service=aura-e2e-ollama recovery=passed\n' "$phase"
      return 0
    fi
    sleep 1
  done
  printf 'AURA_E2E phase=%s service=aura-e2e-ollama recovery=timeout\n' "$phase" >&2
  return 1
}

run_fixture_judge_outage_phase() {
  local phase=$1
  local provider_compose=(docker compose -p aura-e2e-provider -f "$ROOT/deploy/compose/e2e/provider.yaml")
  printf 'AURA_E2E phase=%s service=aura-e2e-judge action=stop\n' "$phase"
  bounded "$step_timeout" "${provider_compose[@]}" stop aura-e2e-judge || return $?
  # Judge availability is independent of Core normal-run availability. Keep
  # the Core proof, but fail closed until the application exposes an existing
  # authorized local-judge work/admission port. A provider /healthz check is
  # intentionally not a substitute for adapter, artifact, and score coverage.
  run_core_normal_run_phase "$phase" || return $?
  assert_core_available || return $?
  printf '%s\n' \
    'AURA_E2E judge application exercise blocked: no existing public/application port admits an authorized artifact and local-judge work item' >&2
  printf '%s\n' \
    'AURA_E2E judge outage is not represented as provider-health success; repair the application boundary before enabling this phase' >&2
  bounded "$step_timeout" "${provider_compose[@]}" start aura-e2e-judge || return $?
  return 1
}

run_observatory_api_outage_phase() {
  local phase=observatory_api service=observatory-api
  printf 'AURA_E2E phase=%s service=%s action=stop\n' "$phase" "$service"
  bounded "$step_timeout" "${compose[@]}" stop "$service" || return $?
  run_core_normal_run_phase "$phase" || return $?
  assert_core_available || return $?
  assert_observatory_api_degraded "$phase-degraded" || return $?
  bounded "$step_timeout" "${compose[@]}" start "$service" || return $?
  wait_running "$service" || return $?
  wait_healthy "$service" || return $?
  assert_observatory_public_surfaces "$phase-recovered" || return $?
  assert_observatory_component_signal "$phase-recovered" || return $?
  printf 'AURA_E2E phase=%s service=%s recovery=passed\n' "$phase" "$service"
}

run_observatory_web_outage_phase() {
  local phase=observatory_web service=observatory-web
  printf 'AURA_E2E phase=%s service=%s action=stop\n' "$phase" "$service"
  bounded "$step_timeout" "${compose[@]}" stop "$service" || return $?
  run_core_normal_run_phase "$phase" || return $?
  assert_core_available || return $?
  assert_observatory_web_degraded "$phase-degraded" || return $?
  bounded "$step_timeout" "${compose[@]}" start "$service" || return $?
  wait_running "$service" || return $?
  wait_healthy "$service" || return $?
  assert_observatory_public_surfaces "$phase-recovered" || return $?
  bounded "$step_timeout" "${compose[@]}" run --rm --no-deps --no-TTY frontend-e2e \
    sh -c "exec pnpm --dir frontend exec playwright test \
      --config=apps/observatory-web-e2e/full-stack.config.ts \
      --grep 'authenticates owner, creates a real Core run, observes it through SSE, inspects measurements, and executes retention'" || return $?
  printf 'AURA_E2E phase=%s web_workflow=authenticated-observatory-recovered\n' "$phase"
  printf 'AURA_E2E phase=%s service=%s recovery=passed\n' "$phase" "$service"
}
