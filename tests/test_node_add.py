from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from conftest import MakeRepo, git

from labctl.cli import cli, menu
from labctl.cli.commands import node as node_module
from labctl.settings import LAB_CONFIG_ENV

FILES = {
    "nodes/frodo/docker-compose.yml": "name: homelab-frodo\n",
    "nodes/frodo/node.env": "DEPLOY_DIR=/srv/docker\nAPPDATA_DIR=$DEPLOY_DIR/app_data\n",
    "nodes/bilbo/docker-compose.yml": "name: homelab-bilbo\n",
    "nodes/bilbo/node.env": "DEPLOY_DIR=/srv/docker\nAPPDATA_DIR=/tank/appdata\n",
    "nodes/cirdan/docker-compose.yml": "name: homelab-cirdan\n",
    "nodes/cirdan/node.env": "DEPLOY_DIR=/home/me/docker\nAPPDATA_DIR=$DEPLOY_DIR/app_data\n",
}


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_repo: MakeRepo) -> Path:
    root = tmp_path / "lab"
    make_repo(root, FILES)
    monkeypatch.setenv(LAB_CONFIG_ENV, str(root))
    monkeypatch.setenv("LABCTL_CONFIG_FILE", str(tmp_path / "labctl.toml"))
    monkeypatch.setattr(node_module, "_check_node", lambda node: None)  # no ssh
    monkeypatch.setattr(node_module, "_tty", lambda: False)
    return root


def add(*args: str, ok: bool = True) -> Result:
    result = CliRunner().invoke(cli.main, ["node", "add", *args])
    assert (result.exit_code == 0) == ok, result.output + repr(result.exception)
    return result


def test_a_node_is_added_not_committed(lab: Path) -> None:
    add("aragorn", "--deploy-dir", "/opt/docker", "--appdata-dir", "$DEPLOY_DIR/appdata")
    assert (lab / "nodes/aragorn/node.env").read_text() == (
        "DEPLOY_DIR=/opt/docker\nAPPDATA_DIR=$DEPLOY_DIR/appdata\n"
    )
    compose = (lab / "nodes/aragorn/docker-compose.yml").read_text()
    assert compose.startswith("name: homelab-aragorn\n")  # the others' prefix
    assert git(lab, "status", "--porcelain") == "?? nodes/aragorn/"  # apply -m commits it


def test_no_terminal_and_no_values_is_an_error(lab: Path) -> None:
    assert "--deploy-dir and --appdata-dir" in add("aragorn", ok=False).output
    assert not (lab / "nodes/aragorn").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["frodo"], "already exists"),
        (["all"], "cannot be a node name"),
        (["_shared"], "cannot be a node name"),
        (["new", "--deploy-dir", "docker", "--appdata-dir", "/a"], "absolute path"),
        (["new", "--deploy-dir", "$DEPLOY_DIR/x", "--appdata-dir", "/a"], "absolute path"),
    ],
)
def test_refused(lab: Path, args: list[str], message: str) -> None:
    assert message in add(*args, ok=False).output


def test_a_terminal_is_offered_what_most_nodes_use(
    lab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    offered: list[str] = []

    def edit_text(title: str, prompt: str, value: str = "", **kwargs: object) -> str:
        offered.append(f"{prompt}={value}")
        return value

    monkeypatch.setattr(node_module, "_tty", lambda: True)
    monkeypatch.setattr(menu, "edit_text", edit_text)
    add("aragorn")
    assert offered == ["DEPLOY_DIR=/srv/docker", "APPDATA_DIR=$DEPLOY_DIR/app_data"]
