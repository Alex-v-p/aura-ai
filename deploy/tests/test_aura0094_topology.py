"""AURA-0094 local judge deployment isolation checks."""

from __future__ import annotations

import subprocess
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


def test_judge_garage_identity_is_distinct_read_only_and_idempotent() -> None:
    services = _services()
    bootstrap = services["observatory-judge-garage-bootstrap"]
    command = str(bootstrap["command"])

    assert bootstrap["profiles"] == ["observatory"]
    assert bootstrap["secrets"] == [
        "observatory-judge-garage-access-key",
        "observatory-judge-garage-secret-key",
    ]
    assert bootstrap["networks"] == ["observatory-object-storage"]
    assert "observatory-garage-bootstrap" in repr(bootstrap["depends_on"])
    assert "bucket allow --read --key" in command
    assert "bucket deny --write --key" in command
    assert "bucket deny --owner --key" in command
    assert 'permission_flags}" != "R"' in command
    assert "key import --yes" in command
    assert "bucket info" in command
    assert "unexpected preexisting bucket grant" in command
    assert 'echo "$${access_key}"' not in command

    rendered = cast(list[str], bootstrap["command"])[0].replace("$$", "$")
    result = subprocess.run(
        ["/bin/sh", "-n"],
        input=rendered,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_judge_worker_gets_only_worker_credentials_and_private_ollama_route() -> None:
    services = _services()
    worker = services["observatory-evaluation-worker"]
    worker_text = repr(worker)

    assert set(worker["networks"]) == {
        "observatory-db",
        "aura-evaluation-bus",
        "observatory-ingest",
        "observatory-object-storage",
        "observatory-judge-egress",
    }
    assert "host.docker.internal:host-gateway" in worker["extra_hosts"]
    assert "observatory-judge-garage-access-key" in worker["secrets"]
    assert "observatory-judge-garage-secret-key" in worker["secrets"]
    assert worker["environment"]["AURA_OBSERVATORY_JUDGE_OBJECT_ACCESS_KEY_FILE"] == (
        "/run/secrets/observatory-judge-garage-access-key"
    )
    assert worker["environment"]["AURA_OBSERVATORY_LOCAL_JUDGE_ORIGIN"] == (
        "http://host.docker.internal:11434"
    )
    assert "ollama" not in repr(worker["depends_on"]).lower()
    assert "ports" not in worker
    assert worker["read_only"] is True
    assert worker["mem_limit"] == "384m"
    assert worker["cpus"] == "0.5"

    for name in (
        "observatory-api",
        "observatory-worker",
        "observatory-evaluation-scheduler",
        "observatory-web",
        "observatory-collector",
        "aura-core-api",
        "aura-core-worker",
    ):
        assert "observatory-judge-garage-access-key" not in repr(services[name])
        assert "observatory-judge-garage-secret-key" not in repr(services[name])
        assert "observatory-judge-egress" not in repr(services[name])

    networks = _document(ROOT / "compose.yaml")["networks"]
    assert cast(dict[str, Any], networks)["observatory-judge-egress"] == {"internal": True}
    assert worker["environment"]["AURA_OBSERVATORY_LOCAL_JUDGE_ORIGIN"] == (
        "http://host.docker.internal:11434"
    )
    for proxy_name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        assert worker["environment"][proxy_name] == ""
    assert worker["environment"]["NO_PROXY"] == "*"
    assert worker["environment"]["no_proxy"] == "*"
    assert "AURA_OBSERVATORY_EVALUATION_OBJECT_ACCESS_KEY_FILE" not in worker_text


def test_judge_secrets_are_file_backed_and_local_http_preserves_them() -> None:
    document = _document(ROOT / "compose.yaml")
    secrets = cast(dict[Any, Any], document["secrets"])
    for name in (
        "observatory-judge-garage-access-key",
        "observatory-judge-garage-secret-key",
    ):
        assert "file" in secrets[name]
        assert "value" not in secrets[name]

    environment = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "AURA_OBSERVATORY_JUDGE_OBJECT_ACCESS_KEY_FILE=" in environment
    assert "AURA_OBSERVATORY_JUDGE_OBJECT_SECRET_KEY_FILE=" in environment
    assert "AURA_OBSERVATORY_LOCAL_JUDGE_ORIGIN=" in environment

    script = (ROOT / "deploy/compose/local-http.sh").read_text(encoding="utf-8")
    assert "AURA_OBSERVATORY_JUDGE_OBJECT_ACCESS_KEY_FILE" in script
    assert "AURA_OBSERVATORY_JUDGE_OBJECT_SECRET_KEY_FILE" in script
    assert "ensure_local_secret AURA_OBSERVATORY_JUDGE_OBJECT_ACCESS_KEY_FILE" in script
    assert "ensure_local_secret AURA_OBSERVATORY_JUDGE_OBJECT_SECRET_KEY_FILE" in script
    assert "AURA_OBSERVATORY_LOCAL_JUDGE_ORIGIN" in script
    assert "AURA_OBSERVATORY_LOCAL_JUDGE_ORIGIN=%s\\n" in script
    assert "must be loopback or RFC1918" in script
    assert "must be the fixed host-gateway Ollama origin or the generated local relay" in script

    local_http = (ROOT / "deploy/compose/local-http.yaml").read_text(encoding="utf-8")
    assert "observatory-judge-garage-bootstrap:" in local_http
    assert "observatory-object-storage, observatory-judge-egress" in local_http
