"""make_planned: what is collected becomes a plan, with why apply is blocked and what it asks
about first. The nodes' paths are not probed here."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from labctl import checks
from labctl.bundle import Desired
from labctl.cli import render
from labctl.collect import Collected, OnNode
from labctl.envfile import CompiledEnv
from labctl.planner import Add, Remove
from labctl.planning import Options, PlanError, Planned, answered, make_planned
from labctl.state import Dirs, NodeState, Scope, Service, Stack

HOME, APPDATA = "/srv/docker", "/srv/appdata"
ENV = CompiledEnv(values={"DEPLOY_DIR": HOME, "APPDATA_DIR": APPDATA})
STACK = Stack("homelab-cirdan", ("docker-compose.yml",))
FILES = {"docker-compose.yml": "x"}
OPTIONS = Options(("cirdan",), False, (), False)


@pytest.fixture(autouse=True)
def no_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checks, "probe", lambda remote, paths: {})
    monkeypatch.setattr(checks, "probe_volumes", lambda remote, keys: {})


def state(*services: str) -> NodeState:
    return NodeState.of(
        "cirdan", *(Service(name, stack=STACK.name) for name in services), stacks=[STACK]
    )


def desired(tmp_path: Path, env: CompiledEnv, *services: str) -> Desired:
    configs = {STACK.name: {"services": {name: {} for name in services}}}
    return Desired("cirdan", tmp_path, env, configs, state(*services), FILES)


def collected(
    tmp_path: Path, wanted: tuple[str, ...], running: tuple[str, ...], source: str = "node"
) -> Collected:
    """cirdan, named and read: ``wanted`` in the lab config, ``running`` there now."""
    found = Collected.of({"cirdan": desired(tmp_path, ENV, *wanted)})
    view = found.nodes["cirdan"]
    view.recorded, view.recorded_dirs, view.source = state(*running), Dirs(HOME, APPDATA), source
    view.on_node = OnNode(None, dict(FILES), names={"docker-compose.yml": "file"})
    found.scope = Scope.build({"cirdan"}, ["cirdan"])
    found.tree, found.base = "tree1", "base1"
    return found


def test_errors_collecting_leave_no_plan(tmp_path: Path) -> None:
    found = collected(tmp_path, ("plex",), ())
    found.errors.append("cirdan: unreachable")
    planned = make_planned(found, OPTIONS)
    assert planned == Planned(reasons=("errors above",), tree="tree1", base="base1")


def test_nothing_to_do_is_neither_blocked_nor_asked_about(tmp_path: Path) -> None:
    planned = make_planned(collected(tmp_path, ("plex",), ("plex",)), OPTIONS)
    assert planned.plan.operations == () and planned.plan.unchanged
    assert (planned.reasons, planned.confirm) == ((), ())
    assert (planned.tree, planned.base, planned.options) == ("tree1", "base1", OPTIONS)
    assert planned.work_error == "" and planned.works is not None


def test_a_removed_service_is_asked_about(tmp_path: Path) -> None:
    planned = make_planned(collected(tmp_path, ("plex",), ("plex", "sonarr")), OPTIONS)
    assert [type(op) for op in planned.plan.operations] == [Remove]
    assert planned.reasons == () and planned.confirm == ("1 service removed",)
    assert planned.work_nodes == {"cirdan"}


def test_a_first_apply_and_the_repo_s_notices_are_asked_about(tmp_path: Path) -> None:
    found = collected(tmp_path, ("plex", "sonarr"), ("plex",), source="first")
    found.repo_notices.append("bilbo: applied from a commit this clone does not have")
    planned = make_planned(found, OPTIONS, notices=["not fetched (timed out)"])
    assert [type(op) for op in planned.plan.operations] == [Add]
    assert planned.repo_notices == (
        "not fetched (timed out)",
        "bilbo: applied from a commit this clone does not have",
    )
    assert planned.confirm == ("a first apply on cirdan", "2 repo notices")


def test_a_variable_without_a_value_blocks(tmp_path: Path) -> None:
    found = collected(tmp_path, ("plex",), ("plex",))
    env = CompiledEnv(values=dict(ENV.values), placeholders=("TZ",))
    found.nodes["cirdan"].desired = desired(tmp_path, env, "plex")
    planned = make_planned(found, OPTIONS)
    assert planned.placeholders == {"cirdan": ("TZ",)} and planned.reasons == ("problems above",)


def test_no_migrate_must_name_a_service(tmp_path: Path) -> None:
    options = Options(("cirdan",), False, ("nosuch",), False)
    with pytest.raises(PlanError, match="nosuch"):
        make_planned(collected(tmp_path, ("plex",), ("plex",)), options)


def test_answers_given_in_a_terminal_go_into_the_options(tmp_path: Path) -> None:
    planned = make_planned(collected(tmp_path, ("plex",), ("plex",)), OPTIONS)
    after = answered(planned, stop=["cirdan/heimdall"], defer=["cirdan/plex"])
    assert after.options is not None
    assert (after.options.stop, after.options.defer) == (("cirdan/heimdall",), ("cirdan/plex",))
    assert after.same_as(planned)  # nothing was in the way: the same plan


def test_the_plan_as_shown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shown = Console(record=True, width=100, highlight=False)
    monkeypatch.setattr(render, "console", shown)
    found = collected(tmp_path, ("plex", "sonarr"), ("plex", "heimdall"))
    render.render_plan(found, make_planned(found, OPTIONS), False)
    assert shown.export_text().splitlines() == [
        "",
        "Stacks",
        "  - cirdan/homelab-cirdan (stays down: not running)",
        "    * remove:  heimdall",
        "    * add:     sonarr",
        "",
        "1 add, 1 remove, 1 unchanged",
    ]
