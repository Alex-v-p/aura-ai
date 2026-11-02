"""AURA-0094 local-http lifecycle and service-isolation assertions."""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]


class _ProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"aura-local-boundary")

    def log_message(self, _format: str, *_args: object) -> None:
        return


def test_local_http_down_preserves_state_but_reset_removes_volumes() -> None:
    script = (ROOT / "deploy/compose/local-http.sh").read_text(encoding="utf-8")
    down_start = script.index("down() {")
    reset_start = script.index("reset() {")
    down = script[down_start:reset_start]
    reset = script[reset_start : script.index("verify_idempotency() {")]
    assert "compose down --remove-orphans" in down
    assert "--volumes" not in down
    assert "compose down --volumes --remove-orphans" in reset
    assert "owner password file preserved" in reset
    assert "ensure_local_secret" in script
    assert "umask 077" in script


def test_observatory_judge_identity_is_never_injected_into_core_or_normal_workers() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    for service in (
        "aura-core-api",
        "aura-core-worker",
        "observatory-api",
        "observatory-worker",
        "observatory-web",
    ):
        block_start = compose.find(f"  {service}:")
        assert block_start >= 0
        block_end = compose.find("\n  ", block_start + 3)
        block = compose[block_start:] if block_end < 0 else compose[block_start:block_end]
        assert "observatory-judge-garage-access-key" not in block
        assert "observatory-judge-garage-secret-key" not in block
        assert "observatory-judge-egress" not in block


def test_core_and_observatory_readiness_have_no_cross_product_dependency() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    core_dependencies = repr(services["aura-core-api"].get("depends_on", {}))
    observatory_dependencies = repr(services["observatory-api"].get("depends_on", {}))
    assert "observatory" not in core_dependencies
    assert "aura-core" not in observatory_dependencies
    assert "aura-core-api" not in repr(services["observatory-web"].get("depends_on", {}))


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is unavailable")
def test_internal_judge_network_blocks_host_gateway_and_public_egress() -> None:
    image = "alpine:3.20"
    image_check = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, check=False, text=True
    )
    if image_check.returncode != 0:
        pytest.skip("pinned probe image is not available locally")

    with socket.socket() as probe_socket:
        probe_socket.bind(("0.0.0.0", 0))
        probe_port = probe_socket.getsockname()[1]
    listener = HTTPServer(("0.0.0.0", probe_port), _ProbeHandler)
    listener_thread = threading.Thread(target=listener.serve_forever, daemon=True)
    listener_thread.start()
    network = f"aura0094-judge-boundary-{uuid.uuid4().hex[:12]}"
    try:
        created = subprocess.run(
            ["docker", "network", "create", "--internal", network],
            capture_output=True,
            check=False,
            text=True,
            timeout=20,
        )
        assert created.returncode == 0, created.stderr

        host_probe = subprocess.run(
            [
                "docker",
                "run",
                "--pull=never",
                "--rm",
                "--network",
                network,
                "--add-host",
                "host.docker.internal:host-gateway",
                image,
                "sh",
                "-c",
                f"wget -q -T 3 -O- http://host.docker.internal:{probe_port} >/dev/null",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=20,
        )
        public_probe = subprocess.run(
            [
                "docker",
                "run",
                "--pull=never",
                "--rm",
                "--network",
                network,
                image,
                "sh",
                "-c",
                "wget -q -T 3 -O- http://example.com >/dev/null",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=20,
        )
        assert host_probe.returncode != 0
        assert public_probe.returncode != 0
    finally:
        listener.shutdown()
        listener.server_close()
        subprocess.run(["docker", "network", "rm", network], capture_output=True, check=False)
