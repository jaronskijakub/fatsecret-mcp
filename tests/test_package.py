"""The package a release publishes, and the versions a release has to agree on.

The wheel and sdist are built from the files of a fresh checkout, with uncommitted changes, the way the
release workflow builds them. Set FATSECRET_PACKAGE_DIST=<directory> to check artefacts already built.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from collections.abc import Iterable
from email.message import Message
from email.parser import Parser
from pathlib import Path, PurePosixPath

import pytest

from fatsecret_mcp.config.loader import (
    ACTING_USER_VAR,
    CONSUMER_KEY_VAR,
    CONSUMER_SECRET_VAR,
    SIGNING_KEY_VAR,
    USERS_VAR,
)

REPOSITORY = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((REPOSITORY / "pyproject.toml").read_text())["project"]
VERSION = PROJECT["version"]
DIST_INFO = f"fatsecret_mcp-{VERSION}.dist-info"
SERVER = json.loads((REPOSITORY / "server.json").read_text())
(REGISTRY_PACKAGE,) = SERVER["packages"]
README = (REPOSITORY / "README.md").read_text()
CHANGELOG = (REPOSITORY / "CHANGELOG.md").read_text()
BUILT_DIST = os.environ.get("FATSECRET_PACKAGE_DIST")
ICON_FILES = "src/fatsecret_mcp/icons/files/"


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if BUILT_DIST is not None:
        return Path(BUILT_DIST)

    checkout = tmp_path_factory.mktemp("checkout")
    built = tmp_path_factory.mktemp("dist")

    for name in _checkout_files():
        target = checkout / name

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPOSITORY / name, target)

    subprocess.run(
        ["uv", "build", "--out-dir", str(built), str(checkout)],
        check=True,
        timeout=120,
    )

    return built


def test_the_wheel_holds_only_the_package_and_its_metadata(dist: Path) -> None:
    names = ["METADATA", "WHEEL", "RECORD", "entry_points.txt", "licenses/LICENSE"]
    metadata = [f"{DIST_INFO}/{name}" for name in names]
    expected = [*_package_files(), *metadata]
    entries = _wheel_entries(dist)
    files = _files(entries)

    assert files == set(expected)
    assert _directories(entries) - _parents(files) == set()


def test_the_sdist_holds_only_the_package_and_its_metadata(dist: Path) -> None:
    sources = [f"src/{module}" for module in _package_files()]
    names = ["PKG-INFO", "pyproject.toml", "README.md", "LICENSE", *sources]
    expected = [f"fatsecret_mcp-{VERSION}/{name}" for name in names]
    entries = _sdist_entries(dist)
    files = _files(entries)

    assert files == set(expected)
    assert _directories(entries) - _parents(files) == set()


def test_the_package_metadata_describes_the_project(dist: Path) -> None:
    metadata = _wheel_metadata(dist)
    urls = metadata.get_all("Project-URL", [])

    assert metadata["Name"] == "fatsecret-mcp"
    assert metadata["Version"] == VERSION
    assert metadata["License-Expression"] == "MIT"
    assert metadata["Requires-Python"] == ">=3.13"
    assert metadata["Description-Content-Type"] == "text/markdown"
    assert metadata.get_payload() == README
    assert f"Repository, {PROJECT['urls']['Repository']}" in urls


def test_the_package_installs_the_fatsecret_mcp_command(dist: Path) -> None:
    text = _wheel_text(dist, f"{DIST_INFO}/entry_points.txt")
    entry_points = configparser.ConfigParser()

    entry_points.read_string(text)

    assert dict(entry_points["console_scripts"]) == {"fatsecret-mcp": "fatsecret_mcp.cli.main:main"}


def test_a_release_names_one_version_everywhere() -> None:
    changelog_heading = rf"^## \[{re.escape(VERSION)}\] - \d{{4}}-\d{{2}}-\d{{2}}$"

    assert SERVER["version"] == VERSION
    assert REGISTRY_PACKAGE["version"] == VERSION
    assert re.search(changelog_heading, CHANGELOG, re.MULTILINE)


def test_the_registry_entry_points_at_this_package() -> None:
    variables = [variable["name"] for variable in REGISTRY_PACKAGE["environmentVariables"]]
    stdio_variables = [CONSUMER_KEY_VAR, CONSUMER_SECRET_VAR, SIGNING_KEY_VAR, USERS_VAR, ACTING_USER_VAR]

    assert SERVER["name"] == "io.github.jaronskijakub/fatsecret-mcp"
    assert SERVER["repository"] == {"url": PROJECT["urls"]["Repository"], "source": "github"}
    assert len(SERVER["description"]) <= 100
    assert REGISTRY_PACKAGE["registryType"] == "pypi"
    assert REGISTRY_PACKAGE["identifier"] == PROJECT["name"]
    assert REGISTRY_PACKAGE["transport"] == {"type": "stdio"}
    assert sorted(variables) == sorted(stdio_variables)


def test_the_readme_names_the_registry_entry_for_the_ownership_check() -> None:
    assert f"<!-- mcp-name: {SERVER['name']} -->" in README.splitlines()


def test_the_readme_links_work_on_pypi() -> None:
    """PyPI keeps the README of each version and resolves no relative links."""
    targets = re.findall(r"\]\(([^)\s]+)\)", README)
    relative = [target for target in targets if not target.startswith("https://")]

    assert targets
    assert relative == []


def _checkout_files() -> list[str]:
    """Tracked and new files, without ignored ones or files deleted from the working tree."""
    listing = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPOSITORY,
        check=True,
        capture_output=True,
        text=True,
    )
    names = listing.stdout.split("\0")

    return [name for name in names if (REPOSITORY / name).is_file()]


def _package_files() -> list[str]:
    """The package's Python files and the connector's icon files, relative to `src/`."""
    names = [name for name in _checkout_files() if _is_package_file(name)]

    return [name.removeprefix("src/") for name in names]


def _is_package_file(name: str) -> bool:
    return name.startswith("src/") and (name.endswith(".py") or name.startswith(ICON_FILES))


def _only(dist: Path, pattern: str) -> Path:
    (path,) = dist.glob(pattern)

    return path


def _wheel_entries(dist: Path) -> set[str]:
    with zipfile.ZipFile(_only(dist, "*.whl")) as archive:
        return set(archive.namelist())


def _wheel_text(dist: Path, name: str) -> str:
    with zipfile.ZipFile(_only(dist, "*.whl")) as archive:
        return archive.read(name).decode()


def _wheel_metadata(dist: Path) -> Message:
    text = _wheel_text(dist, f"{DIST_INFO}/METADATA")

    return Parser().parsestr(text)


def _sdist_entries(dist: Path) -> set[str]:
    with tarfile.open(_only(dist, "*.tar.gz")) as archive:
        members = archive.getmembers()

    names = [_entry_name(member) for member in members]

    return set(names)


def _entry_name(member: tarfile.TarInfo) -> str:
    """A directory gets a trailing slash, as in a wheel."""
    if member.isdir():
        return member.name + "/"

    return member.name


def _files(entries: set[str]) -> set[str]:
    return {entry for entry in entries if not entry.endswith("/")}


def _directories(entries: set[str]) -> set[str]:
    return {entry for entry in entries if entry.endswith("/")}


def _parents(files: Iterable[str]) -> set[str]:
    """Every directory holding one of the files, as an archive entry with a trailing slash."""
    parents: set[str] = set()

    for name in files:
        for parent in PurePosixPath(name).parents:
            if parent.name:
                parents.add(f"{parent}/")

    return parents
