"""labctl add: what a node runs (offers for its dirs), the import of its stacks, the command."""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from conftest import MakeRepo, git, needs_docker

from labctl import adopt as adopt_module
from labctl.adopt import (
    Host,
    Imported,
    env_lines,
    offer_appdata_dir,
    offer_deploy_dir,
    parse_host,
    read_stacks,
)
from labctl.cli import cli, menu
from labctl.cli.commands import add as add_module
from labctl.collect import OnNode
from labctl.remote import MARK
from labctl.repo import ConfigRepo, TagInfo
from labctl.runtime import Container
from labctl.settings import LAB_CONFIG_ENV, Secrets, Settings
from labctl.state import Dirs, Stack


def container(
    name: str, project: str = "", folder: str = "", binds: Sequence[str] = ()
) -> Container:
    files = (f"{folder}/docker-compose.yml",) if folder else ()
    return Container(
        name,
        "running",
        project=project,
        files=files,
        working_dir=folder,
        binds=tuple((f"/in/{i}", source) for i, source in enumerate(binds)),
    )


#############################################################################
# What the node runs
#############################################################################


def test_the_host_read() -> None:
    row = {
        "name": "/heimdall",
        "state": "running",
        "project": "docker",
        "dir": "/srv/docker",
        "files": "/srv/docker/docker-compose.yml",
        "mounts": [
            {"Type": "bind", "Source": "/srv/docker/app_data/heimdall", "Destination": "/c"}
        ],
    }
    output = "\n".join(
        [
            f"{MARK} pointer",
            f"{MARK} compose",
            "2.40.0",
            f"{MARK} user",
            "1000",
            "paul adm docker",
            f"{MARK} containers",
            json.dumps(row),
        ]
    )
    host = parse_host(output)
    assert host.compose == "2.40.0" and host.docker and host.pointer == ""
    assert host.containers[0].binds == (("/c", "/srv/docker/app_data/heimdall"),)
    assert [p.working_dir for p in host.projects] == ["/srv/docker"]
    assert not parse_host(f"{MARK} user\n1000\npaul adm\n").docker
    assert parse_host(f"{MARK} user\n0\nroot\n").docker


def test_deploy_dir_offer() -> None:
    a = container("a", "docker", "/srv/docker")
    b = container("b", "media", "/srv/docker")
    c = container("c", "other", "/opt/other")
    assert offer_deploy_dir(Host(containers=(a, b, c))) == "/srv/docker"
    # A labctl apply's DEPLOY_DIR comes first; a tie goes to the first folder.
    assert offer_deploy_dir(Host(pointer="/home/me/docker", containers=(a,))) == "/home/me/docker"
    assert offer_deploy_dir(Host(containers=(a, c))) == "/opt/other"
    assert offer_deploy_dir(Host()) == ""


def test_appdata_dir_offer() -> None:
    containers = [
        container("heimdall", binds=["/srv/docker/app_data/heimdall", "/var/run/docker.sock"]),
        container("npm", binds=["/srv/docker/app_data/npm/data", "/srv/docker/app_data/npm/le"]),
        container("plex", binds=["/srv/docker/app_data/plex/config", "/mnt/media/movies"]),
        container("sonarr", binds=["/mnt/media/movies", "/etc/localtime"]),
    ]
    assert offer_appdata_dir(containers, "/srv/docker") == "$DEPLOY_DIR/app_data"
    assert offer_appdata_dir(containers, "/opt/docker") == "/srv/docker/app_data"
    # One mounted folder says nothing.
    assert offer_appdata_dir(containers[:1], "/srv/docker") == ""
    assert offer_appdata_dir([], "/srv/docker") == ""


#############################################################################
# The import
#############################################################################


