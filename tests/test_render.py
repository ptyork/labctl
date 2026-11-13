from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from labctl.bundle import Desired
from labctl.checks import Finding, Level
from labctl.cli import render
from labctl.collect import Collected, NodeView, OnNode
from labctl.envfile import CompiledEnv
from labctl.git_sync import Published
from labctl.planner import Migrate, NotNamed, Plan, Update
from labctl.rollout import Applied
from labctl.state import NodeState, Scope, Service, Stack
from labctl.validate import Checked

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


def test_a_checked_node_is_one_line_or_its_errors(shown: Console) -> None:
    state = NodeState.of(
        "cirdan", Service("plex", stack="media"), stacks=[Stack("media", ("m.yml",))]
    )
    desired = Desired("cirdan", Path(), ENV, {"media": {}}, state, {})
    desired.not_stacks["notes.yml"] = "no services"
    render.render_checked(Checked("cirdan", desired))
    render.render_checked(Checked("frodo", None, ["frodo: TZ has no value"]))
    text = shown.export_text()
    assert "! " in text and "notes.yml" in text
    assert "✓ cirdan: 1 stack, 1 service\n" in text
    assert "✗ frodo: TZ has no value" in text and "✓ frodo" not in text


def test_what_became_of_the_commit_applied(shown: Console) -> None:
    root = Path("/lab")
    done = Published("c0ffee1234567", True, "Add plex", tagged=1, remote="origin")
    render.render_applied(Applied(done, "main"), root)
    assert shown.export_text() == '✓ Committed c0ffee1 "Add plex", tagged and pushed to origin\n'

    render.render_applied(Applied(Published("c0ffee1234567"), "main"), root)
    assert "✓ Nothing to commit (c0ffee1); no remote, so it stays in this clone" in (
        shown.export_text()
    )

    failed = Published(
        "c0ffee1234567", True, untagged=(("cirdan", "tag exists"),), remote="origin",
        push_error="rejected",
    )  # fmt: skip
    render.render_applied(Applied(failed, "main", advanced=False), root)
    text = shown.export_text()
    assert "main moved during the apply" in text and "git -C /lab merge c0ffee123456" in text
    assert "! cirdan: applied, not tagged: tag exists" in text
    assert "✓ Committed c0ffee1\n" in text and "! Not pushed (rejected)" in text


def test_checks_list_what_blocks_first(shown: Console) -> None:
    render.render_findings([])
    assert shown.export_text() == ""
    render.render_findings(
        [
            Finding(Level.WARN, "bilbo", "", "/media is missing"),
            Finding(Level.BLOCK, "cirdan", "plex", "plex already has data", flag="--replace-stale"),
        ]
    )
    lines = shown.export_text().splitlines()
    assert lines[1:] == [
        "Checks",
        "  ✗ cirdan: plex: plex already has data (needs --replace-stale)",
        "  ! bilbo: /media is missing",
    ]


def test_the_summary_counts_and_says_why_apply_is_blocked(shown: Console) -> None:
    render.render_summary(Plan(), 0, (), 0)
    assert shown.export_text().strip() == "no service changes, 0 unchanged"
    plan = Plan(operations=(Update("plex", "cirdan"), Update("sonarr", "cirdan")))
    render.render_summary(plan, 2, ("1 check failed",), 1)
    assert shown.export_text().strip() == (
        "2 update, 0 unchanged, files differ on 2 nodes, 1 in the way — blocked: 1 check failed"
    )
