"""AURA-0104 Observatory API Garage identity deployment guards."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[2]


def _document(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _services() -> dict[str, dict[str, Any]]:
    value = _document(ROOT / "compose.yaml").get("services")
    assert isinstance(value, dict)
    return {
        str(name): cast(dict[str, Any], definition)
        for name, definition in cast(dict[Any, Any], value).items()
    }


def test_observatory_api_has_only_its_write_only_garage_identity() -> None:
    services = _services()
    api = services["observatory-api"]
    environment = cast(dict[str, Any], api["environment"])

    assert api["networks"][-1] == "observatory-object-storage"
    assert "aura-object-storage" not in api["networks"]
    assert api["secrets"][-4:] == [
        "observatory-api-garage-access-key",
        "observatory-api-garage-secret-key",
        "observatory-run-evidence-api-access-key",
        "observatory-run-evidence-api-secret-key",
    ]
    assert environment["AURA_OBSERVATORY_EVALUATION_OBJECT_ACCESS_KEY_FILE"] == (
        "/run/secrets/observatory-api-garage-access-key"
    )
    assert environment["AURA_OBSERVATORY_EVALUATION_OBJECT_SECRET_KEY_FILE"] == (
        "/run/secrets/observatory-api-garage-secret-key"
    )
    assert services["observatory-api-garage-bootstrap"]["secrets"] == [
        "observatory-api-garage-access-key",
        "observatory-api-garage-secret-key",
    ]
    for forbidden in (
        "observatory-garage-access-key",
        "observatory-judge-garage-access-key",
        "aura-evaluation-runner-garage-access-key",
        "aura-garage-access-key",
    ):
        assert forbidden not in api["secrets"]


def test_api_bootstrap_is_private_fail_closed_write_only_and_precedes_api() -> None:
    bootstrap = _services()["observatory-api-garage-bootstrap"]
    command = cast(list[str], bootstrap["command"])[0]
    assert bootstrap["profiles"] == ["observatory"]
    assert bootstrap["networks"] == ["observatory-object-storage"]
    assert bootstrap["depends_on"] == {
        "observatory-garage-bootstrap": {"condition": "service_completed_successfully"}
    }
    assert "bucket allow --write --key" in command
    assert "bucket deny --read --key" in command
    assert "bucket deny --owner --key" in command
    assert 'permission_flags}" != "W"' in command
    assert "unexpected preexisting bucket grant" in command
    assert "grant verification failed closed" in command
    assert 'echo "$${access_key}"' not in command

    rendered = command.replace("$$", "$")
    result = subprocess.run(
        ["/bin/sh", "-n"],
        input=rendered,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    api_depends = _services()["observatory-api"]["depends_on"]
    assert api_depends["observatory-api-garage-bootstrap"] == {
        "condition": "service_completed_successfully"
    }


def test_api_bootstrap_suppresses_credential_output_on_provider_failures() -> None:
    command = cast(
        list[str], _services()["observatory-api-garage-bootstrap"]["command"]
    )[0].replace("$$", "$")
    with tempfile.TemporaryDirectory(prefix="aura0104-api-garage-") as directory:
        root = Path(directory)
        fake_garage = root / "garage"
        fake_garage.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *' status') exit 0 ;;\n"
            "  *' key info'*)\n"
            "    if [ \"$AURA_TEST_FAILURE_STAGE\" = import ]; then exit 1; fi\n"
            "    printf '==== BUCKETS FOR THIS KEY ====\\n"
            "Permissions ID Global aliases Local aliases\\n"
            " W deadbeefdeadbeef aura-observatory-evaluation\\n'\n"
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
        access_value = "GKabcdef0123456789abcdef01"
        secret_value = "0123456789abcdef" * 4
        access_file.write_text(access_value + "\n", encoding="utf-8")
        secret_file.write_text(secret_value + "\n", encoding="utf-8")
        for stage, diagnostic in (
            ("import", "Observatory API Garage key import failed closed\n"),
            ("grant", "Observatory API Garage write grant failed closed\n"),
        ):
            rendered = command.replace("/garage --config", f"{fake_garage} --config")
            rendered = rendered.replace(
                "/run/secrets/observatory-api-garage-access-key", str(access_file)
            )
            rendered = rendered.replace(
                "/run/secrets/observatory-api-garage-secret-key", str(secret_file)
            )
            result = subprocess.run(
                ["/bin/sh", "-ec", rendered],
                cwd=ROOT,
                env={
                    **os.environ,
                    "AURA_GARAGE_IMAGE": "garage:test@sha256:" + "a" * 64,
                    "AURA_OBSERVATORY_GARAGE_BUCKET": "aura-observatory-evaluation",
                    "AURA_TEST_FAILURE_STAGE": stage,
                },
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            assert result.returncode == 78
            assert result.stdout == ""
            assert result.stderr == diagnostic
            assert access_value not in result.stdout + result.stderr
            assert secret_value not in result.stdout + result.stderr


def test_api_garage_secrets_are_file_backed_and_local_http_is_safe() -> None:
    document = _document(ROOT / "compose.yaml")
    secrets = cast(dict[str, dict[str, Any]], document["secrets"])
    for name in (
        "observatory-api-garage-access-key",
        "observatory-api-garage-secret-key",
    ):
        assert "file" in secrets[name]
        assert "value" not in secrets[name]

    local_script = (ROOT / "deploy/compose/local-http.sh").read_text(encoding="utf-8")
    for variable in (
        "AURA_OBSERVATORY_API_GARAGE_ACCESS_KEY_FILE",
        "AURA_OBSERVATORY_API_GARAGE_SECRET_KEY_FILE",
    ):
        assert variable in local_script
        assert f"ensure_local_secret {variable}" in local_script
    assert "observatory-api-garage-access-key" in local_script
    assert "observatory-api-garage-secret-key" in local_script

    local_http = (ROOT / "deploy/compose/local-http.yaml").read_text(encoding="utf-8")
    block = local_http.split("  observatory-api-garage-bootstrap:\n", 1)[1].split(
        "\n  authentik-postgres:\n", 1
    )[0]
    assert "local_http_core_garage_data:/var/lib/garage:ro" in block
    assert "local_http_core_garage_config:/run/garage-config:ro" in block
    assert "observatory-object-storage" in local_http