class FakeNode:
    """DEPLOY_DIR on a 'node' that is a local folder."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.downloads: list[list[str]] = []

    def download(self, cwd: str, names: Sequence[str], local: Path) -> None:
        self.downloads.append(list(names))
        for name in names:
            source = self.root / name
            if source.is_dir():
                shutil.copytree(source, local / name)
            else:
                shutil.copy2(source, local / name)


NODE_FILES = {
    "docker-compose.yml": (
        "name: homelab-x\ninclude:\n  - apps/web.yml\n"
        "services:\n  db:\n    image: postgres\n    env_file: ./db.env\n"
    ),
    "apps/web.yml": (
        "services:\n  web:\n    image: nginx\n    volumes:\n      - ./site:/usr/share/nginx/html\n"
    ),
    "db.env": "POSTGRES_DB=app\n",
    ".env": "TZ=UTC\nDB_PASSWORD=hunter2\n",
    "up.sh": "#!/bin/sh\n",
    "notes.yml": "x-notes: true\n",
}


@needs_docker
def test_read_stacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    root = tmp_path / "node"
    for path, text in NODE_FILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    (root / "site").mkdir()
    names = {path.split("/", 1)[0]: "dir" if "/" in path else "file" for path in NODE_FILES}
    names |= {"site": "dir", "app_data": "data"}
    monkeypatch.setattr(
        adopt_module, "read_node", lambda remote, home, hashed, data: OnNode(None, {}, names=names)
    )
    elsewhere = container("tool", "tool", "/opt/tool")
    node = FakeNode(root)

    found = read_stacks(
        settings, node, Host(containers=(elsewhere,)), "/srv/x", ["app_data"], tmp_path / "copy"
    )  # type: ignore[arg-type]

    assert found is not None
    assert found.stacks == (Stack("homelab-x", ("docker-compose.yml",)),)
    assert found.files == ("apps/web.yml", "db.env", "docker-compose.yml")
    assert found.env == {"TZ": "UTC", "DB_PASSWORD": "hunter2"}
    assert found.others == ("notes.yml", "site", "up.sh")  # archived by the first apply
    assert found.skipped == [
        "apps/web.yml: web bind-mounts ./site (a relative path: not imported)",
        "stack tool runs from /opt/tool: labctl's stacks are top-level files of DEPLOY_DIR",
    ]
    assert ["app_data"] not in node.downloads and all("site" not in d for d in node.downloads)


def test_env_lines() -> None:
    env = {"DEPLOY_DIR": "/x", "TZ": "UTC", "PUID": "1000", "API_KEY": "s3cret"}
    lines = env_lines(env, {"TZ": "UTC", "PUID": "1001"}, ["API_KEY"], ["DEPLOY_DIR"])
    assert lines == ["PUID=1000", "API_KEY=_SECRET_"]


#############################################################################
# labctl add
#############################################################################

FILES = {
    "nodes/frodo/docker-compose.yml": "name: homelab-frodo\n",
    "nodes/frodo/node.env": "DEPLOY_DIR=/srv/docker\nAPPDATA_DIR=$DEPLOY_DIR/app_data\n",
    "nodes/bilbo/docker-compose.yml": "name: homelab-bilbo\n",
    "nodes/bilbo/node.env": "DEPLOY_DIR=/srv/docker\nAPPDATA_DIR=/tank/appdata\n",
    "nodes/cirdan/docker-compose.yml": "name: homelab-cirdan\n",
    "nodes/cirdan/node.env": "DEPLOY_DIR=/home/me/docker\nAPPDATA_DIR=$DEPLOY_DIR/app_data\n",
    "nodes/_shared/common.env": "TZ=UTC\n",
}


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_repo: MakeRepo) -> Path:
    root = tmp_path / "lab"
    make_repo(root, FILES)
    monkeypatch.setenv(LAB_CONFIG_ENV, str(root))
    monkeypatch.setenv("LABCTL_CONFIG_FILE", str(tmp_path / "labctl.toml"))
    monkeypatch.setattr(add_module, "_check_node", lambda node: None)  # unreachable
    monkeypatch.setattr(add_module, "is_terminal", lambda: False)
    monkeypatch.setattr(add_module, "check_node", lambda *args: True)  # validate's check
    return root


def add(*args: str, ok: bool = True) -> Result:
    result = CliRunner().invoke(cli.main, ["add", *args])
    assert (result.exit_code == 0) == ok, result.output + repr(result.exception)
    return result


@pytest.fixture
def imported(lab: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Imported:
    """The node is reachable and runs one stack; read_stacks gives this."""
    copy = tmp_path / "copy"
    (copy / "apps").mkdir(parents=True)
    (copy / "docker-compose.yml").write_text("name: homelab-new\n")
    (copy / "apps/web.yml").write_text("services: {}\n")
    found = Imported(
        copy,
        (Stack("homelab-new", ("docker-compose.yml",)),),
        ("apps/web.yml", "docker-compose.yml"),
        {"TZ": "UTC", "PUID": "1000", "API_KEY": "s3cret"},
        ["stack tool runs from /opt/tool: labctl's stacks are top-level files of DEPLOY_DIR"],
        ("up.sh",),
    )
    host = Host(
        compose="2.40.0",
        docker=True,
        containers=(
            container(
                "web", "homelab-new", "/opt/stacks", ["/opt/stacks/data/a", "/opt/stacks/data/b"]
            ),
        ),
    )
    monkeypatch.setattr(add_module, "_check_node", lambda node: host)
    monkeypatch.setattr(adopt_module, "read_stacks", lambda *args: found)
    return found


def test_without_a_terminal_both_dirs_are_needed(lab: Path) -> None:
    assert "--deploy-dir and --appdata-dir" in add("aragorn", ok=False).output
    assert not (lab / "nodes/aragorn").exists()


def test_an_empty_node_is_added_not_committed(lab: Path) -> None:
    result = add("aragorn", "--deploy-dir", "/opt/docker", "--appdata-dir", "$DEPLOY_DIR/appdata")
    assert "aragorn: nothing imported" in result.output  # not reachable
    assert (lab / "nodes/aragorn/node.env").read_text() == (
        "DEPLOY_DIR=/opt/docker\nAPPDATA_DIR=$DEPLOY_DIR/appdata\n"
    )
    compose = (lab / "nodes/aragorn/docker-compose.yml").read_text()
    assert compose.startswith("name: homelab-aragorn\n")  # the others' prefix
    assert git(lab, "status", "--porcelain") == "?? nodes/aragorn/"  # the first apply commits it


def test_a_node_s_stacks_are_imported(lab: Path, imported: Imported) -> None:
    result = add("new", "--deploy-dir", "/opt/stacks", "--appdata-dir", "$DEPLOY_DIR/data")
    folder = lab / "nodes/new"
    assert sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()) == [
        "apps/web.yml",
        "docker-compose.yml",
        "node.env",
    ]
    # TZ is what _shared gives; without a terminal a secret stays as it is, with a warning.
    assert (folder / "node.env").read_text() == (
        "DEPLOY_DIR=/opt/stacks\nAPPDATA_DIR=$DEPLOY_DIR/data\n\nPUID=1000\nAPI_KEY=s3cret\n"
    )
    out = " ".join(result.output.split())
    assert "new: API_KEY look like secrets; kept in node.env as they are" in out
    assert "1 stack imported (2 files)" in out
    assert "not imported: stack tool runs from /opt/tool" in out
    assert "the first apply archives up.sh from DEPLOY_DIR" in out


def test_no_import_starts_empty(lab: Path, imported: Imported) -> None:
    add("new", "--deploy-dir", "/opt/stacks", "--appdata-dir", "/data", "--no-import")
    assert (lab / "nodes/new/docker-compose.yml").read_text().startswith("name: homelab-new\n")
    assert not (lab / "nodes/new/apps").exists()


def test_a_terminal_is_offered_what_the_node_runs(
    lab: Path, imported: Imported, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    offered: list[str] = []

    def edit_text(prompt: str, value: str = "", **kwargs: object) -> str:
        offered.append(f"{prompt}={value}")
        return value

    monkeypatch.setattr(add_module, "is_terminal", lambda: True)
    monkeypatch.setattr(menu, "edit_text", edit_text)
    monkeypatch.setattr(menu, "confirm", lambda *args, **kwargs: True)  # API_KEY is a secret
    add("new")
    assert offered == [
        "DEPLOY_DIR for new=/opt/stacks",
        "APPDATA_DIR for new=$DEPLOY_DIR/data",
    ]
    assert (lab / "nodes/new/node.env").read_text().endswith("PUID=1000\nAPI_KEY=_SECRET_\n")
    secrets = Secrets.load(tmp_path / "secrets.toml")
    assert secrets.get("new", "API_KEY") == "s3cret"


def test_unreachable_in_a_terminal_offers_what_most_nodes_use(
    lab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    offered: list[str] = []

    def edit_text(prompt: str, value: str = "", **kwargs: object) -> str:
        offered.append(f"{prompt}={value}")
        return value

    asked: list[str] = []

    def confirm(prompt: str, **kwargs: object) -> bool:
        asked.append(prompt)
        return True

    monkeypatch.setattr(add_module, "is_terminal", lambda: True)
    monkeypatch.setattr(menu, "edit_text", edit_text)
    monkeypatch.setattr(menu, "confirm", confirm)
    add("aragorn")
    assert asked == ["Add aragorn anyway?"]
    assert offered == [
        "DEPLOY_DIR for aragorn=/srv/docker",
        "APPDATA_DIR for aragorn=$DEPLOY_DIR/app_data",
    ]


def test_unreachable_in_a_terminal_is_asked_first(
    lab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(add_module, "is_terminal", lambda: True)
    monkeypatch.setattr(menu, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(menu, "edit_text", lambda *args, **kwargs: 1 / 0)  # never reached
    assert "Not added" in add("bob", ok=False).output
    assert not (lab / "nodes/bob").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["frodo"], "already exists"),
        (["all"], "cannot be a node name"),
        (["_shared"], "cannot be a node name"),
        (["new", "--deploy-dir", "docker", "--appdata-dir", "/a"], "absolute path"),
        (["new", "--deploy-dir", "$DEPLOY_DIR/x", "--appdata-dir", "/a"], "absolute path"),
        (["new", "--deploy-dir", "/a/b", "--appdata-dir", "/a"], "is inside APPDATA_DIR"),
    ],
)
def test_refused(lab: Path, args: list[str], message: str) -> None:
    assert message in add(*args, ok=False).output


def test_what_the_lab_config_root_has_is_left_to_it(lab: Path, imported: Imported) -> None:
    (lab / "apps").mkdir()
    (lab / "apps/web.yml").write_text("services: {}\n")  # the same: the root supplies it
    add("new", "--deploy-dir", "/opt/stacks", "--appdata-dir", "/data")
    assert not (lab / "nodes/new/apps").exists()
    assert (lab / "nodes/new/docker-compose.yml").is_file()
    (lab / "apps/web.yml").write_text("services: {web: {image: nginx}}\n")
    shutil.rmtree(lab / "nodes/new")
    imported.files = ("apps/web.yml", "docker-compose.yml")  # the run above trimmed it
    out = " ".join(
        add("new", "--deploy-dir", "/opt/stacks", "--appdata-dir", "/data").output.split()
    )
    assert "not imported: apps/web.yml: the lab config's apps has it, different" in out
    assert not (lab / "nodes/new/apps").exists()


#############################################################################
# remove, add's restore, discard
#############################################################################


def test_remove_deletes_only_the_folder(lab: Path) -> None:
    result = CliRunner().invoke(cli.main, ["remove", "cirdan"])
    assert result.exit_code == 0 and "removed nodes/cirdan" in result.output
    assert not (lab / "nodes/cirdan").exists()
    assert (
        git(lab, "status", "--porcelain")
        == " D nodes/cirdan/docker-compose.yml\n D nodes/cirdan/node.env".strip()
    )
    result = CliRunner().invoke(cli.main, ["remove", "cirdan"])
    assert result.exit_code == 1 and "has no node 'cirdan'" in result.output


def test_remove_refuses_changes_no_apply_committed(lab: Path) -> None:
    (lab / "nodes/frodo/node.env").write_text("DEPLOY_DIR=/elsewhere\n")
    result = CliRunner().invoke(cli.main, ["remove", "frodo"])
    assert result.exit_code == 1 and "labctl discard" in result.output
    assert (lab / "nodes/frodo").exists()


def test_add_restores_a_node_the_lab_config_had(lab: Path) -> None:
    repo = ConfigRepo.open(lab)
    info = TagInfo("2026-11-01T09:00:00", "me", "0.1.0", (), Dirs("/home/me/docker", "/a"))
    repo.tag_applied("cirdan", "20261101-090000", repo.head(), info)
    git(lab, "rm", "-rq", "nodes/cirdan")
    repo.commit("retire cirdan")
    repo.tag_applied("cirdan", "20261102-090000", repo.head(), replace(info, retired=True))
    result = add("cirdan")  # no terminal: restored, no dirs needed
    assert "restored nodes/cirdan from applied/cirdan/20261101-090000" in result.output
    assert (lab / "nodes/cirdan/node.env").read_text() == FILES["nodes/cirdan/node.env"]
    shutil.rmtree(lab / "nodes/cirdan")
    assert (
        "--no-restore"
        in add("cirdan", "--deploy-dir", "/x", "--appdata-dir", "/y", ok=False).output
    )
    add("cirdan", "--deploy-dir", "/x", "--appdata-dir", "/y", "--no-restore")
    assert (lab / "nodes/cirdan/node.env").read_text().startswith("DEPLOY_DIR=/x\n")


def test_discard(lab: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    run = CliRunner().invoke
    assert run(cli.main, ["discard"]).output == "Nothing to discard\n"
    (lab / "nodes/frodo/node.env").write_text("DEPLOY_DIR=/elsewhere\n")
    (lab / "nodes/new").mkdir()
    (lab / "nodes/new/node.env").write_text("X=1\n")
    shown = run(cli.main, ["discard", "--dry-run"]).output
    assert "changed  nodes/frodo/node.env" in shown and "new      nodes/new/" in shown
    assert git(lab, "status", "--porcelain") != ""
    result = run(cli.main, ["discard"])
    assert result.exit_code == 1 and "Confirm in a terminal, or pass --yes" in result.output
    result = run(cli.main, ["discard", "--yes"])
    assert (
        result.exit_code == 0 and "discarded 2 changes (git stash: labctl discard " in result.output
    )
    assert git(lab, "status", "--porcelain") == ""


def test_forget_a_node_that_is_gone(lab: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    repo = ConfigRepo.open(lab)
    info = TagInfo("2026-11-03T21:25:37", "me", "0.1.0", (), Dirs("/srv/docker", "/a"))
    repo.tag_applied("ghost", "20261103-212537", repo.head(), info)  # applied; folder never here
    assert repo.retiring(["frodo", "bilbo", "cirdan"]) == ["ghost"]
    run = CliRunner().invoke
    assert "has no node 'ghost'" in run(cli.main, ["remove", "ghost"]).output
    assert "Confirm in a terminal" in run(cli.main, ["remove", "ghost", "--forget"]).output
    result = run(cli.main, ["remove", "ghost", "--forget", "--yes"])
    assert result.exit_code == 0 and "forgot ghost: tagged applied/ghost/" in result.output
    assert repo.retiring(["frodo", "bilbo", "cirdan"]) == []
    last = repo.last_applied("ghost")
    assert last is not None and last.info is not None and last.info.retired
    # A node in the lab config: forgotten, and its folder removed.
    repo.tag_applied("cirdan", "20261101-090000", repo.head(), info)
    result = run(cli.main, ["remove", "cirdan", "--forget", "--yes"])
    assert result.exit_code == 0 and not (lab / "nodes/cirdan").exists()
    assert repo.retiring(["frodo", "bilbo"]) == []
