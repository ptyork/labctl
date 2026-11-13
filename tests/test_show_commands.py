"""labctl ports and labctl status: what they show of what the core read (no node is reached)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result
from conftest import MakeRepo, git

from labctl import port_survey as survey_module
from labctl.cli import cli
from labctl.cli.commands import ports as ports_module
from labctl.cli.commands import status as status_module
from labctl.console import console
from labctl.port_survey import Survey, survey_ports
from labctl.ports import LISTENING, NOT_RUNNING, PROCESS, RUNNING, SERVICE, PortUse
from labctl.remote import MARK, RemoteError
from labctl.repo import ConfigRepo
from labctl.runtime import UNAVAILABLE, Port
from labctl.settings import LAB_CONFIG_ENV, Secrets, Settings, SettingsError

FILES = {
    "nodes/cirdan/node.env": "DEPLOY_DIR=/srv/docker\nSERVER_IP=10.0.0.9\n",
    "nodes/cirdan/stack.yml": "services: {}\n",
    "nodes/frodo/stack.yml": "services: {}\n",
    "nodes/frodo/node.env": "DEPLOY_DIR=/zconfig/docker\nAPPDATA_DIR=/zconfig/appdata\n",
}


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_repo: MakeRepo) -> ConfigRepo:
    repo = make_repo(tmp_path / "lab", FILES)
    monkeypatch.setenv(LAB_CONFIG_ENV, str(repo.root))
    monkeypatch.setenv("LABCTL_CONFIG_FILE", str(tmp_path / "labctl.toml"))
    monkeypatch.setattr(console, "_width", 200)
    return repo


def run(*args: str) -> Result:
    return CliRunner().invoke(cli.main, list(args))


def surveyed(monkeypatch: pytest.MonkeyPatch, survey: Survey) -> list[tuple[str, ...]]:
    named: list[tuple[str, ...]] = []

    def survey_ports(
        settings: object, repo: object, secrets: object, nodes: tuple[str, ...]
    ) -> Survey:
        named.append(nodes)
        return survey

    monkeypatch.setattr(ports_module, "survey_ports", survey_ports)
    return named


def test_ports_shows_the_lab_config_s_ports_and_the_host_s_on_request(
    lab: ConfigRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    uses = [
        PortUse("cirdan", Port("tcp", 32400), "plex", SERVICE, RUNNING),
        PortUse("cirdan", Port("tcp", 80), "traefik", SERVICE, NOT_RUNNING, ("process nginx",)),
        PortUse("cirdan", Port("tcp", 22), "sshd", PROCESS, LISTENING),
    ]
    named = surveyed(monkeypatch, Survey(["cirdan"], uses, warnings=["cirdan: no ss there"]))
    result = run("ports", "cirdan", "--sort", "port")
    assert result.exit_code == 0 and named == [("cirdan",)]
    output = result.output
    assert "! cirdan: no ss there" in output and "Ports on cirdan" in output
    assert "sshd" not in output and "clashes with process nginx" in output
    assert output.index("80/tcp") < output.index("32400/tcp")
    assert "sshd" in run("ports", "cirdan", "--include-host").output


def test_ports_exits_1_when_a_node_could_not_be_read(
    lab: ConfigRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    surveyed(monkeypatch, Survey(["cirdan"], unread=["cirdan: connection refused"]))
    result = run("ports")
    assert result.exit_code == 1 and "✗ cirdan: connection refused" in result.output

    def unknown(*args: object) -> Survey:
        raise ValueError("Unknown node: ghost")

    monkeypatch.setattr(ports_module, "survey_ports", unknown)
    assert "Error: Unknown node: ghost" in run("ports", "ghost").output


def test_status_shows_each_node_and_how_far_behind_it_is(
    lab: ConfigRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    addresses = {"cirdan": ("cirdan.lan", "10.0.0.5"), "frodo": ("frodo.lan", "")}
    monkeypatch.setattr(status_module, "node_address", lambda node: addresses[node])
    git(lab.root, "tag", "-a", "-m", "applied", "applied/frodo/20261101-090000")
    (lab.root / "nodes/frodo/extra.yml").write_text("services: {}\n")
    git(lab.root, "add", "-A")
    git(lab.root, "commit", "-q", "-m", "more")

    result = run("status")
    assert result.exit_code == 0, result.output
    rows = {line.split()[1]: line for line in result.output.splitlines() if "│" in line}
    assert "cirdan.lan" in rows["cirdan"] and "10.0.0.5" in rows["cirdan"]
    assert "/srv/docker" in rows["cirdan"] and "not set" in rows["cirdan"]  # no APPDATA_DIR
    assert "never" in rows["cirdan"]
    assert "does not resolve" in rows["frodo"] and "/zconfig/appdata" in rows["frodo"]
    assert "20261101-090000" in rows["frodo"] and "1 behind" in rows["frodo"]
    assert "! cirdan: SERVER_IP in node.env is 10.0.0.9, but cirdan.lan resolves to 10.0.0.5" in (
        result.output
    )


def test_status_without_a_lab_config_shows_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LAB_CONFIG_ENV, str(tmp_path / "nowhere"))
    monkeypatch.setenv("LABCTL_CONFIG_FILE", str(tmp_path / "labctl.toml"))
    monkeypatch.setattr(console, "_width", 200)
    result = run("status")
    assert result.exit_code == 0 and "CLI Version" in result.output
    assert "Nodes (" not in result.output


def test_a_survey_reads_every_node_and_says_what_it_could_not(
    lab: ConfigRepo, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeRemote:
        def __init__(self, node: str) -> None:
            self.node = node

        def run_script(self, script: str) -> str:
            if self.node == "frodo":
                raise RemoteError("frodo: connection refused")
            return f"{MARK} pointer\n/srv/docker\n{MARK} listeners\n{UNAVAILABLE}\n"

    def unresolved(*args: Any) -> Any:
        raise SettingsError("cirdan: TZ has no value\n(more)")

    monkeypatch.setattr(survey_module, "node_remote", FakeRemote)
    monkeypatch.setattr(survey_module, "resolve_desired", unresolved)
    survey = survey_ports(settings, lab, Secrets({}), ())
    assert survey.nodes == ["cirdan", "frodo"] and survey.uses == []
    assert survey.unread == ["frodo: connection refused"]
    assert survey.warnings == [
        "cirdan: TZ has no value: the lab config's ports are not shown for cirdan",
        "cirdan: no ss there: ports held outside Docker are not shown",
        "cirdan: TZ has no value: the lab config's ports are not shown for frodo",
    ]
    with pytest.raises(ValueError, match="ghost"):
        survey_ports(settings, lab, Secrets({}), ("ghost",))
