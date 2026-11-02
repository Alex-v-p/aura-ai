"""AURA-0120 Garage 2.1 grant-table parsing and isolation checks."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[2]
WORKER_BOOTSTRAP = "observatory-run-evidence-garage-bootstrap"
API_BOOTSTRAP = "observatory-run-evidence-api-garage-bootstrap"
TARGET_BUCKET = "aura-observatory-run-evidence"
PARSE_ERROR = "__AURA_GRANT_PARSE_ERROR__"


def _services() -> dict[str, dict[str, Any]]:
    document = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    services = document.get("services")
    assert isinstance(services, dict)
    return {
        str(name): cast(dict[str, Any], definition)
        for name, definition in cast(dict[object, object], services).items()
    }


def _parser(service: str) -> tuple[str, str]:
    command = str(_services()[service]["command"][0])
    start = command.index("extract_authorized_buckets()")
    end = command.find("extract_permission_flags()", start)
    if end < 0:
        end = command.index("key_info=", start)
    function = command[start:end].replace("$$", "$")
    assert "awk" in function
    assert function.rstrip().endswith("; };")
    return command, function


def _parse(service: str, key_info: str, expected: str = TARGET_BUCKET) -> list[str]:
    _command, function = _parser(service)
    script = (
        f'{function}\nprintf \'%s\\n\' "$KEY_INFO" | '
        'extract_authorized_buckets "$KEY_INFO" "$EXPECTED"'
    )
    result = subprocess.run(
        ["sh", "-eu", "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "KEY_INFO": key_info, "EXPECTED": expected},
    )
    return [line for line in result.stdout.splitlines() if line]


def _garage_key_info(*rows: str) -> str:
    return "\n".join(
        (
            "==== BUCKETS FOR THIS KEY ====",
            "Permissions ID Global aliases Local aliases",
            *rows,
        )
    )


def _run_bootstrap(service: str, key_info: str) -> subprocess.CompletedProcess[str]:
    command = str(_services()[service]["command"][0]).replace("$$", "$")
    command = command.replace("/garage ", "garage ")
    command = re.sub(
        r"cat /run/secrets/[A-Za-z0-9_-]+",
        "printf '%s' fake-secret",
        command,
    )
    fake_garage = """
garage() {
  case "$*" in
    *" key info "*) printf '%s\\n' "$KEY_INFO" ;;
    *" bucket info "*) printf '%s fake-key\\n' "$PERMISSIONS" ;;
    *) return 0 ;;
  esac
}
"""
    return subprocess.run(
        ["sh", "-eu", "-c", fake_garage + command],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "AURA_GARAGE_IMAGE": "garage@sha256:" + "0" * 64,
            "AURA_GARAGE_BUCKET": "aura-documents",
            "AURA_OBSERVATORY_GARAGE_BUCKET": "aura-observatory-evaluation",
            "AURA_OBSERVATORY_RUN_EVIDENCE_OBJECT_BUCKET": TARGET_BUCKET,
            "KEY_INFO": key_info,
            "PERMISSIONS": "W"
            if service == WORKER_BOOTSTRAP
            else "RW",
        },
    )


def test_garage_21_header_is_ignored_by_worker_and_api_parsers() -> None:
    header_only = _garage_key_info()
    for service in (WORKER_BOOTSTRAP, API_BOOTSTRAP):
        assert _parse(service, header_only) == []


def test_valid_target_bucket_rows_parse_for_worker_and_api() -> None:
    target_row = "RW 0123456789abcdef aura-observatory-run-evidence -"
    for service in (WORKER_BOOTSTRAP, API_BOOTSTRAP):
        assert _parse(service, _garage_key_info(target_row)) == [TARGET_BUCKET]


def test_malformed_grant_rows_fail_closed_alone_and_with_valid_target() -> None:
    malformed = "RW not-a-garage-id aura-documents -"
    target = "RW 0123456789abcdef aura-observatory-run-evidence -"
    for service in (WORKER_BOOTSTRAP, API_BOOTSTRAP):
        for key_info in (
            _garage_key_info(malformed),
            _garage_key_info(target, malformed),
        ):
            result = _run_bootstrap(service, key_info)
            assert result.returncode != 0


def test_malformed_bucket_rows_emit_a_bounded_failure_sentinel() -> None:
    malformed = "RW not-a-garage-bucket-id aura-observatory-run-evidence -"
    for service in (WORKER_BOOTSTRAP, API_BOOTSTRAP):
        command, _function = _parser(service)
        assert _parse(service, _garage_key_info(malformed)) == [PARSE_ERROR]
        assert "grep -Fqx -- '__AURA_GRANT_PARSE_ERROR__'" in command
        assert "grant table could not be parsed safely" in command
        assert "exit 78" in command


def test_malformed_single_field_rows_do_not_end_grant_table_silently() -> None:
    malformed = "unexpected-unparsed-row"
    for service in (WORKER_BOOTSTRAP, API_BOOTSTRAP):
        assert _parse(service, _garage_key_info(malformed)) == [PARSE_ERROR]


def test_unrelated_bucket_grants_remain_fail_closed_for_worker_and_api() -> None:
    for index, bucket in enumerate(("aura-documents", "aura-observatory-evaluation")):
        key_info = _garage_key_info(
            f"RW {index + 1:016x} {bucket} -"
        )
        for service in (WORKER_BOOTSTRAP, API_BOOTSTRAP):
            command, _function = _parser(service)
            parsed = _parse(service, key_info)
            assert parsed == [bucket]
            assert set(parsed) - {TARGET_BUCKET}
            assert (
                'grep -Fvx -- "$${AURA_OBSERVATORY_RUN_EVIDENCE_OBJECT_BUCKET}"'
                in command
            )
            assert "unexpected preexisting bucket grant" in command


def test_worker_and_api_verify_only_the_dedicated_permission_modes() -> None:
    services = _services()
    worker_command = str(services[WORKER_BOOTSTRAP]["command"][0])
    api_command = str(services[API_BOOTSTRAP]["command"][0])
    assert "bucket allow --write --key" in worker_command
    assert 'expected W' in worker_command
    assert "bucket deny --owner --key" in worker_command
    assert "bucket allow --read --write --key" in api_command
    assert 'expected RW' in api_command
    assert "bucket deny --owner --key" in api_command
    assert "AURA_OBSERVATORY_RUN_EVIDENCE_OBJECT_BUCKET" in worker_command
    assert "AURA_OBSERVATORY_RUN_EVIDENCE_OBJECT_BUCKET" in api_command
