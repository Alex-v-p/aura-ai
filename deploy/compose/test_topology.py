"""Static deployment topology checks that require no secrets or Docker daemon."""

import ipaddress
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def service_block(compose: str, name: str, next_name: str) -> str:
    # Match service keys at the two-space indentation level.  A dependency
    # entry such as ``aura-web.depends_on.aura-core-api`` is more deeply
    # indented and must not be mistaken for the service declaration.
    start = f"\n  {name}:\n"
    end = f"\n  {next_name}:\n"
    return compose.split(start, 1)[1].split(end, 1)[0]


def compose_service_blocks(compose: str) -> dict[str, str]:
    """Extract two-space service blocks without requiring a Docker daemon."""

    section = compose.split("\nservices:\n", 1)[1]
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in section.splitlines():
        if line and not line.startswith(" "):
            break
        if len(line) - len(line.lstrip()) == 2 and line.strip().endswith(":"):
            current = line.strip()[:-1]
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return {name: "\n".join(lines) for name, lines in blocks.items()}


class ComposeTopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    def test_deterministic_ollama_fixture_models_have_provider_identity_metadata(self) -> None:
        fixture = (ROOT / "deploy/compose/e2e/fake_provider.py").read_text(encoding="utf-8")
        expected = {
            "fixture-chat": "5aab3664f58aecf3728756feafd39dfc723fdfd43dff233a622eea824574d18b",
            "fixture-chat-2": "2da0d79fc1a6e1cb9edef7bc5a3c41ee4e31df2eb8f53f35487639a0f9ed8951",
            "embed": "8a1eed22144c3f04485a69eea62bfd3fa1026de39dc252b77ba9b1f3f08b00ec",
            "qwen3-embedding:4b": (
                "c3e3b7d3a5a23e7f0c3d0d53d5f9d67f03d06f6f85ec5c9d03a8d2c7a8e5f1b4"
            ),
        }
        for name, digest in expected.items():
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertIn(f'"name": "{name}"', fixture)
            self.assertIn(f'"digest": "{digest}"', fixture)
            self.assertRegex(fixture, r'"modified_at": "2026-10-09T00:00:00Z"')

    def test_only_core_entrypoints_receive_provider_egress(self) -> None:
        api = service_block(self.compose, "aura-core-api", "aura-core-worker")
        worker = service_block(self.compose, "aura-core-worker", "aura-core-migrate")
        scheduler = service_block(self.compose, "aura-core-scheduler", "aura-core-migrate")
        web = service_block(self.compose, "aura-web", "aura-core-api")
        migrate = service_block(self.compose, "aura-core-migrate", "aura-core-postgres")

        self.assertIn("AURA_OIDC_ISSUER:", api)
        self.assertIn("AURA_OLLAMA_URL:", api)
        self.assertIn("AURA_OLLAMA_URL:", worker)
        self.assertIn("aura-core-egress", api)
        self.assertIn("aura-core-egress", worker)
        self.assertIn("host.docker.internal:host-gateway", api)
        self.assertIn("host.docker.internal:host-gateway", worker)
        self.assertIn("AURA_ROLE: scheduler", scheduler)
        self.assertIn("AURA_DATABASE_PASSWORD_FILE", scheduler)
        self.assertIn("networks: [aura-core-db, observatory-ingest]", scheduler)
        for forbidden in (
            "AURA_NATS",
            "AURA_VALKEY",
            "AURA_OLLAMA",
            "AURA_OIDC",
            "aura-core-bus",
            "aura-core-cache",
            "aura-core-egress",
            "host.docker.internal",
        ):
            self.assertNotIn(forbidden, scheduler)
        self.assertNotIn("aura-core-egress", web)
        self.assertNotIn("aura-core-egress", migrate)

    def test_core_entrypoints_share_the_validated_context_budget_default(self) -> None:
        api = service_block(self.compose, "aura-core-api", "aura-core-worker")
        worker = service_block(self.compose, "aura-core-worker", "aura-core-migrate")
        environment = (ROOT / ".env.example").read_text(encoding="utf-8")
        setting = "AURA_CONTEXT_TOKEN_BUDGET: ${AURA_CONTEXT_TOKEN_BUDGET:-8192}"

        self.assertIn(setting, api)
        self.assertIn(setting, worker)
        self.assertIn("AURA_CONTEXT_TOKEN_BUDGET=8192", environment)

    def test_core_entrypoints_share_independent_ollama_inventory_timeouts_and_cache(self) -> None:
        api = service_block(self.compose, "aura-core-api", "aura-core-worker")
        worker = service_block(self.compose, "aura-core-worker", "aura-core-migrate")
        environment = (ROOT / ".env.example").read_text(encoding="utf-8")
        settings = {
            "AURA_OLLAMA_INVENTORY_TIMEOUT_SECONDS": "5",
            "AURA_OLLAMA_VERIFICATION_TIMEOUT_SECONDS": "60",
            "AURA_OLLAMA_INVENTORY_CACHE_TTL_SECONDS": "15",
        }

        for name, default in settings.items():
            setting = f"{name}: ${{{name}:-{default}}}"
            self.assertIn(setting, api)
            self.assertIn(setting, worker)
            self.assertIn(f"{name}={default}", environment)

        self.assertIn(
            "AURA_OLLAMA_RUN_TIMEOUT_SECONDS: ${AURA_OLLAMA_RUN_TIMEOUT_SECONDS:-300}",
            worker,
        )
        self.assertIn(
            "AURA_OLLAMA_RUN_TIMEOUT_SECONDS: ${AURA_OLLAMA_RUN_TIMEOUT_SECONDS:-300}",
            api,
        )

    def test_core_entrypoints_share_bounded_telemetry_revision_defaults(self) -> None:
        api = service_block(self.compose, "aura-core-api", "aura-core-worker")
        worker = service_block(self.compose, "aura-core-worker", "aura-core-migrate")
        scheduler = service_block(self.compose, "aura-core-scheduler", "aura-core-migrate")
        environment = (ROOT / ".env.example").read_text(encoding="utf-8")
        settings = {
            "AURA_TELEMETRY_IMAGE_VERSION": "local",
            "AURA_TELEMETRY_CONFIGURATION_VERSION": "local",
        }

        for name, default in settings.items():
            setting = f"{name}: ${{{name}:-{default}}}"
            self.assertIn(setting, api)
            self.assertIn(setting, worker)
            self.assertIn(setting, scheduler)
            self.assertIn(f"{name}={default}", environment)

    def test_telemetry_manifests_declare_v2_and_dual_version_rollout(self) -> None:
        import yaml

        exporter = yaml.safe_load(
            (ROOT / "services/aura-core/resources/component-manifests/telemetry-export.yaml")
            .read_text(encoding="utf-8")
        )
        ingestion = yaml.safe_load(
            (
                ROOT
                / "services/aura-observatory/resources/component-manifests/telemetry-ingestion.yaml"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(exporter["output"]["schema"], "metadata-observation-v2")
        self.assertEqual(exporter["output"]["legacy_schemas"], ["metadata-observation-v1"])
        self.assertEqual(
            exporter["output"]["transport_notes"],
            {
                "otlp": "metadata-observation-v2",
                "structured_container_log": "metadata-observation-v1",
            },
        )
        self.assertIn("metadata_v2_allowlist", exporter["evaluators"])
        self.assertIn("correlation_marker_privacy", exporter["evaluators"])
        self.assertEqual(ingestion["input"]["schema"], "metadata-observation-v1-or-v2")
        self.assertNotIn("accepted_schemas", ingestion["input"])
        self.assertIn("metadata_v2_allowlist", ingestion["evaluators"])
        self.assertIn("correlation_marker_privacy", ingestion["evaluators"])

    def test_core_entrypoints_share_dimension_safe_document_embedding_defaults(self) -> None:
        api = service_block(self.compose, "aura-core-api", "aura-core-worker")
        worker = service_block(self.compose, "aura-core-worker", "aura-core-migrate")
        environment = (ROOT / ".env.example").read_text(encoding="utf-8")
        for service in (api, worker):
            self.assertIn(
                "AURA_DOCUMENT_EMBEDDING_MODEL: "
                "${AURA_DOCUMENT_EMBEDDING_MODEL:-qwen3-embedding:4b}",
                service,
            )
            self.assertIn(
                "AURA_DOCUMENT_EMBEDDING_DIMENSION: ${AURA_DOCUMENT_EMBEDDING_DIMENSION:-768}",
                service,
            )
        self.assertIn("AURA_DOCUMENT_EMBEDDING_MODEL=qwen3-embedding:4b", environment)
        self.assertIn("AURA_DOCUMENT_EMBEDDING_DIMENSION=768", environment)

    def test_browser_e2e_runner_uses_only_the_web_network(self) -> None:
        e2e = compose_service_blocks(self.compose)["frontend-e2e"]

        self.assertIn("BASE_URL: http://aura-web:8080", e2e)
        self.assertIn("networks: [aura-frontend]", e2e)
        self.assertNotIn("aura-edge", e2e)
        self.assertNotIn("aura-core-db", e2e)
        self.assertNotIn("aura-core-egress", e2e)

    def test_only_web_uses_the_host_facing_edge_network(self) -> None:
        web = service_block(self.compose, "aura-web", "aura-core-api")
        for name, next_name in (
            ("aura-core-api", "aura-core-worker"),
            ("aura-core-worker", "aura-core-migrate"),
            ("aura-core-migrate", "aura-core-postgres"),
            ("aura-core-postgres", "aura-nats"),
            ("aura-nats", "aura-valkey"),
            ("aura-valkey", "frontend-checks"),
        ):
            self.assertNotIn("aura-edge", service_block(self.compose, name, next_name))
        self.assertIn("networks: [aura-frontend, aura-edge]", web)
        self.assertIn("aura-edge: {}", self.compose)
        self.assertIn('"127.0.0.1:${AURA_WEB_PORT:-4200}:8080"', web)

    def test_browser_network_uses_an_explicit_operator_configurable_subnet(self) -> None:
        frontend = self.compose.split("  aura-frontend:\n", 1)[1].split(
            "  aura-core-db:\n", 1
        )[0]
        environment = (ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertIn("internal: true", frontend)
        self.assertIn(
            "subnet: ${AURA_FRONTEND_SUBNET:-10.254.241.0/24}",
            frontend,
        )
        self.assertIn("AURA_FRONTEND_SUBNET=10.254.241.0/24", environment)

    def test_local_http_authentik_has_the_observatory_identity_network_alias(self) -> None:
        local_http = (ROOT / "deploy/compose/local-http.yaml").read_text(encoding="utf-8")
        authentik = local_http.split("  authentik-server:\n", 1)[1].split(
            "  authentik-owner-bootstrap:\n", 1
        )[0]

        self.assertIn(
            "observatory-identity-egress:\n        aliases: [authentik.localhost]",
            authentik,
        )

    def test_observatory_web_is_a_loopback_only_production_shell(self) -> None:
        web = self.compose.split("  observatory-web:\n", 1)[1].split(
            "\nsecrets:\n", 1
        )[0]
        dockerfile = (ROOT / "deploy/docker/observatory-web.Dockerfile").read_text(
            encoding="utf-8"
        )
        nginx = (ROOT / "deploy/docker/observatory-web.nginx.conf").read_text(
            encoding="utf-8"
        )

        self.assertIn("profiles: [observatory]", web)
        self.assertIn("dockerfile: deploy/docker/observatory-web.Dockerfile", web)
        self.assertIn('"127.0.0.1:${AURA_OBSERVATORY_PORT:-4300}:8080"', web)
        self.assertIn("networks: [observatory-edge, observatory-api]", web)
        self.assertIn("observatory-api: {condition: service_healthy}", web)
        self.assertIn("read_only: true", web)
        self.assertIn("no-new-privileges:true", web)
        self.assertIn("cap_drop:", web)
        self.assertNotIn("secrets:", web)
        self.assertIn("nx build observatory-web --configuration=production", dockerfile)
        self.assertIn(
            "dist/apps/observatory-web/browser/", dockerfile
        )
        self.assertIn("listen 8080;", nginx)
        self.assertIn("proxy_pass http://observatory-api:8000;", nginx)
        self.assertIn("proxy_set_header Host $http_host;", nginx)
        self.assertIn("proxy_set_header X-Forwarded-Host $http_host;", nginx)
        self.assertNotIn("proxy_set_header X-Forwarded-Host $host;", nginx)
        self.assertIn("proxy_buffering off;", nginx)
        self.assertIn('add_header Cache-Control "no-store" always;', nginx)
        self.assertIn("try_files $uri $uri/ /index.html;", nginx)
        for temp_path in (
            "client_body_temp_path /tmp/client_temp;",
            "proxy_temp_path /tmp/proxy_temp;",
            "fastcgi_temp_path /tmp/fastcgi_temp;",
            "uwsgi_temp_path /tmp/uwsgi_temp;",
            "scgi_temp_path /tmp/scgi_temp;",
        ):
            self.assertIn(temp_path, nginx)
        self.assertNotIn("nginx:1.29-alpine", web)

    def test_observatory_collector_uses_file_backed_native_bearer_auth(self) -> None:
        collector = service_block(self.compose, "observatory-collector", "observatory-web")
        config = (ROOT / "deploy/compose/observatory-collector.yaml").read_text(
            encoding="utf-8"
        )

        self.assertIn("- --config=/etc/otelcol-contrib/config.yaml", collector)
        self.assertIn("- --set=service.telemetry.logs.level=warn", collector)
        self.assertIn("- --set=service.telemetry.logs.disable_caller=true", collector)
        self.assertIn("- --set=service.telemetry.logs.disable_stacktrace=true", collector)
        self.assertNotIn("entrypoint:", collector)
        self.assertIn("secrets: [observatory-ingest-credential]", collector)
        self.assertNotIn("OBSERVATORY_INGEST_CREDENTIAL", collector)
        self.assertIn("bearertokenauth:", config)
        self.assertIn("filename: /run/secrets/observatory-ingest-credential", config)
        self.assertIn("authenticator: bearertokenauth", config)
        self.assertIn("extensions: [bearertokenauth]", config)
        self.assertIn("level: normal", config)
        self.assertNotIn("level: detailed", config)
        self.assertNotIn("server_address", config)
        self.assertNotIn("server_port", config)
        self.assertNotIn("target_info{", config)
        self.assertNotIn("service_instance_id", config)
        self.assertIn("resource:", config)
        self.assertIn("service.instance.id: null", config)
        self.assertIn("port: 8888", config)
        self.assertIn("output_paths: [/dev/null]", config)
        self.assertIn("error_output_paths: [/dev/null]", config)
        self.assertIn('expose: ["4317", "4318", "8888"]', collector)
        self.assertNotIn("Authorization: Bearer", config)
        self.assertNotIn("${OBSERVATORY_INGEST_CREDENTIAL}", config)

    def test_observatory_web_build_context_excludes_secrets_and_unneeded_inputs(self) -> None:
        dockerfile = (ROOT / "deploy/docker/observatory-web.Dockerfile").read_text(
            encoding="utf-8"
        )
        dockerignore = (
            ROOT / "deploy/docker/observatory-web.Dockerfile.dockerignore"
        ).read_text(encoding="utf-8")

        for exclusion in (
            "**/.git",
            "**/.env*",
            "**/.env",
            "**/.env.*",
            "**/.secrets*",
            "**/.secrets",
            "**/.secrets/**",
            "**/.local-state",
            "**/.local-state/**",
            "**/.state",
            "**/.state/**",
            "**/state",
            "**/state/**",
            "**/coverage",
            "**/playwright-report",
            "**/test-results",
        ):
            self.assertIn(exclusion, dockerignore)

        for top_level in (
            ".codex",
            ".agents",
            "docs",
            "evaluations",
            "infra",
            "services",
            "contracts",
            "packages",
            "satellites",
            "work-items",
            "compose.yaml",
            "deploy/*",
        ):
            self.assertIn(top_level, dockerignore)

        self.assertNotIn("\nfrontend\n", dockerignore)
        self.assertNotIn("\nfrontend/*\n", dockerignore)
        self.assertIn("!deploy/docker/observatory-web.Dockerfile", dockerignore)
        self.assertIn("!deploy/docker/observatory-web.nginx.conf", dockerignore)
        self.assertIn("COPY frontend/ ./frontend/", dockerfile)
        self.assertIn(
            "COPY deploy/docker/observatory-web.nginx.conf /etc/nginx/nginx.conf",
            dockerfile,
        )

    def test_root_dockerignore_protects_every_runner_build_context(self) -> None:
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for exclusion in (
            "**/.git",
            "**/.env*",
            "**/.secrets",
            "**/.secrets/**",
            "**/.aura-e2e.*",
            "**/.local-state/**",
            "**/.state/**",
            "**/state/**",
            "**/coverage",
            "**/playwright-report",
            "**/test-results",
        ):
            self.assertIn(exclusion, dockerignore)

        for dockerfile in (
            "aura-api.Dockerfile",
            "aura-worker.Dockerfile",
            "aura-scheduler.Dockerfile",
            "aura-web.Dockerfile",
            "garage-bootstrap.Dockerfile",
            "aura-observatory.Dockerfile",
            "observatory-web.Dockerfile",
        ):
            self.assertIn(f"dockerfile: deploy/docker/{dockerfile}", self.compose)

        aura_web_ignore = (
            ROOT / "deploy/docker/aura-web.Dockerfile.dockerignore"
        ).read_text(encoding="utf-8")
        for exclusion in ("**/.env*", "**/.secrets/**", "**/.aura-e2e.*", "**/state/**"):
            self.assertIn(exclusion, aura_web_ignore)
        self.assertNotIn("!frontend", dockerignore)
        self.assertNotIn("services\n", dockerignore)

    def test_observatory_edge_isolated_to_observatory_web(self) -> None:
        blocks = compose_service_blocks(self.compose)
        edge_members = sorted(
            name for name, body in blocks.items() if "observatory-edge" in body
        )
        self.assertEqual(["observatory-web"], edge_members)
        self.assertIn("  observatory-edge: {}", self.compose)
        self.assertIn("  observatory-api:\n    internal: true", self.compose)
        self.assertIn("  observatory-db:\n    internal: true", self.compose)
        self.assertIn("  observatory-session:\n    internal: true", self.compose)
        self.assertIn("  observatory-ingest:\n    internal: true", self.compose)

    def test_observatory_metrics_use_postgres_provider_without_state_mounts(self) -> None:
        api = service_block(self.compose, "observatory-api", "observatory-worker")
        worker = service_block(self.compose, "observatory-worker", "observatory-migrate")
        settings = (ROOT / "services/aura-observatory/src/aura_observatory/settings.py").read_text(
            encoding="utf-8"
        )
        api_app = (
            ROOT / "services/aura-observatory/src/aura_observatory/entrypoints/api/app.py"
        ).read_text(encoding="utf-8")
        for service in (api, worker):
            self.assertNotIn("AURA_OBSERVATORY_METRICS_FILE", service)
            self.assertNotIn("/state/aura-observatory/metrics", service)
            self.assertNotIn("user: \"10002:10002\"", service)
            self.assertNotIn("volumes:", service)
        self.assertIn("default=None", settings)
        self.assertIn("persisted_metric_series", api_app)
        self.assertIn("metric_series", api_app)

    def test_observatory_full_stack_runner_supplies_owner_login_and_outage_phase(self) -> None:
        runner = (ROOT / "deploy/compose/run-observatory-full-stack-e2e.sh").read_text(
            encoding="utf-8"
        )
        for setting in (
            "AURA_OBSERVATORY_BASE_URL: http://observatory.localhost:4300",
            "AURA_OBSERVATORY_FULL_STACK_BASE_URL: http://observatory.localhost:4300",
            "AURA_OBSERVATORY_CORE_BASE_URL: http://aura-web:8080",
            "AURA_OBSERVATORY_IDENTITY_MODE: external-oidc",
            "AURA_OBSERVATORY_ISSUER: http://aura-e2e-oidc:8081",
            "AURA_OBSERVATORY_IDENTITY_URL: http://aura-e2e-oidc:8081",
            "AURA_OBSERVATORY_OWNER_USERNAME: owner",
            "AURA_OBSERVATORY_OWNER_PASSWORD: fixture-password",
            "AURA_E2E_IDENTITY_MODE: external-oidc",
            "AURA_E2E_ISSUER: http://aura-e2e-oidc:8081",
            "AURA_E2E_IDENTITY_URL: http://aura-e2e-oidc:8081",
            "AURA_E2E_OWNER_USERNAME: owner",
            "AURA_E2E_OWNER_PASSWORD: fixture-password",
        ):
            self.assertIn(setting, runner)
        self.assertIn(
            "--grep 'keeps Core usable after the platform stops Observatory services'",
            runner,
        )
        self.assertIn(
            "--grep 'preserves the owner session across a disposable Observatory restart'",
            runner,
        )
        self.assertIn(
            "--grep 'rejects the old session after reset and permits owner reauthentication'",
            runner,
        )
        self.assertIn("-e AURA_OBSERVATORY_SERVICES_STOPPED=1", runner)
        self.assertIn("-e AURA_OBSERVATORY_LIFECYCLE_PHASE=preserved", runner)
        self.assertIn("-e AURA_OBSERVATORY_LIFECYCLE_PHASE=reset", runner)
        self.assertIn("AURA_OBSERVATORY_LOAD_STORAGE_STATE=1", runner)
        self.assertIn("AURA_E2E_OBSERVATORY_BROWSER_STATE_FILE", runner)
        self.assertIn("test -s \"$browser_state\"", runner)
        self.assertIn('docker volume rm "${project}_observatory_session_data"', runner)
        self.assertNotIn("--save 60 1 --appendonly yes --appendfsync everysec", runner)
        self.assertIn(
            "down --remove-orphans \\\n  observatory-web observatory-api observatory-worker "
            "observatory-collector observatory-session",
            runner,
        )
        self.assertIn("health/live", runner)
        self.assertIn("health/ready", runner)

    def test_observatory_full_stack_runner_allocates_and_cleans_disposable_networks(self) -> None:
        runner = (ROOT / "deploy/compose/run-observatory-full-stack-e2e.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'fixture_root=$(mktemp -d "$ROOT/.aura-e2e.XXXXXX")',
            runner,
        )
        self.assertNotIn("${TMPDIR:-/tmp}/aura-observatory-e2e", runner)
        self.assertIn("assert_fixture_ignored", runner)
        self.assertIn("grep -Fqx '.aura-e2e.*/' \"$ROOT/.gitignore\"", runner)
        self.assertIn(
            "grep -Fqx '**/.aura-e2e.*/**' \"$ROOT/.dockerignore\"", runner
        )
        self.assertIn("umask 077", runner)
        self.assertIn("chmod 0600", runner)
        self.assertIn('env_file="$fixture_root/.env"', runner)
        self.assertNotIn('source "$ROOT/.env"', runner)
        self.assertNotIn('> "$ROOT/.env"', runner)
        self.assertIn('rm -rf "$fixture_root"', runner)
        self.assertIn('if [ "$status" -ne 0 ] && [ "$keep_on_failure" = "1" ]', runner)
        provider_start = runner.index("up --build --detach --wait")
        inventory = runner.index("mapfile -t network_id_array")
        self.assertLess(provider_start, inventory)
        self.assertIn("docker network inspect", runner)
        self.assertIn("mapfile -t network_id_array", runner)
        self.assertIn("10.254.192.0/18", runner)
        self.assertNotIn("172.30.0.0/16", runner)
        self.assertNotIn("172.31.0.0/16", runner)
        self.assertIn("no three free /28 subnets remain", runner)
        self.assertIn("AURA_OBJECT_STORAGE_SUBNET=$object_storage_subnet", runner)
        self.assertIn("AURA_DOCUMENT_PARSER_SUBNET=$document_parser_subnet", runner)
        self.assertIn(
            "print(selected[0], selected[1], selected[2])",
            runner,
        )
        self.assertIn('test -n "$observatory_object_storage_subnet"', runner)
        self.assertIn(
            "AURA_OBSERVATORY_OBJECT_STORAGE_SUBNET=$observatory_object_storage_subnet",
            runner,
        )
        self.assertIn(
            "--profile observatory --profile checks down --volumes --remove-orphans",
            runner,
        )
        self.assertIn("remove_disposable_networks", runner)
        self.assertIn(
            'label=com.docker.compose.project=$project',
            runner,
        )
        self.assertIn('"${project}_"*)', runner)
        self.assertIn('docker network rm "$network_id"', runner)

    def test_observatory_full_stack_override_uses_named_state_and_file_backed_secrets(self) -> None:
        runner = (ROOT / "deploy/compose/run-observatory-full-stack-e2e.sh").read_text(
            encoding="utf-8"
        )
        override = runner.split('cat > "$override" <<\'EOF\'\n', 1)[1].split(
            "\nEOF\n", 1
        )[0]

        for mount in (
            "core_postgres_data:/var/lib/postgresql/data",
            "core_nats_data:/data",
            "core_garage_data:/var/lib/garage",
            "core_garage_config:/run/garage-config",
            "observatory_postgres_data:/var/lib/postgresql/data",
            "observatory_clickhouse_data:/var/lib/clickhouse",
            "observatory_clickhouse_logs:/var/log/clickhouse-server",
            "observatory_session_data:/data",
        ):
            self.assertIn(mount, override)
        self.assertIn("aura-web:\n    ports: !override []", override)
        self.assertIn("observatory-web:\n    ports: !override []", override)
        for secret, environment in (
            (
                "aura-core-database-password",
                "AURA_E2E_CORE_DATABASE_SECRET_SOURCE_FILE",
            ),
            ("aura-oidc-client-secret", "AURA_E2E_OIDC_CLIENT_SECRET_SOURCE_FILE"),
            ("aura-nats-password", "AURA_E2E_NATS_PASSWORD_SECRET_SOURCE_FILE"),
            ("aura-valkey-password", "AURA_E2E_VALKEY_PASSWORD_SECRET_SOURCE_FILE"),
            (
                "aura-garage-rpc-secret",
                "AURA_E2E_GARAGE_RPC_SECRET_SOURCE_FILE",
            ),
            (
                "aura-garage-access-key",
                "AURA_E2E_GARAGE_ACCESS_KEY_SECRET_SOURCE_FILE",
            ),
            (
                "aura-garage-secret-key",
                "AURA_E2E_GARAGE_SECRET_KEY_SECRET_SOURCE_FILE",
            ),
            (
                "observatory-database-password",
                "AURA_E2E_OBSERVATORY_DATABASE_PASSWORD_SECRET_SOURCE_FILE",
            ),
            (
                "observatory-oidc-client-secret",
                "AURA_E2E_OBSERVATORY_OIDC_CLIENT_SECRET_SOURCE_FILE",
            ),
            (
                "observatory-session-password",
                "AURA_E2E_OBSERVATORY_SESSION_PASSWORD_SECRET_SOURCE_FILE",
            ),
            (
                "observatory-ingest-credential",
                "AURA_E2E_OBSERVATORY_INGEST_CREDENTIAL_SECRET_SOURCE_FILE",
            ),
            (
                "observatory-clickhouse-password",
                "AURA_E2E_OBSERVATORY_CLICKHOUSE_PASSWORD_SECRET_SOURCE_FILE",
            ),
        ):
            self.assertIn(f"{secret}: !override", override)
            self.assertIn(f"file: ${{{environment}}}", override)
            self.assertIn(f"{environment}=", runner)

        for secret_name, setting_name in (
            ("aura-core-database-password", "AURA_CORE_DATABASE_PASSWORD_FILE"),
            ("aura-oidc-client-secret", "AURA_OIDC_CLIENT_SECRET_FILE"),
            ("aura-nats-password", "AURA_NATS_PASSWORD_FILE"),
            ("aura-valkey-password", "AURA_VALKEY_PASSWORD_FILE"),
            ("aura-garage-rpc-secret", "AURA_GARAGE_RPC_SECRET_FILE"),
            ("aura-garage-access-key", "AURA_GARAGE_ACCESS_KEY_FILE"),
            ("aura-garage-secret-key", "AURA_GARAGE_SECRET_KEY_FILE"),
            (
                "observatory-database-password",
                "AURA_OBSERVATORY_DATABASE_PASSWORD_FILE",
            ),
            (
                "observatory-oidc-client-secret",
                "AURA_OBSERVATORY_OIDC_CLIENT_SECRET_FILE",
            ),
            (
                "observatory-session-password",
                "AURA_OBSERVATORY_SESSION_PASSWORD_FILE",
            ),
            (
                "observatory-ingest-credential",
                "AURA_OBSERVATORY_INGEST_CREDENTIAL_FILE",
            ),
            (
                "observatory-clickhouse-password",
                "AURA_OBSERVATORY_CLICKHOUSE_PASSWORD_FILE",
            ),
        ):
            self.assertIn(
                f"{setting_name}=/run/secrets/{secret_name}",
                runner,
            )
        self.assertNotIn("environment: AURA_E2E_", override)

        self.assertIn("volumes:\n  core_postgres_data:", override)
        self.assertIn("observatory_postgres_data:", override)
        self.assertIn("--volumes --remove-orphans", runner)
        self.assertIn("cleanup_started=0", runner)
        self.assertIn("trap - EXIT INT TERM", runner)
        self.assertIn('if [ -f "$env_file" ] && [ -f "$override" ]; then', runner)
        self.assertNotIn("chmod 0444", runner)
        self.assertNotIn("$fixture_root/state", runner)
        self.assertNotIn("$fixture_root/core-db", runner)

    def test_normal_composition_keeps_identity_outside_core_and_internal_paths(self) -> None:
        api = service_block(self.compose, "aura-core-api", "aura-core-worker")
        worker = service_block(self.compose, "aura-core-worker", "aura-core-migrate")

        self.assertNotIn("authentik", self.compose.casefold())
        self.assertNotIn("AUTHENTIK", self.compose)
        self.assertNotIn("authentik", api.casefold())
        self.assertNotIn("authentik", worker.casefold())
        for network in ("aura-frontend", "aura-core-db", "aura-core-bus", "aura-core-cache"):
            self.assertIn(f"  {network}:\n    internal: true", self.compose)
        self.assertIn("  aura-core-egress: {}", self.compose)

    def test_document_parser_uses_the_installed_sandbox_entrypoint(self) -> None:
        parser = service_block(self.compose, "aura-document-parser", "aura-garage")
        dockerfile = (ROOT / "deploy/docker/aura-worker.Dockerfile").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "entrypoint: [/app/.venv/bin/python, /usr/local/bin/document-parser-service.py]",
            parser,
        )
        self.assertIn(
            "COPY --chmod=0555 deploy/docker/document_parser_service.py "
            "/usr/local/bin/document-parser-service.py",
            dockerfile,
        )
        self.assertIn(
            'ENTRYPOINT ["/usr/local/bin/document-parser-service.py"]',
            dockerfile,
        )

    def test_identity_override_connects_only_the_api_to_identity_control_network(self) -> None:
        identity = (ROOT / "deploy/compose/local-identity.yaml").read_text(encoding="utf-8")
        api = service_block(identity, "aura-core-api", "authentik-postgres")
        worker = service_block(self.compose, "aura-core-worker", "aura-core-migrate")

        self.assertIn("aura-identity]", api)
        self.assertIn("networks: [aura-identity, aura-identity-db, aura-identity-cache]", identity)
        self.assertNotIn("aura-identity", worker)

    def test_callback_query_values_are_excluded_from_proxy_and_api_access_logs(self) -> None:
        nginx = (ROOT / "deploy/docker/aura-web.nginx.conf").read_text(encoding="utf-8")
        api_dockerfile = (ROOT / "deploy/docker/aura-api.Dockerfile").read_text(encoding="utf-8")
        access_format = nginx.split("log_format aura_access", 1)[1].split(
            "access_log /dev/stdout aura_access;", 1
        )[0]

        self.assertIn("log_format aura_access", nginx)
        self.assertIn('"$request_method $uri $server_protocol"', nginx)
        self.assertIn("access_log /dev/stdout aura_access;", nginx)
        self.assertNotIn("$request_uri", access_format)
        self.assertNotIn("$args", access_format)
        self.assertNotIn("$is_args", access_format)
        self.assertIn('"--no-access-log"', api_dockerfile)

    def test_migration_is_a_one_shot_core_cli_job(self) -> None:
        migrate = service_block(self.compose, "aura-core-migrate", "aura-core-postgres")
        migrate_script = (ROOT / "deploy/docker/aura-core-migrate.sh").read_text(encoding="utf-8")

        self.assertIn("command: [/usr/local/bin/aura-core-migrate]", migrate)
        self.assertIn("healthcheck:\n      disable: true", migrate)
        self.assertIn("/app/.venv/bin/python -m aura_core.entrypoints.cli migrate", migrate_script)
        self.assertIn("/app/.venv/bin/python -m aura_core.entrypoints.cli seed", migrate_script)
        self.assertNotIn("uv run", migrate_script)

    def test_observatory_migration_image_uses_the_single_command_cli(self) -> None:
        dockerfile = (ROOT / "deploy/docker/aura-observatory.Dockerfile").read_text(
            encoding="utf-8"
        )
        expected = (
            'CMD ["/app/.venv/bin/python", "-m", '
            '"aura_observatory.entrypoints.cli"]'
        )
        self.assertIn(expected, dockerfile)
        self.assertNotIn(
            'CMD ["/app/.venv/bin/python", "-m", '
            '"aura_observatory.entrypoints.cli", "migrate"]',
            dockerfile,
        )

        python = ROOT / ".venv/bin/python"
        if not python.is_file():
            self.skipTest("project virtualenv is unavailable for CLI help smoke test")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "services/aura-observatory/src")
        result = subprocess.run(
            [str(python), "-m", "aura_observatory.entrypoints.cli", "--help"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
        )
        self.assertIn("Usage:", result.stdout)

    def test_scheduler_starts_prebuilt_runtime_without_package_manager(self) -> None:
        scheduler_dockerfile = (ROOT / "deploy/docker/aura-scheduler.Dockerfile").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'CMD ["/app/.venv/bin/python", "-c", '
            '"from aura_core.entrypoints.scheduler.app import main; main()"]',
            scheduler_dockerfile,
        )
        self.assertNotIn("CMD [\"uv\"", scheduler_dockerfile)
        self.assertNotIn("uv run", scheduler_dockerfile)

    def test_core_postgres_uses_pgvector_postgresql_17_without_new_exposure(self) -> None:
        postgres = service_block(self.compose, "aura-core-postgres", "aura-nats")

        self.assertIn("image: ${AURA_CORE_POSTGRES_IMAGE:-pgvector/pgvector:pg17}", postgres)
        self.assertIn("aura-core-database-password", postgres)
        self.assertIn(
            "${AURA_STATE_ROOT:?encrypted state root is required}/"
            "aura-core-postgres:/var/lib/postgresql/data",
            postgres,
        )
        self.assertIn("networks: [aura-core-db]", postgres)
        self.assertIn("healthcheck:", postgres)
        self.assertNotIn("ports:", postgres)
        self.assertNotIn("expose:", postgres)

    def test_garage_is_private_digest_qualified_and_core_only(self) -> None:
        garage = service_block(self.compose, "aura-garage", "aura-garage-config")
        config = service_block(self.compose, "aura-garage-config", "aura-garage-bootstrap")
        bootstrap = service_block(
            self.compose, "aura-garage-bootstrap", "observatory-garage-bootstrap"
        )
        api = service_block(self.compose, "aura-core-api", "aura-core-worker")
        worker = service_block(self.compose, "aura-core-worker", "aura-core-migrate")
        environment = (ROOT / ".env.example").read_text(encoding="utf-8")
        runner = (ROOT / "deploy/compose/run-full-stack-e2e.sh").read_text(
            encoding="utf-8"
        )
        observatory_runner = (ROOT / "deploy/compose/run-observatory-full-stack-e2e.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "image: ${AURA_GARAGE_IMAGE:-dxflrs/garage:v2.1.0@sha256:"
            "4c9b34c113e61358466e83fd6e7d66e6d18657ede14b776eb78a93ee8da7cf6a}",
            garage,
        )
        self.assertIn("rpc_secret", config)
        self.assertIn("AURA_GARAGE_IMAGE must use a sha256 digest", config)
        self.assertIn(
            "AURA_GARAGE_IMAGE=dxflrs/garage:v2.1.0@sha256:4c9b34c113e61358466e83fd6e7d66e6d18657ede14b776eb78a93ee8da7cf6a",
            environment,
        )
        self.assertIn("printf 'GK000000000000000000000000\\n'", runner)
        self.assertIn(
            "write_fixture_secret observatory-garage-access-key GK111111111111111111111111",
            observatory_runner,
        )
        self.assertIn("entrypoint: [/garage]", garage)
        self.assertIn("command: [--config, /run/garage-config/garage.toml, server]", garage)
        self.assertIn(
            "test: [CMD, /garage, --config, /run/garage-config/garage.toml, status]",
            garage,
        )
        self.assertNotIn("/bin/sh", garage)
        self.assertIn(
            "image: ${AURA_GARAGE_CONFIG_IMAGE:-busybox:1.37.0@sha256:"
            "bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e}",
            config,
        )
        self.assertIn("network_mode: none", config)
        self.assertIn("/run/secrets/aura-garage-rpc-secret", config)
        self.assertIn("garage-bootstrap.Dockerfile", bootstrap)
        self.assertIn("GARAGE_IMAGE:", bootstrap)
        self.assertIn("node id 2>/dev/null | cut -d@ -f1", bootstrap)
        self.assertIn("/var/lib/garage:ro", bootstrap)
        self.assertNotIn("observatory-garage-access-key", bootstrap)
        self.assertNotIn("observatory-garage-secret-key", bootstrap)
        self.assertIn("aura-object-storage", garage)
        self.assertIn("aura-object-storage", bootstrap)
        observatory_bootstrap = service_block(
            self.compose,
            "observatory-garage-bootstrap",
            "aura-evaluation-runner-garage-bootstrap",
        )
        observatory_worker = service_block(self.compose, "observatory-worker", "observatory-migrate")
        self.assertIn("observatory-object-storage", garage)
        self.assertIn("observatory-garage-access-key", observatory_bootstrap)
        self.assertIn("observatory-garage-secret-key", observatory_bootstrap)
        self.assertIn("AURA_OBSERVATORY_GARAGE_BUCKET", observatory_bootstrap)
        self.assertIn("networks: [observatory-object-storage]", observatory_bootstrap)
        self.assertIn("bucket allow --write --key", observatory_bootstrap)
        self.assertNotIn("bucket allow --read", observatory_bootstrap)
        self.assertIn("bucket deny --read --key", observatory_bootstrap)
        self.assertIn("bucket deny --owner --key", observatory_bootstrap)
        self.assertIn("bucket info", observatory_bootstrap)
        self.assertIn("permission verification failed closed", observatory_bootstrap)
        self.assertIn("extract_permission_flags()", observatory_bootstrap)
        self.assertIn('permission_flags}" != "W"', observatory_bootstrap)
        self.assertNotIn("has_read_grant=", observatory_bootstrap)
        self.assertNotIn("has_owner_grant=", observatory_bootstrap)
        self.assertNotIn("has_write_grant=", observatory_bootstrap)
        self.assertIn("AURA_GARAGE_BUCKET:", observatory_bootstrap)
        self.assertIn('if [ -z "$${AURA_GARAGE_BUCKET}" ]', observatory_bootstrap)
        self.assertIn('[ -n "$${AURA_GARAGE_BUCKET}" ]', observatory_bootstrap)
        self.assertIn("extract_authorized_buckets()", observatory_bootstrap)
        self.assertIn("==== BUCKETS FOR THIS KEY ====", observatory_bootstrap)
        self.assertIn("Permissions", observatory_bootstrap)
        self.assertIn("Global aliases", observatory_bootstrap)
        self.assertIn("Local aliases", observatory_bootstrap)
        self.assertIn("length(fields[2]) != 16", observatory_bootstrap)
        self.assertIn("unexpected preexisting bucket grant", observatory_bootstrap)
        self.assertIn("grant verification failed closed", observatory_bootstrap)
        self.assertIn(
            "aura-garage-bootstrap: {condition: service_completed_successfully}",
            observatory_bootstrap,
        )
        self.assertIn("observatory-garage-access-key", observatory_worker)
        self.assertIn("observatory-garage-secret-key", observatory_worker)
        self.assertIn("observatory-object-storage", observatory_worker)
        self.assertNotIn("observatory-garage-access-key", api)
        self.assertNotIn("observatory-garage-secret-key", api)
        self.assertIn("aura-garage-bootstrap: {condition: service_completed_successfully}", api)
        self.assertIn("aura-garage-bootstrap: {condition: service_completed_successfully}", worker)
        self.assertIn("AURA_OBJECT_STORAGE_ENDPOINT: http://aura-garage:3900", api)
        self.assertIn("AURA_OBJECT_STORAGE_ENDPOINT: http://aura-garage:3900", worker)
        self.assertIn("aura-garage-access-key", api)
        self.assertIn("aura-garage-secret-key", api)
        self.assertNotIn("ports:", garage)
        self.assertNotIn("aura-frontend", garage)
        self.assertIn("  aura-object-storage:\n    internal: true", self.compose)

    def test_observatory_garage_bootstrap_rendered_command_is_shell_valid(self) -> None:
        document = yaml.safe_load(self.compose)
        command = document["services"]["observatory-garage-bootstrap"]["command"][0]
        # Compose converts $$ escapes to literal shell dollars before invoking
        # the service entrypoint. Validate the folded command after that
        # interpolation step so multiline YAML cannot hide shell syntax errors.
        rendered = command.replace("$$", "$")
        result = subprocess.run(
            ["/bin/sh", "-n"],
            input=rendered,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_observatory_garage_rendered_command_reaches_grant_and_verification(self) -> None:
        document = yaml.safe_load(self.compose)
        command = document["services"]["observatory-garage-bootstrap"]["command"][0]
        rendered = command.replace("$$", "$")
        self.assertNotIn("#", rendered)
        for statement in (
            "bucket deny --read --key",
            "bucket deny --owner --key",
            "bucket allow --write --key",
            "bucket info",
            "key info",
        ):
            self.assertIn(statement, rendered)
        self.assertLess(
            rendered.index("bucket allow --write --key"),
            rendered.index("bucket deny --read --key"),
        )
        self.assertNotIn(
            "bucket deny --read --key \"${access_key}\" \"${AURA_OBSERVATORY_GARAGE_BUCKET}\" >/dev/null 2>&1 || true",
            rendered,
        )
        self.assertNotIn(
            "bucket deny --owner --key \"${access_key}\" \"${AURA_OBSERVATORY_GARAGE_BUCKET}\" >/dev/null 2>&1 || true",
            rendered,
        )
        self.assertLess(
            rendered.index("bucket allow --write --key"),
            rendered.index("permission verification failed closed"),
        )

        with tempfile.TemporaryDirectory(prefix="aura-garage-bootstrap-") as directory:
            root = Path(directory)
            fake_garage = root / "garage"
            access_key = "GK111111111111111111111111"
            log = root / "garage.log"
            key_info_count = root / "key-info-count"
            bucket_info_count = root / "bucket-info-count"
            key_info_count.write_text("0", encoding="utf-8")
            bucket_info_count.write_text("0", encoding="utf-8")
            fake_garage.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$GARAGE_TEST_LOG\"\n"
                "case \"$*\" in\n"
                "  *' status') exit 0 ;;\n"
                "  *' node id') printf 'node@local\\n'; exit 0 ;;\n"
                "  *' layout show') printf 'node\\nCurrent cluster layout version: 1\\n'; exit 0 ;;\n"
                "  *' key info'*)\n"
                "    count=$(cat \"$GARAGE_TEST_KEY_INFO_COUNT\")\n"
                "    count=$((count + 1))\n"
                "    printf '%s' \"$count\" > \"$GARAGE_TEST_KEY_INFO_COUNT\"\n"
                "    if [ \"$count\" -eq 1 ]; then\n"
                "      printf 'Key name: fixture\\n==== BUCKETS FOR THIS KEY ====\\nPermissions  ID  Global aliases  Local aliases\\n'\n"
                "    else\n"
                "      printf 'Key name: fixture\\n==== BUCKETS FOR THIS KEY ====\\nPermissions  ID  Global aliases  Local aliases\\n W  7f4d8e7b11112222  aura-observatory-evaluation\\n'\n"
                "    fi\n"
                "    exit 0 ;;\n"
                "  *' bucket info'*)\n"
                "    count=$(cat \"$GARAGE_TEST_BUCKET_INFO_COUNT\")\n"
                "    count=$((count + 1))\n"
                "    printf '%s' \"$count\" > \"$GARAGE_TEST_BUCKET_INFO_COUNT\"\n"
                "    if [ \"$count\" -gt 1 ]; then printf 'W  7f4d8e7b11112222  %s\\n\\n' \"$GARAGE_TEST_ACCESS_KEY\"; fi\n"
                "    exit 0 ;;\n"
                "  *' bucket allow '*) exit 0 ;;\n"
                "  *' bucket deny '*)\n"
                "    if [ \"${GARAGE_TEST_FAIL_DENY:-0}\" -eq 1 ]; then exit 1; fi\n"
                "    exit 0 ;;\n"
                "  *' key import '*) exit 0 ;;\n"
                "esac\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_garage.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            access_file = root / "access-key"
            secret_file = root / "secret-key"
            config_file = root / "garage.toml"
            access_file.write_text(access_key + "\n", encoding="utf-8")
            secret_file.write_text("secret\n", encoding="utf-8")
            config_file.write_text("config\n", encoding="utf-8")
            rendered = rendered.replace(
                "/garage --config", f"{fake_garage} --config"
            )
            rendered = rendered.replace(
                "/run/secrets/observatory-garage-access-key", str(access_file)
            )
            rendered = rendered.replace(
                "/run/secrets/observatory-garage-secret-key", str(secret_file)
            )
            rendered = rendered.replace("/run/garage-config/garage.toml", str(config_file))
            result = subprocess.run(
                ["/bin/sh", "-ec", rendered],
                env={
                    **os.environ,
                    "AURA_GARAGE_IMAGE": "garage:test@sha256:" + "a" * 64,
                    "AURA_GARAGE_BUCKET": "aura-documents",
                    "AURA_OBSERVATORY_GARAGE_BUCKET": "aura-observatory-evaluation",
                    "GARAGE_TEST_LOG": str(log),
                    "GARAGE_TEST_KEY_INFO_COUNT": str(key_info_count),
                    "GARAGE_TEST_BUCKET_INFO_COUNT": str(bucket_info_count),
                    "GARAGE_TEST_ACCESS_KEY": access_key,
                },
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            operations = log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any("bucket deny --read --key" in line for line in operations))
            self.assertTrue(any("bucket deny --owner --key" in line for line in operations))
            self.assertTrue(any("bucket allow --write --key" in line for line in operations))
            self.assertGreaterEqual(sum("bucket info" in line for line in operations), 2)
            self.assertGreaterEqual(sum("key info" in line for line in operations), 2)
            allow_index = next(
                index for index, line in enumerate(operations) if "bucket allow --write --key" in line
            )
            read_deny_index = next(
                index for index, line in enumerate(operations) if "bucket deny --read --key" in line
            )
            owner_deny_index = next(
                index for index, line in enumerate(operations) if "bucket deny --owner --key" in line
            )
            self.assertLess(allow_index, read_deny_index)
            self.assertLess(read_deny_index, owner_deny_index)

            log.write_text("", encoding="utf-8")
            key_info_count.write_text("0", encoding="utf-8")
            bucket_info_count.write_text("0", encoding="utf-8")
            failed_deny = subprocess.run(
                ["/bin/sh", "-ec", rendered],
                env={
                    **os.environ,
                    "AURA_GARAGE_IMAGE": "garage:test@sha256:" + "a" * 64,
                    "AURA_GARAGE_BUCKET": "aura-documents",
                    "AURA_OBSERVATORY_GARAGE_BUCKET": "aura-observatory-evaluation",
                    "GARAGE_TEST_LOG": str(log),
                    "GARAGE_TEST_KEY_INFO_COUNT": str(key_info_count),
                    "GARAGE_TEST_BUCKET_INFO_COUNT": str(bucket_info_count),
                    "GARAGE_TEST_ACCESS_KEY": access_key,
                    "GARAGE_TEST_FAIL_DENY": "1",
                },
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(78, failed_deny.returncode)
            failed_operations = log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any("bucket allow --write --key" in line for line in failed_operations))
            self.assertTrue(any("bucket deny --read --key" in line for line in failed_operations))
            self.assertFalse(any("bucket deny --owner --key" in line for line in failed_operations))
            self.assertEqual(1, sum("bucket info" in line for line in failed_operations))
            self.assertEqual(1, sum("key info" in line for line in failed_operations))

    def test_observatory_garage_parser_matches_pinned_bucket_table(self) -> None:
        document = yaml.safe_load(self.compose)
        command = document["services"]["observatory-garage-bootstrap"]["command"][0]
        rendered = command.replace("$$", "$")
        match = re.search(
            r"extract_authorized_buckets\(\) \{.*?\| awk .*? '(?P<program>.*?)'; \};",
            rendered,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        assert match is not None
        expected = "aura-observatory-evaluation"
        fresh_fixture = (
            "Key name: fixture\n"
            "Key ID: <redacted>\n"
            "==== BUCKETS FOR THIS KEY ====\n"
            "Permissions  ID  Global aliases  Local aliases\n"
        )
        target_fixture = fresh_fixture + " W  7f4d8e7b11112222  aura-observatory-evaluation\n"
        result = subprocess.run(
            ["awk", "-v", f"expected={expected}", match.group("program")],
            input=fresh_fixture,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout.strip())

        target = subprocess.run(
            ["awk", "-v", f"expected={expected}", match.group("program")],
            input=target_fixture,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, target.returncode, target.stderr)
        self.assertEqual(expected, target.stdout.strip())

        combined = subprocess.run(
            ["awk", "-v", f"expected={expected}", match.group("program")],
            input=(
                target_fixture
                + " W  1234567890abcdef  unrelated-bucket\n"
            ),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, combined.returncode, combined.stderr)
        self.assertEqual([expected, "unrelated-bucket"], combined.stdout.splitlines())

        unexpected_fixture = fresh_fixture + " W  1234567890abcdef  unrelated-bucket\n"
        unexpected = subprocess.run(
            ["awk", "-v", f"expected={expected}", match.group("program")],
            input=unexpected_fixture,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, unexpected.returncode, unexpected.stderr)
        self.assertEqual("unrelated-bucket", unexpected.stdout.strip())

    def test_observatory_garage_bootstrap_rejects_missing_core_bucket(self) -> None:
        document = yaml.safe_load(self.compose)
        command = document["services"]["observatory-garage-bootstrap"]["command"][0]
        rendered = command.replace("$$", "$")
        guard_match = re.search(
            r'if \[ -z "\$\{AURA_GARAGE_BUCKET\}" \].*?fi;',
            rendered,
        )
        self.assertIsNotNone(guard_match)
        assert guard_match is not None
        self.assertIn('[ -n "${AURA_GARAGE_BUCKET}" ]', rendered)
        result = subprocess.run(
            ["/bin/sh", "-c", guard_match.group(0)],
            env={
                **os.environ,
                "AURA_GARAGE_BUCKET": "",
                "AURA_OBSERVATORY_GARAGE_BUCKET": "aura-observatory-evaluation",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(78, result.returncode)

    def test_observatory_garage_permission_flags_require_exact_write_row(self) -> None:
        document = yaml.safe_load(self.compose)
        command = document["services"]["observatory-garage-bootstrap"]["command"][0]
        rendered = command.replace("$$", "$")
        match = re.search(
            r"extract_permission_flags\(\) \{.*?\| awk .*? '(?P<program>.*?)'; \};",
            rendered,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        assert match is not None
        access_key = "GK111111111111111111111111"
        for flags in ("W", "R", "RW", "WO", "RWO"):
            result = subprocess.run(
                ["awk", "-v", f"key={access_key}", match.group("program")],
                input=f"{flags}  7f4d8e7b11112222  {access_key}\n",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(flags, result.stdout.strip())
            verification = subprocess.run(
                ["/bin/sh", "-c", 'if [ "$1" != "W" ]; then exit 78; fi', "sh", flags],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0 if flags == "W" else 78, verification.returncode)

        missing = subprocess.run(
            ["awk", "-v", f"key={access_key}", match.group("program")],
            input="Permissions  ID  Global aliases  Local aliases\n",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(0, missing.returncode)
        self.assertEqual("", missing.stdout.strip())

    def test_document_parser_isolated_from_credentials_and_general_egress(self) -> None:
        parser = service_block(self.compose, "aura-document-parser", "aura-garage")
        worker = service_block(self.compose, "aura-core-worker", "aura-core-migrate")
        for forbidden in (
            "AURA_DATABASE",
            "AURA_NATS",
            "AURA_VALKEY",
            "AURA_OLLAMA",
            "AURA_OIDC",
            "AURA_OBJECT_STORAGE",
            "aura-core-db",
            "aura-core-bus",
            "aura-core-cache",
            "aura-object-storage",
            "aura-core-egress",
            "host.docker.internal",
            "secrets:",
        ):
            self.assertNotIn(forbidden, parser)
        self.assertIn("networks: [aura-document-parser]", parser)
        self.assertIn("read_only: true", parser)
        self.assertIn("no-new-privileges:true", parser)
        self.assertIn("cap_drop:", parser)
        self.assertIn('cpus: "1.0"', parser)
        self.assertIn("mem_limit: 512m", parser)
        self.assertIn("pids_limit: 64", parser)
        self.assertIn("/tmp:rw,noexec,nosuid,nodev,size=64m", parser)
        self.assertIn("healthz", parser)
        self.assertIn("aura-document-parser", worker)
        self.assertIn("AURA_DOCUMENT_PARSER_URL: http://aura-document-parser:8080", worker)

    def test_private_parser_and_object_storage_networks_have_deterministic_ipam(self) -> None:
        environment = (ROOT / ".env.example").read_text(encoding="utf-8")
        network_section = self.compose.split("\nnetworks:\n", 1)[1]

        expected = {
            "aura-document-parser": (
                "AURA_DOCUMENT_PARSER_SUBNET",
                "10.254.240.16/28",
            ),
            "aura-object-storage": (
                "AURA_OBJECT_STORAGE_SUBNET",
                "10.254.240.0/28",
            ),
            "observatory-object-storage": (
                "AURA_OBSERVATORY_OBJECT_STORAGE_SUBNET",
                "10.254.242.0/28",
            ),
        }
        for network, (setting, subnet) in expected.items():
            lines = network_section.splitlines()
            start = lines.index(f"  {network}:") + 1
            end = start
            while end < len(lines) and not (
                lines[end].startswith("  ") and not lines[end].startswith("    ")
            ):
                end += 1
            network_block = "\n".join(lines[start:end])
            self.assertIn("internal: true", network_block)
            self.assertIn(
                f"- subnet: ${{{setting}:-{subnet}}}",
                network_block,
            )
            self.assertIn(f"{setting}={subnet}", environment)

        self.assertNotEqual(
            expected["aura-document-parser"][1],
            expected["aura-object-storage"][1],
        )
        lan = ipaddress.ip_network("192.168.0.0/24")
        parsed_subnets = {
            network: ipaddress.ip_network(subnet)
            for network, (_, subnet) in expected.items()
        }
        for subnet in parsed_subnets.values():
            self.assertFalse(subnet.overlaps(lan))
        self.assertFalse(
            parsed_subnets["aura-document-parser"].overlaps(
                parsed_subnets["aura-object-storage"]
            )
        )

        rendered = self._render_compose_config()
        rendered_networks = rendered["networks"]
        for network, subnet in parsed_subnets.items():
            self.assertEqual(
                rendered_networks[network]["ipam"]["config"][0]["subnet"],
                str(subnet),
            )

        expected_membership = {
            "aura-object-storage": {
                "aura-core-api",
                "aura-core-worker",
                "aura-garage",
                "aura-garage-bootstrap",
            },
            "observatory-object-storage": {
                "aura-garage",
            },
            "aura-document-parser": {
                "aura-core-worker",
                "aura-document-parser",
            },
        }
        actual_membership = {
            network: {
                service
                for service, definition in rendered["services"].items()
                if network in definition.get("networks", {})
            }
            for network in expected_membership
        }
        self.assertEqual(actual_membership, expected_membership)

    def _render_compose_config(self) -> dict[str, object]:
        """Render the base file without requiring a Docker daemon or secrets."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_root = root / "state"
            state_root.mkdir()
            env_lines = {
                "AURA_STATE_ROOT": str(state_root),
                "AURA_PUBLIC_ORIGIN": "https://aura.example.invalid",
                "AURA_OIDC_ISSUER": "https://authentik.example.invalid",
                "AURA_OIDC_REDIRECT_URI": "https://aura.example.invalid/api/v1/auth/callback",
                "AURA_OWNER_SUBJECT": "topology-test-owner",
            }
            for name in (
                "AURA_CORE_DATABASE_PASSWORD_FILE",
                "AURA_OIDC_CLIENT_SECRET_FILE",
                "AURA_NATS_PASSWORD_FILE",
                "AURA_VALKEY_PASSWORD_FILE",
                "AURA_GARAGE_RPC_SECRET_FILE",
                "AURA_GARAGE_ACCESS_KEY_FILE",
                "AURA_GARAGE_SECRET_KEY_FILE",
                "AURA_OBSERVATORY_GARAGE_ACCESS_KEY_FILE",
                "AURA_OBSERVATORY_GARAGE_SECRET_KEY_FILE",
            ):
                secret = root / name
                secret.write_text("topology-test-secret\n", encoding="utf-8")
                env_lines[name] = str(secret)
            env_file = root / ".env"
            env_file.write_text(
                "\n".join(f"{name}={value}" for name, value in env_lines.items()) + "\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "--project-directory",
                    str(ROOT),
                    "--env-file",
                    str(env_file),
                    "config",
                    "--format",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return json.loads(result.stdout)

    def _render_local_http_compose_config(self) -> dict[str, object]:
        """Render the disposable overlay with fixture-only secret files."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_root = root / "state"
            state_root.mkdir()
            secret_names = (
                "AURA_CORE_DATABASE_PASSWORD_FILE",
                "AURA_OIDC_CLIENT_SECRET_FILE",
                "AURA_NATS_PASSWORD_FILE",
                "AURA_VALKEY_PASSWORD_FILE",
                "AURA_GARAGE_RPC_SECRET_FILE",
                "AURA_GARAGE_ACCESS_KEY_FILE",
                "AURA_GARAGE_SECRET_KEY_FILE",
                "AURA_OBSERVATORY_DATABASE_PASSWORD_FILE",
                "AURA_OBSERVATORY_OIDC_CLIENT_SECRET_FILE",
                "AURA_OBSERVATORY_SESSION_PASSWORD_FILE",
                "AURA_OBSERVATORY_INGEST_CREDENTIAL_FILE",
                "AURA_OBSERVATORY_CLICKHOUSE_PASSWORD_FILE",
                "AURA_OBSERVATORY_GARAGE_ACCESS_KEY_FILE",
                "AURA_OBSERVATORY_GARAGE_SECRET_KEY_FILE",
                "AUTHENTIK_DATABASE_PASSWORD_FILE",
                "AUTHENTIK_SECRET_KEY_FILE",
                "AUTHENTIK_BOOTSTRAP_PASSWORD_FILE",
                "AUTHENTIK_VALKEY_PASSWORD_FILE",
                "AUTHENTIK_OWNER_PASSWORD_FILE",
            )
            env_lines = {
                "AURA_STATE_ROOT": str(state_root),
                "AURA_PUBLIC_ORIGIN": "https://aura.example.invalid",
                "AURA_OIDC_ISSUER": "https://authentik.example.invalid",
                "AURA_OIDC_AUDIENCE": "aura-web",
                "AURA_OIDC_CLIENT_ID": "aura-web",
                "AURA_OIDC_REDIRECT_URI": "https://aura.example.invalid/api/v1/auth/callback",
                "AURA_OWNER_SUBJECT": "topology-test-owner",
                "AURA_OBSERVATORY_OWNER_SUBJECT": "topology-test-owner",
                "AUTHENTIK_HOST_BROWSER": "https://authentik.example.invalid",
            }
            for name in secret_names:
                secret = root / name
                secret.write_text("topology-test-secret\n", encoding="utf-8")
                env_lines[name] = str(secret)
            env_file = root / ".env"
            env_file.write_text(
                "\n".join(f"{name}={value}" for name, value in env_lines.items()) + "\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "--project-directory",
                    str(ROOT),
                    "--env-file",
                    str(env_file),
                    "-f",
                    str(ROOT / "compose.yaml"),
                    "-f",
                    str(ROOT / "deploy/compose/local-identity.yaml"),
                    "-f",
                    str(ROOT / "deploy/compose/local-http.yaml"),
                    "--profile",
                    "local-identity",
                    "--profile",
                    "observatory",
                    "config",
                    "--format",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return json.loads(result.stdout)

    def test_local_http_observatory_postgres_healthcheck_has_no_nul(self) -> None:
        rendered = self._render_local_http_compose_config()
        test = rendered["services"]["observatory-postgres"]["healthcheck"]["test"]
        expected = (
            r'''test "$$(tr -d '\000' </proc/1/cmdline)" = postgres && '''
            r'''pg_isready -U "$${POSTGRES_USER}" -d "$${POSTGRES_DB}"'''
        )
        self.assertEqual(["CMD-SHELL", expected], test)
        self.assertFalse(any("\x00" in value for value in test if isinstance(value, str)))

    def test_full_stack_runner_uses_the_pinned_garage_default(self) -> None:
        runner = (ROOT / "deploy/compose/run-full-stack-e2e.sh").read_text(encoding="utf-8")
        digest = (
            "dxflrs/garage:v2.1.0@sha256:"
            "4c9b34c113e61358466e83fd6e7d66e6d18657ede14b776eb78a93ee8da7cf6a"
        )
        self.assertIn(f"default_garage_image='{digest}'", runner)
        self.assertIn(
            'export AURA_GARAGE_IMAGE="${AURA_GARAGE_IMAGE:-$default_garage_image}"',
            runner,
        )

    def test_core_postgres_image_is_inherited_by_local_and_e2e_overlays(self) -> None:
        postgres = service_block(self.compose, "aura-core-postgres", "aura-nats")
        expected_image = "image: ${AURA_CORE_POSTGRES_IMAGE:-pgvector/pgvector:pg17}"

        self.assertIn(expected_image, postgres)
        for path in (
            ROOT / "deploy/compose/e2e/external-aura.yaml",
            ROOT / "deploy/compose/e2e/local-aura.yaml",
        ):
            overlay = path.read_text(encoding="utf-8")
            self.assertNotIn("  aura-core-postgres:\n", overlay)
            self.assertNotIn("postgres:", overlay)
        local_http = (ROOT / "deploy/compose/local-http.yaml").read_text(encoding="utf-8")
        self.assertIn("  aura-core-postgres:\n", local_http)
        self.assertIn("volumes: !override", local_http)

    def test_full_stack_fixture_keeps_external_provider_outside_aura_project(self) -> None:
        external = (ROOT / "deploy/compose/e2e/external-aura.yaml").read_text(encoding="utf-8")
        provider = (ROOT / "deploy/compose/e2e/provider.yaml").read_text(encoding="utf-8")
        runner = (ROOT / "deploy/compose/run-full-stack-e2e.sh").read_text(encoding="utf-8")

        self.assertIn("external: true", external)
        self.assertIn("name: aura-e2e-provider", external)
        self.assertIn("AURA_OIDC_ISSUER: http://aura-e2e-oidc:8081", external)
        self.assertIn("AURA_OLLAMA_URL: http://aura-e2e-ollama:8081", external)
        self.assertIn("name: aura-e2e-provider", provider)
        self.assertIn("AURA_E2E_MODE", runner)
        self.assertIn("local-identity", runner)
        self.assertIn('e2e_project="aura-e2e-$fixture_suffix"', runner)
        self.assertIn('-p "$e2e_project"', runner)
        self.assertIn("-p aura-e2e-provider", runner)
        self.assertIn("--profile checks build frontend-e2e", runner)

    def test_local_identity_uses_a_verified_digest_default_with_a_guarded_override(self) -> None:
        identity = (ROOT / "deploy/compose/local-identity.yaml").read_text(encoding="utf-8")
        environment = (ROOT / ".env.example").read_text(encoding="utf-8")
        digest = "sha256:76bf433fd434c067cb25dc3e197cee793998441cda912441ea275293e76cc32c"

        self.assertIn(digest, identity)
        self.assertIn(f"AUTHENTIK_IMAGE=ghcr.io/goauthentik/server@{digest}", environment)
        self.assertIn('case "$${AUTHENTIK_IMAGE}" in *@sha256:*)', identity)

    def test_local_http_is_explicit_loopback_only_identity_mode(self) -> None:
        local_http = (ROOT / "deploy/compose/local-http.yaml").read_text(encoding="utf-8")
        script = (ROOT / "deploy/compose/local-http.sh").read_text(encoding="utf-8")

        self.assertIn("AURA_ENVIRONMENT: development", local_http)
        self.assertIn('AURA_SECURE_COOKIES: "false"', local_http)
        self.assertIn("AURA_PUBLIC_ORIGIN: http://aura.localhost:4200", local_http)
        self.assertIn(
            "AURA_OIDC_ISSUER: http://authentik.localhost:9000/application/o/aura-web/", local_http
        )
        self.assertIn("aliases: [authentik.localhost]", local_http)
        self.assertIn("127.0.0.1:${AUTHENTIK_PORT:-9000}:9000", local_http)
        self.assertIn("aura-identity-edge: {}", local_http)
        self.assertIn("authentik-owner-bootstrap:", local_http)
        self.assertIn("profiles: [local-http-bootstrap]", local_http)
        owner_start = local_http.index("  authentik-owner-bootstrap:\n")
        owner_block = local_http[owner_start:].split("\nsecrets:", 1)[0]
        self.assertIn(
            "AURA_OBSERVATORY_AUTHENTIK_CLIENT_ID: "
            "${AURA_OBSERVATORY_OIDC_CLIENT_ID:-aura-observatory}",
            owner_block,
        )
        self.assertIn(
            "AURA_OBSERVATORY_AUTHENTIK_REDIRECT_URI: http://observatory.localhost:4300/api/v1/auth/callback",
            owner_block,
        )
        self.assertNotIn("aura-identity-edge", owner_block)
        self.assertIn("blueprint_attempt=1", owner_block)
        self.assertIn("blueprint_max_attempts=30", owner_block)
        self.assertIn(
            'if [ "$${blueprint_attempt}" -ge "$${blueprint_max_attempts}" ]; then',
            owner_block,
        )
        self.assertIn("sleep 2", owner_block)
        self.assertIn(
            "Authentik OIDC dependencies did not become ready within 60 seconds",
            owner_block,
        )
        server_start = local_http.index("  authentik-server:\n")
        server_end = local_http.index("  authentik-owner-bootstrap:\n")
        self.assertIn("aura-identity-edge: {}", local_http[server_start:server_end])
        self.assertIn("ak apply_blueprint /blueprints/custom/aura-oidc.yaml", local_http)
        self.assertIn(
            "./deploy/authentik/bootstrap-local-owner.py:/bootstrap-local-owner.py:ro", local_http
        )
        self.assertIn(
            "./deploy/compose/local-http.sh up", (ROOT / "README.md").read_text(encoding="utf-8")
        )
        self.assertIn("verify-idempotency", script)
        self.assertIn("--profile local-http-bootstrap", script)
        self.assertIn("--profile observatory", script)
        self.assertIn("AURA_OBSERVATORY_OWNER_SUBJECT=$subject", script)
        self.assertIn("AURA_LOCAL_OLLAMA_RELAY_TARGET", script)
        self.assertIn("systemd-run --user", script)
        self.assertIn("socat", script)
        self.assertIn('"${relay_command[@]}"', script)
        self.assertIn("bind=127.0.0.1", script)
        self.assertIn('AURA_OLLAMA_URL=%s\\n', script)
        self.assertIn("OLLAMA_RELAY_URL=http://host.docker.internal:$relay_port", script)
        self.assertIn("AURA_LOCAL_OLLAMA_RELAY_PORT must be 1-5 decimal digits", script)
        self.assertIn("AURA_LOCAL_OLLAMA_RELAY_TARGET must be an IPv4 address and port", script)
        self.assertIn(
            "AURA_LOCAL_OLLAMA_RELAY_TARGET must not self-forward the loopback listener",
            script,
        )
        self.assertIn("stop_ollama_relay", script)
        self.assertIn("trap cleanup_failed_up ERR EXIT", script)
        self.assertIn("trap 'cleanup_interrupted_up 130' INT", script)
        self.assertIn("trap 'cleanup_interrupted_up 143' TERM", script)
        self.assertIn("trap - ERR INT TERM EXIT", script)
        self.assertIn("if ! systemctl --user is-active --quiet", script)
        down_start = script.index("\ndown() {")
        reset_start = script.index("\nreset() {")
        down_block = script[down_start:reset_start]
        reset_block = script[reset_start:]
        self.assertIn("stop_ollama_relay", down_block)
        self.assertIn("stop_ollama_relay", reset_block)
        self.assertLess(
            down_block.index("stop_ollama_relay"), down_block.index('[ -f "$ENV_FILE" ]')
        )
        self.assertLess(
            reset_block.index("stop_ollama_relay"), reset_block.index('[ -f "$ENV_FILE" ]')
        )
        self.assertNotIn("eval ", script)
        self.assertNotIn("bind=0.0.0.0", script)
        for variable in (
            "AURA_PUBLIC_ORIGIN",
            "AURA_OIDC_ISSUER",
            "AURA_OIDC_REDIRECT_URI",
            "AURA_OWNER_SUBJECT",
            "AURA_OBSERVATORY_OWNER_SUBJECT",
            "AUTHENTIK_HOST_BROWSER",
        ):
            self.assertIn(f"-u {variable}", script)
        environment = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("AURA_LOCAL_OLLAMA_RELAY_TARGET=", environment)
        self.assertIn("AURA_LOCAL_OLLAMA_RELAY_PORT=11435", environment)
        self.assertIn(
            "Never rotate",
            (ROOT / "deploy/authentik/bootstrap-local-owner.py").read_text(encoding="utf-8"),
        )

    def test_local_http_replaces_all_host_state_with_project_scoped_named_volumes(self) -> None:
        local_http = (ROOT / "deploy/compose/local-http.yaml").read_text(encoding="utf-8")

        expected_mounts = {
            "local_http_core_postgres_data:/var/lib/postgresql/data",
            "local_http_core_nats_data:/data",
            "local_http_core_garage_data:/var/lib/garage",
            "local_http_core_garage_config:/run/garage-config",
            "local_http_authentik_postgres_data:/var/lib/postgresql/data",
            "local_http_authentik_media:/media",
            "local_http_observatory_postgres_data:/var/lib/postgresql/data",
            "local_http_observatory_clickhouse_data:/var/lib/clickhouse",
            "local_http_observatory_clickhouse_logs:/var/log/clickhouse-server",
            "local_http_observatory_session_data:/data",
        }
        for mount in expected_mounts:
            self.assertIn(f"- {mount}", local_http)
        session_block = local_http.split("  observatory-session:\n", 1)[1].split(
            "  aura-core-api:\n", 1
        )[0]
        self.assertNotIn("command:", session_block)
        self.assertNotIn("${AURA_STATE_ROOT}", local_http)
        self.assertNotIn("${AURA_STATE_ROOT:?", local_http)
        self.assertIn("volumes: !override", local_http)
        for volume in (
            "local_http_core_postgres_data:",
            "local_http_core_nats_data:",
            "local_http_core_garage_data:",
            "local_http_core_garage_config:",
            "local_http_authentik_postgres_data:",
            "local_http_authentik_media:",
            "local_http_observatory_postgres_data:",
            "local_http_observatory_clickhouse_data:",
            "local_http_observatory_clickhouse_logs:",
            "local_http_observatory_session_data:",
        ):
            self.assertIn(volume, local_http)
        self.assertIn(
            "./deploy/docker/garage.toml:/etc/garage/garage.toml.template:ro",
            local_http,
        )
        self.assertIn(
            "./deploy/authentik/blueprints:/blueprints/custom:ro",
            local_http,
        )

    def test_local_http_documents_urls_stop_and_explicit_disposable_reset(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        docs_readme = (ROOT / "docs/README.md").read_text(encoding="utf-8")
        for document in (readme, docs_readme):
            self.assertIn("./deploy/compose/local-http.sh up", document)
            self.assertIn("./deploy/compose/local-http.sh down", document)
            self.assertIn("./deploy/compose/local-http.sh reset", document)
            self.assertIn("http://aura.localhost:4200", document)
            self.assertIn("http://observatory.localhost:4300", document)
            self.assertIn("http://authentik.localhost:9000", document)
        self.assertIn("AURA_STATE_ROOT", docs_readme)
        self.assertIn("sole", docs_readme)
        self.assertIn("exception", docs_readme)

    def test_local_http_edge_networks_have_exact_merged_membership(self) -> None:
        documents = [
            (ROOT / "compose.yaml").read_text(encoding="utf-8"),
            (ROOT / "deploy/compose/local-identity.yaml").read_text(encoding="utf-8"),
            (ROOT / "deploy/compose/local-http.yaml").read_text(encoding="utf-8"),
        ]
        merged: dict[str, str] = {}
        for document in documents:
            merged.update(compose_service_blocks(document))

        edge_members = sorted(name for name, body in merged.items() if "aura-edge" in body)
        identity_edge_members = sorted(
            name for name, body in merged.items() if "aura-identity-edge" in body
        )
        self.assertEqual(["aura-web"], edge_members)
        self.assertEqual(["authentik-server"], identity_edge_members)

    def test_local_owner_bootstrap_does_not_emit_credentials(self) -> None:
        bootstrap = (ROOT / "deploy/authentik/bootstrap-local-owner.py").read_text(encoding="utf-8")

        self.assertIn("from authentik.common.oauth.constants import SubModes", bootstrap)
        self.assertIn("SubModes.HASHED_USER_ID", bootstrap)
        self.assertIn("user.uid", bootstrap)
        self.assertIn("user.check_password(password)", bootstrap)
        self.assertIn("user.is_staff or user.is_superuser", bootstrap)
        self.assertNotIn("user.is_staff =", bootstrap)
        self.assertNotIn("user.is_superuser =", bootstrap)
        self.assertIn("AURA_OWNER_SUBJECT=", bootstrap)
        self.assertNotIn("print(password", bootstrap)
        self.assertIn("Never rotate an existing", bootstrap)

    def test_local_owner_password_file_is_exclusive_no_follow_and_preserving(self) -> None:
        script = (ROOT / "deploy/compose/local-http.sh").read_text(encoding="utf-8")

        self.assertIn("os.lstat(path)", script)
        self.assertIn("stat.S_ISLNK(metadata.st_mode)", script)
        self.assertIn("stat.S_ISREG(metadata.st_mode)", script)
        self.assertIn("os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW", script)
        self.assertIn("os.open(path, flags, 0o600)", script)
        self.assertIn("metadata.st_mode & 0o077", script)
        self.assertNotIn('chmod 600 "$path"', script)

    def test_local_owner_password_file_preserves_existing_and_rejects_symlink(self) -> None:
        script = (ROOT / "deploy/compose/local-http.sh").read_text(encoding="utf-8")
        helper = script.split("die() {", 1)[1].split("\nwrite_local_environment()", 1)[0]
        shell = (
            "ROOT=" + str(ROOT) + "\n"
            "ENV_FILE=/dev/null\n"
            "LOCAL_ENV_FILE=/dev/null\n"
            "die() { printf '%s\\n' \"$1\" >&2; exit 1; }\n"
            "configured_value() { local key=$1 value; value=${!key-}; printf '%s' \"$value\"; }\n"
            "absolute_path() { case $1 in /*) printf '%s' \"$1\" ;; "
            '*) printf \'%s/%s\' "$ROOT" "$1" ;; esac; }\n'
            + "ensure_owner_password() {"
            + helper.split("ensure_owner_password() {", 1)[1].split("\n}\n", 1)[0]
            + "\n}\nensure_owner_password\n"
        )

        def run_helper(path: Path) -> subprocess.CompletedProcess[str]:
            environment = os.environ.copy()
            environment["AUTHENTIK_OWNER_PASSWORD_FILE"] = str(path)
            return subprocess.run(
                ["bash", "-c", shell],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            password = root / "owner-password"
            created = run_helper(password)
            self.assertEqual(0, created.returncode, created.stderr)
            original_contents = password.read_bytes()
            self.assertEqual(0o600, stat.S_IMODE(password.stat().st_mode))

            password.chmod(0o400)
            preserved = run_helper(password)
            self.assertEqual(0, preserved.returncode, preserved.stderr)
            self.assertEqual(original_contents, password.read_bytes())
            self.assertEqual(0o400, stat.S_IMODE(password.stat().st_mode))

            target = root / "target"
            target.write_text("unchanged\n", encoding="utf-8")
            link = root / "owner-link"
            link.symlink_to(target)
            rejected = run_helper(link)
            self.assertNotEqual(0, rejected.returncode)
            self.assertEqual("unchanged\n", target.read_text(encoding="utf-8"))

    def test_local_http_launcher_cleans_relay_on_failed_up_and_keeps_successful_relay(self) -> None:
        script = ROOT / "deploy/compose/local-http.sh"

        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            bin_dir = fixture / "bin"
            bin_dir.mkdir()
            events = fixture / "events.log"
            env_file = fixture / ".env"
            local_env_file = fixture / ".env.local-http"
            password = fixture / "owner-password"
            env_file.write_text(
                "\n".join(
                    (
                        "# launcher test environment",
                        f"AURA_NATS_BOOTSTRAP_PASSWORD_FILE={fixture / 'bootstrap-password'}",
                        f"AURA_NATS_DISPATCH_PASSWORD_FILE={fixture / 'dispatch-password'}",
                        f"AURA_NATS_RUNNER_PASSWORD_FILE={fixture / 'runner-password'}",
                        f"AURA_EVALUATION_RUNNER_DATABASE_PASSWORD_FILE={fixture / 'runner-database-password'}",
                        f"AURA_OBSERVATORY_GARAGE_ACCESS_KEY_FILE={fixture / 'observatory-access'}",
                        f"AURA_OBSERVATORY_GARAGE_SECRET_KEY_FILE={fixture / 'observatory-secret'}",
                        f"AURA_EVALUATION_RUNNER_GARAGE_ACCESS_KEY_FILE={fixture / 'runner-access'}",
                        f"AURA_EVALUATION_RUNNER_GARAGE_SECRET_KEY_FILE={fixture / 'runner-secret'}",
                        "",
                    )
                ),
                encoding="utf-8",
            )

            def install_stub(name: str, body: str) -> None:
                path = bin_dir / name
                path.write_text(
                    "#!/usr/bin/env bash\nset -Eeuo pipefail\n" + body,
                    encoding="utf-8",
                )
                path.chmod(0o755)

            install_stub(
                "systemctl",
                """
printf 'systemctl %s\\n' "$*" >>"$AURA_TEST_EVENTS"
case "${2:-}" in
  show|stop|reset-failed) exit 0 ;;
  is-active)
    [[ "${AURA_TEST_SYSTEMCTL_ACTIVE_FAILURE:-}" != 1 ]]
    ;;
  *) exit 0 ;;
esac
""",
            )
            install_stub(
                "systemd-run",
                """
printf 'systemd-run %s\\n' "$*" >>"$AURA_TEST_EVENTS"
exit 0
""",
            )
            install_stub("socat", "exit 0\n")
            install_stub(
                "docker",
                """
printf 'docker %s\\n' "$*" >>"$AURA_TEST_EVENTS"
if [[ "$*" == *" run --rm authentik-owner-bootstrap"* ]]; then
  if [[ "${AURA_TEST_COMPOSE_FAILURE:-}" == bootstrap ]]; then
    exit 1
  fi
  printf 'AURA_OWNER_SUBJECT=test-owner-subject\\n'
  exit 0
fi
if [[ "$*" == *" up --build --detach --wait"* ]]; then
  if [[ "${AURA_TEST_BLOCK_PHASE:-}" == auth ]] &&
     [[ "$*" == *"authentik-server authentik-worker"* ]]; then
    printf 'blocked-auth\\n' >>"$AURA_TEST_EVENTS"
    while :; do sleep 1; done
  fi
  if [[ "${AURA_TEST_COMPOSE_FAILURE:-}" == auth ]] &&
     [[ "$*" == *"authentik-server authentik-worker"* ]]; then
    exit 1
  fi
  if [[ "${AURA_TEST_COMPOSE_FAILURE:-}" == final ]] &&
     [[ "$*" != *"authentik-server authentik-worker"* ]]; then
    exit 1
  fi
fi
exit 0
""",
            )

            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{bin_dir}:{environment['PATH']}",
                    "AURA_ENV_FILE": str(env_file),
                    "AURA_LOCAL_ENV_FILE": str(local_env_file),
                    "AUTHENTIK_OWNER_PASSWORD_FILE": str(password),
                    "AURA_LOCAL_OLLAMA_RELAY_TARGET": "192.168.0.40:11434",
                    "AURA_LOCAL_OLLAMA_RELAY_PORT": "11435",
                    "AURA_TEST_EVENTS": str(events),
                }
            )

            def run_launcher(
                failure: str,
                *,
                active_failure: bool = False,
                target: str | None = None,
                relay_port: str | None = None,
            ) -> subprocess.CompletedProcess[str]:
                events.write_text("", encoding="utf-8")
                run_environment = environment | {
                    "AURA_TEST_COMPOSE_FAILURE": failure,
                    "AURA_TEST_SYSTEMCTL_ACTIVE_FAILURE": "1" if active_failure else "",
                }
                if target is not None:
                    run_environment["AURA_LOCAL_OLLAMA_RELAY_TARGET"] = target
                if relay_port is not None:
                    run_environment["AURA_LOCAL_OLLAMA_RELAY_PORT"] = relay_port
                return subprocess.run(
                    [str(script), "up"],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=run_environment,
                )

            for failure in ("bootstrap", "final"):
                result = run_launcher(failure)
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                stops = [
                    line
                    for line in events.read_text(encoding="utf-8").splitlines()
                    if line.startswith("systemctl --user stop ")
                ]
                self.assertGreaterEqual(len(stops), 2, failure)

            result = run_launcher("none", active_failure=True)
            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            stops = [
                line
                for line in events.read_text(encoding="utf-8").splitlines()
                if line.startswith("systemctl --user stop ")
            ]
            self.assertGreaterEqual(len(stops), 2, "is-active failure")

            for target, relay_port, label in (
                ("127.0.0.1:01135", "01135", "loopback self-target"),
                ("8.8.8.8:11434", "11435", "public relay target"),
                ("192.168.0.40:11434", "114350", "overlong relay port"),
            ):
                result = run_launcher("none", target=target, relay_port=relay_port)
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertNotIn(
                    "systemd-run ", events.read_text(encoding="utf-8"), label
                )

            result = run_launcher("none")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            events_text = events.read_text(encoding="utf-8")
            self.assertEqual(
                1,
                sum(line.startswith("systemctl --user stop ") for line in events_text.splitlines()),
            )
            self.assertIn(
                "AURA_OLLAMA_URL=http://host.docker.internal:11435\n",
                local_env_file.read_text(encoding="utf-8"),
            )

            for interrupt, expected_status in (
                (signal.SIGINT, 130),
                (signal.SIGTERM, 143),
            ):
                events.write_text("", encoding="utf-8")
                run_environment = environment | {
                    "AURA_TEST_COMPOSE_FAILURE": "none",
                    "AURA_TEST_BLOCK_PHASE": "auth",
                    "AURA_TEST_SYSTEMCTL_ACTIVE_FAILURE": "",
                }
                process = subprocess.Popen(
                    [str(script), "up"],
                    start_new_session=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=run_environment,
                )
                try:
                    for _ in range(200):
                        if "blocked-auth" in events.read_text(encoding="utf-8"):
                            break
                        time.sleep(0.01)
                    else:
                        self.fail("launcher did not reach the blocked startup phase")
                    os.killpg(process.pid, interrupt)
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    stdout, stderr = process.communicate(timeout=5)
                    self.fail(f"launcher did not terminate: {stdout}{stderr}")
                self.assertEqual(expected_status, process.returncode, stdout + stderr)
                event_lines = events.read_text(encoding="utf-8").splitlines()
                self.assertGreaterEqual(
                    sum(line.startswith("systemctl --user stop ") for line in event_lines),
                    2,
                    interrupt.name,
                )
                self.assertNotIn(
                    " run --rm authentik-owner-bootstrap",
                    events.read_text(encoding="utf-8"),
                )


if __name__ == "__main__":
    unittest.main()
