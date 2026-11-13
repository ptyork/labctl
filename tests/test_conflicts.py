"""Answering conflicts: asked about each in a terminal, the flags without one; the plan's lines."""

from __future__ import annotations

from dataclasses import replace

import click
import pytest
from test_runtime import ours, run, web_node

from labctl.cli import menu
from labctl.cli.commands import plan as plan_module
from labctl.cli.commands.plan import _answer_conflicts
from labctl.cli.render import choice_label, describe, render_runtime
from labctl.planning import Options, Planned
from labctl.runtime import DEFER, Answers, Container, Port, Runtime, resolve

OPTIONS = Options(nodes=("cirdan",), replace_stale=False, no_migrate=(), verbose=False)
NGINX = "cirdan/container/nginx"


def conflicted() -> Runtime:
    want, have = web_node()
    nginx = Container("nginx", "running", image="nginx", ports=(Port("tcp", 80),))
    return run(want, have, {"cirdan": (ours("web"), nginx)})


def planned(runtime: Runtime) -> Planned:
    return Planned(runtime=runtime, options=OPTIONS)


def test_without_a_terminal_an_unanswered_conflict_stops_apply() -> None:
    with pytest.raises(click.ClickException, match="--stop-unmanaged or --defer-blocked"):
        _answer_conflicts(planned(conflicted()), terminal=False)
    answered = planned(conflicted())
    assert _answer_conflicts(replace_runtime(answered), terminal=False)[1] is False


def replace_runtime(p: Planned) -> Planned:
    """The plan as made with --defer-blocked: nothing left to ask."""
    return replace(p, runtime=resolve(p.runtime, Answers(defer_blocked=True)))


def test_a_terminal_is_asked_about_each(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[str, list[str], str]] = []

    def choose(title: str, options: list[str], selected: int = 0, header: str = "") -> int:
        asked.append((title, options, header))
        return 1  # don't start

    monkeypatch.setattr(menu, "choose", choose)
    result, was_asked = _answer_conflicts(planned(conflicted()), terminal=True)
    assert was_asked and result.options is not None and result.options.defer == (NGINX,)
    assert result.runtime.answered == ((NGINX, DEFER),)
    ((title, options, header),) = asked
    assert title == "In the way on cirdan"
    assert header == "cirdan: container nginx (nginx) holds 80/tcp, which heimdall publishes"
    assert options == [
        "Stop it (restart policy set to no; undo starts it again)",
        "Don't start heimdall (created, not started)",
        "Cancel the apply",
    ]


def test_cancel_stops_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plan_module.menu, "choose", lambda *args, **kwargs: 2)
    with pytest.raises(click.ClickException, match="Not applied"):
        _answer_conflicts(planned(conflicted()), terminal=True)


def test_the_plan_says_what_apply_asks_or_does(capsys: pytest.CaptureFixture[str]) -> None:
    runtime = conflicted()
    (conflict,) = runtime.in_the_way
    assert describe(conflict).startswith("cirdan: container nginx")
    assert choice_label(conflict, DEFER, done=True).startswith("heimdall created, not started")
    render_runtime(runtime)
    out = " ".join(capsys.readouterr().out.split())
    assert "apply asks: stop it / don't start heimdall / cancel" in out
    render_runtime(replace_runtime(planned(runtime)).runtime)
    assert "→ heimdall created, not started" in " ".join(capsys.readouterr().out.split())
