"""Static deployment topology checks that require no secrets or Docker daemon."""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

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
        self.assertIn("host.docker.internal:host-gateway", api)
        self.assertIn("host.docker.internal:host-gateway", worker)
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

    def test_browser_e2e_runner_uses_only_the_web_network(self) -> None:
        e2e = self.compose.split("  frontend-e2e:\n", 1)[1].split("\nsecrets:\n", 1)[0]

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

    def test_core_postgres_image_is_inherited_by_local_and_e2e_overlays(self) -> None:
        postgres = service_block(self.compose, "aura-core-postgres", "aura-nats")
        expected_image = "image: ${AURA_CORE_POSTGRES_IMAGE:-pgvector/pgvector:pg17}"

        self.assertIn(expected_image, postgres)
        for path in (
            ROOT / "deploy/compose/local-http.yaml",
            ROOT / "deploy/compose/e2e/external-aura.yaml",
            ROOT / "deploy/compose/e2e/local-aura.yaml",
        ):
            overlay = path.read_text(encoding="utf-8")
            self.assertNotIn("  aura-core-postgres:\n", overlay)
            self.assertNotIn("postgres:", overlay)

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
        self.assertNotIn("aura-identity-edge", owner_block)
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
        for variable in (
            "AURA_PUBLIC_ORIGIN",
            "AURA_OIDC_ISSUER",
            "AURA_OIDC_REDIRECT_URI",
            "AURA_OWNER_SUBJECT",
            "AUTHENTIK_HOST_BROWSER",
        ):
            self.assertIn(f"-u {variable}", script)
        self.assertIn(
            "Never rotate",
            (ROOT / "deploy/authentik/bootstrap-local-owner.py").read_text(encoding="utf-8"),
        )

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


if __name__ == "__main__":
    unittest.main()
