"""Static deployment topology checks that require no secrets or Docker daemon."""

from pathlib import Path
import unittest


ROOT = Path(__file__).parents[2]


def service_block(compose: str, name: str, next_name: str) -> str:
    return compose.split(f"  {name}:\n", 1)[1].split(f"  {next_name}:\n", 1)[0]


class ComposeTopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    def test_only_core_entrypoints_receive_provider_egress(self) -> None:
        api = service_block(self.compose, "aura-core-api", "aura-core-worker")
        worker = service_block(self.compose, "aura-core-worker", "aura-core-migrate")
        web = service_block(self.compose, "aura-web", "aura-core-api")
        migrate = service_block(self.compose, "aura-core-migrate", "aura-core-postgres")

        self.assertIn("AURA_OIDC_ISSUER:", api)
        self.assertIn("AURA_OLLAMA_URL:", api)
        self.assertIn("AURA_OLLAMA_URL:", worker)
        self.assertIn("aura-core-egress", api)
        self.assertIn("aura-core-egress", worker)
        self.assertIn('host.docker.internal:host-gateway', api)
        self.assertIn('host.docker.internal:host-gateway', worker)
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

    def test_browser_e2e_runner_uses_only_the_web_network(self) -> None:
        e2e = self.compose.split("  frontend-e2e:\n", 1)[1].split("\nsecrets:\n", 1)[0]

        self.assertIn("BASE_URL: http://aura-web:8080", e2e)
        self.assertIn("networks: [aura-frontend]", e2e)
        self.assertNotIn("aura-core-db", e2e)
        self.assertNotIn("aura-core-egress", e2e)

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

    def test_identity_override_connects_only_the_api_to_identity_control_network(self) -> None:
        identity = (ROOT / "deploy/compose/local-identity.yaml").read_text(encoding="utf-8")
        api = service_block(identity, "aura-core-api", "authentik-postgres")
        worker = service_block(self.compose, "aura-core-worker", "aura-core-migrate")

        self.assertIn("networks: [aura-identity]", api)
        self.assertIn("networks: [aura-identity, aura-identity-db, aura-identity-cache]", identity)
        self.assertNotIn("aura-identity", worker)

    def test_callback_query_values_are_excluded_from_proxy_and_api_access_logs(self) -> None:
        nginx = (ROOT / "deploy/docker/aura-web.nginx.conf").read_text(encoding="utf-8")
        api_dockerfile = (ROOT / "deploy/docker/aura-api.Dockerfile").read_text(
            encoding="utf-8"
        )
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
        migrate_script = (ROOT / "deploy/docker/aura-core-migrate.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("command: [/usr/local/bin/aura-core-migrate]", migrate)
        self.assertIn("healthcheck:\n      disable: true", migrate)
        self.assertIn("/app/.venv/bin/python -m aura_core.entrypoints.cli migrate", migrate_script)
        self.assertIn("/app/.venv/bin/python -m aura_core.entrypoints.cli seed", migrate_script)
        self.assertNotIn("uv run", migrate_script)

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
        self.assertGreaterEqual(runner.count("-p aura-e2e"), 6)
        self.assertIn("--profile checks build frontend-e2e", runner)

    def test_local_identity_uses_a_verified_digest_default_with_a_guarded_override(self) -> None:
        identity = (ROOT / "deploy/compose/local-identity.yaml").read_text(encoding="utf-8")
        environment = (ROOT / ".env.example").read_text(encoding="utf-8")
        digest = "sha256:76bf433fd434c067cb25dc3e197cee793998441cda912441ea275293e76cc32c"

        self.assertIn(digest, identity)
        self.assertIn(f"AUTHENTIK_IMAGE=ghcr.io/goauthentik/server@{digest}", environment)
        self.assertIn('case "$${AUTHENTIK_IMAGE}" in *@sha256:*)', identity)


if __name__ == "__main__":
    unittest.main()
