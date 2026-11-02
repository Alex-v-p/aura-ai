"""AURA-0100 local-http Compose merge and Observatory startup regressions."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).parents[2]
COMPOSE_FILES = (
    "compose.yaml",
    "deploy/compose/local-identity.yaml",
    "deploy/compose/local-http.yaml",
)
LOCAL_HTTP_SCRIPT = ROOT / "deploy/compose/local-http.sh"


class _ComposeLoader(yaml.SafeLoader):
    pass


def _compose_override(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    return loader.construct_sequence(node, deep=True)  # pyright: ignore[reportArgumentType]


_ComposeLoader.add_constructor("!override", _compose_override)


def _document(path: Path) -> dict[str, Any]:
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)
    assert isinstance(document, dict)
    return cast(dict[str, Any], document)


def _services(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    services = document.get("services")
    assert isinstance(services, dict)
    return {
        str(name): cast(dict[str, Any], definition)
        for name, definition in cast(dict[Any, Any], services).items()
    }


def _extracted_secret_helper() -> str:
    script = LOCAL_HTTP_SCRIPT.read_text(encoding="utf-8")
    marker = 'python3 - "$path" "$kind" "$managed_default" <<\'PY\'\n'
    _, remainder = script.split(marker, 1)
    helper, _ = remainder.split("\nPY\n", 1)
    return helper + "\n"


def _run_secret_helper(
    path: Path,
    kind: str,
    *,
    managed_default: bool,
    default_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if default_path is None:
        environment.pop("AURA_LOCAL_DEFAULT_SECRET_PATH", None)
    else:
        environment["AURA_LOCAL_DEFAULT_SECRET_PATH"] = str(default_path)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _extracted_secret_helper(),
            str(path),
            kind,
            "1" if managed_default else "0",
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
        env=environment,
    )


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


@pytest.mark.parametrize(
    ("failure_stage", "expected_diagnostic"),
    (
        ("import", "Core Garage key import failed closed\n"),
        ("grant", "Core Garage grant failed closed\n"),
    ),
)
def test_core_garage_bootstrap_suppresses_credential_command_output_and_fails_closed(
    failure_stage: str, expected_diagnostic: str
) -> None:
    document = _document(ROOT / "compose.yaml")
    services = _services(document)
    command = str(services["aura-garage-bootstrap"]["command"][0]).replace("$$", "$")

    with tempfile.TemporaryDirectory(prefix="aura0100-core-garage-output-") as directory:
        root = Path(directory)
        fake_garage = root / "garage"
        fake_garage.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *' status') exit 0 ;;\n"
            "  *' node id') printf 'node@fixture\\n'; exit 0 ;;\n"
            "  *' layout show') printf 'node@fixture\\n'; exit 0 ;;\n"
            "  *' key info'*)\n"
            "    if [ \"$AURA_TEST_FAILURE_STAGE\" = import ]; then exit 1; fi\n"
            "    exit 0 ;;\n"
            "  *' bucket info'*) exit 0 ;;\n"
            "  *' key import'*)\n"
            "    printf 'credential-bearing import stdout %s\\n' \"$*\"\n"
            "    printf 'credential-bearing import stderr %s\\n' \"$*\" >&2\n"
            "    exit 1 ;;\n"
            "  *' bucket allow '*)\n"
            "    printf 'credential-bearing grant stdout %s\\n' \"$*\"\n"
            "    printf 'credential-bearing grant stderr %s\\n' \"$*\" >&2\n"
            "    exit 1 ;;\n"
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_garage.chmod(0o700)
        access_file = root / "access-key"
        secret_file = root / "secret-key"
        access_value = "synthetic-access-fixture"
        secret_value = "synthetic-secret-fixture"
        access_file.write_text(access_value + "\n", encoding="utf-8")
        secret_file.write_text(secret_value + "\n", encoding="utf-8")
        rendered = command.replace("/garage --config", f"{fake_garage} --config")
        rendered = rendered.replace(
            "/run/secrets/aura-garage-access-key", str(access_file)
        )
        rendered = rendered.replace(
            "/run/secrets/aura-garage-secret-key", str(secret_file)
        )
        result = subprocess.run(
            ["/bin/sh", "-ec", rendered],
            cwd=ROOT,
            env={
                **os.environ,
                "AURA_GARAGE_IMAGE": "garage:test@sha256:" + "a" * 64,
                "AURA_GARAGE_BUCKET": "aura-documents",
                "AURA_TEST_FAILURE_STAGE": failure_stage,
            },
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

        assert result.returncode == 78
        assert result.stdout == ""
        assert result.stderr == expected_diagnostic
        assert access_value not in result.stdout + result.stderr
        assert secret_value not in result.stdout + result.stderr


def test_local_http_managed_access_id_generation_is_secure_silent_and_stable() -> None:
    with tempfile.TemporaryDirectory(prefix="aura0100-access-id-") as directory:
        path = Path(directory) / "observatory-garage-access-key"
        first = _run_secret_helper(
            path, "garage-access", managed_default=True, default_path=path
        )
        assert first.returncode == 0, first.stderr
        assert first.stdout == first.stderr == ""
        generated = path.read_bytes()
        assert re.fullmatch(rb"GK[0-9a-f]{24}\n", generated)
        assert _mode(path) == 0o600

        second = _run_secret_helper(
            path, "garage-access", managed_default=True, default_path=path
        )
        assert second.returncode == 0, second.stderr
        assert second.stdout == second.stderr == ""
        assert path.read_bytes() == generated
        assert _mode(path) == 0o600


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_local_http_valid_existing_access_id_is_preserved_byte_for_byte(mode: int) -> None:
    with tempfile.TemporaryDirectory(prefix="aura0100-access-id-") as directory:
        path = Path(directory) / "observatory-garage-access-key"
        original = b"GK0123456789abcdef01234567\n"
        path.write_bytes(original)
        path.chmod(mode)

        result = _run_secret_helper(
            path, "garage-access", managed_default=True, default_path=path
        )

        assert result.returncode == 0, result.stderr
        assert path.read_bytes() == original
        assert _mode(path) == mode


def test_local_http_invalid_managed_default_access_id_is_atomically_replaced() -> None:
    with tempfile.TemporaryDirectory(prefix="aura0100-access-id-") as directory:
        path = Path(directory) / "observatory-garage-access-key"
        legacy = b"legacy-url-safe_access-id~value\n"
        path.write_bytes(legacy)
        path.chmod(0o600)
        original_inode = path.stat().st_ino

        result = _run_secret_helper(
            path, "garage-access", managed_default=True, default_path=path
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == result.stderr == ""
        replacement = path.read_bytes()
        assert replacement != legacy
        assert re.fullmatch(rb"GK[0-9a-f]{24}\n", replacement)
        assert path.stat().st_ino != original_inode
        assert _mode(path) == 0o600
        assert list(path.parent.glob(f".{path.name}.tmp-*")) == []


def test_local_http_managed_garage_secret_generation_is_exact_secure_and_stable() -> None:
    with tempfile.TemporaryDirectory(prefix="aura0100-garage-secret-") as directory:
        path = Path(directory) / "observatory-garage-secret-key"
        first = _run_secret_helper(
            path, "garage-secret", managed_default=True, default_path=path
        )
        assert first.returncode == 0, first.stderr
        assert first.stdout == first.stderr == ""
        generated = path.read_bytes()
        assert re.fullmatch(rb"[0-9a-f]{64}\n", generated)
        assert _mode(path) == 0o600

        second = _run_secret_helper(
            path, "garage-secret", managed_default=True, default_path=path
        )
        assert second.returncode == 0, second.stderr
        assert second.stdout == second.stderr == ""
        assert path.read_bytes() == generated
        assert _mode(path) == 0o600


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_local_http_valid_existing_garage_secret_is_preserved_byte_for_byte(
    mode: int,
) -> None:
    with tempfile.TemporaryDirectory(prefix="aura0100-garage-secret-") as directory:
        path = Path(directory) / "observatory-garage-secret-key"
        original = b"0123456789abcdef" * 4 + b"\n"
        path.write_bytes(original)
        path.chmod(mode)

        result = _run_secret_helper(
            path, "garage-secret", managed_default=True, default_path=path
        )

        assert result.returncode == 0, result.stderr
        assert path.read_bytes() == original
        assert _mode(path) == mode


def test_local_http_invalid_managed_default_garage_secret_is_rotated_silently() -> None:
    with tempfile.TemporaryDirectory(prefix="aura0100-garage-secret-") as directory:
        path = Path(directory) / "observatory-garage-secret-key"
        legacy = b"legacy-url-safe_secret~value\n"
        path.write_bytes(legacy)
        path.chmod(0o600)
        original_inode = path.stat().st_ino

        result = _run_secret_helper(
            path, "garage-secret", managed_default=True, default_path=path
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == result.stderr == ""
        replacement = path.read_bytes()
        assert replacement != legacy
        assert re.fullmatch(rb"[0-9a-f]{64}\n", replacement)
        assert legacy.decode() not in result.stdout + result.stderr
        assert replacement.decode() not in result.stdout + result.stderr
        assert path.stat().st_ino != original_inode
        assert _mode(path) == 0o600
        assert list(path.parent.glob(f".{path.name}.tmp-*")) == []


def test_local_http_custom_garage_secret_classification_is_exact_and_fail_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="aura0100-garage-secret-") as directory:
        root = Path(directory)
        path = root / "custom-garage-secret-key"
        default_path = root / "observatory-garage-secret-key"
        legacy = b"legacy-url-safe_secret~value\n"
        path.write_bytes(legacy)
        path.chmod(0o600)

        result = _run_secret_helper(
            path,
            "garage-secret",
            managed_default=True,
            default_path=default_path,
        )

        assert result.returncode != 0
        assert "invalid provider format" in result.stderr
        assert path.read_bytes() == legacy
        assert _mode(path) == 0o600


def test_local_http_valid_custom_garage_secret_is_preserved() -> None:
    with tempfile.TemporaryDirectory(prefix="aura0100-garage-secret-") as directory:
        root = Path(directory)
        path = root / "custom-garage-secret-key"
        original = b"abcdef0123456789" * 4 + b"\n"
        path.write_bytes(original)
        path.chmod(0o600)

        result = _run_secret_helper(
            path,
            "garage-secret",
            managed_default=True,
            default_path=root / "observatory-garage-secret-key",
        )

        assert result.returncode == 0, result.stderr
        assert path.read_bytes() == original
        assert _mode(path) == 0o600


def test_local_http_malformed_oversized_custom_garage_secret_is_bounded_and_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="aura0100-garage-secret-") as directory:
        root = Path(directory)
        path = root / "custom-garage-secret-key"
        malformed = b"a" * 65 + b"\n"
        path.write_bytes(malformed)
        path.chmod(0o600)

        result = _run_secret_helper(
            path,
            "garage-secret",
            managed_default=True,
            default_path=root / "observatory-garage-secret-key",
        )

        assert result.returncode != 0
        assert "invalid provider format" in result.stderr
        assert path.read_bytes() == malformed
        assert _mode(path) == 0o600


def test_local_http_custom_invalid_access_id_fails_closed_without_replacement() -> None:
    with tempfile.TemporaryDirectory(prefix="aura0100-access-id-") as directory:
        root = Path(directory)
        path = root / "custom-garage-access-key"
        legacy = b"legacy-url-safe_access-id~value\n"
        path.write_bytes(legacy)
        path.chmod(0o600)

        result = _run_secret_helper(
            path,
            "garage-access",
            managed_default=True,
            default_path=root / "observatory-garage-access-key",
        )

        assert result.returncode != 0
        assert "invalid provider format" in result.stderr
        assert path.read_bytes() == legacy
        assert _mode(path) == 0o600


@pytest.mark.parametrize(
    "case",
    ["symlink", "directory", "insecure", "empty"],
)
def test_local_http_invalid_existing_secret_paths_remain_fail_closed(case: str) -> None:
    with tempfile.TemporaryDirectory(prefix="aura0100-secret-path-") as directory:
        root = Path(directory)
        path = root / "managed-access-key"
        target = root / "target"
        if case == "symlink":
            target.write_bytes(b"GK0123456789abcdef01234567\n")
            target.chmod(0o600)
            path.symlink_to(target)
        elif case == "directory":
            path.mkdir()
        else:
            path.write_bytes(b"" if case == "empty" else b"legacy\n")
            path.chmod(0o600 if case == "empty" else 0o644)

        result = _run_secret_helper(
            path, "garage-access", managed_default=True, default_path=path
        )

        assert result.returncode != 0
        if case == "symlink":
            assert path.is_symlink()
            assert path.resolve() == target
        elif case == "directory":
            assert path.is_dir()
        elif case == "empty":
            assert path.read_bytes() == b""
            assert _mode(path) == 0o600
        else:
            assert path.read_bytes() == b"legacy\n"
            assert _mode(path) == 0o644


@pytest.mark.parametrize("kind", ["nats", "database"])
def test_local_http_other_secret_kinds_preserve_existing_values(kind: str) -> None:
    with tempfile.TemporaryDirectory(prefix="aura0100-secret-kind-") as directory:
        path = Path(directory) / "secret"
        original = f"existing-{kind}-value\n".encode()
        path.write_bytes(original)
        path.chmod(0o600)

        result = _run_secret_helper(path, kind, managed_default=False)

        assert result.returncode == 0, result.stderr
        assert path.read_bytes() == original
        assert _mode(path) == 0o600


def _merged_compose_config() -> dict[str, Any]:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is unavailable")
    command = ["docker", "compose"]
    env_file = ROOT / ".env"
    if env_file.exists():
        command.extend(["--env-file", str(env_file)])
    local_env_file = ROOT / ".env.local-http"
    if local_env_file.exists():
        command.extend(["--env-file", str(local_env_file)])
    for compose_file in COMPOSE_FILES:
        command.extend(["-f", compose_file])
    command.extend(
        [
            "--profile",
            "local-identity",
            "--profile",
            "observatory",
            "config",
            "--format",
            "json",
        ]
    )
    environment = os.environ.copy()
    environment.setdefault("AURA_OWNER_SUBJECT", "local-owner")
    environment.setdefault("AURA_OBSERVATORY_OWNER_SUBJECT", "local-owner")
    environment.setdefault("AURA_SECURE_COOKIES", "false")
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(f"supported local-http Compose config failed: {result.stderr}")
    document = json.loads(result.stdout)
    assert isinstance(document, dict)
    return cast(dict[str, Any], document)


def test_local_http_evaluation_runtimes_use_the_uv_python_cmds() -> None:
    expected = {
        "observatory-evaluation-worker": (
            "evaluation-worker",
            'CMD ["/app/.venv/bin/python", "-m", '
            '"aura_observatory.entrypoints.evaluation_worker"]',
        ),
        "observatory-evaluation-scheduler": (
            "evaluation-scheduler",
            'CMD ["/app/.venv/bin/python", "-m", '
            '"aura_observatory.entrypoints.evaluation_scheduler"]',
        ),
    }
    source_services = _services(_document(ROOT / "compose.yaml"))
    dockerfile = (ROOT / "deploy/docker/aura-observatory.Dockerfile").read_text(
        encoding="utf-8"
    )
    cmd_lines = [line.strip() for line in dockerfile.splitlines() if line.startswith("CMD ")]

    for service_name, (target, expected_cmd) in expected.items():
        source_build = cast(dict[str, Any], source_services[service_name]["build"])
        assert source_build["dockerfile"] == "deploy/docker/aura-observatory.Dockerfile"
        assert source_build["target"] == target
        assert "command" not in source_services[service_name]
        assert "entrypoint" not in source_services[service_name]
        assert cmd_lines.count(expected_cmd) == 1

    assert not any('CMD ["python"' in line for line in cmd_lines)

    rendered_services = _services(_merged_compose_config())
    for service_name, (target, _expected_cmd) in expected.items():
        rendered_build = cast(dict[str, Any], rendered_services[service_name]["build"])
        assert rendered_build["dockerfile"] == "deploy/docker/aura-observatory.Dockerfile"
        assert rendered_build["target"] == target
        assert rendered_services[service_name].get("command") is None
        assert rendered_services[service_name].get("entrypoint") is None


def _process_identity_probe(
    service: dict[str, Any],
    module: str,
) -> str:
    healthcheck = cast(dict[str, Any], service["healthcheck"])
    command = cast(list[Any], healthcheck["test"])
    assert isinstance(command, list)
    assert command[:3] == ["CMD", "/app/.venv/bin/python", "-c"]
    assert len(command) == 4
    probe = command[3]
    assert isinstance(probe, str)
    assert (
        f'target = [b"/app/.venv/bin/python", b"-m", b"{module}"]' in probe
    )
    assert "for pid in os.listdir(\"/proc\")" in probe
    assert 'with open(f"/proc/{pid}/cmdline", "rb")' in probe
    assert "if argv[:3] == target:" in probe
    assert "import aura_observatory" not in probe
    compile(probe, f"<{module}-healthcheck>", "exec")
    return probe


def _run_process_identity_probe(
    probe: str,
    processes: list[list[bytes]],
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="aura0100-proc-") as directory:
        proc_root = Path(directory) / "proc"
        proc_root.mkdir()
        for index, argv in enumerate(processes, start=100):
            process_root = proc_root / str(index)
            process_root.mkdir()
            (process_root / "cmdline").write_bytes(b"\0".join(argv) + b"\0")

        isolated_probe = probe.replace('"/proc', f'"{proc_root}')
        return subprocess.run(
            [sys.executable, "-c", isolated_probe],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )


def test_observatory_healthchecks_use_exact_stdlib_process_identity_probes() -> None:
    expected_modules = {
        "observatory-evaluation-worker":
            "aura_observatory.entrypoints.evaluation_worker",
        "observatory-evaluation-scheduler":
            "aura_observatory.entrypoints.evaluation_scheduler",
    }
    source_services = _services(_document(ROOT / "compose.yaml"))
    rendered_services = _services(_merged_compose_config())

    for source_name, services in (
        ("source", source_services),
        ("rendered", rendered_services),
    ):
        for service_name, module in expected_modules.items():
            probe = _process_identity_probe(services[service_name], module)
            target_argv = [
                b"/app/.venv/bin/python",
                b"-m",
                module.encode(),
            ]
            self_argv = [
                b"/app/.venv/bin/python",
                b"-c",
                b"healthcheck probe",
            ]
            wrong_argv = [
                b"/app/.venv/bin/python",
                b"-m",
                b"aura_observatory.entrypoints.other_service",
            ]

            success = _run_process_identity_probe(probe, [target_argv, self_argv])
            assert success.returncode == 0, (
                f"{source_name} {service_name} probe rejected its target: "
                f"{success.stderr}"
            )
            self_match = _run_process_identity_probe(probe, [self_argv])
            assert self_match.returncode != 0, (
                f"{source_name} {service_name} probe matched its own -c process"
            )
            wrong_match = _run_process_identity_probe(probe, [wrong_argv])
            assert wrong_match.returncode != 0, (
                f"{source_name} {service_name} probe matched another module"
            )


def test_local_http_nats_merge_keeps_disposable_data_and_entrypoint_bind() -> None:
    overlay = _services(_document(ROOT / "deploy/compose/local-http.yaml"))
    assert overlay["aura-nats"]["volumes"] == [
        "local_http_core_nats_data:/data",
        "./deploy/docker/nats-entrypoint.sh:/usr/local/bin/aura-nats-entrypoint:ro",
    ]

    merged = _services(_merged_compose_config())
    nats_volumes = merged["aura-nats"]["volumes"]
    assert isinstance(nats_volumes, list)
    assert {
        (volume["type"], volume["target"], volume.get("read_only", False))
        for volume in cast(list[dict[str, Any]], nats_volumes)
    } == {
        ("volume", "/data", False),
        ("bind", "/usr/local/bin/aura-nats-entrypoint", True),
    }


def test_local_http_merge_preserves_the_observatory_migration_gate_chain() -> None:
    services = _services(_merged_compose_config())
    migration = services["observatory-migrate"]
    migration_dependencies = cast(dict[str, Any], migration["depends_on"])
    assert {
        name: dependency["condition"]
        for name, dependency in migration_dependencies.items()
    } == {
        "observatory-postgres": "service_healthy",
        "observatory-clickhouse": "service_healthy",
    }

    for service_name in (
        "observatory-api",
        "observatory-worker",
        "observatory-evaluation-worker",
        "observatory-evaluation-scheduler",
    ):
        dependencies = cast(dict[str, Any], services[service_name]["depends_on"])
        assert (
            dependencies["observatory-migrate"]["condition"]
            == "service_completed_successfully"
        ), service_name
    assert services["observatory-web"]["depends_on"]["observatory-api"]["condition"] == (
        "service_healthy"
    )
