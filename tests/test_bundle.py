from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from labctl.bundle import (
    Desired,
    Location,
    check_dirs,
    discover_stacks,
    resolve_desired,
    stack_candidates,
)
from labctl.compose import ComposeError
from labctl.settings import Secrets, Settings
from labctl.state import Stack


def write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


def test_stack_candidates_skip_included_and_disabled_files(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "main.yml": "include: [part.yml, {path: [./long.yml]}, apps/app.yml]\n",
            "part.yml": "include:\n  - deepest.yaml\n",  # top-level files are followed
            "long.yml": "services: {}\n",
            "deepest.yaml": "services: {}\n",
            "apps/app.yml": "include: [../other.yaml]\n",  # files in folders are not read
            "other.yaml": "services: {}\n",
            "old.disabled.yml": "services: {}\n",
            "broken.yml": "services: [\n",
            "folder.yml/x": "",  # a folder is not a file
            "notes.txt": "",
        },
    )
    location = Location(str(tmp_path))
    assert stack_candidates(location, location.names()) == ["broken.yml", "main.yml", "other.yaml"]


docker = pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker CLI")


@pytest.fixture
def repo(tmp_path: Path) -> Settings:
    write(
        tmp_path / "repo",
        {
            "apps/plex.yml": "services:\n  plex:\n    image: plex\n"
            "    volumes: ['${APPDATA_DIR}/plex:/config']\n",
            "nodes/frodo/node.env": "DEPLOY_DIR=/srv/docker\nAPPDATA_DIR=/srv/appdata\n",
        },
    )
    return Settings(lab_config=tmp_path / "repo", config_dir=tmp_path / "config")


def resolve(settings: Settings, tmp_path: Path, files: dict[str, str]) -> Desired:
    write(settings.nodes_dir / "frodo", files)
    return resolve_desired(settings, "frodo", Secrets(), tmp_path / "work", None)


@docker
def test_stacks_are_found_named_and_resolved_as_on_the_node(repo: Settings, tmp_path: Path) -> None:
    desired = resolve(
        repo,
        tmp_path,
        {
            "media.yml": "name: media\ninclude: [apps/plex.yml]\n",
            "media-extra.yml": "name: media\nservices: {tdarr: {image: tdarr}}\n",
            "infra.yml": "include: [infra-part.yml]\nservices: {npm: {image: npm}}\n",
            "infra-part.yml": "services:\n  proxy: {image: proxy, volumes: ['./certs:/certs']}\n",
            "settings.yml": "theme: dark\n",
        },
    )
    state = desired.state
    assert dict(state.stacks) == {
        "docker": Stack("docker", ("infra.yml",)),  # no name: DEPLOY_DIR's folder name
        "media": Stack("media", ("media-extra.yml", "media.yml")),
    }
    assert {n: s.stack for n, s in state.services.items()} == {
        "npm": "docker",
        "proxy": "docker",
        "plex": "media",
        "tdarr": "media",
    }
    assert state.services["plex"].data_paths == ("plex",)
    assert list(desired.not_stacks) == ["settings.yml"]
    assert "additional properties 'theme' not allowed" in desired.not_stacks["settings.yml"]
    # Paths compose resolved under the local bundle are reported as they are on the node.
    proxy = desired.service_config("proxy")
    assert proxy["volumes"][0]["source"] == "/srv/docker/certs"
    assert "infra-part.yml" in desired.files


@docker
def test_service_names_must_be_unique_on_a_node(repo: Settings, tmp_path: Path) -> None:
    with pytest.raises(ComposeError, match="'plex' is in two stacks, 'a' and 'b'"):
        resolve(
            repo,
            tmp_path,
            {
                "a.yml": "name: a\ninclude: [apps/plex.yml]\n",
                "b.yml": "name: b\nservices: {plex: {image: plex}}\n",
            },
        )


def test_node_files_may_not_shadow_the_lab_config(repo: Settings, tmp_path: Path) -> None:
    with pytest.raises(ComposeError, match="would both be DEPLOY_DIR/apps"):
        resolve(repo, tmp_path, {"apps/mine.yml": "services: {}\n"})


@docker
def test_discovery_works_on_any_folder_of_compose_files(tmp_path: Path) -> None:
    """The same rule finds stacks in a node's DEPLOY_DIR (run through ssh there)."""
    home = tmp_path / "docker"
    write(
        home,
        {
            "stack.yml": "name: one\ninclude: [part.yml]\n",
            "part.yml": "services: {a: {image: alpine}}\n",
            "stack2.yml": "services: {b: {image: alpine}}\n",
            "notes.yml": "theme: dark\n",
        },
    )
    stacks, not_stacks = discover_stacks(Location(str(home)))
    assert stacks == [Stack("docker", ("stack2.yml",)), Stack("one", ("stack.yml",))]
    assert list(not_stacks) == ["notes.yml"]


def test_appdata_dir_is_independent_of_deploy_dir_or_inside_it() -> None:
    check_dirs("cirdan", "/home/paul/docker", "/home/paul/docker/app_data")
    check_dirs("frodo", "/zconfig/docker", "/zconfig/appdata")
    check_dirs("x", "/srv/docker", "/srv/docker-data")  # a sibling that shares a prefix
    with pytest.raises(ComposeError, match=r"DEPLOY_DIR \(/home/paul/docker\) is APPDATA_DIR"):
        check_dirs("bilbo", "/home/paul/docker", "/home/paul/docker")
    with pytest.raises(ComposeError, match="is inside APPDATA_DIR"):
        check_dirs("x", "/home/paul/docker", "/home/paul")
