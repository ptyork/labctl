from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from labctl.bundle import Desired
from labctl.cli import render
from labctl.collect import Collected, NodeView, OnNode
from labctl.envfile import CompiledEnv
from labctl.planner import Migrate, NotNamed, Plan, Update
from labctl.state import NodeState, Scope, Service, Stack

ENV = CompiledEnv(values={"DEPLOY_DIR": "/srv/docker", "APPDATA_DIR": "/srv/appdata"})


@pytest.fixture
def shown(monkeypatch: pytest.MonkeyPatch) -> Console:
    out = Console(record=True, width=100, highlight=False)
    monkeypatch.setattr(render, "console", out)
    return out


def test_nodes_included_and_skipped(shown: Console) -> None:
    state = NodeState.of("bilbo")
    collected = Collected.of(
        {n: Desired(n, Path(), ENV, {}, state, {}) for n in ("bilbo", "cirdan", "frodo")}
    )
    collected.scope = Scope.build({"bilbo", "cirdan", "frodo"}, ["frodo", "bilbo"])
    render.render_nodes(collected)
    assert shown.export_text() == "Nodes included:  bilbo, frodo\nNodes skipped:   cirdan\n"


def test_file_operations_per_node(shown: Console) -> None:
    state = NodeState.of("bilbo")
    wanted = {"apps/a.yml": "1", "apps/b.yml": "2", ".env": "new", "docker-compose.yml": "x"}
    on_node = OnNode(
        None,
        {".env": "old", "docker-compose.yml": "x", "up.sh": "u"},
        names={".env": "file", "docker-compose.yml": "file", "up.sh": "file"},
    )
    bilbo = NodeView(Desired("bilbo", Path(), ENV, {}, state, wanted), state, on_node=on_node)
    collected = Collected({"bilbo": bilbo}, Scope.build({"bilbo"}, ["bilbo"]))
    assert render.render_files(collected, collected.scope, verbose=False) == 1
    assert shown.export_text() == (
        "\nFile operations on bilbo\n"
        "  - Add:     apps/ (2 files)\n"
        "  - Update:  .env\n"
        "  - Archive: up.sh\n"
    )


def test_stacks_list_what_changes_under_the_stack_it_ends_up_in(shown: Console) -> None:
    old, new = Stack("docker", ("docker-compose.yml",)), Stack("homelab-bilbo", ("c.yml",))
    before = NodeState.of("bilbo", Service("npm", stack="docker", config='{"a":1}'), stacks=[old])
    after = NodeState.of(
        "bilbo", Service("npm", stack="homelab-bilbo", config='{"a":1}'), stacks=[new]
    )
    bilbo = NodeView(Desired("bilbo", Path(), ENV, {}, after, {}), before)
    collected = Collected({"bilbo": bilbo}, Scope.build({"bilbo"}, ["bilbo"]))
    plan = Plan(operations=(Update("npm", "bilbo"),))
    render.render_stacks(plan, collected, render.Runtime(), set(), {}, verbose=False)
    assert shown.export_text() == (
        "\nStacks\n"
        "  - bilbo/docker (remove)\n"
        "  - bilbo/homelab-bilbo (add)\n"
        "    * update:  npm (move from bilbo/docker)\n"
    )


def test_a_migrate_with_a_node_not_named_says_what_to_do(shown: Console) -> None:
    plex = Migrate(("plexms",), "bilbo", "cirdan", ("plex",), ("plex",))
    render.render_problems((NotNamed(plex, "bilbo"),), {})
    assert (
        "plex moves bilbo → cirdan: name bilbo too, or start it fresh with --no-migrate plex"
        in (shown.export_text())
    )


def test_an_unread_node_not_named_is_one_line(shown: Console) -> None:
    state = NodeState.of("bilbo")
    collected = Collected.of(
        {n: Desired(n, Path(), ENV, {}, state, {}) for n in ("bilbo", "cirdan")}
    )
    collected.scope = Scope.build({"bilbo", "cirdan"}, ["cirdan"])
    collected.nodes["bilbo"].unread = "unreachable: Connection timed out"
    render.render_nodes(collected)
    assert shown.export_text().splitlines()[-1] == (
        "! bilbo not read (unreachable: Connection timed out): planned as of its last apply"
    )
