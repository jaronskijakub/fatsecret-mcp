"""The built image over HTTP. Opt in with FATSECRET_CONTAINER_IMAGE=<image>."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest

from fatsecret_mcp.icons.links import PUBLIC_FILES
from tests.auth.test_http import PASSWORD_HASH

IMAGE = os.environ.get("FATSECRET_CONTAINER_IMAGE")
PORT = 8765
BASE_URL = "https://diary.example.com"

pytestmark = pytest.mark.skipif(IMAGE is None, reason="set FATSECRET_CONTAINER_IMAGE to test a built image")


def docker(*arguments: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["docker", *arguments],
        check=True,
        capture_output=True,
        text=True,
        input=input_text,
        timeout=30,
    )

    return result.stdout.strip()


@pytest.fixture(scope="module")
def container() -> Iterator[tuple[str, httpx2.Client]]:
    assert IMAGE is not None
    users = [
        {
            "id": "test-user",
            "display_name": "Test User",
            "fatsecret_access_token": "test-token",
            "fatsecret_access_secret": "test-secret",
            "connector_password_hash": PASSWORD_HASH,
            "daily_targets": {"kcal": 2000, "protein": 120, "fat": 70, "carbohydrate": 220},
            "timezone": "Europe/Warsaw",
        }
    ]
    environ = {
        "PORT": str(PORT),
        "FATSECRET_CONSUMER_KEY": "test-consumer-key",
        "FATSECRET_CONSUMER_SECRET": "test-consumer-secret",
        "FATSECRET_MCP_SIGNING_KEY": "container-test-signing-key-0123456789abcdef",
        "FATSECRET_MCP_BASE_URL": BASE_URL,
        "FATSECRET_MCP_USERS": json.dumps(users),
    }
    environment_arguments: list[str] = []

    for name, value in environ.items():
        environment_arguments.extend(["--env", f"{name}={value}"])

    started = time.monotonic()
    container_id = docker(
        "run", "--detach", "--pull=never", "--publish", f"127.0.0.1::{PORT}", *environment_arguments, IMAGE
    )

    try:
        address = docker("port", container_id, str(PORT))

        with httpx2.Client(
            base_url="http://" + address,
            timeout=1,
            trust_env=False,
        ) as client:
            while time.monotonic() - started < 10:
                try:
                    response = client.get("/health")

                    if response.status_code == 200:
                        break
                except httpx2.TransportError:
                    pass

                time.sleep(0.05)
            else:
                logs = docker("logs", container_id)

                pytest.fail("Image did not become healthy within 10 seconds:\n" + logs)

            elapsed = time.monotonic() - started

            print(f"\nContainer start to HTTP health: {elapsed:.3f} s")
            yield container_id, client
    finally:
        docker("rm", "--force", container_id)


def test_image_serves_http_on_port_from_environment(container: tuple[str, httpx2.Client]) -> None:
    _, client = container
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_image_advertises_connector_sign_in(container: tuple[str, httpx2.Client]) -> None:
    _, client = container
    resource = client.get("/.well-known/oauth-protected-resource/mcp")
    authorization = client.get("/.well-known/oauth-authorization-server")

    assert resource.status_code == 200
    assert resource.json()["resource"] == BASE_URL + "/mcp"
    assert resource.json()["authorization_servers"] == [BASE_URL + "/"]
    assert authorization.status_code == 200
    assert authorization.json()["issuer"] == BASE_URL + "/"
    assert authorization.json()["token_endpoint"] == BASE_URL + "/token"
    assert authorization.json()["code_challenge_methods_supported"] == ["S256"]


def test_image_requires_sign_in_for_mcp(container: tuple[str, httpx2.Client]) -> None:
    _, client = container
    response = client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["www-authenticate"]
    assert BASE_URL + "/.well-known/oauth-protected-resource/mcp" in response.headers["www-authenticate"]


def test_image_serves_the_connector_icons_without_sign_in(container: tuple[str, httpx2.Client]) -> None:
    _, client = container

    for name, media_type in PUBLIC_FILES.items():
        response = client.get("/" + name)

        assert response.status_code == 200
        assert response.headers["content-type"] == media_type


def test_image_runs_as_non_root_and_passes_its_health_check(container: tuple[str, httpx2.Client]) -> None:
    container_id, _ = container
    uid = docker("exec", container_id, "python", "-c", "import os; print(os.getuid())")

    assert int(uid) > 0
    health = "starting"

    for _ in range(50):
        health = docker("inspect", "--format", "{{.State.Health.Status}}", container_id)

        if health == "healthy":
            break

        time.sleep(0.1)

    assert health == "healthy"


def test_build_context_accepts_only_package_metadata_python_sources_and_icons(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    context = tmp_path / "build"
    exported = tmp_path / "exported"
    allowed = {
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "LICENSE",
        "src/fatsecret_mcp/__init__.py",
        "src/fatsecret_mcp/cli/main.py",
        "src/fatsecret_mcp/icons/files/fs-512.png",
    }
    excluded = {
        "operator-notes.md",
        "settings.env",
        "tests/test_example.py",
        "src/other_package/__init__.py",
        "src/fatsecret_mcp/notes.md",
        "src/fatsecret_mcp/__pycache__/old.pyc",
    }

    for name in allowed | excluded:
        path = context / name

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test build context\n")

    (context / ".dockerignore").write_text((repository / ".dockerignore").read_text())
    docker(
        "build",
        "--file",
        "-",
        "--output",
        f"type=local,dest={exported}",
        str(context),
        input_text="FROM scratch\nCOPY . /\n",
    )
    files = {path.relative_to(exported).as_posix() for path in exported.rglob("*") if path.is_file()}

    assert files == allowed
